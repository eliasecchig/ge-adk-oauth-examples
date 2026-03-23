# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import google.auth
import httpx
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.apps.jsonrpc.jsonrpc_app import CallContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.agent_execution.context import RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AuthorizationCodeOAuthFlow,
    OAuth2SecurityScheme,
    OAuthFlows,
    SecurityScheme,
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    EXTENDED_AGENT_CARD_PATH,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.cloud import logging as google_cloud_logging

from app.agent import app as adk_app
from app import auths
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback

setup_telemetry()

py_logger = logging.getLogger(__name__)

_, project_id = google.auth.default()
logging_client = google_cloud_logging.Client()
cloud_logger = logging_client.logger(__name__)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")
artifact_service = (
    GcsArtifactService(bucket_name=logs_bucket_name)
    if logs_bucket_name
    else InMemoryArtifactService()
)

# ---------------------------------------------------------------------------
# App-level OAuth token validation.
# Since Cloud Run must be --allow-unauthenticated (Gemini Enterprise sends
# the user's OAuth token, not a service identity token), we validate the
# token ourselves: check that its aud/azp matches our OAuth client ID.
# ---------------------------------------------------------------------------
EXPECTED_OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")

# Paths that don't require authentication (A2A discovery)
PUBLIC_PATHS = {
    "/.well-known/agent-card.json",
    "/a2a/app/.well-known/agent-card.json",
}

# Simple TTL cache for validated tokens to avoid hitting tokeninfo on every request
_token_cache: dict[str, float] = {}
_TOKEN_CACHE_TTL = 300  # 5 minutes


def _is_public_path(path: str) -> bool:
    return any(path.endswith(p) for p in PUBLIC_PATHS)


async def _validate_token(token: str) -> bool:
    """Validate a Bearer token by checking its aud/azp against our OAuth client ID."""
    if not EXPECTED_OAUTH_CLIENT_ID:
        # No client ID configured — skip validation (local dev)
        return True

    # Check cache
    now = time.time()
    if token in _token_cache and _token_cache[token] > now:
        return True

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://oauth2.googleapis.com/tokeninfo?access_token={token}",
                timeout=5,
            )
        if resp.status_code != 200:
            py_logger.warning("Token validation failed: tokeninfo returned %s", resp.status_code)
            return False

        info = resp.json()
        token_client_id = info.get("azp") or info.get("aud", "")
        if token_client_id != EXPECTED_OAUTH_CLIENT_ID:
            py_logger.warning(
                "Token validation failed: azp/aud '%s' does not match expected '%s'",
                token_client_id, EXPECTED_OAUTH_CLIENT_ID,
            )
            return False

        # Cache the validated token until it expires
        expires_in = int(info.get("expires_in", _TOKEN_CACHE_TTL))
        _token_cache[token] = now + min(expires_in, _TOKEN_CACHE_TTL)

        # Evict expired entries periodically
        if len(_token_cache) > 100:
            _token_cache.clear()

        return True
    except Exception as e:
        py_logger.error("Token validation error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Custom CallContextBuilder: extracts the OAuth Bearer token from the HTTP
# Authorization header and stores it in ServerCallContext.state so that the
# A2aAgentExecutor can propagate it to the ADK session state.
# ---------------------------------------------------------------------------
class OAuthCallContextBuilder(CallContextBuilder):
    """Extracts the Bearer token from the HTTP request and places it in state."""

    def build(self, request: Request) -> ServerCallContext:
        state: dict = {}
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            state[auths.TOKEN_CACHE_KEY] = token
            print(f"[OAuthCallContextBuilder] Injected token from header under key '{auths.TOKEN_CACHE_KEY}'", flush=True)
        return ServerCallContext(state=state)


# ---------------------------------------------------------------------------
# Custom A2aAgentExecutor: overrides _prepare_session to inject the OAuth
# token from call_context.state into run_request.state_delta, which
# Runner.run_async() applies to the session before the agent executes.
# ---------------------------------------------------------------------------
class OAuthA2aAgentExecutor(A2aAgentExecutor):
    """Extends A2aAgentExecutor to propagate call_context.state via state_delta."""

    async def _prepare_session(self, context: RequestContext, run_request, runner: Runner):
        session = await super()._prepare_session(context, run_request, runner)

        # Inject call_context.state into run_request.state_delta so that
        # Runner.run_async() applies it to the session before tool execution.
        if context.call_context and context.call_context.state:
            delta = {}
            for key, value in context.call_context.state.items():
                if key == "method":
                    continue
                delta[key] = value
            if delta:
                run_request.state_delta = {**(run_request.state_delta or {}), **delta}
                print(f"[OAuthA2aAgentExecutor] Set state_delta keys: {list(delta.keys())}", flush=True)

        return session


# ---------------------------------------------------------------------------
# Wire it all together
# ---------------------------------------------------------------------------
runner = Runner(
    app=adk_app,
    artifact_service=artifact_service,
    session_service=InMemorySessionService(),
)

agent_executor = OAuthA2aAgentExecutor(runner=runner)

request_handler = DefaultRequestHandler(
    agent_executor=agent_executor, task_store=InMemoryTaskStore()
)

A2A_RPC_PATH = f"/a2a/{adk_app.name}"


async def build_dynamic_agent_card() -> AgentCard:
    """Builds the Agent Card dynamically from the root_agent."""
    # Declare OAuth2 security scheme so clients (Gemini Enterprise) know
    # this agent requires a user OAuth token with drive.readonly scope.
    oauth2_scheme = SecurityScheme(
        OAuth2SecurityScheme(
            description="Google OAuth2 for Drive access",
            flows=OAuthFlows(
                authorization_code=AuthorizationCodeOAuthFlow(
                    authorization_url=auths.AUTHORIZATION_URL,
                    token_url=auths.TOKEN_URL,
                    scopes=auths.SCOPES,
                ),
            ),
        )
    )

    agent_card_builder = AgentCardBuilder(
        agent=adk_app.root_agent,
        capabilities=AgentCapabilities(streaming=True),
        rpc_url=f"{os.getenv('APP_URL', 'http://0.0.0.0:8000')}{A2A_RPC_PATH}",
        agent_version=os.getenv("AGENT_VERSION", "0.1.0"),
        security_schemes={"google_drive_oauth": oauth2_scheme},
    )
    agent_card = await agent_card_builder.build()
    return agent_card


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    agent_card = await build_dynamic_agent_card()
    a2a_app = A2AFastAPIApplication(
        agent_card=agent_card,
        http_handler=request_handler,
        context_builder=OAuthCallContextBuilder(),
    )
    a2a_app.add_routes_to_app(
        app_instance,
        agent_card_url=f"{A2A_RPC_PATH}{AGENT_CARD_WELL_KNOWN_PATH}",
        rpc_url=A2A_RPC_PATH,
        extended_agent_card_url=f"{A2A_RPC_PATH}{EXTENDED_AGENT_CARD_PATH}",
    )
    yield


app = FastAPI(
    title="adk-a2a-cr-oauth",
    description="API for interacting with the Agent adk-a2a-cr-oauth",
    lifespan=lifespan,
)


@app.middleware("http")
async def validate_oauth_token(request: Request, call_next):
    """Reject requests without a valid OAuth token (except public paths)."""
    if _is_public_path(request.url.path):
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"error": "Missing Bearer token"})

    token = auth_header[7:]
    if not await _validate_token(token):
        return JSONResponse(status_code=401, content={"error": "Invalid or unauthorized token"})

    return await call_next(request)


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    cloud_logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
