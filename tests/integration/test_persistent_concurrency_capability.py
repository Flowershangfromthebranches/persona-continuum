"""Persistent concurrency capability reaches the app + adaptive material retry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from persona_continuum.agent.prompt_transport import capability_for_mode
from persona_continuum.application.material_intelligence import EvidenceUnit
from persona_continuum.application.material_pipeline import (
    MaterialPipelineMetrics,
    ResolvedExecutionProfile,
)
from persona_continuum.domain.persona import PersonaType
from persona_continuum.performance.concurrency_cache import (
    resolve_independent_session_capability,
)
from persona_continuum.performance.runtime_capability_store import (
    CapabilityState,
    ProbeStatus,
    RuntimeCapabilityStore,
    RuntimeIdentity,
)

CHAT = "chat_import"
BASE = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def _identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        adapter_id="grok",
        binary_identity="bin-1",
        binary_version="1.0.25",
        model_id="grok-4.6",
        credential_identity_hash="cred-1",
        runtime_origin="xai",
    )


def test_probe_written_by_one_process_is_read_by_a_new_app_process(app) -> None:
    path = app.config.database_path
    # "Probe process": a standalone store writes the verified capability.
    writer = RuntimeCapabilityStore(path)
    writer.record_probe(
        _identity(),
        probe_status=ProbeStatus.VERIFIED,
        max_verified=4,
        parallel_independent_sessions_verified=True,
        probe_sample_count=7,
    )
    writer.close()

    # "App process": a brand-new store instance (what a restart builds).
    reader = RuntimeCapabilityStore(path)
    assert reader.verified_limit(_identity()) == 4
    reader.close()

    # The app's configured store resolves the same capability.
    capability = resolve_independent_session_capability(
        "grok",
        binary_identity="bin-1",
        binary_version="1.0.25",
        model_id="grok-4.6",
        credential_identity_hash="cred-1",
        runtime_origin="xai",
    )
    assert capability.state == CapabilityState.VERIFIED
    assert capability.effective_limit == 4


def _profile(window: int) -> ResolvedExecutionProfile:
    return ResolvedExecutionProfile(
        adapter_id="fake",
        model_id="synthetic",
        context_window=window,
        native_context_window=window,
        runtime_effective_context=window,
        remaining_context_tokens=window,
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
        workload_context_scope="per_window",
        parallel_independent_sessions=True,
        max_parallel_independent_sessions=4,
        effective_independent_session_concurrency=4,
    )


def _units(persona_id: str, source_id: str, count: int, *, text: str) -> list[EvidenceUnit]:
    return [
        EvidenceUnit(
            id=f"cap_{index:05d}",
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


@pytest.mark.anyio
async def test_material_adaptive_retry_only_requeues_failed_window(app) -> None:
    persona = app.personas.create(
        display_name="Adaptive retry",
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
    calls: list[int] = []
    state = {"first": True}

    async def analyzer(phase, payload):
        calls.append(len(payload.get("target_units") or []))
        if state["first"]:
            state["first"] = False
            raise RuntimeError("HTTP 429 too many concurrent sessions")
        return {"units": []}

    metrics = MaterialPipelineMetrics()
    # Completing without raising proves the failed window was requeued rather
    # than failing the whole task.
    await service._classify_persisted_with_agent(
        persona.id,
        analyzer,
        execution_profile=_profile(128_000),
        metrics=metrics,
    )
    assert metrics.window_retries >= 1
    assert metrics.concurrency_downgrades >= 1
    assert metrics.analysis_windows >= 1
