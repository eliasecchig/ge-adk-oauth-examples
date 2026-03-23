# ADK OAuth Examples

Two reference implementations showing how to build Google Drive reader agents with OAuth 2.0 authentication, deployed to Google Cloud and registered with Gemini Enterprise.

Both agents do the same thing — read files from Google Drive on behalf of the user — but use different deployment targets and protocols.

| | `adk-ae-oauth` | `adk-a2a-cr-oauth` |
|---|---|---|
| **Deployment target** | Vertex AI Agent Engine | Cloud Run (FastAPI) |
| **Protocol** | ADK HTTP | A2A JSON-RPC |
| **Entry point** | `AgentEngineApp` | `A2AFastAPIApplication` |
| **OAuth flow** | ADK's `request_credential()` cycle | Gemini Enterprise handles consent, token arrives via HTTP header |
| **Token delivery** | `tool_context.state["temp:<AUTH_ID>"]` | HTTP `Authorization: Bearer` header (extracted by custom `CallContextBuilder` + `A2aAgentExecutor`) |
| **Infrastructure auth** | Agent Engine manages access | `--allow-unauthenticated` + app-level token validation |
| **Registration** | `agent-starter-pack register-gemini-enterprise` | Same CLI + `--authorization-id` for OAuth |

## Key Findings

These examples were built while investigating how OAuth works across ADK deployment targets with Gemini Enterprise. The findings below are hard-won lessons that aren't well documented elsewhere.

### 1. Token delivery differs by deployment target

**Agent Engine**: Gemini Enterprise injects the user's OAuth token into the ADK session state at `temp:<AUTH_ID>`, where `<AUTH_ID>` is the authorization resource ID used during registration. The `negotiate_creds()` function in `tools.py` checks both `tool_context.state[TOKEN_CACHE_KEY]` and `tool_context.state["temp:TOKEN_CACHE_KEY"]`.

**A2A on Cloud Run**: Gemini Enterprise sends the user's OAuth token as the HTTP `Authorization: Bearer` header on every POST request to the A2A endpoint. The ADK's `A2aAgentExecutor` does **not** automatically extract this token into session state — you need custom components to bridge this gap (see Architecture section below).

### 2. Cloud Run IAM auth is incompatible with user OAuth tokens

Cloud Run's `--no-allow-unauthenticated` expects an OIDC identity token (with the service URL as audience) in the `Authorization` header. Gemini Enterprise sends the user's OAuth access token (with `aud=<oauth-client-id>` and `scope=drive.readonly`) in that same header. These are fundamentally incompatible — Cloud Run IAM rejects the user token with a 401.

**You must use `--allow-unauthenticated`** for A2A agents that receive user OAuth tokens from Gemini Enterprise, and implement app-level token validation instead.

### 3. Ingress restrictions don't help

Setting `--ingress=internal-and-cloud-load-balancing` blocks Gemini Enterprise traffic because Gemini Enterprise calls Cloud Run over the public internet, not via GCP-internal networking.

### 4. The ADK A2A executor doesn't propagate call context to session state

The `A2aAgentExecutor._prepare_session()` creates sessions with empty state. Even though the A2A SDK's `CallContextBuilder` can extract HTTP headers into `ServerCallContext.state`, the executor never merges this into the ADK session. Furthermore, `Runner.run_async()` fetches a fresh session from the session service, so modifying the session object returned by `_prepare_session()` has no effect.

The fix is to use `run_request.state_delta` — the `AgentRunRequest` model has a `state_delta` field that `Runner.run_async()` applies to the session before tool execution.

### 5. `InMemorySessionService.create_session` strips `temp:` prefixed keys

The `extract_state_delta` function in ADK strips keys with the `temp:` prefix during session creation. If you inject tokens with `temp:` prefixed keys at session creation time, they'll be dropped. Use non-prefixed keys when injecting directly into session state.

### 6. OAuth client configuration matters

The OAuth client used for Gemini Enterprise registration must be a **Web Application** type (not IAP-created) with these redirect URIs:
- `https://vertexaisearch.cloud.google.com/oauth-redirect`
- `https://vertexaisearch.cloud.google.com/static/oauth/oauth.html`

The Drive API must be enabled in the project, and the OAuth consent screen must include the `drive.readonly` scope.

## Architecture: A2A + Cloud Run + OAuth

```
User (Gemini Enterprise UI)
    |
    | 1. User asks: "Read my Drive file"
    | 2. Gemini Enterprise triggers OAuth consent (via registered authorization)
    | 3. User authorizes drive.readonly scope
    |
    v
Gemini Enterprise
    |
    | 4. POST /a2a/app with Authorization: Bearer <user-oauth-token>
    |    (token has aud=<oauth-client-id>, scope=drive.readonly)
    |
    v
Cloud Run (--allow-unauthenticated)
    |
    | 5. App-level middleware validates token aud/azp matches OAUTH_CLIENT_ID
    |
    v
OAuthCallContextBuilder
    |
    | 6. Extracts Bearer token from Authorization header
    |    Stores in ServerCallContext.state[TOKEN_CACHE_KEY]
    |
    v
OAuthA2aAgentExecutor._prepare_session()
    |
    | 7. Copies call_context.state into run_request.state_delta
    |    (NOT into session.state — that gets overwritten by Runner.run_async)
    |
    v
Runner.run_async()
    |
    | 8. Applies state_delta to session before agent execution
    |
    v
negotiate_creds() in read_drive_file tool
    |
    | 9. Finds token in tool_context.state[TOKEN_CACHE_KEY]
    |    Creates google.oauth2.credentials.Credentials(token=<access_token>)
    |
    v
Google Drive API
    |
    | 10. Reads file content using the user's OAuth token
    |
    v
Agent returns file content to user
```

### Custom Components (in `fast_api_app.py`)

**`OAuthCallContextBuilder`** — Implements `CallContextBuilder` from the A2A SDK. Extracts the Bearer token from the HTTP `Authorization` header and stores it in `ServerCallContext.state` under the `TOKEN_CACHE_KEY`.

**`OAuthA2aAgentExecutor`** — Extends `A2aAgentExecutor`. Overrides `_prepare_session()` to copy `call_context.state` into `run_request.state_delta` (not `session.state` directly, which gets overwritten). This ensures the token survives into `tool_context.state` when the tool executes.

**Token validation middleware** — FastAPI middleware that validates the Bearer token by calling Google's `tokeninfo` endpoint and checking that `azp`/`aud` matches the expected `OAUTH_CLIENT_ID`. Caches validated tokens for 5 minutes. Public paths (agent card discovery) are exempt.

### App-Level Security Model

Since Cloud Run IAM auth can't be used (see Finding #2), security is enforced at the application level:

- **Network**: `--ingress=all` (required — Gemini Enterprise uses public internet)
- **IAM**: `--allow-unauthenticated` (required — user OAuth tokens aren't identity tokens)
- **App validation**: Middleware checks every non-discovery request for a valid Bearer token with the correct `aud`/`azp`
- **Result**: Only tokens issued by your registered OAuth client are accepted — functionally equivalent to authenticated access

### A2A Protocol and OAuth

The A2A spec (v0.3.0) supports declaring OAuth requirements via `securitySchemes` in the agent card:

```json
{
  "securitySchemes": {
    "google_drive_oauth": {
      "type": "oauth2",
      "flows": {
        "authorizationCode": {
          "authorizationUrl": "https://accounts.google.com/o/oauth2/auth",
          "tokenUrl": "https://oauth2.googleapis.com/token",
          "scopes": {
            "https://www.googleapis.com/auth/drive.readonly": "Google Drive API (read-only)"
          }
        }
      }
    }
  }
}
```

However, the spec treats OAuth tokens as transport-level credentials (sent via `Authorization` header), not application-level credentials embedded in the message payload. This creates a conflict when the service infrastructure also uses the `Authorization` header for its own authentication (as Cloud Run does).

A cleaner design would separate these concerns — service-to-service auth at the transport level, user-delegated credentials in the A2A message payload. See Christian Posta's [blog post on A2A OAuth user delegation](https://blog.christianposta.com/setting-up-a2a-oauth-user-delegation/) for a broader discussion of this pattern.

## Prerequisites

- Google Cloud project with billing enabled
- `gcloud` CLI authenticated
- `uv` (Python package manager)
- Gemini Enterprise instance
- OAuth 2.0 client (Web Application type) with Drive API enabled

## Quick Start

See each project's own README for setup and deployment instructions:
- [`adk-ae-oauth/README.md`](adk-ae-oauth/README.md) — Agent Engine deployment
- [`adk-a2a-cr-oauth/README.md`](adk-a2a-cr-oauth/README.md) — Cloud Run + A2A deployment
