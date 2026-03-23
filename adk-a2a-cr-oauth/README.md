# adk-a2a-cr-oauth

Google Drive reader agent deployed to Cloud Run via the A2A protocol, with OAuth handled by Gemini Enterprise.

Scaffolded with [`agent-starter-pack`](https://github.com/GoogleCloudPlatform/agent-starter-pack) (`--agent adk_a2a --deployment-target cloud_run`), then customized with OAuth credential injection and app-level token validation.

See the [root README](../README.md) for architectural details and key findings about how OAuth works across ADK deployment targets.

## How It Works

1. Agent is registered with Gemini Enterprise, linked to an OAuth authorization resource (`google-drive-auth`) that declares `drive.readonly` scope
2. When a user asks to read a Drive file, Gemini Enterprise triggers OAuth consent
3. Gemini Enterprise sends the user's OAuth token as `Authorization: Bearer` on every A2A POST request
4. Custom `OAuthCallContextBuilder` extracts the token from the HTTP header
5. Custom `OAuthA2aAgentExecutor` injects it into session state via `state_delta`
6. `negotiate_creds()` finds the token in `tool_context.state` and uses it to call the Drive API

## Project Structure

```
adk-a2a-cr-oauth/
├── app/
│   ├── agent.py               # Root agent with read_drive_file tool
│   ├── auths.py               # OAuth config (TOKEN_CACHE_KEY, scopes, auth scheme)
│   ├── tools.py               # negotiate_creds() + read_drive_file()
│   ├── fast_api_app.py        # A2A server with custom OAuth components
│   └── app_utils/
│       ├── telemetry.py       # Cloud Trace + logging
│       └── typing.py          # Feedback model
├── tools/
│   ├── register_oauth.py      # Register OAuth resource with Discovery Engine
│   └── test_a2a_oauth.py      # 3-layer integration test
├── Dockerfile
├── Makefile
└── pyproject.toml
```

### Key Files

**`app/fast_api_app.py`** — The main application file containing three custom components:
- `OAuthCallContextBuilder` — Extracts Bearer token from HTTP headers into A2A call context
- `OAuthA2aAgentExecutor` — Propagates call context state into ADK session via `state_delta`
- Token validation middleware — Validates token `aud`/`azp` against `OAUTH_CLIENT_ID`

**`app/tools.py`** — `negotiate_creds()` implements a three-stage credential resolution:
1. Check `tool_context.state` for cached/injected token (both direct key and `temp:` prefixed)
2. Check `tool_context.get_auth_response()` for ADK OAuth flow response (local dev)
3. Fall back to `request_credential()` if client credentials are configured (local dev), otherwise return an error

**`app/auths.py`** — OAuth configuration. `TOKEN_CACHE_KEY` is set from the `AUTH_ID` env var (default: `google-drive-auth`) and must match the authorization resource ID used during Gemini Enterprise registration.

## Requirements

- Google Cloud project with billing, Drive API enabled
- `gcloud`, `uv`, `make`
- OAuth 2.0 client (Web Application type) with redirect URIs:
  - `https://vertexaisearch.cloud.google.com/oauth-redirect`
  - `https://vertexaisearch.cloud.google.com/static/oauth/oauth.html`
- Gemini Enterprise instance

## Setup & Deployment

### 1. Install dependencies

```bash
make install
```

### 2. Local development

```bash
# ADK Web UI (interactive OAuth flow via browser)
make playground

# Or start the A2A backend server
make local-backend
```

### 3. Deploy to Cloud Run

```bash
gcloud config set project <your-project-id>
make deploy
```

The service deploys with `--allow-unauthenticated` because Gemini Enterprise sends user OAuth tokens (not service identity tokens) in the `Authorization` header — Cloud Run IAM would reject these with 401. App-level token validation secures the endpoint instead.

Set the `OAUTH_CLIENT_ID` env var on Cloud Run to enable token validation:

```bash
gcloud run services update adk-a2a-cr-oauth \
  --region=us-central1 \
  --set-env-vars="OAUTH_CLIENT_ID=<your-oauth-client-id>"
```

### 4. Register OAuth authorization

```bash
make register-oauth
```

This interactively registers an OAuth authorization resource with the Discovery Engine API. You'll need your OAuth client ID, client secret, and the `AUTH_ID` (default: `google-drive-auth`).

### 5. Register with Gemini Enterprise

```bash
make register-gemini-enterprise
```

Use the `--authorization-id` flag to link the OAuth resource. The agent card now includes `securitySchemes` declaring the OAuth2 requirement.

### 6. IAM permissions

Grant the Discovery Engine service agent access to invoke Cloud Run:

```bash
gcloud projects add-iam-policy-binding <project-id> \
  --member="serviceAccount:service-<project-number>@gcp-sa-discoveryengine.iam.gserviceaccount.com" \
  --role="roles/run.servicesInvoker" \
  --condition=None --quiet
```

Note: Use `roles/run.servicesInvoker` at the **project level** (not `roles/run.invoker` at the service level).

## Commands

| Command | Description |
|---------|-------------|
| `make install` | Install dependencies |
| `make playground` | Launch ADK Web UI for local dev |
| `make local-backend` | Start FastAPI server with hot-reload |
| `make deploy` | Deploy to Cloud Run |
| `make register-oauth` | Register OAuth resource with Discovery Engine |
| `make register-gemini-enterprise` | Register agent with Gemini Enterprise |
| `make inspector` | Launch A2A Protocol Inspector |
| `make lint` | Run code quality checks |
| `make test` | Run unit and integration tests |

## Testing

```bash
# Start the local server
make local-backend

# Run the integration test (steps 1-2: agent card + A2A without OAuth)
uv run python tools/test_a2a_oauth.py

# Run all tests including Drive file read (needs a real file ID)
TEST_FILE_ID=<your-file-id> uv run python tools/test_a2a_oauth.py
```

## Security Considerations

- The service is `--allow-unauthenticated` at the Cloud Run level (required for Gemini Enterprise compatibility)
- App-level middleware validates every Bearer token's `aud`/`azp` against `OAUTH_CLIENT_ID`
- Agent card discovery endpoints are exempt from auth (required for A2A protocol)
- `--ingress=all` is required because Gemini Enterprise calls Cloud Run over the public internet
- Do not commit `OAUTH_CLIENT_SECRET` to the repo — use env vars or Secret Manager
