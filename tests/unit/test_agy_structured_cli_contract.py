from unittest.mock import AsyncMock

import pytest

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters.gemini import GeminiCliAdapter
from persona_continuum.agent.models import AgentSessionConfig, AgentTurn, StructuredOutputMode
from persona_continuum.agent.response_collector import AgentResponseCollector, AgentRuntimeError
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.application.job_progress import is_retryable_failure


@pytest.mark.anyio
@pytest.mark.parametrize("partial", [b"", b"startup banner"])
@pytest.mark.parametrize("invalid", [False, True])
async def test_agy_prompt_contract_and_nonretryable_exit(monkeypatch, partial, invalid):
    adapter = GeminiCliAdapter()
    adapter._resolved_binary = "/synthetic/agy"
    session = AgentSession(config=AgentSessionConfig(
        room_id="test", participant_id="test", persona_id="test",
    ))
    transport = AsyncMock()
    transport.read.side_effect = [partial, b""] if partial else [b""]
    transport.wait.return_value = 1
    transport.process_alive = False
    transport.stderr_tail = (
        "--json-schema can only be used when --output-format is 'json' or 'stream-json'"
        if invalid else "temporary transport failure"
    )
    spawn = AsyncMock(return_value=transport)
    monkeypatch.setattr(SubprocessAgentTransport, "spawn", spawn)
    turn = AgentTurn(user_message="classify", expected_output={
        "type": "object", "required": ["units"],
        "properties": {"units": {"type": "array"}},
    })
    transport.readline = AsyncMock(side_effect=[b""])
    transport.write = AsyncMock()
    collector = AgentResponseCollector(protocol="plain_cli")
    await collector.collect(adapter.send(session, turn))
    argv = spawn.call_args.args[1]
    assert adapter.structured_output_mode == StructuredOutputMode.PROMPT_ONLY
    assert "--json-schema" not in argv
    assert "--input-format" in argv
    assert "stream-json" in argv
    assert "--output-format" in argv
    written = b"".join(call.args[0] for call in transport.write.await_args_list)
    assert b"[EXPECTED OUTPUT SCHEMA]" in written
    assert b"units" in written
    with pytest.raises(AgentRuntimeError) as caught:
        collector.require_text(phase="material_classification")
    assert caught.value.retriable is (not invalid)
    if invalid:
        assert caught.value.code == "AGENT_CLI_INVALID_ARGUMENT"
        assert not is_retryable_failure(caught.value.as_failure())
    spawn.assert_awaited_once()
