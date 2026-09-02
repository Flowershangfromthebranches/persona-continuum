from __future__ import annotations

import pytest

from persona_continuum.agent.adapters.grok import GrokBuildAdapter
from persona_continuum.agent.models import (
    AgentEventType,
    AgentSessionConfig,
    AgentStatus,
    AgentTurn,
)


def test_grok_headless_argv() -> None:
    adapter = GrokBuildAdapter()
    cfg = AgentSessionConfig(
        room_id="room_1",
        participant_id="slot_1",
        persona_id="elon_musk",
        model_id="grok-4.6",
        reasoning_effort="high",
    )
    argv = adapter.build_headless_argv("grok", cfg, "Explain colony on Mars")
    assert argv[0] == "grok"
    assert argv[1] == "-p"
    assert argv[2] == "Explain colony on Mars"
    assert "--output-format" in argv
    assert "streaming-json" in argv
    assert "--model" in argv
    assert "grok-4.6" in argv
    assert "--effort" in argv
    assert "high" in argv


@pytest.mark.anyio
async def test_grok_broken_binary_not_ready(tmp_path) -> None:
    # A script where --version exits with error
    mock_script = tmp_path / "mock_grok_broken.py"
    mock_script.write_text(
        r"""#!/usr/bin/env python3
import sys
sys.stderr.write("Fatal error in grok\n")
sys.exit(1)
"""
    )
    mock_script.chmod(0o755)

    adapter = GrokBuildAdapter()
    adapter.binary_candidates = [str(mock_script)]
    adapter._resolved_binary = str(mock_script)

    probe = await adapter.probe()
    assert probe.status == AgentStatus.BROKEN
    assert "error" in (probe.status_detail or "").lower()


def test_grok_parse_models_filters_greeting_and_defaults() -> None:
    raw_output = """You are logged in with grok.com.

Default model: grok-4.6

Available models:
  * grok-4.6 (default)
  - grok-4.5
  - ocx-gpt-5-6-sol
"""
    adapter = GrokBuildAdapter()
    models = adapter._parse_models(raw_output)
    model_ids = [m.id for m in models]
    assert "You" not in model_ids
    assert "default" not in model_ids
    assert "available" not in model_ids
    assert "grok-4.6" in model_ids
    assert "grok-4.5" in model_ids
    assert "ocx-gpt-5-6-sol" in model_ids


@pytest.mark.anyio
async def test_grok_requires_acp_authentication(tmp_path) -> None:
    from persona_continuum.agent.protocols.acp import AuthRequiredError

    mock_script = tmp_path / "mock_grok_auth.py"
    mock_script.write_text(
        r"""#!/usr/bin/env python3
import sys
import json

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue

    req_id = req.get("id")
    method = req.get("method")

    if method == "initialize":
        res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": 1,
                "authMethods": [{"id": "xai_key", "type": "api_key"}]
            }
        }
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
"""
    )
    mock_script.chmod(0o755)

    adapter = GrokBuildAdapter()
    adapter.binary_candidates = [str(mock_script)]
    adapter._resolved_binary = str(mock_script)

    cfg = AgentSessionConfig(
        room_id="r1",
        participant_id="p1",
        persona_id="persona_elon",
    )
    with pytest.raises(AuthRequiredError):
        await adapter.create_session(cfg)


@pytest.mark.anyio
async def test_grok_session_new_error_does_not_create_fake_session(tmp_path) -> None:
    mock_script = tmp_path / "mock_grok_sess_fail.py"
    mock_script.write_text(
        r"""#!/usr/bin/env python3
import sys
import json

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue

    req_id = req.get("id")
    method = req.get("method")

    if method == "initialize":
        res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"protocolVersion": 1, "authMethods": []},
        }
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    elif method == "session/new":
        err_res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": "Quota exceeded"},
        }
        sys.stdout.write(json.dumps(err_res) + "\n")
        sys.stdout.flush()
"""
    )
    mock_script.chmod(0o755)

    adapter = GrokBuildAdapter()
    adapter.binary_candidates = [str(mock_script)]
    adapter._resolved_binary = str(mock_script)

    cfg = AgentSessionConfig(room_id="r1", participant_id="p1", persona_id="persona_elon")
    with pytest.raises(RuntimeError) as exc_info:
        await adapter.create_session(cfg)
    assert "session/new failed" in str(exc_info.value)


@pytest.mark.anyio
async def test_grok_real_acp_agent_message_chunk_shape(tmp_path) -> None:
    mock_script = tmp_path / "mock_grok_real_update.py"
    mock_script.write_text(
        r"""#!/usr/bin/env python3
import sys
import json

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue

    req_id = req.get("id")
    method = req.get("method")

    if method == "initialize":
        res = {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": 1, "authMethods": []}}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    elif method == "session/new":
        res = {"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": "sess_real_acp_888"}}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    elif method == "session/prompt":
        # Stream official ACP V1 session/update structure
        update_notif = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess_real_acp_888",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "Official xAI Grok ACP streamed text"
                    }
                }
            }
        }
        sys.stdout.write(json.dumps(update_notif) + "\n")
        sys.stdout.flush()

        # Prompt response completes turn
        resp = {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
"""
    )
    mock_script.chmod(0o755)

    adapter = GrokBuildAdapter()
    adapter.binary_candidates = [str(mock_script)]
    adapter._resolved_binary = str(mock_script)

    cfg = AgentSessionConfig(room_id="r1", participant_id="p1", persona_id="persona_elon")
    session = await adapter.create_session(cfg)
    turn = AgentTurn(turn_id="t1", user_message="Tell me about Mars")

    events = []
    async for ev in adapter.send(session, turn):
        events.append(ev)

    chunk_events = [e for e in events if e.type == AgentEventType.CHUNK]
    assert len(chunk_events) == 1
    assert chunk_events[0].content == "Official xAI Grok ACP streamed text"

    done_ev = next(e for e in events if e.type == AgentEventType.DONE)
    assert done_ev.content == "Official xAI Grok ACP streamed text"
    await adapter.close(session)


@pytest.mark.anyio
async def test_acp_parses_params_update_content_text(tmp_path) -> None:
    from persona_continuum.agent.protocols.acp import ACPAdapter

    mock_script = tmp_path / "mock_generic_acp_parser.py"
    mock_script.write_text(
        r"""#!/usr/bin/env python3
import sys
import json

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue

    req_id = req.get("id")
    method = req.get("method")

    if method == "initialize":
        res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"protocolVersion": 1, "authMethods": []},
        }
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    elif method == "session/new":
        res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"sessionId": "sess_acp_parse_1"},
        }
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    elif method == "session/prompt":
        # Thought update
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess_acp_parse_1",
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "Deep thinking about physics"}
                }
            }
        }) + "\n")
        sys.stdout.flush()

        # Message update
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess_acp_parse_1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Physics answer content"}
                }
            }
        }) + "\n")
        sys.stdout.flush()

        resp = {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "stop"}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
"""
    )
    mock_script.chmod(0o755)

    adapter = ACPAdapter(
        adapter_id="test_acp_parser",
        name="Test ACP Parser",
        binary_candidates=[str(mock_script)],
        acp_args=[],
    )
    cfg = AgentSessionConfig(room_id="r1", participant_id="p1", persona_id="p1")
    session = await adapter.create_session(cfg)
    turn = AgentTurn(turn_id="t1", user_message="Physics question")

    events = []
    async for ev in adapter.send(session, turn):
        events.append(ev)

    ev_types = [e.type for e in events]
    assert AgentEventType.THINKING in ev_types
    assert AgentEventType.CHUNK in ev_types
    assert AgentEventType.DONE in ev_types

    thinking_ev = next(e for e in events if e.type == AgentEventType.THINKING)
    assert thinking_ev.thinking == "Deep thinking about physics"

    chunk_ev = next(e for e in events if e.type == AgentEventType.CHUNK)
    assert chunk_ev.content == "Physics answer content"

    await adapter.close(session)


@pytest.mark.anyio
async def test_acp_authenticate_before_session_new(tmp_path, app) -> None:
    from persona_continuum.agent.protocols.acp import ACPAdapter

    log_file = tmp_path / "acp_auth_order.log"
    mock_script = tmp_path / "mock_acp_auth_order.py"
    mock_script.write_text(
        f"""#!/usr/bin/env python3
import sys
import json

log_path = "{log_file}"

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue

    method = req.get("method")
    req_id = req.get("id")

    with open(log_path, "a") as f:
        f.write(method + "\\n")

    if method == "initialize":
        res = {{
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {{
                "protocolVersion": 1,
                "authMethods": [{{"id": "api_key", "type": "key"}}]
            }}
        }}
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()
    elif method == "authenticate":
        res = {{"jsonrpc": "2.0", "id": req_id, "result": {{"status": "authenticated"}}}}
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()
    elif method == "session/new":
        res = {{"jsonrpc": "2.0", "id": req_id, "result": {{"sessionId": "sess_auth_ok"}}}}
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()
"""
    )
    mock_script.chmod(0o755)

    adapter = ACPAdapter(
        adapter_id="test_acp_auth",
        name="Test ACP Auth",
        binary_candidates=[str(mock_script)],
        acp_args=[],
    )
    app.credentials.create(
        credential_id="acp-test-key",
        provider="custom",
        api_key="secret_acp_token_123",
        base_url="https://unused.example/v1",
    )
    adapter.credential_manager = app.credentials
    cfg = AgentSessionConfig(
        room_id="r1",
        participant_id="p1",
        persona_id="p1",
        auth_profile_id="acp-test-key",
    )
    session = await adapter.create_session(cfg)
    assert session.session_data.get("acp_session_id") == "sess_auth_ok"
    await adapter.close(session)

    # Assert authenticate occurred before session/new
    order = log_file.read_text().splitlines()
    assert "initialize" in order
    assert "authenticate" in order
    assert "session/new" in order
    assert order.index("authenticate") < order.index("session/new")


@pytest.mark.anyio
async def test_acp_no_unverified_model_effort_startup_flags(tmp_path) -> None:
    from persona_continuum.agent.protocols.acp import ACPAdapter

    argv_log = tmp_path / "acp_argv.log"
    mock_script = tmp_path / "mock_acp_argv.py"
    mock_script.write_text(
        f"""#!/usr/bin/env python3
import sys
import json

with open("{argv_log}", "w") as f:
    f.write(" ".join(sys.argv) + "\\n")

while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        req = json.loads(line.strip())
        req_id = req.get("id")
        method = req.get("method")
        if method == "initialize":
            res = {{
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {{"protocolVersion": 1, "authMethods": []}}
            }}
            sys.stdout.write(json.dumps(res) + "\\n")
            sys.stdout.flush()
        elif method == "session/new":
            res = {{"jsonrpc": "2.0", "id": req_id, "result": {{"sessionId": "sess_argv"}}}}
            sys.stdout.write(json.dumps(res) + "\\n")
            sys.stdout.flush()
    except Exception:
        pass
"""
    )
    mock_script.chmod(0o755)

    adapter = ACPAdapter(
        adapter_id="test_acp_flags",
        name="Test ACP Flags",
        binary_candidates=[str(mock_script)],
        acp_args=["acp", "server"],
    )
    cfg = AgentSessionConfig(
        room_id="r1",
        participant_id="p1",
        persona_id="p1",
        model_id="unverified-model",
        reasoning_effort="high",
    )
    session = await adapter.create_session(cfg)
    await adapter.close(session)

    # Check argv log: should contain 'acp server' and NOT '--model unverified-model'
    logged_argv = argv_log.read_text().strip()
    assert "acp server" in logged_argv
    assert "--model" not in logged_argv
    assert "--effort" not in logged_argv
