"""Local-material performance contracts.

These tests measure call topology and affected-set size, not provider latency.
They intentionally use one provider-neutral evidence/runtime fixture.
"""

from __future__ import annotations

import json

import pytest

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.application.material_intelligence import (
    EvidenceUnit,
    FusedEvidence,
    MaterialIntelligenceService,
)
from persona_continuum.application.material_pipeline import (
    GlobalCandidateIndex,
    PreLLMDeduplicator,
    ResolvedExecutionProfile,
    build_analysis_windows,
)
from persona_continuum.domain.persona import PersonaType


def _unit(index: int, text: str, *, source: str | None = None) -> EvidenceUnit:
    return EvidenceUnit(
        id=f"evu_{index:05d}",
        persona_id="persona_perf",
        source_id=source or f"src_{index // 100:03d}",
        source_locator={"message_index": index},
        text=text,
        normalized_text=text.casefold(),
        relationship_entities=[f"entity_{index % 97}"],
        dimension_candidates=["decisions_and_behavior"],
    )


def test_case_a_1000_short_chats_pack_into_tens_of_windows() -> None:
    manager = AgentContextBudgetManager(default_context_window_tokens=32_768)
    units = [_unit(index, f"第{index}条对话：我会记录事实、决定与结果。") for index in range(1000)]
    windows = build_analysis_windows(
        units,
        estimate_tokens=manager.estimate_tokens,
        target_tokens=8_000,
    )
    assert sum(len(window.evidence_unit_ids) for window in windows) == 1000
    assert 7 <= len(windows) <= 60


def test_case_b_5000_chats_candidate_index_is_bounded() -> None:
    units = [_unit(index, f"unique-message-{index}") for index in range(5000)]
    groups = GlobalCandidateIndex().groups(units)
    assert all(2 <= len(group.evidence_ids) <= 12 for group in groups)
    assert sum(len(group.evidence_ids) for group in groups) < 5_000


def test_case_c_100k_character_profile_is_losslessly_windowed() -> None:
    manager = AgentContextBudgetManager(default_context_window_tokens=32_768)
    text = "人物在不同阶段记录决定、关系、失败与不确定性。" * 2200
    units = [_unit(index, text[index : index + 800]) for index in range(0, len(text), 800)]
    windows = build_analysis_windows(
        units,
        estimate_tokens=manager.estimate_tokens,
        target_tokens=12_000,
    )
    assert [item for window in windows for item in window.evidence_unit_ids] == [
        unit.id for unit in units
    ]
    assert all(window.token_estimate <= 13_000 for window in windows)


def test_case_d_40_percent_duplicates_are_removed_before_agent() -> None:
    unique = [_unit(index, f"stable claim {index}") for index in range(600)]
    duplicates = [
        _unit(600 + index, f"stable claim {index}", source=f"dup_{index}") for index in range(400)
    ]
    result = PreLLMDeduplicator().deduplicate([*unique, *duplicates])
    assert len(result.units) == 1000
    assert len(result.canonical_units) == 600
    assert sum(len(values) for values in result.supporting_units.values()) == 1000


def test_case_e_10000_plus_20_incremental_only_selects_affected_chunks() -> None:
    historical = [_unit(index, f"historical-{index}") for index in range(10_000)]
    delta = [_unit(10_000 + index, f"delta-{index}") for index in range(20)]
    affected_ids = {unit.id for unit in delta}
    groups = GlobalCandidateIndex().groups([*historical, *delta], affected_ids=affected_ids)
    touched = {item for group in groups for item in group.evidence_ids}
    assert affected_ids <= touched
    assert len(touched) <= 260  # 20 delta + at most one 12-item chunk per entity signal.


def test_case_f_overlapping_files_keep_every_provenance_locator() -> None:
    units = [
        _unit(1, "same fact", source="a.txt"),
        _unit(2, "same fact", source="b.md"),
        _unit(3, "same fact", source="chat.json"),
    ]
    result = PreLLMDeduplicator().deduplicate(units)
    assert len(result.canonical_units) == 1
    assert {item.source_id for item in result.units} == {"a.txt", "b.md", "chat.json"}
    assert set(result.supporting_units[result.canonical_units[0].id]) == {item.id for item in units}


def test_case_g_long_item_is_never_silently_truncated() -> None:
    manager = AgentContextBudgetManager(default_context_window_tokens=128_000)
    text = "long evidence " * 12_000
    unit = _unit(1, text)
    windows = build_analysis_windows(
        [unit], estimate_tokens=manager.estimate_tokens, target_tokens=64_000
    )
    assert windows[0].evidence_unit_ids == [unit.id]
    assert text in windows[0].text


def test_case_h_cjk_token_estimation_is_conservative() -> None:
    manager = AgentContextBudgetManager()
    chinese = "这是大量超短中文消息" * 100
    english = "a" * len(chinese)
    assert manager.estimate_tokens(chinese) >= len(chinese)
    assert manager.estimate_tokens(chinese) > manager.estimate_tokens(english) * 3


def test_runtime_matrix_resolves_by_capability_not_provider_name() -> None:
    snapshots = [
        {"persistent_session": False},  # stateless API
        {"capabilities": {"persistent_session": False}},  # stateless plain CLI
        {"capabilities": {"persistent_session": True, "parallel_safe": False}},
        {"capabilities": {"persistent_session": True, "parallel_safe": True}},
        {"capabilities": {"structured_output": True}},
        {"capabilities": {"structured_output": False}},
        {"context_window": 8_192, "context_window_source": "config_override"},
        {"selected_model": {"context_window": 1_000_000}},
    ]
    profiles = [ResolvedExecutionProfile.resolve(item) for item in snapshots]
    assert len(profiles) == 8
    assert profiles[2].parallel_safe is False
    assert profiles[3].parallel_safe is True
    assert profiles[4].structured_output_mode == "native"
    assert profiles[6].context_window == 8_192
    assert profiles[6].context_window_source == "config_override"
    assert profiles[7].context_window == 1_000_000


def test_selected_model_context_window_propagates_from_probe_snapshot() -> None:
    snapshot = {
        "effective_model": "large-model",
        "models": [
            {"id": "small-model", "context_window": 8_192},
            {"id": "large-model", "context_window": 262_144},
        ],
    }
    manager = AgentContextBudgetManager()
    profile = ResolvedExecutionProfile.resolve(snapshot)
    assert profile.context_window == 262_144
    assert profile.context_window_source == "provider_reported"
    assert manager.context_window(snapshot) == 262_144


@pytest.mark.anyio
async def test_singleton_fusion_makes_zero_agent_calls() -> None:
    unit = _unit(1, "single provenance-preserving claim")
    fused = FusedEvidence(
        id="evf_single",
        persona_id=unit.persona_id,
        canonical_claim=unit.text,
        evidence_type=unit.evidence_type,
        supporting_evidence_ids=[unit.id],
        unique_evidence_ids=[unit.id],
        source_ids=[unit.source_id],
    )
    service = MaterialIntelligenceService.__new__(MaterialIntelligenceService)
    service.context_budget_manager = AgentContextBudgetManager()
    calls = 0

    async def analyzer(phase: str, payload: dict):
        nonlocal calls
        calls += 1
        return {"claims": []}

    result = await service._fuse_with_agent([fused], [unit], analyzer)
    assert calls == 0
    assert result == [fused]


@pytest.mark.anyio
async def test_persisted_intelligence_cache_skips_identical_material(app) -> None:
    persona = app.personas.create(
        display_name="Cache Subject",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    source = app.personas.add_source_text(
        persona.id,
        title="cache-material",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at="2026-08-28T00:00:00+00:00",
        content="我会记录决定、结果和不确定性。",
        metadata={"source_kind": "user_provided"},
    )
    classify_calls = 0

    async def analyzer(phase: str, payload: dict):
        nonlocal classify_calls
        if phase == "classify":
            classify_calls += 1
            return {
                "units": [
                    {
                        "id": item["id"],
                        "claims": [item["text"]],
                        "dimension_scores": {"decisions_and_behavior": 0.9},
                    }
                    for item in payload["units"]
                ]
            }
        return {"clusters": [], "contradictions": [], "claims": []}

    snapshot = {
        "effective_model": "same-model",
        "effective_reasoning": "high",
        "context_window": 32_768,
    }
    await app.material_intelligence.analyze_sources_async(
        persona.id,
        [source.id],
        runtime_snapshot=snapshot,
        agent_analyzer=analyzer,
    )
    second = await app.material_intelligence.analyze_sources_async(
        persona.id,
        [source.id],
        runtime_snapshot=snapshot,
        agent_analyzer=analyzer,
    )
    assert classify_calls == 1
    assert second.progress["performance_metrics"]["cache_hits"]["intelligence"] == 1


@pytest.mark.anyio
async def test_incremental_agent_intelligence_only_reads_delta(app) -> None:
    persona = app.personas.create(
        display_name="Incremental Subject",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )

    def add_source(title: str, messages: list[str]):
        return app.personas.add_source_text(
            persona.id,
            title=title,
            source_type="json",
            canonical_url=None,
            publisher="user",
            author="user",
            published_at=None,
            accessed_at="2026-08-28T00:00:00+00:00",
            content='{"messages":'
            + json.dumps(
                [{"sender": "me", "content": item} for item in messages],
                ensure_ascii=False,
            )
            + "}",
            metadata={"source_kind": "user_provided"},
        )

    historical = add_source("history", [f"历史消息 {index}" for index in range(100)])
    delta = add_source("delta", ["增量消息 A", "增量消息 B"])
    classified_texts: list[str] = []

    async def analyzer(phase: str, payload: dict):
        if phase == "classify":
            classified_texts.extend(item["text"] for item in payload["units"])
            return {
                "units": [
                    {
                        "id": item["id"],
                        "claims": [item["text"]],
                        "dimension_scores": {"identity_and_timeline": 0.8},
                    }
                    for item in payload["units"]
                ]
            }
        return {"clusters": [], "contradictions": [], "claims": []}

    await app.material_intelligence.analyze_sources_async(
        persona.id, [historical.id], agent_analyzer=analyzer
    )
    classified_texts.clear()
    job = await app.material_intelligence.analyze_sources_async(
        persona.id,
        [historical.id, delta.id],
        incremental=True,
        agent_analyzer=analyzer,
    )
    assert classified_texts == ["增量消息 A", "增量消息 B"]
    metrics = job.progress["performance_metrics"]
    assert metrics["incremental_affected_units"] <= 14
