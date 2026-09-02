"""40% Material Classification stall regression tests.

Root cause regression coverage: a 1M-context model must not produce near-1M
single Analysis Windows, the evidence body must appear exactly once per
prompt, and the transport budget must bound the batch independently of the
model context.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.errors import PromptTransportLimitExceededError
from persona_continuum.agent.models import AgentSessionConfig
from persona_continuum.agent.prompt_transport import capability_for_mode
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
from persona_continuum.application.job_progress import WorkerState
from persona_continuum.application.material_intelligence import (
    MATERIAL_AGENT_SYSTEM_PROMPTS,
    EvidenceUnit,
    MaterialJobStatus,
    MaterialPipelineMetrics,
)
from persona_continuum.application.material_pipeline import (
    AnalysisWindow,
    CandidateGroup,
    ResolvedExecutionProfile,
    material_batch_target_tokens,
)
from persona_continuum.application.persona_creation_service import PersonaCreationJob
from persona_continuum.domain.persona import PersonaType

ONE_MILLION = 1_048_576


def _million_profile(transport: dict[str, object] | None = None) -> ResolvedExecutionProfile:
    snapshot: dict[str, object] = {
        "effective_model": "fake-gpt-5",
        "context_window": ONE_MILLION,
    }
    if transport is not None:
        snapshot["prompt_transport"] = transport
    return ResolvedExecutionProfile.resolve(snapshot)


def _sources_for(app, persona, count: int) -> list[str]:
    return [
        app.personas.add_source_text(
            persona.id,
            title=f"stall-fixture-{index}",
            source_type="txt",
            canonical_url=None,
            publisher="user",
            author="user",
            published_at=None,
            accessed_at="2026-08-29T00:00:00+00:00",
            content=f"占位材料 {index}",
            metadata={"provenance": "user_provided", "source_kind": "user_provided"},
        ).id
        for index in range(count)
    ]


def _units(
    count: int,
    *,
    persona_id: str,
    source_ids: list[str],
    chars_per_unit: int = 8_000,
) -> list[EvidenceUnit]:
    return [
        EvidenceUnit(
            id=f"evidence-{index}",
            persona_id=persona_id,
            source_id=source_ids[index % len(source_ids)],
            text=f"记录{index}：" + "字" * chars_per_unit,
            normalized_text=f"记录{index}：" + "字" * chars_per_unit,
        )
        for index in range(count)
    ]


def _subject_persona(app):
    return app.personas.create(
        display_name="Stall Fix Subject",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )


def _classify_analyzer(requests: list[dict]):
    async def analyzer(phase: str, payload: dict):
        assert phase == "classify"
        requests.append(payload)
        return {
            "units": [
                {
                    "id": unit["id"],
                    "dimension_scores": {"decisions_and_behavior": 0.9},
                    "evidence_type": "decision_example",
                    "confidence": 0.8,
                    "claims": [{"content": "会先列出事实", "confidence": 0.8}],
                }
                for unit in payload["units"]
            ]
        }

    return analyzer


@pytest.mark.anyio
async def test_1m_model_never_packs_near_full_window(app) -> None:
    """A 1M context window alone must not create 800K+ single windows."""

    service = app.material_intelligence
    persona = _subject_persona(app)
    source_ids = _sources_for(app, persona, 4)
    requests: list[dict] = []
    metrics = MaterialPipelineMetrics()
    profile = _million_profile()
    await service._classify_with_agent(
        _units(60, persona_id=persona.id, source_ids=source_ids),
        _classify_analyzer(requests),
        execution_profile=profile,
        metrics=metrics,
    )
    target = metrics.max_batch_target_tokens
    assert target is not None and 0 < target <= 500_000
    assert metrics.max_serialized_prompt_tokens is None or (
        metrics.max_serialized_prompt_tokens <= target
    )
    assert len(requests) > 1


@pytest.mark.anyio
async def test_evidence_body_appears_exactly_once(app) -> None:
    """analysis_window carries metadata only; body text lives in units[].text."""

    service = app.material_intelligence
    persona = _subject_persona(app)
    source_ids = _sources_for(app, persona, 4)
    requests: list[dict] = []
    units = _units(6, persona_id=persona.id, source_ids=source_ids, chars_per_unit=10_000)
    await service._classify_with_agent(
        units,
        _classify_analyzer(requests),
        execution_profile=_million_profile({"transport_mode": "rpc"}),
    )
    assert requests
    for request in requests:
        assert "text" not in request["analysis_window"]
        serialized = json.dumps(request, ensure_ascii=False)
        for unit in request["units"]:
            assert serialized.count(unit["text"]) == 1


@pytest.mark.anyio
async def test_argv_transport_bounds_batch_regardless_of_model_context(app) -> None:
    """A 1M model behind a 64KB ARGV transport must batch at the transport limit."""

    service = app.material_intelligence
    persona = _subject_persona(app)
    source_ids = _sources_for(app, persona, 4)
    requests: list[dict] = []
    metrics = MaterialPipelineMetrics()
    argv = capability_for_mode("argv")
    profile = _million_profile(argv.as_dict())
    await service._classify_with_agent(
        _units(40, persona_id=persona.id, source_ids=source_ids),
        _classify_analyzer(requests),
        execution_profile=profile,
        metrics=metrics,
    )
    assert metrics.max_batch_target_tokens <= argv.prompt_token_budget()
    assert metrics.max_serialized_prompt_bytes is not None
    assert metrics.max_serialized_prompt_bytes <= argv.safe_prompt_bytes
    assert metrics.transport_mode == "argv"


@pytest.mark.anyio
async def test_semantic_relation_rebatches_for_argv_transport(app) -> None:
    """Relation requests obey transport bytes even when model context is 1M."""

    service = app.material_intelligence
    persona = _subject_persona(app)
    source_ids = _sources_for(app, persona, 4)
    units = _units(24, persona_id=persona.id, source_ids=source_ids)
    units = [
        item.model_copy(
            update={
                "metadata": {
                    "evidence_intelligence": {
                        "claims": [
                            {"content": f"主张{item.id}-" + "字" * 1_000}
                            for _ in range(4)
                        ]
                    }
                }
            }
        )
        for item in units
    ]
    groups = [
        CandidateGroup(
            id=f"group-{index}",
            evidence_ids=[f"evidence-{index * 2}", f"evidence-{index * 2 + 1}"],
            signals=["lexical"],
        )
        for index in range(12)
    ]
    requests: list[dict] = []

    async def analyzer(phase: str, payload: dict) -> dict:
        assert phase == "relate"
        requests.append(payload)
        return {"clusters": [], "contradictions": []}

    argv = capability_for_mode("argv")
    metrics = MaterialPipelineMetrics()
    await service._relate_with_agent(
        units,
        [],
        analyzer,
        candidate_groups=groups,
        execution_profile=_million_profile(argv.as_dict()),
        metrics=metrics,
    )

    assert len(requests) > 1
    system_bytes = len(MATERIAL_AGENT_SYSTEM_PROMPTS["relate"].encode("utf-8"))
    assert all(
        len(json.dumps(request, ensure_ascii=False).encode("utf-8")) + system_bytes
        <= argv.safe_prompt_bytes
        for request in requests
    )
    assert metrics.agent_turns["relation"] == len(requests)


@pytest.mark.anyio
async def test_stdin_and_rpc_transports_allow_larger_batches_than_argv(app) -> None:
    """Large-prompt transports must not be clamped to the tiny ARGV ceiling."""

    service = app.material_intelligence
    persona = _subject_persona(app)
    source_ids = _sources_for(app, persona, 4)
    argv_metrics = MaterialPipelineMetrics()
    rpc_metrics = MaterialPipelineMetrics()
    await service._classify_with_agent(
        _units(40, persona_id=persona.id, source_ids=source_ids),
        _classify_analyzer([]),
        execution_profile=_million_profile(capability_for_mode("argv").as_dict()),
        metrics=argv_metrics,
    )
    await service._classify_with_agent(
        _units(40, persona_id=persona.id, source_ids=source_ids),
        _classify_analyzer([]),
        execution_profile=_million_profile(capability_for_mode("rpc").as_dict()),
        metrics=rpc_metrics,
    )
    assert rpc_metrics.max_batch_target_tokens > argv_metrics.max_batch_target_tokens
    assert rpc_metrics.max_batch_target_tokens > 400_000


@pytest.mark.anyio
async def test_oversized_single_unit_is_semantically_chunked(app) -> None:
    """A single evidence unit above the transport budget is chunked, not dropped."""

    service = app.material_intelligence
    persona = _subject_persona(app)
    source_ids = _sources_for(app, persona, 1)
    requests: list[dict] = []
    metrics = MaterialPipelineMetrics()
    argv = capability_for_mode("argv")
    profile = _million_profile(argv.as_dict())
    big_unit = _units(1, persona_id=persona.id, source_ids=source_ids, chars_per_unit=40_000)[0]
    result = await service._classify_with_agent(
        [big_unit],
        _classify_analyzer(requests),
        execution_profile=profile,
        metrics=metrics,
    )
    assert metrics.chunked_oversized_units == 1
    assert requests
    chunk_ids = [unit["id"] for request in requests for unit in request["units"]]
    assert chunk_ids and all(chunk_id.startswith("evidence-0#c") for chunk_id in chunk_ids)
    # Provenance survives chunking, and the chunk result merged back into the
    # original evidence row.
    for request in requests:
        for unit in request["units"]:
            assert unit["text"]
    merged = next(item for item in result if item.id == "evidence-0")
    assert merged.text == big_unit.text
    assert merged.metadata.get("evidence_intelligence")


@pytest.mark.anyio
async def test_window_progress_reports_dynamic_state(app) -> None:
    """The classification phase reports window-level progress instead of one static number."""

    service = app.material_intelligence
    persona = _subject_persona(app)
    source_ids = _sources_for(app, persona, 4)
    requests: list[dict] = []
    events: list[dict] = []

    async def window_progress(info: dict) -> None:
        events.append(dict(info))

    await service._classify_with_agent(
        _units(6, persona_id=persona.id, source_ids=source_ids, chars_per_unit=5_000),
        _classify_analyzer(requests),
        execution_profile=_million_profile(capability_for_mode("argv").as_dict()),
        window_progress=window_progress,
    )
    states = [event["prompt_state"] for event in events]
    assert states[0] == "PREPARING_INPUT"
    assert "PROMPT_BUILDING" in states
    assert "CHECKPOINTING" in states
    totals = {event["windows_total"] for event in events}
    assert totals == {len(requests)}
    completed = [event.get("windows_completed") for event in events if "windows_completed" in event]
    assert completed[-1] == len(requests)


@pytest.mark.anyio
async def test_batch_target_helper_respects_all_three_budgets() -> None:
    profile = _million_profile()
    no_transport = material_batch_target_tokens(profile, phase_usable_budget=1_000_000)
    assert no_transport == int(profile.preferred_working_context * 0.92)
    argv_capped = material_batch_target_tokens(
        profile.model_copy(update={"prompt_transport": capability_for_mode("argv")}),
        phase_usable_budget=1_000_000,
    )
    assert argv_capped <= capability_for_mode("argv").prompt_token_budget()
    assert material_batch_target_tokens(profile, phase_usable_budget=5_000) == int(5_000 * 0.92)


@pytest.mark.anyio
async def test_retry_reuses_completed_windows_by_evidence_coverage(app, monkeypatch) -> None:
    """A retry must not re-run windows that already persisted their evidence."""

    persona = app.personas.create(
        display_name="Stall Retry Subject",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    first = app.personas.add_source_text(
        persona.id,
        title="retry-a",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at="2026-08-29T00:00:00+00:00",
        content="我会先把事实列出来，再决定下一步。",
        metadata={"provenance": "user_provided", "source_kind": "user_provided"},
    )
    second_text = "风险出现时，我会记录决策原因。"
    second = app.personas.add_source_text(
        persona.id,
        title="retry-b",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at="2026-08-29T00:00:00+00:00",
        content=second_text,
        metadata={"provenance": "user_provided", "source_kind": "user_provided"},
    )

    def one_window_per_unit(units, **kwargs):
        return [
            AnalysisWindow(
                id=f"aw-retry-{index}",
                evidence_unit_ids=[unit.id],
                source_ids=[unit.source_id],
                text=unit.text,
                token_estimate=16,
            )
            for index, unit in enumerate(units)
        ]

    monkeypatch.setattr(
        "persona_continuum.application.material_intelligence.build_analysis_windows",
        one_window_per_unit,
    )
    runtime_snapshot = {
        "effective_model": "fake-gpt-5",
        "capabilities": {"persistent_session": True},
    }

    def analyzer_for(failing_text: str | None):
        async def analyzer(phase: str, payload: dict):
            if phase != "classify":
                return {"claims": [], "clusters": [], "contradictions": []}
            texts = [unit["text"] for unit in payload["units"]]
            if failing_text is not None and failing_text in texts:
                raise RuntimeError("classification_window_failure")
            return {
                "units": [
                    {
                        "id": unit["id"],
                        "dimension_scores": {"decisions_and_behavior": 0.9},
                        "evidence_type": "decision_example",
                        "confidence": 0.8,
                        "claims": [{"content": "会先列出事实", "confidence": 0.8}],
                    }
                    for unit in payload["units"]
                ]
            }

        return analyzer

    with pytest.raises(RuntimeError, match="classification_window_failure"):
        await app.material_intelligence.analyze_sources_async(
            persona.id,
            [first.id, second.id],
            runtime_snapshot=runtime_snapshot,
            agent_analyzer=analyzer_for(second_text),
            agent_phases=("classify",),
        )

    seen_texts: list[list[str]] = []

    async def recording_analyzer(phase: str, payload: dict):
        if phase == "classify":
            seen_texts.append([unit["text"] for unit in payload["units"]])
        return {
            "units": [
                {
                    "id": unit["id"],
                    "dimension_scores": {"decisions_and_behavior": 0.9},
                    "evidence_type": "decision_example",
                    "confidence": 0.8,
                }
                for unit in payload.get("units", [])
            ]
        }

    job = await app.material_intelligence.analyze_sources_async(
        persona.id,
        [first.id, second.id],
        runtime_snapshot=runtime_snapshot,
        agent_analyzer=recording_analyzer,
        agent_phases=("classify",),
    )
    assert job.status == MaterialJobStatus.READY_FOR_COMPILATION
    # Only the evidence whose window failed is re-analyzed; the persisted
    # window is reused through its evidence fingerprint cache key.
    assert seen_texts == [[second_text]]


@pytest.mark.anyio
async def test_reap_stalled_jobs_fails_dead_worker_rows(app) -> None:
    """A running row without a live worker becomes a typed retryable failure."""

    service = app.persona_creation
    job = PersonaCreationJob(
        id="pcjob_stalled_reap",
        display_name="Stall Reap Subject",
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="private_materials",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
        status="extracting",
    )
    service._save(job)
    stale_at = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    job.progress.touch_worker(WorkerState.RUNNING, heartbeat_at=stale_at)
    service._save(job)

    reaped = service.reap_stalled_jobs()
    assert "pcjob_stalled_reap" in reaped
    failed = service.get_job("pcjob_stalled_reap")
    assert failed.status == "failed"
    assert failed.failure_json["code"] == "JOB_STALLED"
    assert failed.failure_json["retriable"] is True
    assert failed.progress.worker_state == WorkerState.LOST

    # A fresh heartbeat must never be reaped.
    healthy = PersonaCreationJob(
        id="pcjob_stalled_healthy",
        display_name="Stall Healthy Subject",
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="private_materials",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
        status="extracting",
    )
    service._save(healthy)
    healthy.progress.touch_worker(WorkerState.RUNNING)
    service._save(healthy)
    assert "pcjob_stalled_healthy" not in service.reap_stalled_jobs()
    assert service.get_job("pcjob_stalled_healthy").status == "extracting"


@pytest.mark.anyio
async def test_executor_reports_dispatch_states_and_model_running(app) -> None:
    """MODEL_RUNNING is only reported after the Adapter dispatch completed."""

    executor = AgentRuntimeExecutor()
    adapter = FakeAgentAdapter(chunk_delay_sec=0.0)
    binding = await executor.open_session(
        adapter,
        AgentSessionConfig(
            room_id="room",
            participant_id="participant",
            persona_id="persona",
            model_id="fake-gpt-5",
        ),
    )
    states: list[str] = []
    await executor.execute_text(
        binding,
        system_prompt="system",
        user_message="hello",
        phase="material_classification",
        stream=False,
        state_callback=states.append,
    )
    assert "WAITING_SCHEDULER" in states
    assert "SENDING_PROMPT" in states
    assert "MODEL_RUNNING" in states
    assert states.index("WAITING_SCHEDULER") < states.index("MODEL_RUNNING")


@pytest.mark.anyio
async def test_executor_rejects_prompt_over_transport_limit(app) -> None:
    """An ARGV transport refuses a prompt it cannot carry — loudly, not by hanging."""

    executor = AgentRuntimeExecutor()
    adapter = FakeAgentAdapter(chunk_delay_sec=0.0, context_window=ONE_MILLION)
    adapter.prompt_transport_mode = "argv"
    binding = await executor.open_session(
        adapter,
        AgentSessionConfig(
            room_id="room",
            participant_id="participant",
            persona_id="persona",
            model_id="fake-gpt-5",
        ),
    )
    with pytest.raises(PromptTransportLimitExceededError):
        await executor.execute_text(
            binding,
            system_prompt="system",
            # 80KB of ASCII stays inside the model's planning context budget
            # but exceeds the 64KB ARGV transport ceiling.
            user_message="a" * 80_000,
            phase="material_classification",
            stream=False,
        )
