from __future__ import annotations

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.models import (
    AgentEventType,
    AgentSessionConfig,
    AgentTurn,
)


@pytest.mark.anyio
async def test_fake_adapter_turn_streaming() -> None:
    adapter = FakeAgentAdapter(chunk_delay_sec=0.0)
    config = AgentSessionConfig(
        room_id="room_1",
        participant_id="p_1",
        persona_id="persona_steve",
        model_id="fake-gpt-5",
        reasoning_effort="high",
    )
    session = await adapter.create_session(config)
    assert session.is_active

    turn = AgentTurn(
        system_prompt="You are Steve Jobs.",
        user_message="Tell us about the Macintosh.",
        transcript_history=[],
    )

    events = []
    async for ev in adapter.send(session, turn):
        events.append(ev)

    event_types = [e.type for e in events]
    assert AgentEventType.THINKING in event_types
    assert AgentEventType.CHUNK in event_types
    assert AgentEventType.DONE in event_types

    done_ev = next(e for e in events if e.type == AgentEventType.DONE)
    assert len(done_ev.content) > 0

    await adapter.close(session)
    assert not session.is_active


@pytest.mark.anyio
async def test_fake_adapter_simulated_crash() -> None:
    adapter = FakeAgentAdapter(simulate_crash_on_turn=1)
    config = AgentSessionConfig(
        room_id="room_crash",
        participant_id="p_1",
        persona_id="persona_crash",
        model_id="fake-gpt-5",
    )
    session = await adapter.create_session(config)

    turn = AgentTurn(
        system_prompt="System",
        user_message="Hello",
    )

    events = []
    async for ev in adapter.send(session, turn):
        events.append(ev)

    assert any(e.type == AgentEventType.ERROR for e in events)


@pytest.mark.anyio
async def test_fake_adapter_tool_call() -> None:
    adapter = FakeAgentAdapter()
    config = AgentSessionConfig(
        room_id="room_tool",
        participant_id="p_tool",
        persona_id="persona_tool",
        model_id="fake-gpt-5",
    )
    session = await adapter.create_session(config)

    turn = AgentTurn(
        system_prompt="System",
        user_message="TRIGGER_TOOL_CALL: search memories",
        tools=[{"type": "function", "function": {"name": "persona_search_memories"}}],
    )

    events = []
    async for ev in adapter.send(session, turn):
        events.append(ev)

    tool_call_ev = next((e for e in events if e.type == AgentEventType.TOOL_CALL), None)
    assert tool_call_ev is not None
    assert tool_call_ev.tool_name == "persona_search_memories"
