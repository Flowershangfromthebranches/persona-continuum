from unittest.mock import AsyncMock

import pytest

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters.gemini import GeminiCliAdapter
from persona_continuum.agent.models import AgentSessionConfig, AgentTurn
from persona_continuum.agent.prompt_transport import resolve_prompt_transport_capability
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport


@pytest.mark.anyio
async def test_agy_stream_json_writes_prompt_on_stdin(monkeypatch) -> None:
    adapter = GeminiCliAdapter()
    adapter._resolved_binary = "/synthetic/agy"
    assert resolve_prompt_transport_capability(adapter).transport_mode == "stdin"
    session = AgentSession(
        config=AgentSessionConfig(room_id="t", participant_id="t", persona_id="t")
    )
    transport = AsyncMock()
    transport.process_alive = True
    transport.readline = AsyncMock(
        side_effect=[
            b'{"type":"init"}\n',
            b'{"type":"result","status":"SUCCESS","response":"ok","usage":{"input_tokens":12}}\n',
        ]
    )
    transport.write = AsyncMock()
    monkeypatch.setattr(SubprocessAgentTransport, "spawn", AsyncMock(return_value=transport))
    events = []
    async for event in adapter.send(session, AgentTurn(user_message="hello")):
        events.append(event)
    argv = SubprocessAgentTransport.spawn.call_args.args[1]
    assert "--input-format" in argv and "stream-json" in argv
    assert any(arg.startswith("-p") for arg in argv)
    written = transport.write.await_args.args[0].decode("utf-8")
    assert '"message"' in written or '"prompt"' in written
    assert "hello" in written
    assert any(event.content == "ok" for event in events)


def test_gemini_argv_fallback_still_declared() -> None:
    adapter = GeminiCliAdapter()
    adapter._force_argv = True
    adapter._resolved_binary = "/synthetic/agy"
    assert resolve_prompt_transport_capability(adapter).transport_mode == "argv"
