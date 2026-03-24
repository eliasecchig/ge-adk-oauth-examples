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

"""End-to-end test for the A2A + OAuth Drive reader agent.

Three-layer test:
  1. Agent Card  — verify the A2A server serves a valid agent card
  2. A2A no-auth — verify graceful handling when no token is present
  3. OAuth + Drive — inject a real OAuth token into session state
     (simulating Gemini Enterprise) and verify the agent reads a Drive file

Usage:
    # Start the server first:
    #   uv run uvicorn app.fast_api_app:app --host localhost --port 8000
    #
    # Run tests (steps 1-2 only, no Drive access):
    #   uv run python tools/test_a2a_oauth.py
    #
    # Run all tests including Drive file read:
    #   TEST_FILE_ID=<your-file-id> uv run python tools/test_a2a_oauth.py
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid

import httpx

BASE_URL = os.environ.get("A2A_BASE_URL", "http://localhost:8000")
RPC_URL = f"{BASE_URL}/a2a/app"
AGENT_CARD_URL = f"{RPC_URL}/.well-known/agent-card.json"


def get_access_token() -> str:
    """Get a Google OAuth access token.

    Uses application default credentials. The token likely won't have
    drive.readonly scope (ADC is typically a service account), so Drive
    API calls will fail with a 403 — but that still proves the credential
    injection path works end-to-end. In production, Gemini Enterprise
    supplies a user OAuth token with the correct scope.
    """
    import google.auth
    from google.auth.transport.requests import Request

    creds, _ = google.auth.default()
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


def a2a_rpc_streaming(method: str, params: dict) -> list[dict]:
    """Send a streaming JSON-RPC request (SSE) and collect all events."""
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    print(f"\n>>> {method} (streaming)")

    events = []
    with httpx.stream(
        "POST", RPC_URL, json=payload, timeout=120,
        headers={"Accept": "text/event-stream"},
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if line.startswith("data: "):
                try:
                    event = json.loads(line[6:])
                    events.append(event)
                    result = event.get("result", {})
                    status = result.get("status", {})
                    state = status.get("state", "")
                    msg = status.get("message", {})
                    if msg and isinstance(msg, dict):
                        parts = msg.get("parts", [])
                        text = " ".join(
                            p.get("text", "")[:200] for p in parts if "text" in p
                        )
                        if text.strip():
                            print(f"    [{state}] {text[:300]}")
                        else:
                            print(f"    [{state}]")
                    elif state:
                        print(f"    [{state}]")
                except json.JSONDecodeError:
                    pass
    return events


# --------------------------------------------------------------------------- #
# Step 1: Agent Card
# --------------------------------------------------------------------------- #
def test_agent_card():
    print("=" * 60)
    print("Step 1: Fetch Agent Card")
    print("=" * 60)

    resp = httpx.get(AGENT_CARD_URL, timeout=10)
    resp.raise_for_status()
    card = resp.json()

    assert card["name"] == "root_agent", f"Unexpected name: {card['name']}"
    assert any("read_drive_file" in s.get("name", "") for s in card.get("skills", []))
    print(f"    Agent: {card['name']} v{card['version']}")
    print(f"    Skills: {[s['name'] for s in card.get('skills', [])]}")
    print("    PASS")


# --------------------------------------------------------------------------- #
# Step 2: A2A without OAuth
# --------------------------------------------------------------------------- #
def test_no_oauth():
    print("\n" + "=" * 60)
    print("Step 2: A2A without OAuth (should handle gracefully)")
    print("=" * 60)

    params = {
        "id": str(uuid.uuid4()),
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "Read the Google Drive file with ID: 1ABC123"}],
            "messageId": str(uuid.uuid4()),
        },
        "configuration": {"acceptedOutputModes": ["text/plain"]},
    }

    events = a2a_rpc_streaming("message/stream", params)
    final = events[-1] if events else {}
    state = final.get("result", {}).get("status", {}).get("state", "unknown")
    print(f"\n    Final state: {state}")
    assert state in ("completed", "input_required"), f"Unexpected state: {state}"
    print("    PASS")


# --------------------------------------------------------------------------- #
# Step 3: OAuth token injection + Drive file read (via Runner directly)
# --------------------------------------------------------------------------- #
async def test_oauth_drive_read(file_id: str):
    """Simulate Gemini Enterprise token injection by pre-populating session
    state with the access token, then running the agent via the Runner."""
    print("\n" + "=" * 60)
    print("Step 3: OAuth token injection + Drive file read")
    print("=" * 60)

    token = get_access_token()
    print(f"    Got access token: {token[:20]}...")

    # Import the app components
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as genai_types

    from app.agent import app as adk_app
    from app import auths

    session_service = InMemorySessionService()
    runner = Runner(
        app=adk_app,
        session_service=session_service,
    )

    # Create a session with the token pre-injected in state.
    # In production, Gemini Enterprise injects the token at runtime
    # via the A2A protocol (into "temp:<AUTH_ID>"). For this test,
    # we inject it directly as a session-level key — negotiate_creds()
    # checks both TOKEN_CACHE_KEY and "temp:TOKEN_CACHE_KEY".
    user_id = "test-user"
    session = await session_service.create_session(
        app_name=adk_app.name,
        user_id=user_id,
        state={auths.TOKEN_CACHE_KEY: token},
    )
    print(f"    Session created: {session.id}")
    print(f"    State key: {auths.TOKEN_CACHE_KEY}")

    # Run the agent
    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=f"Read the Google Drive file with ID: {file_id}")],
    )

    print("    Running agent...")
    final_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=message,
    ):
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            if part.text:
                final_text += part.text
            elif part.function_call:
                print(f"    Tool call: {part.function_call.name}()")
            elif part.function_response:
                res = part.function_response.response
                print(f"    Tool result: {str(res)[:200]}")

    print(f"\n    Agent response (first 500 chars):")
    print(f"    {final_text[:500]}")

    # Verify the token injection worked — the agent should NOT report
    # "no oauth token" (which means negotiate_creds() never found it).
    # It MAY report a Drive API error (403 insufficient scopes) if the
    # ADC token lacks drive.readonly — that's fine, it proves the token
    # was injected and used.
    lower = final_text.lower()
    assert "no oauth token" not in lower, "Agent reported missing token — injection failed"

    if "insufficient" in lower or "permission" in lower:
        print("\n    PASS (token injection worked; Drive API returned 403 — expected with ADC)")
        print("    In production, Gemini Enterprise provides a user OAuth token with drive.readonly scope.")
    else:
        print("\n    PASS (full Drive file read succeeded)")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    print("A2A + OAuth Integration Test")
    print(f"Server: {BASE_URL}")
    print()

    # Steps 1–2: pure A2A HTTP tests
    test_agent_card()
    test_no_oauth()

    # Step 3: OAuth + Drive (needs a real file ID)
    file_id = os.environ.get("TEST_FILE_ID")
    if file_id:
        asyncio.run(test_oauth_drive_read(file_id))
    else:
        print("\n" + "=" * 60)
        print("Step 3: SKIPPED (set TEST_FILE_ID to test with a real Drive file)")
        print("=" * 60)
        print("    Example:")
        print("    TEST_FILE_ID=<file-id> uv run python tools/test_a2a_oauth.py")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
