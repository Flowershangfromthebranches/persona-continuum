from __future__ import annotations

import asyncio
import json
import sys

import pytest

from persona_continuum.agent.adapter import safe_exec_cmd
from persona_continuum.agent.models import (
    AgentEventType,
    AgentSessionConfig,
    AgentTurn,
)
from persona_continuum.agent.protocols.plain_cli import PlainCliAdapter


@pytest.mark.anyio
async def test_plain_cli_process_exit_is_reported_and_cleaned() -> None:
    adapter = PlainCliAdapter(
        adapter_id="slow-cli",
        name="Slow CLI",
        binary_candidates=[sys.executable],
        exec_args=["-c", "import time; time.sleep(10)", "-p"],
    )
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="room-timeout",
            participant_id="participant-timeout",
            persona_id="persona-timeout",
            allow_mcp=False,
            extra={"turn_timeout_seconds": 5.0},
        )
    )

    events = [event async for event in adapter.send(session, AgentTurn(user_message="hello"))]

    assert events[-1].type == AgentEventType.ERROR
    assert events[-1].error == "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT: CLI returned no response"
    assert session.session_data["active_proc"] is None


@pytest.mark.anyio
async def test_plain_cli_reports_headless_tool_permission_denial(tmp_path) -> None:
    script = tmp_path / "permission_denied.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('A tool required read_file permission that headless mode cannot "
        "prompt for; auto-denied.')\n"
    )
    script.chmod(0o755)
    adapter = PlainCliAdapter(
        adapter_id="permission-cli",
        name="Permission CLI",
        binary_candidates=[str(script)],
        exec_args=["-p"],
    )
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="room-permission",
            participant_id="participant-permission",
            persona_id="persona-permission",
        )
    )

    events = [event async for event in adapter.send(session, AgentTurn(user_message="hello"))]

    assert events[-1].type == AgentEventType.ERROR
    assert events[-1].error == "AGENT_PERMISSION_BLOCKED"
    assert events[-1].metadata["failure"]["retriable"] is False
    assert "headless mode" in events[-1].metadata["failure"]["message"]


@pytest.mark.anyio
async def test_safe_exec_cancellation_reaps_process() -> None:
    task = asyncio.create_task(
        safe_exec_cmd(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=30.0,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_plain_cli_places_runtime_options_before_print_prompt(tmp_path) -> None:
    script = tmp_path / "capture_args.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps(sys.argv[1:]))\n"
    )
    script.chmod(0o755)
    adapter = PlainCliAdapter(
        adapter_id="print-cli",
        name="Print CLI",
        binary_candidates=[str(script)],
        exec_args=["-p"],
        model_flag="--model",
        reasoning_flag="--effort",
    )
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="room",
            participant_id="participant",
            persona_id="persona",
            model_id="model-real",
            reasoning_effort="high",
        )
    )

    events = [event async for event in adapter.send(session, AgentTurn(user_message="probe"))]
    payload = "".join(event.content for event in events if event.type == AgentEventType.CHUNK)

    assert json.loads(payload) == [
        "--model",
        "model-real",
        "--effort",
        "high",
        "-p",
        "[SYSTEM INSTRUCTIONS]\n\n[USER INPUT]\nprobe",
    ]
