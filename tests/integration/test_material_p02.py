"""P0.2 transport, bounded dispatch, routing and V4 resume acceptance."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from persona_continuum.agent.adapters.gemini import GeminiCliAdapter
from persona_continuum.agent.adapters.other_vendors import KimiAdapter, QoderAdapter, QwenAdapter
from persona_continuum.agent.prompt_transport import (
    capability_for_mode,
    resolve_prompt_transport_capability,
)
from persona_continuum.application.classification_dispatch import ClassificationDispatch
from persona_continuum.application.material_chat import ConversationTurn
from persona_continuum.application.material_intelligence import EvidenceUnit
from persona_continuum.application.material_pipeline import (
    MaterialPipelineMetrics,
    ResolvedExecutionProfile,
    build_analysis_windows,
    material_batch_target_tokens,
)
from persona_continuum.application.semantic_gate import SemanticGate
from persona_continuum.domain.persona import PersonaType


def turn(index, text="嗯", **extra):
    return ConversationTurn(
        id=str(index),
        anchor_id=str(index),
        evidence_unit_ids=[str(index)],
        source_id="source",
        source_kind="chat_import",
        speaker_role="target_persona",
        text=text,
        start_time=(datetime(2023, 1, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
        **extra,
    )


def test_actual_plain_cli_transports():
    for adapter in [QwenAdapter(), KimiAdapter(), QoderAdapter(), GeminiCliAdapter()]:
        expected = (
            "argv" if any(flag in {"-p", "--print"} for flag in adapter.exec_args) else "stdin"
        )
        if getattr(adapter, "_use_stream_json", lambda: False)():
            expected = "stdin"
        cap = resolve_prompt_transport_capability(adapter)
        assert cap.transport_mode == expected
    cap = resolve_prompt_transport_capability(QwenAdapter())
    assert cap.transport_mode == "stdin" and cap.supports_large_prompt
    assert cap.safe_prompt_bytes > 1024 * 1024
    gemini = GeminiCliAdapter()
    expected_gemini = "stdin" if gemini._is_agy_binary() else "argv"
    assert resolve_prompt_transport_capability(gemini).transport_mode == expected_gemini


def test_100k_turn_transport_windows():
    turns = [turn(index, "今天讨论一个普通话题") for index in range(100_000)]
    counts = []
    for mode in ("unknown", "stdin"):
        profile = ResolvedExecutionProfile(
            usable_context_budget=100_000,
            preferred_working_context=100_000,
            prompt_transport=capability_for_mode(mode),
        )
        tokens = material_batch_target_tokens(profile, phase_usable_budget=100_000)
        windows = build_analysis_windows(
            turns,
            estimate_tokens=lambda text: len(text),
            target_tokens=tokens,
            max_units=1200,
        )
        counts.append(len(windows))
    assert counts[1] < counts[0] / 2


def test_gate_context_reserve_and_document():
    gate = SemanticGate("balanced")
    assert gate.decide(turn(0, "好"), turn(0, "以后别联系了", semantic_role="context_only")).bypass
    assert gate.decide(turn(1, "嗯"), turn(0, "你还喜欢我吗", semantic_role="context_only")).bypass
    assert gate.decide(turn(2, "我决定辞职")).bypass
    assert gate.decide(turn(3, "<voice>" + "这是长语音" * 60 + "</voice>")).bypass
    decisions = [gate.decide(turn(index)) for index in range(10, 500)]
    assert any(not decision.selected for decision in decisions)
    assert any(decision.reserve for decision in decisions)
    for month in ("2023-01", "2024-04", "2025-09", "2026-01"):
        item = turn(0).model_copy(update={"start_time": month + "-01T00:00:00"})
        assert SemanticGate("balanced").decide(item).reserve
    a, b = SemanticGate("balanced"), SemanticGate("balanced")
    assert [a.decide(turn(i)) for i in range(300)] == [b.decide(turn(i)) for i in range(300)]
    assert all(SemanticGate("full").decide(turn(i)).selected for i in range(100))
    assert gate.decide(turn(0).model_copy(update={"source_kind": "document"})).selected


@pytest.mark.anyio
async def test_queue_saturation_bounded_failure_and_cancellation():
    metrics = MaterialPipelineMetrics()
    done = []

    async def job(index):
        await asyncio.sleep(0.02)
        if index == 1:
            raise RuntimeError("one window failed")
        done.append(index)

    with pytest.raises(RuntimeError, match="one window failed"):
        async with ClassificationDispatch(4, metrics) as dispatch:
            for index in range(40):
                await dispatch.submit(lambda index=index: job(index))
    assert len(done) == 39 and metrics.peak_active_classification_workers == 4
    assert metrics.average_active_classification_workers > 3
    assert metrics.peak_window_queue_depth <= 8
    assert metrics.active_classification_workers == 0

    async def cancelled():
        async with ClassificationDispatch(2, MaterialPipelineMetrics()) as dispatch:
            await dispatch.submit(lambda: asyncio.sleep(10))
            await asyncio.sleep(10)

    task = asyncio.create_task(cancelled())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_persisted_upgrade_preserves_completed_and_raw_lanes(app):
    persona = app.personas.create(
        display_name="P02 fixture",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    source = app.personas.add_source_text(
        persona.id,
        title="chat",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="fixture",
        metadata={},
    )
    service = app.material_intelligence
    service.semantic_gate_mode = "balanced"
    service.batch_size = 7
    service.analysis_window_max_units = 10
    profile = ResolvedExecutionProfile(
        parallel_safe=True, prompt_transport=capability_for_mode("stdin")
    )
    units = []
    for i in range(200):
        target = i % 2 == 0
        units.append(
            EvidenceUnit(
                id=f"p02_{i:04d}",
                persona_id=persona.id,
                source_id=source.id,
                source_locator={"segment_index": i},
                text="嗯" if target else "今天怎么样",
                normalized_text="嗯" if target else "今天怎么样",
                source_kind="chat_import",
                speaker="target" if target else "exporter",
                speaker_role="target_persona" if target else "exporter",
                timestamp=(datetime(2023, 1, 1, tzinfo=UTC) + timedelta(minutes=i)).isoformat(),
                metadata={"semantic_status": "target_pending" if target else "context_only"},
            )
        )
    # 20% of target messages already durably reviewed under V4.
    cached = set()
    for unit in units[:40:2]:
        unit.metadata.update(
            semantic_status="reviewed_no_independent_evidence",
            classification_contract="conversation-evidence-v4",
        )
        cached.add(unit.id)
    service._persist_units(units)
    calls = []

    async def analyzer(phase, payload):
        calls.extend(payload["target_units"])
        await asyncio.sleep(0.001)
        return {"units": []}

    metrics = MaterialPipelineMetrics()
    await service._classify_persisted_with_agent(
        persona.id, analyzer, execution_profile=profile, metrics=metrics
    )
    assert cached.isdisjoint(calls)
    rows = [unit for batch in service._iter_units_batched(persona.id) for unit in batch]
    assert [(u.id, u.text, u.source_locator) for u in rows] == [
        (u.id, u.text, u.source_locator) for u in units
    ]
    skipped = [u for u in rows if u.metadata.get("semantic_status") == "semantic_gate_skipped"]
    assert skipped and metrics.semantic_skipped > 0
    assert all(service._classification_done(u, profile) for u in rows)
    from persona_continuum.application.chat_style_profiler import ChatStyleProfiler

    style = ChatStyleProfiler().profile(persona.id, rows)
    assert style is not None and style.corpus_size == 100
    episodes = service._build_episodes_persisted(persona.id)
    episode_ids = {eid for episode in episodes for eid in episode.evidence_unit_ids}
    assert {u.id for u in skipped} <= episode_ids
    assert len(episode_ids) == 200
    calls.clear()
    await service._classify_persisted_with_agent(
        persona.id, analyzer, execution_profile=profile, metrics=MaterialPipelineMetrics()
    )
    assert not calls
    stale = skipped[0].model_copy(deep=True)
    stale.metadata["semantic_gate_policy"] = "old"
    assert not service._classification_done(stale, profile)
    service.semantic_gate_mode = "full"
    assert not service._classification_done(skipped[0], profile)
    assert all(service._classification_done(u, profile) for u in rows if u.id in cached)
