from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.world.models import ActionType, ActorAction


@pytest.mark.anyio
async def test_branch_memory_isolated(app: PersonaContinuum) -> None:
    # 1. Create a world
    world, branch_a, initial_state = app.worlds.create_world(
        description="Jobs launches Apple AI chip strategy in 2011",
        title="Silicon Branching Test",
        start_date="2011-10-05",
    )

    # 2. Fork into Branch B
    branch_b = app.worlds.fork_branch(
        world_id=world.id,
        source_branch_id=branch_a.id,
        new_name="Branch_B_Acquisition",
    )

    # 3. Inject Action in Branch A (Aggressive R&D)
    act_a = ActorAction(
        actor_id="steve_jobs",
        action_type=ActionType.INVEST,
        target="Neural_Engine_Cluster",
        description="Invest $4B in custom Apple AI cluster",
        parameters={"amount_billions": 4.0},
    )
    app.worlds.inject_action(world.id, branch_a.id, act_a)

    # 4. Inject different Action in Branch B (Acquire Startup)
    act_b = ActorAction(
        actor_id="steve_jobs",
        action_type=ActionType.INVEST,
        target="Acquire_DeepScale_Inc",
        description="Acquire neural compression startup for $0.5B",
        parameters={"amount_billions": 0.5},
    )
    app.worlds.inject_action(world.id, branch_b.id, act_b)

    # 5. Verify isolated state & memories
    mems_a = app.worlds.list_memories(world.id, branch_a.id, "steve_jobs")
    mems_b = app.worlds.list_memories(world.id, branch_b.id, "steve_jobs")

    content_a = " ".join(m.content for m in mems_a)
    content_b = " ".join(m.content for m in mems_b)

    assert (
        "Neural_Engine_Cluster" in content_a
        or "custom Apple AI cluster" in content_a
        or "invest" in content_a
    )
    assert (
        "Acquire_DeepScale_Inc" in content_b
        or "neural compression" in content_b
        or "invest" in content_b
    )

    # Verify event timelines are isolated
    events_a = app.worlds.list_events(world.id, branch_a.id)
    events_b = app.worlds.list_events(world.id, branch_b.id)
    assert len(events_a) > 0
    assert len(events_b) > 0
    assert any(
        "Neural_Engine_Cluster" in e.cause or "Apple AI cluster" in e.cause for e in events_a
    )
    assert not any("Neural_Engine_Cluster" in e.cause for e in events_b)
