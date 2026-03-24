# Agent Guide: ADK Agents with OAuth + Gemini Enterprise

Practical learnings from building and deploying ADK agents with OAuth across two deployment targets — Vertex AI Agent Engine and Cloud Run (A2A protocol) — both registered with Gemini Enterprise. These supplement the official documentation with implementation details discovered while building these examples.

This guide covers both projects in this repo:
- **`adk-ae-oauth`** — Agent Engine deployment, ADK's native OAuth flow
- **`adk-a2a-cr-oauth`** — Cloud Run deployment via A2A JSON-RPC, custom OAuth bridge

---

## 1. ADK Internals

### 1.1. `App` vs `AgentEngineApp` — choose by deployment target

| Target | Entry point class | Base class | Module |
|---|---|---|---|
| Cloud Run (A2A) | `App` | `google.adk.apps.App` | `google.adk.apps` |
| Agent Engine | `AgentEngineApp` (custom subclass) | `vertexai.agent_engines.templates.adk.AdkApp` | `vertexai.agent_engines.templates.adk` |

On Cloud Run, `App` is wrapped in `A2AFastAPIApplication` + `A2aAgentExecutor`. On Agent Engine, `AgentEngineApp` wraps `App` and adds telemetry, logging, feedback collection, and `register_operations()` for custom methods.

### 1.2. `AgentEngineApp` deployment pattern

```python
from vertexai.agent_engines.templates.adk import AdkApp

class AgentEngineApp(AdkApp):
    def set_up(self) -> None:
        vertexai.init()
        setup_telemetry()
        super().set_up()

    def register_operations(self) -> dict[str, list[str]]:
        operations = super().register_operations()
        operations[""] = operations.get("", []) + ["register_feedback"]
        return operations

agent_engine = AgentEngineApp(
    app=adk_app,  # plain ADK App instance
    artifact_service_builder=lambda: GcsArtifactService(...) or InMemoryArtifactService(),
)
```

Custom methods are exposed to Agent Engine via `register_operations()`. The entrypoint for deployment is `app.agent_engine_app:agent_engine`.

### 1.3. `A2aAgentExecutor` creates sessions with empty state

The ADK's `A2aAgentExecutor._prepare_session()` creates sessions with `state={}`, completely ignoring `context.call_context.state` from the A2A SDK. If you need to pass data from the HTTP request (like OAuth tokens) into the agent's session state, you must subclass `A2aAgentExecutor` and override `_prepare_session`.

### 1.4. `Runner.run_async()` fetches a fresh session — direct state modifications are lost

Even if you modify `session.state` in `_prepare_session()`, `Runner.run_async()` internally calls `self._get_or_create_session()` which fetches a *fresh* session from the session service. Your modifications to the session object are silently discarded.

**The fix**: use `run_request.state_delta` instead. The `AgentRunRequest` model has a `state_delta` field that `Runner.run_async()` applies to the session *before* tool execution:

```python
class OAuthA2aAgentExecutor(A2aAgentExecutor):
    async def _prepare_session(self, context, run_request, runner):
        session = await super()._prepare_session(context, run_request, runner)
        if context.call_context and context.call_context.state:
            delta = {}
            for key, value in context.call_context.state.items():
                if key == "method":  # Skip A2A SDK internal key
                    continue
                delta[key] = value
            if delta:
                run_request.state_delta = {**(run_request.state_delta or {}), **delta}
        return session
```

This is the single most critical insight for A2A deployments: there are two separate session lookups in the ADK flow, and modifying the session from the first lookup has no effect because `run_async` fetches a fresh copy.

### 1.5. `InMemorySessionService.create_session()` strips `temp:` prefixed keys

The `extract_state_delta()` function in ADK drops keys with the `temp:` prefix during session creation. If you inject tokens with `temp:` prefixed keys at session creation time (e.g., for testing), the values silently disappear. Use non-prefixed keys when creating sessions programmatically.

### 1.6. `tool_context.request_credential()` only works in ADK Web UI — not through A2A

The ADK's `request_credential()` / `get_auth_response()` OAuth cycle is designed for the ADK Web UI (local dev). Tokens sent via this mechanism never reach the agent when accessed through the A2A JSON-RPC protocol. For A2A agents, the token must arrive via HTTP headers and be bridged into session state. On Agent Engine, Gemini Enterprise injects tokens directly into session state, bypassing `request_credential()` entirely.

### 1.7. `EventActions` has no `tool_calls` or `tool_results` attributes

Tool calls and results are embedded in `event.content.parts` as `part.function_call` and `part.function_response`, not on `event.actions`. Code that assumes `event.actions.tool_calls` will throw `AttributeError`. The correct iteration pattern:

```python
async for event in runner.run_async(user_id=..., session_id=..., new_message=...):
    if not event.content or not event.content.parts:
        continue
    for part in event.content.parts:
        if part.text:
            ...  # agent response text
        elif part.function_call:
            ...  # tool invocation
        elif part.function_response:
            ...  # tool result
```

### 1.8. ADK A2A classes are marked EXPERIMENTAL

`A2aAgentExecutor` and related classes emit `UserWarning: [EXPERIMENTAL]` at import. This is informational, not an error — but pin your `google-adk` version to avoid breaking changes.

### 1.9. `State` class wraps a dict with `_value` and `_delta`

The `State` class has `_value` (original state) and `_delta` (changes). `__getitem__` checks `_delta` first, then `_value`. This means the preferred path for injecting data is through `state_delta` on the run request, which gets merged into `_delta` before tool execution.

---

## 2. OAuth Token Flow

### 2.1. Token delivery differs by deployment target

| | Agent Engine | Cloud Run (A2A) |
|---|---|---|
| **Who handles OAuth consent** | Gemini Enterprise | Gemini Enterprise |
| **How token arrives** | Injected into `tool_context.state["temp:<AUTH_ID>"]` | HTTP `Authorization: Bearer` header |
| **Custom bridge needed** | No — Agent Engine manages session state | Yes — `OAuthCallContextBuilder` + `OAuthA2aAgentExecutor` |
| **Token format** | Raw access token string | Raw access token string |

### 2.2. Three-stage credential negotiation pattern

The same `negotiate_creds()` function works for both targets, distinguished by which stage succeeds first:

```python
def negotiate_creds(tool_context):
    # Stage 1: Check state for cached/injected token
    # - Agent Engine production: finds token at "temp:<AUTH_ID>" (injected by GE)
    # - A2A production: finds token at TOKEN_CACHE_KEY (injected via state_delta)
    # - Local dev: finds cached credential dict from previous calls
    token = tool_context.state.get(TOKEN_CACHE_KEY)
    if not token:
        token = tool_context.state.get(f"temp:{TOKEN_CACHE_KEY}")
    if token:
        if isinstance(token, dict):
            # Local dev: full credential dict with refresh_token
            creds = Credentials.from_authorized_user_info(token, scopes)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                tool_context.state[TOKEN_CACHE_KEY] = json.loads(creds.to_json())
            return creds
        elif isinstance(token, str):
            # Production: raw access token string
            return Credentials(token=token)

    # Stage 2: Check ADK auth response (local ADK Web UI after OAuth callback)
    if exchanged := tool_context.get_auth_response(AUTH_CONFIG):
        creds = Credentials(
            token=exchanged.oauth2.access_token,
            refresh_token=exchanged.oauth2.refresh_token,
            token_uri=..., client_id=..., client_secret=..., scopes=...,
        )
        tool_context.state[TOKEN_CACHE_KEY] = json.loads(creds.to_json())
        return creds

    # Stage 3: Initiate OAuth flow (local dev only)
    # Guard: only call request_credential if client_id/secret are configured
    if AUTH_CREDENTIAL.oauth2.client_id and AUTH_CREDENTIAL.oauth2.client_secret:
        tool_context.request_credential(AUTH_CONFIG)
        return {"pending": True, "message": "Awaiting user authentication"}

    return {"status": "error", "message": "No OAuth token available"}
```

| Stage | Agent Engine | A2A (Cloud Run) | Local Dev |
|---|---|---|---|
| **Stage 1** | Succeeds (GE injects token as string) | Succeeds (state_delta injects token) | Succeeds after first auth (cached dict) |
| **Stage 2** | Skipped | Skipped | Used after user consents in ADK Web UI |
| **Stage 3** | Never reached | Never reached (guard fails — no client_id/secret on CR) | Triggered on first call |

### 2.3. Cached token format: `dict` vs `str`

Stage 1 must handle both formats:

- `isinstance(token, dict)` — Local dev: full credential object with `refresh_token`, `client_id`, etc. Can be refreshed.
- `isinstance(token, str)` — Production: raw access token injected by Gemini Enterprise. No refresh capability (Gemini Enterprise handles renewal).

If you try to call `.refresh()` on a string token, it crashes.

### 2.4. `AUTH_CONFIG` requires `client_id` and `client_secret`

Calling `tool_context.request_credential(AUTH_CONFIG)` triggers ADK to validate the config's `client_id` and `client_secret`. If these env vars are not set (as on Cloud Run in production), the call crashes. Guard Stage 3 with a check: `if client_id and client_secret`.

### 2.5. `TOKEN_CACHE_KEY` must match the AUTH_ID used during registration

The key used to look up the token in session state must match the `AUTH_ID` from the OAuth authorization resource registered with Discovery Engine. Default: `google-drive-auth`. If they don't match, `negotiate_creds()` looks under the wrong key and never finds the token.

### 2.6. The A2A token flow gap requires two custom components

For Cloud Run (A2A) deployments, the token arrives as an HTTP header but tools expect it in `tool_context.state`. Two custom classes bridge this:

| Layer | What | Component |
|---|---|---|
| HTTP → A2A state | Extract Bearer token from header | `OAuthCallContextBuilder` |
| A2A state → ADK session | Bridge via `state_delta` | `OAuthA2aAgentExecutor` |

Without both, the token is present on the HTTP request but invisible to `tool_context.state`.

---

## 3. OAuth Configuration (`auths.py`)

```python
# --- Endpoints ---
AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# --- Scopes ---
SCOPES = {"https://www.googleapis.com/auth/drive.readonly": "Google Drive API (read-only)"}

# --- Token cache key (must match AUTH_ID in Discovery Engine registration) ---
TOKEN_CACHE_KEY = os.environ.get("AUTH_ID", "google-drive-auth")

# --- OAuth scheme + credential (used ONLY for local ADK Web UI dev) ---
AUTH_SCHEME = OAuth2(flows=OAuthFlows(authorizationCode=OAuthFlowAuthorizationCode(...)))
AUTH_CREDENTIAL = AuthCredential(
    auth_type=AuthCredentialTypes.OAUTH2,
    oauth2=OAuth2Auth(
        client_id=os.environ.get("OAUTH_CLIENT_ID", ""),
        client_secret=os.environ.get("OAUTH_CLIENT_SECRET", ""),
    ),
)
AUTH_CONFIG = AuthConfig(auth_scheme=AUTH_SCHEME, raw_auth_credential=AUTH_CREDENTIAL)
```

| Config | Used in production | Used in local dev | Purpose |
|---|---|---|---|
| `AUTHORIZATION_URL` / `TOKEN_URL` | No (A2A agent card only) | Yes (ADK Web UI) | Google OAuth endpoints |
| `SCOPES` | Yes (token caching) | Yes (credential creation) | Permissions requested |
| `TOKEN_CACHE_KEY` | Yes (state lookup key) | Yes (state cache key) | Where to find/store the token |
| `AUTH_SCHEME` | No | Yes (Stage 2/3) | OpenAPI OAuth2 flow definition |
| `AUTH_CREDENTIAL` | No | Yes (Stage 2/3) | Client ID/secret for token exchange |
| `AUTH_CONFIG` | No | Yes (passed to `get_auth_response()` / `request_credential()`) | Combined scheme + credential |

In production, `AUTH_CREDENTIAL.oauth2.client_id` and `client_secret` default to `""` — they are never used. The actual credentials are stored in the Discovery Engine authorization resource.

---

## 4. A2A Protocol

### 4.1. Auth is transport-level, not payload-level

The A2A specification defines authentication via `securitySchemes` in the agent card, with tokens sent via the HTTP `Authorization` header. There is no built-in mechanism for passing user-delegated credentials separately from service-level auth in the request payload. This creates a conflict when the hosting platform (Cloud Run) also uses the `Authorization` header for its own IAM auth.

### 4.2. Agent card discovery must remain public

The `/.well-known/agent-card.json` endpoint must be accessible without authentication so that A2A clients can discover agent capabilities. Any auth middleware must whitelist this path.

### 4.3. `SecurityScheme` is a Pydantic `RootModel` (discriminated union)

When declaring OAuth in the agent card:

- **Wrong**: `SecurityScheme(oauth2_security_scheme=OAuth2SecurityScheme(...))` — silently produces `mutualTLS` type
- **Correct**: `SecurityScheme(OAuth2SecurityScheme(...))` — pass the scheme as the positional root argument

If you get this wrong, your agent card declares the wrong auth type and clients won't perform OAuth.

### 4.4. The A2A SDK injects a `method` key into `ServerCallContext.state`

When building state from a request, the SDK adds a `method` key. When iterating over state to inject into the ADK session, skip this key to avoid conflicts.

### 4.5. A2A streaming uses SSE (Server-Sent Events)

The `message/stream` JSON-RPC method returns SSE with `data:` prefixed lines. Set `Accept: text/event-stream` and parse SSE format — don't expect a single JSON response. Final event state is either `completed` or `input_required`.

### 4.6. Agent card URL format for Cloud Run

The agent card URL follows: `https://<service-name>-<project-number>.<region>.run.app/a2a/<app-name>/.well-known/agent-card.json`. The `<app-name>` comes from the ADK `App` instance name (typically the root agent name, e.g., `app`).

---

## 5. Cloud Run + Gemini Enterprise

### 5.1. Cloud Run IAM auth and user OAuth tokens are fundamentally incompatible

`--no-allow-unauthenticated` expects an OIDC identity token (with the service URL as audience) in the `Authorization: Bearer` header. Gemini Enterprise sends the user's OAuth access token (with `aud=<oauth-client-id>` and `scope=drive.readonly`) in that same header. Cloud Run IAM rejects this token with 401 before the request ever reaches your application.

**You must use `--allow-unauthenticated`** and implement app-level token validation instead.

### 5.2. IAP has the same conflict

IAP also consumes the `Authorization` header for OIDC validation. It rejects the user's OAuth token before it reaches the application, so IAP is not a viable alternative.

### 5.3. `--ingress=internal-and-cloud-load-balancing` blocks Gemini Enterprise

Gemini Enterprise calls Cloud Run services over the *public internet*, not via GCP-internal networking. Setting ingress to anything other than `all` blocks all Gemini Enterprise requests (returns 404).

### 5.4. Use `roles/run.servicesInvoker` at project level

The Discovery Engine service agent (`service-{PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com`) needs `roles/run.servicesInvoker` granted at the *project* level. This is different from the commonly-used `roles/run.invoker` at the service level.

### 5.5. App-level token validation pattern

Since Cloud Run must be `--allow-unauthenticated`, enforce security with FastAPI middleware:

```python
@app.middleware("http")
async def validate_oauth_token(request, call_next):
    if _is_public_path(request.url.path):  # agent card discovery exempt
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"error": "Missing Bearer token"})
    token = auth_header[7:]
    if not await _validate_token(token):  # check aud/azp via tokeninfo, with TTL cache
        return JSONResponse(status_code=401, content={"error": "Invalid token"})
    return await call_next(request)
```

Set `OAUTH_CLIENT_ID` as a Cloud Run env var. When unset (local dev), skip validation. Cache validated tokens with a TTL (e.g., 5 minutes) to avoid hitting the tokeninfo endpoint on every request.

### 5.6. Hardening options when using OAuth without IAM

When Cloud Run is `--allow-unauthenticated`, consider:

1. **Validate the JWT at the A2A server early** — reject invalid tokens before routing to the agent backend. Without this, an attacker could send a fake JWT with arbitrary claims that gets processed until it reaches the agent.
2. **Place an API Gateway in front of Cloud Run** — configure the gateway to validate tokens before forwarding, and restrict Cloud Run to only accept invocations from the gateway. Add a load balancer and Cloud Armor for rate limiting, WAF rules, and DDoS mitigation.
3. **Use VPC Service Controls** — place both GE and Cloud Run inside the same VPC SC perimeter. GE traffic routes internally, allowing `--ingress=internal` on Cloud Run. You still need `--allow-unauthenticated`, but network access is restricted to the perimeter.

### 5.7. Gemini Enterprise sends the user's OAuth token, not a service identity token

The `Authorization: Bearer` header on A2A requests from Gemini Enterprise contains the *user's* OAuth access token (obtained from the consent flow). Token introspection via `https://oauth2.googleapis.com/tokeninfo` reveals `azp`/`aud` matching your OAuth client ID, not a service account.

### 5.7. `gcloud run logs read` is now `gcloud run services logs read`

The CLI command changed. `gcloud run logs read` returns "Invalid choice: 'logs'".

### 5.8. Python `logging` output may not appear in Cloud Run logs

Standard Python `logging.getLogger().info()` may not show in Cloud Run structured logs. `print(..., flush=True)` reliably appears in `gcloud run services logs read` output, as it writes to stderr which Cloud Run always captures.

---

## 6. Agent Engine + Gemini Enterprise

### 6.1. Agent Engine handles token injection natively

On Agent Engine, Gemini Enterprise injects the user's OAuth token into `tool_context.state["temp:<AUTH_ID>"]` automatically. No custom bridge code is needed — the platform manages the session and state persistence, so `negotiate_creds()` Stage 1 finds the token immediately.

### 6.2. Agent Engine manages sessions internally

When using `agent_engine` as the deployment target, Agent Engine manages sessions. If your code sets a `session_type`, clear it — Agent Engine overrides it.

### 6.3. Deployment entrypoint is the `AgentEngineApp` instance

The `make deploy` command passes `--entrypoint-module=app.agent_engine_app --entrypoint-object=agent_engine`. This tells Agent Engine to use the `AgentEngineApp` subclass as the entry point, not the plain `App`.

### 6.4. `register_operations()` exposes custom methods

Custom methods (like `register_feedback`) are registered via `register_operations()`:

```python
def register_operations(self) -> dict[str, list[str]]:
    operations = super().register_operations()
    operations[""] = operations.get("", []) + ["register_feedback"]
    return operations
```

Without this, Agent Engine won't route calls to your custom methods.

### 6.5. Deployment metadata is saved locally

After deployment, `deploy.py` writes `deployment_metadata.json` with the Agent Engine resource name. This allows subsequent commands to reference the deployed agent without prompting.

---

## 7. agent-starter-pack & Registration

### 7.1. Scaffolding commands

```bash
# Agent Engine deployment
uvx agent-starter-pack create <name> --agent adk --deployment-target agent_engine --prototype -y

# A2A on Cloud Run
uvx agent-starter-pack create <name> --agent adk_a2a --deployment-target cloud_run --prototype -y
```

Never write A2A code from scratch — the import paths and `AgentCard` schema change across versions. Always scaffold first, then customize.

### 7.2. `register-gemini-enterprise` does three things

1. Creates an Agent resource under Discovery Engine, pointing to the agent's endpoint (Agent Engine resource name or Cloud Run agent card URL).
2. Grants IAM permissions (`roles/run.servicesInvoker` at project level to Discovery Engine SA).
3. Links OAuth authorization when `--authorization-id` is passed.

### 7.3. OAuth authorization resources have deletion propagation delays

After deleting an agent linked to an OAuth authorization resource, the resource may remain "in use" for several minutes. Registering a new agent with the same authorization during this window returns HTTP 400: `"One or more of the referenced authorizations are already in use by an agent."` Workaround: register without `authorizationConfig` first, then patch it later — or wait.

### 7.4. Cannot delete an authorization resource while an agent references it

The Discovery Engine API rejects deletion of an authorization resource that is still linked to an agent via `authorizationConfig`. Sequence: unregister agent → delete auth resource → re-create auth resource → re-register agent.

### 7.5. Discovery Engine API is accessed via REST, not gcloud

`gcloud discovery-engine` is not a valid command. You must use direct REST calls:

```bash
curl -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "X-Goog-User-Project: <project>" \
  "https://global-discoveryengine.googleapis.com/v1alpha/..."
```

### 7.6. OAuth resource registration payload

```json
{
    "oauthConfig": {
        "clientId": "<CLIENT_ID>",
        "clientSecret": "<CLIENT_SECRET>",
        "authorizationUri": "https://accounts.google.com/o/oauth2/v2/auth?access_type=offline&prompt=consent&scope=<SCOPES>",
        "tokenUri": "https://oauth2.googleapis.com/token",
        "redirectUri": "https://vertexaisearch.cloud.google.com/oauth-redirect"
    }
}
```

The `authorizationUri` must include query parameters like `access_type=offline` and `prompt=consent`.

### 7.7. Discovery Engine endpoint location mapping

The location in the resource name (global/us/eu) doesn't directly map to the API endpoint hostname. You need to map explicitly: `global` and `us` → `global-discoveryengine.googleapis.com`, `eu` → `eu-discoveryengine.googleapis.com`.

---

## 8. OAuth Client & Consent Screen

### 8.1. OAuth client must be Web Application type, created via Cloud Console

OAuth clients created via `gcloud iap oauth-clients create` are IAP-specific and don't support custom redirect URIs. Gemini Enterprise requires these redirect URIs:
- `https://vertexaisearch.cloud.google.com/oauth-redirect`
- `https://vertexaisearch.cloud.google.com/static/oauth/oauth.html`

There is no programmatic API to create standard Web Application OAuth clients. This step requires manual Cloud Console access.

### 8.2. Internal OAuth consent screens skip scope pre-registration

For apps with `orgInternalOnly: true`, Google does not enforce scope pre-registration on the consent screen. The `drive.readonly` scope embedded in the authorization URI is sufficient.

### 8.3. Drive API must be explicitly enabled

The Drive API (`drive.googleapis.com`) must be enabled on the project before the agent can use it or before you can add drive scopes to the consent screen. Use `gcloud services enable drive.googleapis.com`.

### 8.4. No CLI/API exists to create OAuth clients or configure consent screen scopes

Both creating Web Application OAuth clients and adding scopes to the consent screen must be done through the Google Cloud Console UI. These steps cannot be automated.

---

## 9. Correct Imports

### A2A SDK (Cloud Run deployments)

```python
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.apps.jsonrpc.jsonrpc_app import CallContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.agent_execution.context import RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities, AgentCard,
    AuthorizationCodeOAuthFlow, OAuth2SecurityScheme,
    OAuthFlows, SecurityScheme,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, EXTENDED_AGENT_CARD_PATH

from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
from google.adk.runners import Runner
```

### ADK OAuth (both targets)

```python
from fastapi.openapi.models import OAuth2, OAuthFlowAuthorizationCode, OAuthFlows
from google.adk.auth.auth_credential import AuthCredential, AuthCredentialTypes, OAuth2Auth
from google.adk.auth.auth_tool import AuthConfig
```

### Agent Engine deployments

```python
from vertexai.agent_engines.templates.adk import AdkApp
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
```

---

## 10. Debugging Playbook

### Open the service to diagnose auth issues

Temporarily set `--allow-unauthenticated` and add logging middleware to see what tokens/identities actually arrive. This separates "is the caller blocked at infra level?" from "is the application logic failing?"

### Token introspection for identity discovery

Add temporary middleware that calls `https://oauth2.googleapis.com/tokeninfo?access_token=<token>` and logs the full response. This reveals the caller's identity, scopes, audience, and expiry. This is how we discovered Gemini Enterprise sends the user's OAuth token, not a service account token.

### Confirm state injection end-to-end (A2A only)

Add print statements in: (1) `CallContextBuilder.build()` — token extracted from header, (2) `_prepare_session()` — token injected into `state_delta`, (3) `negotiate_creds()` — token found in `tool_context.state`. The first two can succeed while the third fails if `state_delta` is not used (direct `session.state` modification gets overwritten by `Runner.run_async`).

### Test locally before deploying

**A2A (Cloud Run)**: Spin up with `uvicorn app.fast_api_app:app --host localhost --port 8000` and send A2A requests with curl. A fake/ADC token confirms the plumbing works — errors like "insufficient scopes" prove the injection path is wired correctly even if the token is invalid for Drive.

```bash
curl -X POST http://localhost:8000/a2a/app \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -d '{"jsonrpc":"2.0","id":"test","method":"message/send","params":{
    "message":{"role":"user","parts":[{"text":"Read file 1abc2def3ghi"}]},
    "configuration":{"acceptedOutputModes":["text"]}}}'
```

**Agent Engine**: Use `make playground` to launch the ADK Web UI on port 8501. Test the full OAuth flow interactively with `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET` set in `.env`.

### Use `print(flush=True)` for Cloud Run debugging

Python `logging` module output can be swallowed in Cloud Run structured logs. `print(..., flush=True)` reliably appears in `gcloud run services logs read` output.

### ADC tokens don't have Drive scopes

Application Default Credentials tokens don't include `drive.readonly` scope. When testing locally with `gcloud auth print-access-token`, the Drive API call returns 403 "insufficient scopes" — this proves the injection pipeline works even though the token can't read actual files.

---

## 11. Architecture Summary

### Agent Engine

```
User → Gemini Enterprise UI → OAuth consent → Gemini Enterprise
  → Agent Engine (managed infra, sessions handled automatically)
    → tool_context.state["temp:<AUTH_ID>"] = token
      → negotiate_creds() Stage 1 → Credentials(token=...) → Drive API
```

No custom bridge code needed. Agent Engine handles everything.

### Cloud Run (A2A)

```
User → Gemini Enterprise UI → OAuth consent → Gemini Enterprise
  → POST /a2a/app with Authorization: Bearer <user-oauth-token>
    → App-level middleware validates token aud/azp
      → OAuthCallContextBuilder extracts token → ServerCallContext.state
        → OAuthA2aAgentExecutor copies to run_request.state_delta
          → Runner.run_async applies state_delta to session
            → negotiate_creds() Stage 1 → Credentials(token=...) → Drive API
```

| Layer | Configuration | Why |
|---|---|---|
| Cloud Run IAM | `--allow-unauthenticated` | GE sends user OAuth tokens, not identity tokens |
| Cloud Run Ingress | `--ingress=all` | GE calls over public internet |
| App-level Auth | Middleware validates `azp`/`aud` vs `OAUTH_CLIENT_ID` | Replaces Cloud Run IAM |
| Token Extraction | `OAuthCallContextBuilder` | Extracts Bearer token from HTTP header |
| Session Bridge | `OAuthA2aAgentExecutor` with `state_delta` | NOT `session.state` — that gets overwritten |
| Agent Card | `securitySchemes` with `oauth2` type | Declares OAuth requirement to A2A clients |
| Gemini Enterprise | Registered with `authorizationConfig` | Links the OAuth authorization resource |
