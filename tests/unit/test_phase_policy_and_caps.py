"""Phase working policy and dynamic unit/episode caps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.models import EffectiveModelCapabilities
from persona_continuum.agent.phase_policy import (
    BASELINE_EPISODES_CAP,
    BASELINE_UNITS_CAP,
    auto_episodes_cap,
    auto_units_cap,
    default_phase_context_policy,
)
from persona_continuum.application.material_intelligence import EvidenceUnit
from persona_continuum.application.material_pipeline import (
    ResolvedExecutionProfile,
    build_analysis_windows,
)
from persona_continuum.config import Config


def test_phase_ratios_are_not_global_half() -> None:
    policy = default_phase_context_policy()
    classify = policy.working_target(
        500_000, phase="material_classification", verified=True, fresh_stateless=True
    )
    compile_target = policy.working_target(
        500_000, phase="persona_compilation", verified=True
    )
    unknown = policy.working_target(500_000, phase="mystery", verified=False)
    assert classify is not None and compile_target is not None and unknown is not None
    assert classify > compile_target > unknown
    assert 0.75 * 500_000 <= classify <= 0.85 * 500_000


def test_dynamic_caps_grow_with_working_budget() -> None:
    small = auto_episodes_cap(65_536, token_budget_verified=False)
    large = auto_episodes_cap(500_000, token_budget_verified=False)
    assert small == BASELINE_EPISODES_CAP
    assert large is not None and large > small
    assert auto_units_cap(500_000, token_budget_verified=False) > BASELINE_UNITS_CAP
    assert auto_episodes_cap(500_000, token_budget_verified=True) is None
    assert auto_units_cap(500_000, token_budget_verified=True) is None


def test_config_phase_ratios_change_working_target() -> None:
    baseline = AgentContextBudgetManager()
    config = Config(
        phase_working_ratios={
            "material_classification": 0.90,
            "research": 0.40,
            "persona_compilation": 0.30,
            "audit": 0.55,
        }
    )
    custom = AgentContextBudgetManager(
        phase_policy=default_phase_context_policy(config.phase_working_ratios)
    )
    model = EffectiveModelCapabilities(
        effective_context_window=500_000,
        remaining_context_tokens=500_000,
        remaining_context_verified=True,
        context_verified=True,
        context_capability_source="runtime_reported",
    )
    classify_base = baseline.budget_for(model=model, phase="material_classification")
    classify_custom = custom.budget_for(model=model, phase="material_classification")
    research_base = baseline.budget_for(model=model, phase="public_research")
    research_custom = custom.budget_for(model=model, phase="public_research")
    compile_base = baseline.budget_for(model=model, phase="persona_compilation")
    compile_custom = custom.budget_for(model=model, phase="persona_compilation")
    audit_base = baseline.budget_for(model=model, phase="audit")
    audit_custom = custom.budget_for(model=model, phase="audit")
    assert classify_custom.phase_working_target != classify_base.phase_working_target
    assert research_custom.phase_working_target != research_base.phase_working_target
    assert compile_custom.phase_working_target != compile_base.phase_working_target
    assert audit_custom.phase_working_target != audit_base.phase_working_target
    assert classify_custom.phase_working_target > classify_base.phase_working_target
    assert research_custom.phase_working_target < research_base.phase_working_target


def test_remaining_80k_caps_working_below_native_500k() -> None:
    manager = AgentContextBudgetManager()
    model = EffectiveModelCapabilities(
        native_context_window=500_000,
        effective_context_window=500_000,
        remaining_context_tokens=80_000,
        remaining_context_verified=True,
        remaining_context_source="runtime_reported",
        context_verified=True,
        context_capability_source="runtime_reported",
        persistent_session=True,
    )
    budget = manager.budget_for(model=model, phase="material_classification")
    assert budget.remaining_context_tokens == 80_000
    assert budget.usable_context_budget is not None
    assert budget.usable_context_budget <= 80_000
    assert budget.phase_working_target is not None
    assert budget.phase_working_target <= budget.usable_context_budget
    assert budget.evidence_token_budget <= 80_000


def test_remaining_80k_from_execution_profile_dict() -> None:
    manager = AgentContextBudgetManager()
    profile = ResolvedExecutionProfile(
        native_context_window=500_000,
        context_window=500_000,
        runtime_effective_context=500_000,
        remaining_context_tokens=80_000,
        remaining_context_verified=True,
        remaining_context_source="runtime_reported",
        context_verified=True,
        context_capability_source="runtime_reported",
        persistent_session=True,
    )
    budget = manager.budget_for(
        model=profile.model_dump(mode="json"),
        phase="material_classification",
    )
    assert budget.remaining_context_tokens == 80_000
    assert budget.usable_context_budget is not None
    assert budget.usable_context_budget <= 80_000
    assert budget.phase_working_target is not None
    assert budget.phase_working_target <= 80_000


def test_explicit_cap_still_honored() -> None:
    assert auto_episodes_cap(500_000, configured=2) == 2
    assert auto_units_cap(500_000, configured=10) == 10


def test_500k_working_budget_is_not_stuck_at_24_episodes() -> None:
    base = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
    units = [
        EvidenceUnit(
            id=f"u{index:04d}",
            persona_id="p",
            source_id="s1",
            speaker="对方",
            speaker_role="target_persona",
            timestamp=(base + timedelta(hours=3 * index)).isoformat(),
            text="短句",
            normalized_text="短句",
            source_kind="chat_import",
        )
        for index in range(80)
    ]
    cap = auto_episodes_cap(400_000, token_budget_verified=True)
    windows = build_analysis_windows(
        units,
        estimate_tokens=lambda text: max(1, len(text) // 4),
        target_tokens=400_000,
        max_units=None,
        max_episodes_per_window=cap,
    )
    assert len(windows) == 1
    assert len(windows[0].episodes) == 80
    assert cap is None


def test_verified_budget_drops_windows_from_64k_to_500k() -> None:
    base = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
    units = [
        EvidenceUnit(
            id=f"u{index:04d}",
            persona_id="p",
            source_id="s1",
            speaker="对方",
            speaker_role="target_persona",
            timestamp=(base + timedelta(minutes=index)).isoformat(),
            text="合成对话内容，用来填满分析窗口。" * 40,
            normalized_text="合成",
            source_kind="chat_import",
        )
        for index in range(400)
    ]

    def pack(window: int):
        working = int(window * 0.80)
        return build_analysis_windows(
            units,
            estimate_tokens=lambda text: max(1, len(text) // 4),
            target_tokens=working,
            max_units=auto_units_cap(working, token_budget_verified=True),
            max_episodes_per_window=auto_episodes_cap(working, token_budget_verified=True),
        )

    small = pack(65_536)
    large = pack(500_000)
    assert len(large) < len(small)
    assert all((w.flush_reason or "end") != "episode_cap" for w in small + large)
