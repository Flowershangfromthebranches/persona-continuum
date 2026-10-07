"""Persisted production-path context scale: SQLite -> turns -> windows.

Does not send private chat or call an external model.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from persona_continuum.agent.prompt_transport import capability_for_mode
from persona_continuum.application.material_intelligence import EvidenceUnit
from persona_continuum.application.material_pipeline import (
    MaterialPipelineMetrics,
    ResolvedExecutionProfile,
)
from persona_continuum.domain.persona import PersonaType

CHAT = "chat_import"
BASE = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
WINDOWS = (65_536, 131_072, 200_000, 500_000, 1_000_000)
HIDDEN_UNIT_CAP = 1_200


def _profile(window: int, *, remaining: int | None = None) -> ResolvedExecutionProfile:
    remaining_value = window if remaining is None else remaining
    return ResolvedExecutionProfile(
        adapter_id="fake",
        model_id="synthetic",
        context_window=window,
        native_context_window=window,
        runtime_effective_context=window,
        remaining_context_tokens=remaining_value,
        remaining_context_verified=True,
        remaining_context_source="runtime_reported",
        context_verified=True,
        context_capability_source="runtime_reported",
        context_window_source="runtime_reported",
        usable_context_budget=max(1, int(window * 0.9)),
        phase_working_target=max(1, int(window * 0.8)),
        parallel_safe=True,
        prompt_transport=capability_for_mode("stdin"),
        persistent_session=False,
    )


def _units(persona_id: str, source_id: str, count: int, *, text: str) -> list[EvidenceUnit]:
    return [
        EvidenceUnit(
            id=f"scale_{index:05d}",
            persona_id=persona_id,
            source_id=source_id,
            source_locator={"segment_index": index},
            speaker="对方",
            speaker_role="target_persona",
            timestamp=(BASE + timedelta(minutes=index * 3)).isoformat(),
            text=text,
            normalized_text=text,
            source_kind=CHAT,
            metadata={"semantic_status": "target_pending"},
        )
        for index in range(count)
    ]


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return int(ordered[index])


def _reset_pending(service, persona_id: str) -> None:
    for batch in service._iter_units_batched(persona_id):
        changed = []
        for unit in batch:
            metadata = dict(unit.metadata)
            metadata["semantic_status"] = "target_pending"
            metadata.pop("classification_contract", None)
            metadata.pop("classification_status", None)
            metadata.pop("intelligence_cache_key", None)
            changed.append(
                unit.model_copy(
                    update={
                        "metadata": metadata,
                        # Must stay agent-stamped so ON CONFLICT can overwrite
                        # the completed V4 checkpoint for the next window size.
                        "extraction_method": "deterministic_plus_agent",
                    }
                )
            )
        service._persist_units(changed)


@pytest.mark.anyio
async def test_10000_short_turns_not_split_by_hidden_1200_cap(app) -> None:
    persona = app.personas.create(
        display_name="Scale 10k",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    source = app.personas.add_source_text(
        persona.id,
        title="synthetic",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="synthetic",
        metadata={},
    )
    service = app.material_intelligence
    service.semantic_gate_mode = "full"
    service.turn_gap_seconds = 0
    service.analysis_window_max_units = None
    service.analysis_window_max_episodes = None
    service._persist_units(_units(persona.id, source.id, 10_000, text="短"))
    calls: list[int] = []

    async def analyzer(phase, payload):
        calls.append(len(payload.get("target_units") or payload.get("turns") or []))
        return {"units": []}

    metrics = MaterialPipelineMetrics()
    await service._classify_persisted_with_agent(
        persona.id,
        analyzer,
        execution_profile=_profile(500_000),
        metrics=metrics,
    )
    hidden_cap_groups = (10_000 + HIDDEN_UNIT_CAP - 1) // HIDDEN_UNIT_CAP
    assert metrics.agent_calls >= 1
    assert metrics.turn_cap_hit_count == 0
    assert metrics.analysis_windows < hidden_cap_groups
    assert metrics.agent_calls < hidden_cap_groups
    assert max(calls) > HIDDEN_UNIT_CAP


@pytest.mark.anyio
async def test_v4_checkpoint_survives_context_change(app) -> None:
    persona = app.personas.create(
        display_name="V4 keep",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    source = app.personas.add_source_text(
        persona.id,
        title="synthetic",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="synthetic",
        metadata={},
    )
    units = _units(persona.id, source.id, 40, text="已完成的句子")
    for unit in units[:20]:
        unit.metadata.update(
            semantic_status="reviewed_no_independent_evidence",
            classification_contract="conversation-evidence-v4",
        )
    service = app.material_intelligence
    service.semantic_gate_mode = "full"
    service.turn_gap_seconds = 0
    service._persist_units(units)
    seen: list[str] = []

    async def analyzer(phase, payload):
        for item in payload.get("target_units") or []:
            seen.append(str(item.get("id") if isinstance(item, dict) else item))
        for item in payload.get("units") or []:
            if isinstance(item, dict) and item.get("semantic_role") == "target":
                seen.append(str(item.get("id") or ""))
        return {"units": []}

    await service._classify_persisted_with_agent(
        persona.id,
        analyzer,
        execution_profile=_profile(500_000),
        metrics=MaterialPipelineMetrics(),
    )
    completed = {unit.id for unit in units[:20]}
    assert completed.isdisjoint(seen)


@pytest.mark.anyio
async def test_remaining_80k_caps_persisted_working_budget(app) -> None:
    persona = app.personas.create(
        display_name="Remaining 80k",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    source = app.personas.add_source_text(
        persona.id,
        title="synthetic",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="synthetic",
        metadata={},
    )
    long_text = "这是一条合成目标人格发言，用来填满分析窗口。" * 12
    service = app.material_intelligence
    service.semantic_gate_mode = "full"
    service.turn_gap_seconds = 0
    service.analysis_window_max_units = None
    service.analysis_window_max_episodes = None
    service._persist_units(_units(persona.id, source.id, 80, text=long_text))
    profile = _profile(500_000, remaining=80_000)
    profile.persistent_session = True
    metrics = MaterialPipelineMetrics()

    async def analyzer(phase, payload):
        return {"units": []}

    await service._classify_persisted_with_agent(
        persona.id,
        analyzer,
        execution_profile=profile,
        metrics=metrics,
    )
    assert metrics.analysis_windows >= 1
    assert metrics.max_batch_target_tokens is not None
    assert metrics.max_batch_target_tokens <= 80_000


@pytest.mark.anyio
async def test_each_analysis_window_uses_an_independent_logical_session(app) -> None:
    """PER_WINDOW isolation: every window carries its own participant id."""

    persona = app.personas.create(
        display_name="Window isolation",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    source = app.personas.add_source_text(
        persona.id,
        title="synthetic",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="synthetic",
        metadata={},
    )
    service = app.material_intelligence
    service.semantic_gate_mode = "full"
    service.turn_gap_seconds = 0
    service.analysis_window_max_units = None
    service.analysis_window_max_episodes = None
    long_text = "这是一条合成目标人格发言，用来填满分析窗口。" * 12
    service._persist_units(_units(persona.id, source.id, 60, text=long_text))
    participants: list[str] = []

    async def analyzer(phase, payload):
        participants.append(str(payload.get("_participant_id") or ""))
        return {"units": []}

    await service._classify_persisted_with_agent(
        persona.id,
        analyzer,
        execution_profile=_profile(128_000),
        metrics=MaterialPipelineMetrics(),
    )
    assert participants
    assert all(participants), "every window must name its logical session"
    assert len(set(participants)) == len(participants), (
        "analysis windows must not share one logical session"
    )


@pytest.mark.anyio
async def test_persisted_production_windows_drop_from_128k_to_500k(app, tmp_path) -> None:
    persona = app.personas.create(
        display_name="Scale path",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    source = app.personas.add_source_text(
        persona.id,
        title="synthetic",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="synthetic",
        metadata={},
    )
    long_text = "这是一条合成目标人格发言，用来填满分析窗口。" * 12
    service = app.material_intelligence
    service.semantic_gate_mode = "full"
    service.turn_gap_seconds = 0
    service.analysis_window_max_units = None
    service.analysis_window_max_episodes = None
    service._persist_units(_units(persona.id, source.id, 2_000, text=long_text))
    estimate = service.context_budget_manager.estimate_tokens

    rows = []
    for window in WINDOWS:
        calls: list[int] = []
        prompt_tokens: list[int] = []

        async def analyzer(phase, payload, bucket=calls, tokens=prompt_tokens):
            bucket.append(len(payload.get("target_units") or []))
            tokens.append(estimate(payload))
            return {"units": []}

        metrics = MaterialPipelineMetrics()
        started = datetime.now(UTC)
        await service._classify_persisted_with_agent(
            persona.id,
            analyzer,
            execution_profile=_profile(window),
            metrics=metrics,
        )
        elapsed = (datetime.now(UTC) - started).total_seconds()
        assert metrics.raw_message_count == 2_000
        assert metrics.analysis_windows >= 1, (
            f"expected production windows for {window}; "
            f"calls={metrics.agent_calls} pending={metrics.raw_message_count}"
        )
        reasons = {
            "token_budget": metrics.context_cap_hit_count,
            "unit_cap": metrics.turn_cap_hit_count,
            "episode_cap": metrics.episode_cap_hit_count,
            "transport_cap": metrics.transport_cap_hit_count,
        }
        rows.append(
            {
                "runtime_context": window,
                "target_turns": 2_000,
                "windows": metrics.analysis_windows,
                "initial_windows": metrics.initial_analysis_windows or metrics.analysis_windows,
                "agent_calls": max(metrics.agent_calls, len(calls)),
                "rebatched_windows": metrics.rebatched_windows,
                "packing_accuracy": metrics.packing_accuracy,
                "rebatched_window_ratio": metrics.rebatched_window_ratio,
                "rebatch_reasons": dict(metrics.rebatch_reasons),
                "avg_prompt_tokens": round(sum(prompt_tokens) / max(1, len(prompt_tokens)))
                if prompt_tokens
                else None,
                "p50_prompt_tokens": _percentile(prompt_tokens, 0.50),
                "p95_prompt_tokens": _percentile(prompt_tokens, 0.95),
                "p50_utilization": metrics.prompt_budget_utilization_p50,
                "p95_utilization": metrics.prompt_budget_utilization_p95,
                "budget_utilization": metrics.prompt_budget_utilization_avg,
                "estimation_error_p50": metrics.estimation_error_p50,
                "estimation_error_p95": metrics.estimation_error_p95,
                "turns_per_window": round(2_000 / max(1, metrics.analysis_windows), 2),
                "episodes_per_window": metrics.episodes_per_window_avg,
                "elapsed": elapsed,
                "cap_trigger_reason": reasons,
                "max_batch_target_tokens": metrics.max_batch_target_tokens,
            }
        )
        _reset_pending(service, persona.id)

    by_window = {row["runtime_context"]: row for row in rows}
    assert by_window[500_000]["windows"] < by_window[131_072]["windows"]
    assert by_window[500_000]["agent_calls"] < by_window[131_072]["agent_calls"]
    five = by_window[500_000]
    assert five["rebatched_windows"] <= max(1, int(five["initial_windows"] * 0.25))
    assert five["packing_accuracy"] >= 0.75
    assert five["estimation_error_p95"] >= -0.10
    out = Path("docs/reports/implementation/context-window-scale-benchmark.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "path": "sqlite_stream -> ConversationTurn -> episode packing -> "
                "dynamic planner -> producer queue -> classification windows",
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
