from __future__ import annotations

import json

import pytest

from persona_continuum.world.actions import ActionResolver
from persona_continuum.world.agent_runtime import ActorRuntime
from persona_continuum.world.models import (
    ActionType,
    Actor,
    ActorType,
    MemoryCategory,
    ResolutionStatus,
    WorldMemoryRecord,
    WorldSeed,
    WorldState,
)


def _actor() -> Actor:
    return Actor(
        id="steve_jobs",
        name="Steve Jobs",
        actor_type=ActorType.PERSONA_ACTOR,
        persona_id="steve_jobs",
        organization_id="apple_corp",
        agent_runtime_id="fake_agent",
        goals=["Build Apple AI chip capability"],
        resources={"capital_access_billions": 5.0},
    )


@pytest.mark.anyio
async def test_persona_actor_calls_llm(app) -> None:
    runtime = ActorRuntime(app)
    adapter = app.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    proposal = await runtime.reason(
        world_id="world_test",
        branch_id="branch_main",
        actor=_actor(),
        state=WorldState(timestamp="2013-01-01"),
        seed=WorldSeed(start_date="2013-01-01"),
        memories=[],
        recent_events=[],
    )
    assert proposal.actor == "steve_jobs"
    assert len(adapter.sent_turns) == 1  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_actor_calls_llm(app) -> None:
    await test_persona_actor_calls_llm(app)


@pytest.mark.anyio
async def test_actor_context_contains_world_state(app) -> None:
    runtime = ActorRuntime(app)
    adapter = app.agent_registry.get_adapter("fake_agent")
    state = WorldState(
        timestamp="2013-01-01",
        economy={"market_demand": 1.2},
        organizations={"apple_corp": {"cash_reserves_billions": 80}},
    )
    await runtime.reason(
        world_id="world_context",
        branch_id="main",
        actor=_actor(),
        state=state,
        seed=WorldSeed(start_date="2013-01-01"),
        memories=[],
        recent_events=[],
    )
    sent_turn = adapter.sent_turns[-1][1]  # type: ignore[attr-defined]
    payload = json.loads(sent_turn.full_prompt.split("\n", 2)[2].split("\n输出字段", 1)[0])
    assert payload["current_state"]["economy"]["market_demand"] == 1.2


@pytest.mark.anyio
async def test_actor_cannot_access_future_information(app) -> None:
    runtime = ActorRuntime(app)
    adapter = app.agent_registry.get_adapter("fake_agent")
    future = WorldMemoryRecord(
        persona_id="steve_jobs",
        memory_type=MemoryCategory.WORLD_EXPERIENCE,
        content="Secret future ChatGPT launch knowledge",
        occurred_at="2022-11-30",
    )
    await runtime.reason(
        world_id="world_future",
        branch_id="main",
        actor=_actor(),
        state=WorldState(timestamp="2013-01-01"),
        seed=WorldSeed(start_date="2013-01-01"),
        memories=[future],
        recent_events=[],
    )
    prompt = adapter.sent_turns[-1][1].full_prompt  # type: ignore[attr-defined]
    assert "Secret future ChatGPT launch knowledge" not in prompt


@pytest.mark.anyio
async def test_action_proposal_generated(app) -> None:
    runtime = ActorRuntime(app)
    proposal = await runtime.reason(
        world_id="world_proposal",
        branch_id="main",
        actor=_actor(),
        state=WorldState(timestamp="2013-01-01"),
        seed=WorldSeed(start_date="2013-01-01"),
        memories=[],
        recent_events=[],
    )
    assert proposal.action_type in set(ActionType)
    assert proposal.reasoning_summary


@pytest.mark.anyio
async def test_persona_context_injected(app) -> None:
    app.personas.create_from_manifest(
        {
            "id": "steve_jobs",
            "display_name": "Steve Jobs",
            "aliases": ["Jobs"],
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )
    runtime = ActorRuntime(app)
    adapter = app.agent_registry.get_adapter("fake_agent")
    await runtime.reason(
        world_id="world_persona_context",
        branch_id="main",
        actor=_actor(),
        state=WorldState(timestamp="2013-01-01"),
        seed=WorldSeed(start_date="2013-01-01"),
        memories=[],
        recent_events=[],
    )
    prompt = adapter.sent_turns[-1][1].system_prompt  # type: ignore[attr-defined]
    assert "Steve Jobs" in prompt
    assert '"persona_id": "steve_jobs"' in prompt


@pytest.mark.anyio
async def test_action_generated_by_llm(app) -> None:
    runtime = ActorRuntime(app)
    proposal = await runtime.reason(
        world_id="world_llm_origin",
        branch_id="main",
        actor=_actor(),
        state=WorldState(timestamp="2013-01-01"),
        seed=WorldSeed(start_date="2013-01-01"),
        memories=[],
        recent_events=[],
    )
    trace = runtime.get_trace("world_llm_origin", "main", "steve_jobs")
    assert proposal.actor == "steve_jobs"
    assert trace["decision_source"] == "llm"
    assert trace["agent_id"] == "fake_agent"


def test_action_resolver_checks_constraints() -> None:
    actor = _actor()
    proposal_data = {
        "actor": actor.id,
        "intent": "Launch a major chip program",
        "action_type": "launch_project",
        "target": "neural_engine",
        "reasoning_summary": "Strategic independence",
        "expected_effect": "Internal chip capability",
        "confidence": 0.9,
        "parameters": {"budget_billions": 20.0, "team_engineers": 1000},
    }
    from persona_continuum.world.models import ActionProposal

    proposal = ActionProposal.model_validate(proposal_data)
    state = WorldState(
        timestamp="2013-01-01",
        organizations={"apple_corp": {"cash_reserves_billions": 1.0, "employees_count": 10}},
    )
    resolution, _, _ = ActionResolver().resolve_proposal(proposal, actor, state)
    assert resolution.status in {ResolutionStatus.FAILURE, ResolutionStatus.PARTIAL_SUCCESS}
    assert resolution.success_probability <= 0.05


@pytest.mark.anyio
async def test_world_loop_generates_events(app) -> None:
    world, branch, _ = app.worlds.create_world(
        description="Steve Jobs directs an Apple AI chip strategy in 2013",
        start_date="2013-01-01",
        initial_actors=["steve_jobs"],
    )
    _, events, _ = await app.worlds.step_branch(world.id, branch.id)
    assert events
    assert all("proposal" in event.data for event in events if event.data.get("action_id"))


@pytest.mark.anyio
async def test_world_state_changes_after_action(app) -> None:
    world, branch, before = app.worlds.create_world(
        description="Steve Jobs evaluates Apple AI silicon",
        start_date="2013-01-01",
        initial_actors=["steve_jobs"],
    )
    after, events, _ = await app.worlds.step_branch(world.id, branch.id)
    assert after.model_dump() != before.model_dump()
    assert after.timestamp != before.timestamp
    assert any(event.data.get("decision_source") == "llm" for event in events)
