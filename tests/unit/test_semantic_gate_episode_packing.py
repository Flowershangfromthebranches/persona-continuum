"""P0.3 / P1 unit tests: episode-aware window packing, gate resolver, ShadowGate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from persona_continuum.application.material_intelligence import EvidenceUnit
from persona_continuum.application.material_pipeline import (
    MaterialPipelineMetrics,
    build_analysis_windows,
    build_conversation_episodes,
)
from persona_continuum.application.semantic_gate import (
    LARGE_CHAT_TARGET_MESSAGE_THRESHOLD,
    resolve_task_semantic_gate_mode,
)
from persona_continuum.application.shadow_gate import evaluate_shadow_gate

BASE = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
CHAT = "chat_import"


def _chat_unit(index: int, text: str, at: datetime, role: str = "target_persona") -> EvidenceUnit:
    return EvidenceUnit(
        id=f"u{index:04d}",
        persona_id="p",
        source_id="s1",
        source_locator={"segment_index": index},
        speaker="对方",
        speaker_role=role,
        timestamp=at.isoformat(),
        text=text,
        normalized_text=text,
        source_kind=CHAT,
        metadata={"semantic_status": "target_pending"},
    )


def _est(text: str) -> int:
    return max(1, len(text) // 4)


# ------------------------------------------------------- P0.3-A/B packing --
def test_two_hour_gap_no_longer_forces_window_flush():
    """A silent evening must not become a separate model dispatch."""

    units = [
        _chat_unit(0, "早上聊的第一句", BASE),
        _chat_unit(1, "晚上聊的一句", BASE + timedelta(hours=3)),
    ]
    windows = build_analysis_windows(
        units, estimate_tokens=_est, target_tokens=10**9, max_units=1200
    )
    assert len(windows) == 1
    assert len(windows[0].episodes) == 2


def test_episodes_still_exist_as_semantic_blocks():
    units = [
        _chat_unit(0, "第一条", BASE),
        _chat_unit(1, "第二条", BASE + timedelta(hours=3)),
    ]
    episodes = build_conversation_episodes(units, episode_gap_seconds=7200)
    assert [episode.unit_ids for episode in episodes] == [["u0000"], ["u0001"]]


def test_episode_markers_present_in_window_text():
    units = [
        _chat_unit(0, "第一条", BASE),
        _chat_unit(1, "第二条", BASE + timedelta(hours=3)),
    ]
    window = build_analysis_windows(
        units, estimate_tokens=_est, target_tokens=10**9, max_units=1200
    )[0]
    assert window.text.count("[EPISODE_BEGIN") == 2
    assert window.text.count("[EPISODE_END") == 2
    assert "EPISODE_BEGIN" in window.text
    episode_ids = {episode["episode_id"] for episode in window.episodes}
    assert len(episode_ids) == 2
    assert all(episode["start_time"] and episode["end_time"] for episode in window.episodes)


def test_legacy_episode_flush_reproduces_old_topology():
    units = [
        _chat_unit(0, "第一条", BASE),
        _chat_unit(1, "第二条", BASE + timedelta(hours=3)),
    ]
    windows = build_analysis_windows(
        units,
        estimate_tokens=_est,
        target_tokens=10**9,
        max_units=1200,
        episode_flush=True,
    )
    assert len(windows) == 2


def test_max_episodes_per_window_caps_packing():
    units = [
        _chat_unit(index, f"第{index}句", BASE + timedelta(hours=3 * index)) for index in range(5)
    ]
    windows = build_analysis_windows(
        units,
        estimate_tokens=_est,
        target_tokens=10**9,
        max_units=None,
        max_episodes_per_window=2,
    )
    assert [len(window.episodes) for window in windows] == [2, 2, 1]


def test_budget_constrained_packing_preserves_every_unit():
    units = [
        _chat_unit(index, f"第{index}句一些内容", BASE + timedelta(hours=3 * index))
        for index in range(12)
    ]
    windows = build_analysis_windows(units, estimate_tokens=_est, target_tokens=120)
    assert sum(len(window.evidence_unit_ids) for window in windows) == len(units)


def test_non_chat_document_packing_unchanged():
    units = [
        EvidenceUnit(
            id=f"d{index:04d}",
            persona_id="p",
            source_id="s1",
            text="正文内容" * 20,
            normalized_text="正文内容" * 20,
            source_kind="md",
            timestamp=None,
        )
        for index in range(500)
    ]
    windows = build_analysis_windows(
        units, estimate_tokens=lambda _value: 5, target_tokens=10**6, max_units=1200
    )
    assert len(windows) == 1


def test_context_only_units_do_not_consume_target_ceiling():
    units = [_chat_unit(index, "x" * 40, BASE) for index in range(10)]
    context_units = []
    for unit in units[5:]:
        context_units.append(unit.model_copy(update={"semantic_role": "context_only"}))
    units = [*units[:5], *context_units]
    windows = build_analysis_windows(
        units, estimate_tokens=lambda _value: 5, target_tokens=10**6, max_units=3
    )
    assert len(windows) == 2  # 5 targets / cap 3


# ----------------------------------------------------- P0.3-C new metrics --
def test_metrics_record_and_finalize_window_metrics():
    units = [
        _chat_unit(index, f"第{index}句", BASE + timedelta(hours=3 * index)) for index in range(6)
    ]
    windows = build_analysis_windows(
        units, estimate_tokens=_est, target_tokens=10**6, max_units=None
    )
    metrics = MaterialPipelineMetrics()
    metrics.record_analysis_windows(windows, target_tokens=10**6)
    metrics.finalize_window_metrics()
    assert metrics.episodes_total == 6
    assert metrics.episodes_per_window_avg == pytest.approx(6.0)
    assert metrics.episodes_per_window_max == 6
    assert metrics.prompt_budget_utilization_avg <= 1.0
    assert metrics.prompt_budget_utilization_p50 <= metrics.prompt_budget_utilization_p95


# ------------------------------------------------------------ P1-C policy --
def test_auto_small_chat_stays_full():
    mode, reason = resolve_task_semantic_gate_mode("auto", chat_target_messages=50)
    assert mode == "full"
    assert "small" in reason


def test_auto_large_chat_without_acceptance_stays_full():
    mode, reason = resolve_task_semantic_gate_mode(
        "auto",
        chat_target_messages=LARGE_CHAT_TARGET_MESSAGE_THRESHOLD * 10,
        shadow_gate_accepted=False,
    )
    assert mode == "full"
    assert "not_accepted" in reason


def test_auto_large_chat_with_acceptance_enables_balanced():
    mode, reason = resolve_task_semantic_gate_mode(
        "auto",
        chat_target_messages=LARGE_CHAT_TARGET_MESSAGE_THRESHOLD * 10,
        shadow_gate_accepted=True,
    )
    assert mode == "balanced"
    assert "accepted" in reason


def test_explicit_task_mode_passes_through():
    mode, reason = resolve_task_semantic_gate_mode("fast")
    assert (mode, reason) == ("fast", "task_explicit")


def test_unknown_task_mode_falls_back_to_auto_rules():
    mode, _reason = resolve_task_semantic_gate_mode("mystery")
    assert mode == "full"


# ------------------------------------------- P1-A/B ShadowGate evaluation --
def _classified_pair(index: int, at: datetime, *, evidence: bool) -> list[EvidenceUnit]:
    status = "evidence_extracted" if evidence else "reviewed_no_independent_evidence"
    text = (
        "我喜欢现在的工作，准备今年考个证书。"
        if evidence
        else "今天天气不错啊，我们出去走走吧，顺便在附近逛逛，晚上再一起吃个饭。"
    )
    anchor = EvidenceUnit(
        id=f"e{index:04d}",
        persona_id="p",
        source_id="s1",
        source_locator={"segment_index": index},
        speaker="对方",
        speaker_role="target_persona",
        timestamp=at.isoformat(),
        text=text,
        normalized_text=text,
        source_kind=CHAT,
        dimension_candidates=["decisions_and_behavior"] if evidence else [],
        relationship_entities=["同事"] if evidence else [],
        confidence=0.85 if evidence else 0.4,
        metadata={"semantic_status": status, "classification_contract": "conversation-evidence-v4"},
    )
    return [anchor]


def test_shadow_gate_reports_recall_over_classified_turns():
    units: list[EvidenceUnit] = []
    index = 0
    at = BASE
    for _round in range(20):
        # Three routine turns then one evidence turn: a turn directly after a
        # bypass-strength statement is selected by design (critical_context),
        # so keep the ratio realistic by separating them.
        for _neg in range(3):
            units.extend(_classified_pair(index, at, evidence=False))
            index += 1
            at += timedelta(minutes=5)
        units.extend(_classified_pair(index, at, evidence=True))
        index += 1
        at += timedelta(minutes=5)
    report = evaluate_shadow_gate(units, mode="balanced")
    assert report.evidence_turns_total > 0
    assert report.units_total == len(units)
    # Evidence-bearing statements carry first-person stance features, so the
    # deterministic bypass should capture a healthy share of them.
    assert report.evidence_recall_overall > 0.5
    assert 0.0 < report.selected_ratio < 1.0
    assert report.temporal_months_total == 1
    assert report.temporal_coverage == 1.0
    # Every selected-but-negative turn is a false positive the report must
    # be able to express; recall thresholds live in the acceptance test.
    assert report.reviewed_turns_total == 60


def test_shadow_gate_accepts_perfect_gate():
    """A mode where everything is selected recalls every evidence turn."""

    units: list[EvidenceUnit] = []
    for index in range(10):
        units.extend(_classified_pair(index, BASE + timedelta(minutes=index), evidence=True))
    report = evaluate_shadow_gate(units, mode="full")
    assert report.accepted
    assert report.evidence_recall_overall == 1.0


def test_shadow_gate_report_has_no_message_text():
    units = _classified_pair(0, BASE, evidence=True)
    payload = evaluate_shadow_gate(units, mode="balanced").model_dump(mode="json")
    serialized = str(payload)
    assert "我喜欢现在的工作" not in serialized


# ------------------------------------------------------- P1-D resume兼容 --
def _profile() -> object:
    from persona_continuum.application.material_pipeline import ResolvedExecutionProfile

    return ResolvedExecutionProfile()


def test_completed_v4_rows_reusable_across_gate_mode_switch():
    """已完成的 26094 条在 full→balanced 切换后必须继续复用，不重新调模型。"""

    from persona_continuum.application.material_intelligence import (
        CLASSIFICATION_CONTRACT_V4,
        SEMANTIC_EVIDENCE_EXTRACTED,
        SEMANTIC_REVIEWED_NO_EVIDENCE,
        MaterialIntelligenceService,
    )

    service = MaterialIntelligenceService.__new__(MaterialIntelligenceService)
    service.semantic_gate_mode = "full"
    profile = _profile()
    for status in (SEMANTIC_EVIDENCE_EXTRACTED, SEMANTIC_REVIEWED_NO_EVIDENCE):
        unit = EvidenceUnit(
            id="done1",
            persona_id="p",
            source_id="s1",
            text="x",
            normalized_text="x",
            source_kind=CHAT,
            metadata={
                "semantic_status": status,
                "classification_contract": CLASSIFICATION_CONTRACT_V4,
            },
        )
        assert service._classification_done(unit, profile, gate_mode="balanced")
        assert service._classification_done(unit, profile, gate_mode="full")


def test_skipped_row_reconsidered_only_when_mode_matches():
    from persona_continuum.application.material_intelligence import (
        MaterialIntelligenceService,
    )
    from persona_continuum.application.semantic_gate import (
        SEMANTIC_GATE_POLICY_VERSION as GATE_V,
    )

    service = MaterialIntelligenceService.__new__(MaterialIntelligenceService)
    service.semantic_gate_mode = "full"
    profile = _profile()
    unit = EvidenceUnit(
        id="skip1",
        persona_id="p",
        source_id="s1",
        text="嗯",
        normalized_text="嗯",
        source_kind=CHAT,
        metadata={
            "semantic_status": "semantic_gate_skipped",
            "semantic_gate_policy": GATE_V,
            "semantic_gate_mode": "balanced",
        },
    )
    # Skipped under balanced: done while balanced is active, owed again on full.
    assert service._classification_done(unit, profile, gate_mode="balanced")
    assert not service._classification_done(unit, profile, gate_mode="full")
