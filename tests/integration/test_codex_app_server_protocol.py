from __future__ import annotations

import asyncio
import json

import pytest

from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.models import (
    AgentEventType,
    AgentSessionConfig,
    AgentTurn,
)


@pytest.mark.anyio
async def test_codex_real_thread_start_response_shape(tmp_path) -> None:
    mock_script = tmp_path / "mock_codex_v2.py"
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
        res = {"jsonrpc": "2.0", "id": req_id, "result": {"serverInfo": {"name": "codex-v2"}}}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    elif method == "initialized":
        pass
    elif method == "thread/start":
        # Official Codex app-server V2 thread/start response shape
        res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "thread": {
                    "id": "thr_v2_official_789"
                }
            }
        }
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
"""
    )
    mock_script.chmod(0o755)

    adapter = CodexAdapter()
    adapter.binary_candidates = [str(mock_script)]
    adapter._resolved_binary = str(mock_script)

    cfg = AgentSessionConfig(
        room_id="room_1",
        participant_id="slot_1",
        persona_id="persona_steve",
        model_id="o3-mini",
    )
    session = await adapter.create_session(cfg)
    assert session.session_data.get("thread_id") == "thr_v2_official_789"
    await adapter.close(session)


@pytest.mark.anyio
async def test_codex_turn_start_response_does_not_finish_turn(tmp_path) -> None:
    mock_script = tmp_path / "mock_codex_streaming.py"
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
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}}) + "\n")
        sys.stdout.flush()
    elif method == "thread/start":
        res = {"jsonrpc": "2.0", "id": req_id, "result": {"thread": {"id": "thr_stream"}}}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    elif method == "turn/start":
        # First acknowledge turn creation with result.turn.id
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "turn": {
                    "id": "turn_v2_active_456"
                }
            }
        }) + "\n")
        sys.stdout.flush()

        # Stream reasoning delta
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "method": "item/reasoning/delta",
            "params": {"item": {"thinking": "Thinking deeply..."}}
        }) + "\n")
        sys.stdout.flush()

        # Stream message delta
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "method": "item/agentMessage/delta",
            "params": {"item": {"text": "Complete streaming answer."}}
        }) + "\n")
        sys.stdout.flush()

        # Complete turn
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {"status": "completed"}
        }) + "\n")
        sys.stdout.flush()
"""
    )
    mock_script.chmod(0o755)

    adapter = CodexAdapter()
    adapter.binary_candidates = [str(mock_script)]
    adapter._resolved_binary = str(mock_script)

    cfg = AgentSessionConfig(
        room_id="room_1",
        participant_id="slot_1",
        persona_id="persona_steve",
    )
    session = await adapter.create_session(cfg)
    turn = AgentTurn(turn_id="turn_test_1", user_message="Hello Codex")

    events = []
    async for ev in adapter.send(session, turn):
        events.append(ev)

    ev_types = [e.type for e in events]
    assert AgentEventType.THINKING in ev_types
    assert AgentEventType.CHUNK in ev_types
    assert AgentEventType.DONE in ev_types

    done_ev = next(e for e in events if e.type == AgentEventType.DONE)
    assert done_ev.content == "Complete streaming answer."
    assert done_ev.metadata.get("cancelled") is not True

    assert session.session_data.get("active_turn_id") == "turn_v2_active_456"
    await adapter.close(session)


@pytest.mark.anyio
async def test_codex_failed_turn_reports_retriable_transport_error(tmp_path) -> None:
    """A failed app-server turn must not degrade into the exec fallback.

    The real CLI emits ``turn/completed`` with ``turn.status=failed`` after a
    transient upstream stream disconnect.  With an explicit reasoning effort
    requested, an exec fallback would masquerade as a permanent
    REASONING_BINDING_UNVERIFIED failure; the adapter must instead surface a
    retriable transport error so the caller can recover the session.
    """
    mock_script = tmp_path / "mock_codex_failed_turn.py"
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
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}}) + "\n")
        sys.stdout.flush()
    elif method == "thread/start":
        res = {"jsonrpc": "2.0", "id": req_id, "result": {"thread": {"id": "thr_fail"}}}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    elif method == "turn/start":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"turn": {"id": "turn_fail_1"}},
        }) + "\n")
        sys.stdout.flush()
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": "thr_fail",
                "turn": {
                    "id": "turn_fail_1",
                    "status": "failed",
                    "error": {"message": "stream disconnected before completion"},
                },
            },
        }) + "\n")
        sys.stdout.flush()
"""
    )
    mock_script.chmod(0o755)

    adapter = CodexAdapter()
    adapter.binary_candidates = [str(mock_script)]
    adapter._resolved_binary = str(mock_script)

    cfg = AgentSessionConfig(
        room_id="room_fail",
        participant_id="slot_fail",
        persona_id="persona_fail",
        model_id="gpt-5.6-sol",
        reasoning_effort="low",
    )
    session = await adapter.create_session(cfg)
    assert session.session_data.get("mode") == "app_server"

    turn = AgentTurn(turn_id="turn_fail_test", user_message="Hello failing Codex")
    events = []
    async for ev in adapter.send(session, turn):
        events.append(ev)

    error_events = [ev for ev in events if ev.type == AgentEventType.ERROR]
    assert len(error_events) == 1, events
    assert error_events[0].metadata.get("failure_code") == "AGENT_TRANSPORT_ERROR"
    assert "stream disconnected" in str(error_events[0].metadata.get("failure", {}))
    # The misleading exec-fallback failure code must never appear here.
    assert all(
        ev.metadata.get("failure_code") != "REASONING_BINDING_UNVERIFIED" for ev in events
    )
    assert not any(
        ev.metadata.get("fallback") == "codex_exec_json" for ev in events
    )
    await adapter.close(session)


@pytest.mark.anyio
async def test_codex_external_cancel_sends_turn_interrupt(tmp_path) -> None:
    log_file = tmp_path / "codex_reqs.jsonl"
    mock_script = tmp_path / "mock_codex_ext_cancel.py"
    mock_script.write_text(
        f"""#!/usr/bin/env python3
import sys
import json
import time

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

    with open(log_path, "a") as f:
        f.write(json.dumps(req) + "\\n")

    req_id = req.get("id")
    method = req.get("method")

    if method == "initialize":
        sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{}}}}) + "\\n")
        sys.stdout.flush()
    elif method == "thread/start":
        res = {{"jsonrpc": "2.0", "id": req_id, "result": {{"thread": {{"id": "thr_ext_123"}}}}}}
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()
    elif method == "turn/start":
        sys.stdout.write(json.dumps({{
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {{"turn": {{"id": "turn_ext_456"}}}}
        }}) + "\\n")
        sys.stdout.flush()
        sys.stdout.write(json.dumps({{
            "jsonrpc": "2.0",
            "method": "item/agentMessage/delta",
            "params": {{"item": {{"text": "Generating long answer..."}}}}
        }}) + "\\n")
        sys.stdout.flush()
    elif method == "turn/interrupt":
        params = req.get("params", {{}})
        sys.stdout.write(json.dumps({{
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {{"status": "ok"}}
        }}) + "\\n")
        sys.stdout.flush()
        sys.stdout.write(json.dumps({{
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {{"status": "interrupted"}}
        }}) + "\\n")
        sys.stdout.flush()
"""
    )
    mock_script.chmod(0o755)

    adapter = CodexAdapter()
    adapter.binary_candidates = [str(mock_script)]
    adapter._resolved_binary = str(mock_script)

    cfg = AgentSessionConfig(
        room_id="room_1",
        participant_id="slot_1",
        persona_id="persona_steve",
    )
    session = await adapter.create_session(cfg)
    turn = AgentTurn(turn_id="turn_test_ext", user_message="Generate novel")

    events = []

    async def _run_send():
        async for ev in adapter.send(session, turn):
            events.append(ev)

    task = asyncio.create_task(_run_send())

    # Wait until turn/start returns and active_turn_id is populated
    for _ in range(30):
        if session.session_data.get("active_turn_id") == "turn_ext_456":
            break
        await asyncio.sleep(0.05)

    assert session.session_data.get("active_turn_id") == "turn_ext_456"

    # Verify process is still running prior to cancel
    proc = session.session_data.get("proc")
    assert proc is not None
    assert proc.returncode is None

    # Call external cancel
    await adapter.cancel(session)
    await task

    # Assert stdin received turn/interrupt
    assert log_file.exists()
    logs = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    interrupt_reqs = [r for r in logs if r.get("method") == "turn/interrupt"]
    assert len(interrupt_reqs) == 1
    int_params = interrupt_reqs[0].get("params", {})
    assert int_params.get("threadId") == "thr_ext_123"
    assert int_params.get("turnId") == "turn_ext_456"

    # Assert turn was completed with cancelled=True and process is still open
    done_ev = next(e for e in events if e.type == AgentEventType.DONE)
    assert done_ev.metadata.get("cancelled") is True
    assert proc.returncode is None

    await adapter.close(session)


@pytest.mark.anyio
async def test_codex_cancel_waits_for_interrupted_completion(tmp_path) -> None:
    mock_script = tmp_path / "mock_codex_wait_int.py"
    mock_script.write_text(
        r"""#!/usr/bin/env python3
import sys
import json
import time

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
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}}) + "\n")
        sys.stdout.flush()
    elif method == "thread/start":
        res = {"jsonrpc": "2.0", "id": req_id, "result": {"thread": {"id": "thr_wait"}}}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    elif method == "turn/start":
        res = {"jsonrpc": "2.0", "id": req_id, "result": {"turn": {"id": "turn_wait_1"}}}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    elif method == "turn/interrupt":
        # Simulate slight delay then send interrupted turn/completed
        time.sleep(0.05)
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {"status": "interrupted"}
        }) + "\n")
        sys.stdout.flush()
"""
    )
    mock_script.chmod(0o755)

    adapter = CodexAdapter()
    adapter.binary_candidates = [str(mock_script)]
    adapter._resolved_binary = str(mock_script)

    cfg = AgentSessionConfig(room_id="r1", participant_id="p1", persona_id="persona_steve")
    session = await adapter.create_session(cfg)
    turn = AgentTurn(turn_id="t1", user_message="Wait test")

    events = []

    async def _send_loop():
        async for ev in adapter.send(session, turn):
            events.append(ev)

    send_task = asyncio.create_task(_send_loop())
    while session.session_data.get("active_turn_id") != "turn_wait_1":
        await asyncio.sleep(0.02)

    await adapter.cancel(session)
    await send_task

    done_ev = next(e for e in events if e.type == AgentEventType.DONE)
    assert done_ev.metadata.get("cancelled") is True
    await adapter.close(session)


@pytest.mark.anyio
async def test_codex_exec_jsonl_parses_agent_message_and_omits_default_model(tmp_path) -> None:
    args_file = tmp_path / "codex_args.json"
    mock_script = tmp_path / "mock_codex_exec.py"
    mock_script.write_text(
        f'''#!/usr/bin/env python3
import json
import sys

if "app-server" in sys.argv:
    raise SystemExit(0)

with open({str(args_file)!r}, "w") as handle:
    json.dump(sys.argv[1:], handle)

print(json.dumps({{"type": "thread.started", "thread_id": "thread_1"}}))
message = {{
    "searched": True,
    "sources": [{{"url": "https://openai.com/about/", "title": "About | OpenAI"}}],
}}
print(json.dumps({{
    "type": "item.completed",
    "item": {{
        "type": "agent_message",
        "text": json.dumps(message, separators=(",", ":")),
    }},
}}))
print(json.dumps({{
    "type": "turn.completed",
    "usage": {{"input_tokens": 10, "output_tokens": 5}},
}}))
'''
    )
    mock_script.chmod(0o755)

    adapter = CodexAdapter()
    adapter.binary_candidates = [str(mock_script)]
    adapter._resolved_binary = str(mock_script)
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="research:test",
            participant_id="probe",
            persona_id="probe",
            model_id="default",
        )
    )

    events = [
        event
        async for event in adapter.send(
            session,
            AgentTurn(user_message="probe"),
        )
    ]

    assert [event.content for event in events if event.type == AgentEventType.CHUNK] == [
        '{"searched":true,"sources":[{"url":"https://openai.com/about/","title":"About | OpenAI"}]}'
    ]
    done = next(event for event in events if event.type == AgentEventType.DONE)
    assert done.metadata["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert "model=default" not in args_file.read_text()
