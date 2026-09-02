from __future__ import annotations

import pytest

from persona_continuum.world.agent_runtime import ActorRuntime
from persona_continuum.world.decision import ActorDecisionEngine
from persona_continuum.world.models import Actor, ActorType, WorldSeed, WorldState


@pytest.mark.anyio
async def test_actor_decision_engine_delegates_to_agent_runtime(app) -> None:
    runtime = ActorRuntime(app)
    engine = ActorDecisionEngine(runtime)
    actor = Actor(
        id="steve_jobs",
        name="Steve Jobs",
        actor_type=ActorType.PERSONA_ACTOR,
        persona_id="steve_jobs",
        agent_runtime_id="fake_agent",
        goals=["Build independent Apple AI compute"],
    )
    proposal = await engine.decide(
        world_id="world-decision",
        branch_id="main",
        actor=actor,
        state=WorldState(timestamp="2013-01-01"),
        seed=WorldSeed(start_date="2013-01-01"),
        memories=[],
        recent_events=[],
    )
    adapter = app.agent_registry.get_adapter("fake_agent")
    assert proposal.actor == actor.id
    assert len(adapter.sent_turns) == 1  # type: ignore[attr-defined]
