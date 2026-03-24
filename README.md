# ADK OAuth Examples

Two reference implementations of a Google Drive reader agent with OAuth 2.0, deployed to Google Cloud and registered with [Gemini Enterprise](https://cloud.google.com/products/gemini/enterprise).

Both agents do the same thing — read files from Google Drive on behalf of the user — but use different deployment targets and protocols.

| | `adk-ae-oauth` | `adk-a2a-cr-oauth` |
|---|---|---|
| **Deployment target** | Vertex AI Agent Engine | Cloud Run (FastAPI) |
| **Protocol** | ADK HTTP | A2A JSON-RPC |
| **Entry point** | `AgentEngineApp` | `A2AFastAPIApplication` |
| **OAuth flow** | ADK's `request_credential()` cycle | Gemini Enterprise handles consent, token arrives via HTTP header |
| **Token delivery** | `tool_context.state["temp:<AUTH_ID>"]` | HTTP `Authorization: Bearer` header (extracted via custom `CallContextBuilder` + `A2aAgentExecutor`) |
| **Infrastructure auth** | Agent Engine manages access | `--allow-unauthenticated` + app-level token validation |

## How OAuth Works

### Agent Engine

Gemini Enterprise handles the OAuth consent flow and injects the user's access token directly into `tool_context.state["temp:<AUTH_ID>"]`. No custom bridge code is needed — Agent Engine manages sessions and token delivery natively.

### A2A on Cloud Run

Gemini Enterprise handles the OAuth consent flow and sends the user's access token as the HTTP `Authorization: Bearer` header on each request. Two custom components bridge the token from the HTTP layer into ADK session state:

```
Gemini Enterprise
    │  POST /a2a/app with Authorization: Bearer <user-oauth-token>
    v
Cloud Run (--allow-unauthenticated)
    │  App-level middleware validates token aud/azp
    v
OAuthCallContextBuilder
    │  Extracts Bearer token → ServerCallContext.state
    v
OAuthA2aAgentExecutor
    │  Copies call_context.state → run_request.state_delta
    v
Runner.run_async()
    │  Applies state_delta to session
    v
negotiate_creds() → Credentials(token=...) → Google Drive API
```

**`OAuthCallContextBuilder`** extracts the Bearer token from the HTTP header and stores it in `ServerCallContext.state`.

**`OAuthA2aAgentExecutor`** overrides `_prepare_session()` to copy `call_context.state` into `run_request.state_delta`, ensuring the token reaches `tool_context.state` when tools execute.

**Token validation middleware** checks that the Bearer token's `azp`/`aud` matches the expected OAuth client ID (via Google's `tokeninfo` endpoint, with caching). Agent card discovery paths are exempt.

### Cloud Run Security Model

Cloud Run IAM expects OIDC identity tokens in the `Authorization` header, while Gemini Enterprise sends OAuth access tokens — a token type mismatch. A2A agents receiving user OAuth tokens from Gemini Enterprise must use `--allow-unauthenticated` with `--ingress=all`, and enforce security at the application level instead.

When deploying without IAM, consider these hardening options:

1. **Validate tokens at the A2A server** — reject invalid tokens before routing to the agent backend.
2. **Place an API Gateway in front of Cloud Run** — validate tokens at the gateway, restrict Cloud Run to gateway-only traffic, and add Cloud Armor for rate limiting and DDoS mitigation.
3. **Use VPC Service Controls** — place both Gemini Enterprise and Cloud Run inside the same VPC SC perimeter, allowing `--ingress=internal` on Cloud Run while network access is restricted to the perimeter.

## Prerequisites

- Google Cloud project with billing enabled
- `gcloud` CLI authenticated
- `uv` (Python package manager)
- Gemini Enterprise instance
- OAuth 2.0 client (Web Application type) with Drive API enabled and these redirect URIs:
  - `https://vertexaisearch.cloud.google.com/oauth-redirect`
  - `https://vertexaisearch.cloud.google.com/static/oauth/oauth.html`

## Quick Start

See each project's README for setup and deployment instructions:
- [`adk-ae-oauth/README.md`](adk-ae-oauth/README.md) — Agent Engine deployment
- [`adk-a2a-cr-oauth/README.md`](adk-a2a-cr-oauth/README.md) — Cloud Run + A2A deployment

## Agent Guide

See [`AGENT_GUIDE.md`](AGENT_GUIDE.md) for a detailed reference on ADK internals, A2A protocol details, OAuth token flow mechanics, Cloud Run deployment patterns, and debugging strategies — all based on building these examples.

## Acknowledgements

Built on top of [`practical-gcp-examples`](https://github.com/richardhe-fundamenta/practical-gcp-examples) by [richardhe-fundamenta](https://github.com/richardhe-fundamenta).
