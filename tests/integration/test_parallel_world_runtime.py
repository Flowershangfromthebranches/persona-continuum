from __future__ import annotations

from pathlib import Path

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.world.models import ActionType, ActorAction


@pytest.fixture
def test_continuum(tmp_path: Path) -> PersonaContinuum:
    cfg = Config(data_dir=tmp_path / "persona_data")
    continuum = PersonaContinuum(cfg, include_fake_agent=True)
    continuum.init()
    yield continuum
    continuum.close()


@pytest.mark.anyio
async def test_scene_engine_reuses_tavern(test_continuum: PersonaContinuum) -> None:
    """Requirement 8 & Acceptance: Scene Engine bridges multi-actor dialogues."""
    for persona_id, display_name in (
        ("steve_jobs", "Steve Jobs"),
        ("jensen_huang", "Jensen Huang"),
    ):
        test_continuum.personas.create_from_manifest(
            {
                "id": persona_id,
                "display_name": display_name,
                "persona_type": "historical",
                "run_mode": "continuation",
            }
        )
    world, branch, state = test_continuum.worlds.create_world(
        description="Jobs meets Jensen Huang in 2012 to negotiate GPU supply.",
        start_date="2012-03-01",
        initial_actors=["steve_jobs", "jensen_huang"],
    )

    summary, ev = await test_continuum.worlds.trigger_scene(
        world_id=world.id,
        branch_id=branch.id,
        actor_ids=["steve_jobs", "jensen_huang"],
        topic="Semiconductor supply negotiation and custom CUDA integration",
    )

    assert "Jobs" in summary or "Steve" in summary
    assert "Jensen" in summary
    assert ev.id is not None
    assert "steve_jobs" in ev.actors
    assert "jensen_huang" in ev.actors

    # Verify writeback to memory and relationships
    updated_actors = test_continuum.worlds.list_actors(world.id, branch.id)
    steve_updated = next(a for a in updated_actors if a.id == "steve_jobs")
    assert any(
        "Scene" in m.get("content", "") or "discussion" in m.get("content", "").lower()
        for m in steve_updated.memory
    )


@pytest.mark.anyio
async def test_world_resume(test_continuum: PersonaContinuum, tmp_path: Path) -> None:
    """Requirement 12, 13 & Acceptance: World state and branches resume cleanly from DB."""
    # 1. Create and step world in first session
    world, branch, state = test_continuum.worlds.create_world(
        description="2011 Jobs AI Chip Strategy with multiple branches and adaptive time",
        start_date="2011-10-05",
        metadata={
            "initial_actors": [
                {
                    "id": "steve_jobs",
                    "name": "Steve Jobs",
                    "actor_type": "persona_actor",
                    "identity": {"role": "CEO", "organization": "apple_corp"},
                },
                {
                    "id": "jensen_huang",
                    "name": "Jensen Huang",
                    "actor_type": "persona_actor",
                    "identity": {"role": "CEO", "organization": "nvidia_corp"},
                },
            ],
            "initial_organizations": {
                "apple_corp": {"name": "Apple Inc.", "cash_reserves_billions": 81.6}
            },
        },
    )

    # Step clock
    state_after_step, events, step_unit = await test_continuum.worlds.step_branch(
        world.id, branch.id
    )
    assert state_after_step.timestamp != "2011-10-05"

    # Inject action
    act = ActorAction(
        actor_id="steve_jobs",
        action_type=ActionType.INVEST,
        target="tsmc_dedicated_line",
        description="Apple invests $2B in dedicated advanced lithography lines.",
        parameters={"amount_billions": 2.0},
    )
    res, state_after_action, ev = test_continuum.worlds.inject_action(world.id, branch.id, act)
    assert res.success is True

    # Fork branch
    forked_branch = test_continuum.worlds.fork_branch(
        world_id=world.id,
        source_branch_id=branch.id,
        new_name="Branch_Alternative_Roadmap",
    )
    assert forked_branch.id != branch.id

    # 2. Close container and instantiate brand new PersonaContinuum instance pointing to same DB
    data_dir = test_continuum.config.data_dir
    test_continuum.close()

    new_cfg = Config(data_dir=data_dir)
    resumed_continuum = PersonaContinuum(new_cfg, include_fake_agent=True)
    resumed_continuum.init()

    try:
        # Verify world loaded
        resumed_world = resumed_continuum.worlds.get_world(world.id)
        assert resumed_world is not None
        assert resumed_world.id == world.id
        assert resumed_world.seed.start_date == "2011-10-05"

        # Verify branches loaded
        branches = resumed_continuum.worlds.list_branches(world.id)
        assert len(branches) == 2
        branch_names = [b.name for b in branches]
        assert "main" in branch_names
        assert "Branch_Alternative_Roadmap" in branch_names

        # Verify events persisted
        persisted_events = resumed_continuum.worlds.list_events(world.id, branch.id)
        assert len(persisted_events) >= 2

        # Verify actors persisted
        actors = resumed_continuum.worlds.list_actors(world.id, branch.id)
        assert len(actors) >= 2

        # Verify evaluation executes on resumed world
        evaluation = await resumed_continuum.worlds.evaluate_question(
            world_id=world.id,
            question="Will Apple succeed in custom AI chip independence?",
            branch_count=3,
        )
        assert evaluation.total_branches >= 2
        assert len(evaluation.distribution) > 0
    finally:
        resumed_continuum.close()
