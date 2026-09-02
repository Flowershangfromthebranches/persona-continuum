from __future__ import annotations

import pytest

from persona_continuum.world.actions import ActionResolver
from persona_continuum.world.actors import ActorManager
from persona_continuum.world.branch import BranchManager
from persona_continuum.world.builder import WorldBuilderService
from persona_continuum.world.evaluator import OutcomeEvaluator
from persona_continuum.world.firewall import TemporalKnowledgeFirewall
from persona_continuum.world.models import (
    ActionType,
    ActorAction,
    WorldSeed,
)
from persona_continuum.world.state import WorldStateManager
from tests.fixtures.demo_industry_world import demo_seed_metadata


def test_world_builder_creates_seed() -> None:
    """Requirement 1 & Acceptance: WorldBuilder creates structured seed."""
    builder = WorldBuilderService()
    desc = "乔布斯在2011年没有去世，并决定推动 Apple 建立自研 AI 芯片与神经网络战略。"

    seed = builder.build_seed(
        description=desc,
        baseline="real_world",
        start_date="2011-10-05",
        simulation_end="2030",
    )

    assert isinstance(seed, WorldSeed)
    assert seed.baseline_world == "real_world"
    assert seed.start_date == "2011-10-05"
    assert seed.simulation_end == "2030"
    assert len(seed.divergence) >= 1
    assert "乔布斯" in seed.divergence[0].condition or "Jobs" in seed.divergence[0].condition
    assert len(seed.immutable_facts) >= 1
    assert len(seed.rules) >= 1

    # Negative test: World builder rejects outcome prediction statements
    with pytest.raises(ValueError, match="forbidden outcome prediction"):
        builder.build_seed(description="Apple 最终战胜 NVIDIA 统治全球数据中心市场。")


def test_temporal_firewall_blocks_future_information() -> None:
    """Requirement 2 & Acceptance: Temporal Knowledge Firewall isolates post-T real world info."""
    firewall = TemporalKnowledgeFirewall()

    # 1. Test prompt constraint instructions
    instruction = firewall.build_firewall_prompt_instruction("2014-06-01")
    assert "2014-06-01" in instruction
    assert "TEMPORAL KNOWLEDGE FIREWALL" in instruction
    assert "conversational ai assistant" in instruction.lower()

    # 2. Test memory filtering
    memories = [
        {"id": "m1", "occurred_at": "2010-01-27", "content": "iPad announcement"},
        {"id": "m2", "occurred_at": "2013-09-10", "content": "Apple A7 64-bit chip release"},
        {"id": "m3", "occurred_at": "2022-11-30", "content": "ChatGPT release"},
        {"id": "m4", "occurred_at": "2024-02-02", "content": "Apple Vision Pro launch"},
    ]

    filtered = firewall.filter_memories(memories, "2014-06-01")
    retained_ids = [m["id"] for m in filtered]
    assert "m1" in retained_ids
    assert "m2" in retained_ids
    assert "m3" not in retained_ids
    assert "m4" not in retained_ids

    # 3. Test violation detector
    violations = firewall.check_knowledge_violation(
        "The team benchmarked against a conversational ai assistant "
        "and a frontier reasoning model.",
        "2014-01-01",
    )
    assert len(violations) >= 2
    assert any("conversational ai assistant" in v.lower() for v in violations)


def test_world_state_snapshot_restore() -> None:
    """Requirement 3 & Acceptance: World state capture, modification, and deep restoration."""
    mgr = WorldStateManager()
    builder = WorldBuilderService()
    seed = builder.build_seed(
        "Novacore pushes its custom AI silicon program in 2011.",
        start_date="2011-10-05",
        metadata=demo_seed_metadata(),
    )

    state_init = mgr.initialize_state_from_seed(seed)
    assert state_init.timestamp == "2011-10-05"
    assert "novacore_corp" in state_init.organizations
    assert "novacore_ai_chip_initiative" in state_init.active_projects

    # Snapshot
    snap = mgr.snapshot(state_init, "world_test", "branch_main")
    assert snap.state.timestamp == "2011-10-05"

    # Mutate state
    state_mutated = state_init.model_copy(deep=True)
    state_mutated.timestamp = "2015-01-01"
    state_mutated.organizations["novacore_corp"]["cash_reserves_billions"] = 120.0
    state_mutated.active_projects["novacore_ai_chip_initiative"]["progress_percent"] = 85

    # Compute diff
    diff_result = mgr.diff(state_init, state_mutated)
    assert diff_result["from_timestamp"] == "2011-10-05"
    assert diff_result["to_timestamp"] == "2015-01-01"
    assert "novacore_corp" in diff_result["organization_changes"]
    assert "novacore_ai_chip_initiative" in diff_result["project_changes"]

    # Restore from snapshot and verify complete rollback
    restored = mgr.restore(snap)
    assert restored.timestamp == "2011-10-05"
    assert restored.organizations["novacore_corp"]["cash_reserves_billions"] == 81.6
    assert restored.active_projects["novacore_ai_chip_initiative"]["progress_percent"] == 5


def test_actor_action_changes_world() -> None:
    """Requirements 4, 5 & Acceptance: Actor actions resolve against resources and mutate state."""
    state_mgr = WorldStateManager()
    actor_mgr = ActorManager()
    resolver = ActionResolver()

    builder = WorldBuilderService()
    seed = builder.build_seed(
        "Novacore starts its silicon program in 2011.",
        start_date="2011-10-05",
        metadata=demo_seed_metadata(),
    )
    state = state_mgr.initialize_state_from_seed(seed)
    actors = actor_mgr.initialize_actors_for_world(seed)

    ceo = actors["ada_lovelace"]
    ceo.identity["organization"] = "novacore_corp"

    # Launch project action
    action = ActorAction(
        actor_id="ada_lovelace",
        action_type=ActionType.LAUNCH_PROJECT,
        description="Launch Novacore Datacenter Accelerator Project",
        parameters={
            "project_id": "novacore_datacenter_accelerator",
            "name": "Novacore Datacenter Accelerator",
            "budget_billions": 4.0,
            "team_engineers": 300,
        },
    )

    resolution, new_state = resolver.resolve_action(action, ceo, state)

    assert resolution.success is True
    assert resolution.success_probability > 0.8
    assert "novacore_datacenter_accelerator" in new_state.active_projects
    assert new_state.active_projects["novacore_datacenter_accelerator"]["budget_billions"] == 4.0
    # Cash deducted from the actor's organization
    expected_cash = state.organizations["novacore_corp"]["cash_reserves_billions"]
    assert new_state.organizations["novacore_corp"]["cash_reserves_billions"] == expected_cash - 4.0


def test_branch_independence() -> None:
    """Requirement 9 & Acceptance: Multiple branches maintain independent states."""
    branch_mgr = BranchManager()
    state_mgr = WorldStateManager()
    builder = WorldBuilderService()

    seed = builder.build_seed(
        "2011 custom silicon divergence",
        start_date="2011-10-05",
        metadata=demo_seed_metadata(),
    )
    root_state = state_mgr.initialize_state_from_seed(seed)

    root_branch = branch_mgr.create_root_branch("world_1", "main", root_state)
    snap = state_mgr.snapshot(root_state, "world_1", root_branch.id)

    # Fork Branch A: Aggressive investment
    branch_a = branch_mgr.fork_branch("world_1", root_branch.id, snap, "Branch_A_Aggressive")
    branch_a.current_state.active_projects["novacore_ai_chip_initiative"]["budget_billions"] = 15.0  # type: ignore[index]
    branch_a.current_state.organizations["novacore_corp"]["cash_reserves_billions"] -= 15.0  # type: ignore[index]
    branch_mgr.update_branch_state(branch_a.id, branch_a.current_state)  # type: ignore[arg-type]

    # Fork Branch B: Consumer focus only
    branch_b = branch_mgr.fork_branch("world_1", root_branch.id, snap, "Branch_B_Consumer")
    branch_b.current_state.active_projects["novacore_ai_chip_initiative"]["budget_billions"] = 1.0  # type: ignore[index]
    branch_b.current_state.organizations["novacore_corp"]["cash_reserves_billions"] -= 1.0  # type: ignore[index]
    branch_mgr.update_branch_state(branch_b.id, branch_b.current_state)  # type: ignore[arg-type]

    # Assert strict independence
    assert (
        branch_a.current_state.active_projects["novacore_ai_chip_initiative"]["budget_billions"]
        == 15.0  # type: ignore[index]
    )
    assert (
        branch_b.current_state.active_projects["novacore_ai_chip_initiative"][
            "budget_billions"
        ]
        == 1.0  # type: ignore[index]
    )
    assert (
        root_branch.current_state.active_projects["novacore_ai_chip_initiative"]["budget_billions"]
        == 5.0  # type: ignore[index]
    )


def test_outcome_evaluator_uses_metrics() -> None:
    """Requirement 10 & Acceptance: Evaluator uses metrics and reports branch distribution."""
    evaluator = OutcomeEvaluator()
    branch_mgr = BranchManager()
    state_mgr = WorldStateManager()
    builder = WorldBuilderService()

    seed = builder.build_seed(
        "Novacore vs Helion compute rivalry 2011",
        start_date="2011-10-05",
        metadata=demo_seed_metadata(),
    )
    base_state = state_mgr.initialize_state_from_seed(seed)

    branches = []
    # Branch 1: leading project progress and event momentum
    b1 = branch_mgr.create_root_branch("world_eval", "b1_breakthrough", base_state)
    b1.current_state.active_projects["novacore_ai_chip_initiative"]["progress_percent"] = 100  # type: ignore[index]
    for i in range(12):
        b1.current_state.events.append(f"evt_{i}")  # type: ignore[union-attr]
    branches.append(b1)

    # Branch 2: stalled program, few events
    b2 = branch_mgr.create_root_branch("world_eval", "b2_stall", base_state)
    b2.current_state.active_projects["novacore_ai_chip_initiative"]["progress_percent"] = 10  # type: ignore[index]
    branches.append(b2)

    # Branch 3: stalled program, few events
    b3 = branch_mgr.create_root_branch("world_eval", "b3_stall", base_state)
    b3.current_state.active_projects["novacore_ai_chip_initiative"]["progress_percent"] = 15  # type: ignore[index]
    branches.append(b3)

    evaluation = evaluator.evaluate_simulation(
        world_id="world_eval",
        question="Does the program break through or stall?",
        branches=branches,
        metrics={
            "event_momentum": 0.3,
            "project_progress": 0.4,
            "capability_maturity": 0.1,
            "resource_strength": 0.1,
            "relationship_stability": 0.1,
        },
    )

    assert evaluation.total_branches == 3
    assert evaluation.distribution["Leading Outcome"] == 1
    # The two stalled branches must both classify below the leading branch.
    stalled = [
        r for r in evaluation.branch_results if r["branch_id"] in (b2.id, b3.id)
    ]
    assert all(r["scenario"] != "Leading Outcome" for r in stalled)
    assert "real-world probability" not in evaluation.causal_summary.lower()
    assert "Evaluated across 3 independent counterfactual branch(es)" in evaluation.causal_summary
    # The generic evaluator must not name specific companies or domains.
    assert "novacore" not in evaluation.causal_summary.lower()
    assert "apple" not in evaluation.causal_summary.lower()
    assert "nvidia" not in evaluation.causal_summary.lower()
