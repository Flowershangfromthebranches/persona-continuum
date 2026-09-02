from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.domain.memory import MemoryRecord, MemoryType
from persona_continuum.room.tool_broker import PersonaToolBroker


@pytest.mark.anyio
async def test_tool_broker_execution(app: PersonaContinuum) -> None:
    app.personas.create_from_manifest(
        {
            "id": "steve_jobs",
            "display_name": "Steve Jobs",
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )
    app.memories.add_memory(
        MemoryRecord(
            id="mem_ne_1",
            persona_id="steve_jobs",
            type=MemoryType.EPISODIC,
            source_kind="seed",
            content="NeXT was founded in 1985 after leaving Apple.",
        )
    )

    broker = PersonaToolBroker(app)
    tools = broker.get_tool_definitions()
    assert len(tools) == 3
    tool_names = [t["function"]["name"] for t in tools]
    assert "persona_search_memories" in tool_names
    assert "persona_get_relationship" in tool_names
    assert "persona_get_runtime_state" in tool_names

    # Test search memories
    res = await broker.execute_tool(
        persona_id="steve_jobs",
        tool_name="persona_search_memories",
        arguments={"query": "NeXT", "limit": 5},
    )
    assert res.get("status") == "success"
    assert len(res.get("memories", [])) >= 1
    assert "NeXT" in res["memories"][0]["content"]

    # Test runtime state
    state_res = await broker.execute_tool(
        persona_id="steve_jobs",
        tool_name="persona_get_runtime_state",
        arguments={"branch_id": "main"},
    )
    assert state_res.get("status") == "success"
    assert "emotions" in state_res
    assert "needs" in state_res
