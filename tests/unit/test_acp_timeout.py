from __future__ import annotations

import asyncio

import pytest

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.models import (
    AgentEventType,
    AgentSessionConfig,
    AgentTurn,
)
from persona_continuum.agent.protocols.acp import ACPAdapter


class _Reader:
    async def readline(self) -> bytes:
        await asyncio.sleep(10)
        return b""


class _Writer:
    def write(self, data: bytes) -> None:
        del data

    async def drain(self) -> None:
        return None


class _Process:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdin = _Writer()
        self.stdout = _Reader()

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


@pytest.mark.anyio
async def test_acp_send_reports_process_exit_and_closes_session() -> None:
    adapter = ACPAdapter(
        adapter_id="slow-acp",
        name="Slow ACP",
        binary_candidates=[],
    )
    session = AgentSession(
        config=AgentSessionConfig(
            room_id="room-timeout",
            participant_id="participant-timeout",
            persona_id="persona-timeout",
            allow_mcp=False,
            extra={"turn_timeout_seconds": 5.0},
        ),
        session_data={
            "proc": _Process(),
            "acp_session_id": "session-timeout",
            "msg_id": 10,
        },
    )

    events = [event async for event in adapter.send(session, AgentTurn(user_message="hello"))]

    assert events[-1].type == AgentEventType.ERROR
    assert events[-1].error == "ACP process exited before a complete JSON frame was received"
    assert session.is_active is False
