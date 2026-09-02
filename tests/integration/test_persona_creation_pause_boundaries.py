"""Safe-pause boundary and race tests (P0-3).

Semantics under test: an atomic unit that already started (one agent turn,
one batch, one audit call) may finish and checkpoint, but once a pause is
requested NO new atomic unit may start -- no next agent turn, no next batch,
no queued dimension, no audit, no compile -- until Resume.

The pause request is control-plane state (``JobControlRegistry``): the tests
below inject it exactly where the user's click would land, including from
inside a running model call, and verify the worker never consults a stale
``job_config`` snapshot.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.models import AgentEvent, AgentEventType, ModelCapability
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.application.persona_creation_service import (
    REQUIRED_DIMENSIONS,
    PersonaCreationJob,
    _PauseRequested,
)
from persona_continuum.config import Config
from persona_continuum.domain.persona import PersonaType

PROBE_DIM = REQUIRED_DIMENSIONS[0]


@pytest.fixture()
def env(tmp_path):
    config = Config(data_dir=tmp_path / "pc-data")
    continuum = PersonaContinuum(config, include_fake_agent=True)
    continuum.init()
    yield continuum
    continuum.close()


def _add_sources(continuum: PersonaContinuum, persona_id: str, n: int, size: int = 64) -> list[str]:
    ids = []
    for i in range(n):
        row = continuum.personas.add_source_text(
            persona_id,
            title=f"PS{i}",
            source_type="web",
            canonical_url=f"https://pause.test/{i}",
            publisher="P",
            author="A",
            published_at=None,
            accessed_at=None,
            content=f"pause boundary evidence body {i} " + "x" * size,
            hash="",
            metadata={},
        )
        ids.append(row.id)
    return ids


def _make_job(
    continuum: PersonaContinuum,
    agent_id: str = "fake_agent",
    model_id: str = "fake-gpt-5",
) -> PersonaCreationJob:
    persona = continuum.personas.create(
        display_name="Pause Subject",
        aliases=[],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode="counterfactual_continuation",
    )
    task = continuum.compilation.create_task(persona.id)
    job = PersonaCreationJob(
        id="pcjob_pause_test",
        display_name="Pause Subject",
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        creation_mode="fictional",
        runtime_source="test",
        agent_id=agent_id,
        model_id=model_id,
        persona_id=persona.id,
        compilation_task_id=task.id,
    )
    continuum.persona_creation._save(job)
    return continuum.persona_creation.get_job(job.id)


def _prepare_run(continuum: PersonaContinuum, job: PersonaCreationJob, n_sources: int = 1) -> None:
    """Put the job where the lifecycle test puts it: ready for extraction."""
    job.creation_mode = "fictional"
    job.source_ids = _add_sources(continuum, job.persona_id or "", n_sources)
    job.source_count = len(job.source_ids)
    job.status = "extracting"
    # Skip the material-analysis window so the pause lands in extraction.
    job.job_config["material_analysis"] = {"id": "pmjob", "status": "completed"}
    continuum.persona_creation._save(job)


def _artifact_payload(dimension: str, source_id: str, serial: int) -> dict[str, Any]:
    return {
        "artifact_id": f"a_{dimension}_{serial}",
        "schema_version": "1.1",
        "dimension": dimension,
        "source_ids": [source_id],
        "claims": [
            {
                "content": f"c {dimension}",
                "source_id": source_id,
                "claim_type": "historical_inference",
                "confidence": 0.6,
            }
        ],
        "memories": [],
        "extracted_components": {},
        "conflicts": [],
        "uncertainty": {"level": 0.3, "notes": []},
        "created_by": "rec",
        "artifact_hash": f"h_{dimension}_{serial}",
    }


class PauseInjectingRuntime(FakeAgentAdapter):
    """Fake runtime that requests a pause from inside a running model call.

    This simulates the real user behaviour: the HTTP pause request arrives
    while the worker is in the middle of an agent turn.
    """

    def __init__(
        self,
        request_pause: Callable[[str], Any],
        *,
        adapter_id: str = "pause_fake",
        pause_on: str = "dimension",
        context_window: int | None = None,
        models: list[ModelCapability] | None = None,
    ) -> None:
        super().__init__(
            adapter_id=adapter_id, name="Pause", context_window=context_window, models=models
        )
        self._request_pause = request_pause
        self.pause_on = pause_on
        self.dimension_prompts: list[str] = []
        self.audit_prompts: list[str] = []
        self.other_prompts: list[str] = []
        self.pause_injected = False

    def _inject_pause(self, session: Any) -> None:
        if self.pause_injected:
            return
        self.pause_injected = True
        job_id = str(session.config.room_id).split(":", 1)[1]
        self._request_pause(job_id)

    async def send(self, session, turn):  # type: ignore[no-untyped-def]
        prompt = turn.user_message
        if "严格最终质量门禁" in (turn.system_prompt or ""):
            if self.pause_on == "audit":
                self._inject_pause(session)
            self.audit_prompts.append(prompt)
            payload = {
                "status": "pass",
                "dimensions": {d: "pass" for d in REQUIRED_DIMENSIONS},
                "issues": [],
            }
        elif "提取维度" in prompt:
            if self.pause_on == "dimension":
                self._inject_pause(session)
            self.dimension_prompts.append(prompt)
            dimension = next(
                (d for d in REQUIRED_DIMENSIONS if f"提取维度 {d}" in prompt),
                REQUIRED_DIMENSIONS[0],
            )
            src = (session.config.extra.get("source_ids") or ["s0"])[0]
            payload = _artifact_payload(dimension, src, len(self.dimension_prompts))
        else:
            if self.pause_on == "other":
                self._inject_pause(session)
            self.other_prompts.append(prompt)
            payload = {"question": "q", "dimension": REQUIRED_DIMENSIONS[0]}
        yield AgentEvent(type=AgentEventType.DONE, content=json.dumps(payload, ensure_ascii=False))


async def _wait_for_status(
    continuum: PersonaContinuum, job_id: str, statuses: set[str], timeout: float = 90.0
) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = continuum.persona_creation.get_job(job_id).status
    while loop.time() < deadline:
        status = continuum.persona_creation.get_job(job_id).status
        if status in statuses:
            return status
        await asyncio.sleep(0.05)
    return status


# ---------------------------------------------------------------------------
# Test B: a stale worker save must not clobber a pause request
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stale_worker_save_cannot_clobber_pause_request(env) -> None:
    job = _make_job(env)
    # The worker holds this object for minutes.
    stale_worker_view = env.persona_creation.get_job(job.id)

    # The API writes the pause request to the control plane.
    env.persona_creation.job_control.request_pause(job.id)

    # The worker saves its own progress from its stale object, which does not
    # know about the pause and even carries a pre-pause job_config.
    stale_worker_view.source_count = 99
    stale_worker_view.job_config.pop("pause_requested", None)
    env.persona_creation._save(stale_worker_view)

    fresh = env.persona_creation.get_job(job.id)
    # Data-plane progress landed...
    assert fresh.source_count == 99
    # ...but the control-plane request survived the stale save.
    assert fresh.job_config.get("pause_requested") is True
    assert env.persona_creation.job_control.pause_requested(job.id) is True
    assert env.persona_creation.job_control.get(job.id).pause_requested_at is not None


@pytest.mark.anyio
async def test_pause_only_cleared_after_worker_reached_paused(env) -> None:
    job = _make_job(env)
    env.persona_creation.job_control.request_pause(job.id)
    assert env.persona_creation.job_control.pause_requested(job.id) is True
    # A plain progress save must not silently consume the request.
    env.persona_creation._save(env.persona_creation.get_job(job.id))
    assert env.persona_creation.job_control.pause_requested(job.id) is True


# ---------------------------------------------------------------------------
# Test A: pause during an agent turn -- the turn finishes, the next never starts
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pause_during_agent_turn_completes_unit_then_pauses(env) -> None:
    runtime = PauseInjectingRuntime(env.persona_creation.job_control.request_pause)
    env.agent_registry.register_adapter(runtime)
    await env.agent_discovery.scan(force_refresh=True)
    job = _make_job(env, agent_id=runtime.adapter_id)
    _prepare_run(env, job)
    await env.persona_creation.resume_job(job.id)

    status = await _wait_for_status(
        env, job.id, {"paused", "completed", "completed_with_gaps", "failed", "failed_quality_gate"}
    )
    assert status == "paused"
    # The pause landed inside the first dimension turn; only the in-flight
    # turns (bounded by dimension concurrency = 3) may have completed.
    assert 1 <= len(runtime.dimension_prompts) <= 3
    # No new atomic work after the pause request: no audit call ever started.
    assert runtime.audit_prompts == []
    fresh = env.persona_creation.get_job(job.id)
    assert fresh.job_config.get("pause_requested") is None or fresh.status == "paused"


# ---------------------------------------------------------------------------
# Test C: pause during a batch -- batch 1 checkpoints, batch 2 never starts
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pause_during_batch_stops_next_batch(env) -> None:
    runtime = PauseInjectingRuntime(env.persona_creation.job_control.request_pause)
    env.agent_registry.register_adapter(runtime)
    await env.agent_discovery.scan(force_refresh=True)
    job = _make_job(env, agent_id=runtime.adapter_id)
    _prepare_run(env, job, n_sources=6)
    source_by_id = {s.id: s for s in env.personas.get_sources(job.persona_id or "")}
    # Large items force the budget manager to plan several batches.
    items = [
        {
            "evidence_id": source.id,
            "source_ids": [source.id],
            "evidence_type": "source",
            "verbatim_samples": [],
            "intelligence": {},
            "content": source.content + "y" * 9000,
        }
        for source in source_by_id.values()
    ]
    with pytest.raises(_PauseRequested):
        await env.persona_creation._run_dimension_extraction(
            job,
            dimension=PROBE_DIM,
            evidence_items=items,
            participant_id=PROBE_DIM,
            mode="full",
            source_by_id=source_by_id,
            context_manager=AgentContextBudgetManager(),
            phase="dimension_extraction",
        )
    # Batch 1 ran (its turn is the one that injected the pause); the manager
    # never started batch 2.
    assert len(runtime.dimension_prompts) == 1


# ---------------------------------------------------------------------------
# Test D: queued dimension (semaphore waiting) must not start after a pause
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_queued_dimension_never_starts_after_pause(env) -> None:
    env.config.persona_dimension_concurrency = 1
    runtime = PauseInjectingRuntime(
        env.persona_creation.job_control.request_pause, adapter_id="pause_fake_serial"
    )
    env.agent_registry.register_adapter(runtime)
    await env.agent_discovery.scan(force_refresh=True)
    job = _make_job(env, agent_id=runtime.adapter_id)
    _prepare_run(env, job)
    with pytest.raises(_PauseRequested):
        await env.persona_creation._extract_dimensions(job)
    # Dimension 1 was running and completed its atomic turn; dimension 2 was
    # queued on the semaphore and must never have started a model call.
    assert len(runtime.dimension_prompts) == 1
    assert f"提取维度 {REQUIRED_DIMENSIONS[1]}" not in runtime.dimension_prompts[0]


# ---------------------------------------------------------------------------
# Tests E/F/G: stage, audit and compile boundaries
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stage_transition_is_a_pause_boundary(env) -> None:
    runtime = PauseInjectingRuntime(
        env.persona_creation.job_control.request_pause, adapter_id="pause_fake_stage"
    )
    env.agent_registry.register_adapter(runtime)
    await env.agent_discovery.scan(force_refresh=True)
    job = _make_job(env)
    _prepare_run(env, job)
    env.persona_creation.job_control.request_pause(job.id)
    with pytest.raises(_PauseRequested):
        # Entering "compiling" means new work is about to start.
        await env.persona_creation._set_stage(job, "compiling", "compiling")
    assert runtime.audit_prompts == []


@pytest.mark.anyio
async def test_global_audit_start_is_a_pause_boundary(env) -> None:
    runtime = PauseInjectingRuntime(
        env.persona_creation.job_control.request_pause, adapter_id="pause_fake_audit"
    )
    env.agent_registry.register_adapter(runtime)
    await env.agent_discovery.scan(force_refresh=True)
    job = _make_job(env)
    _prepare_run(env, job)
    env.persona_creation.job_control.request_pause(job.id)
    with pytest.raises(_PauseRequested):
        await env.persona_creation._run_global_audits(job)
    assert runtime.audit_prompts == []


@pytest.mark.anyio
async def test_audit_repair_start_is_a_pause_boundary(env) -> None:
    runtime = PauseInjectingRuntime(
        env.persona_creation.job_control.request_pause, adapter_id="pause_fake_repair"
    )
    env.agent_registry.register_adapter(runtime)
    await env.agent_discovery.scan(force_refresh=True)
    job = _make_job(env, agent_id=runtime.adapter_id)
    _prepare_run(env, job)
    env.persona_creation.job_control.request_pause(job.id)
    with pytest.raises(_PauseRequested):
        await env.persona_creation._repair_audit_dimensions(job, {PROBE_DIM}, attempt=1)
    assert runtime.dimension_prompts == []


# ---------------------------------------------------------------------------
# Test H: a persisted pause request must survive a restart
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_persisted_pause_request_survives_restart(env, tmp_path) -> None:
    job = _make_job(env)
    _prepare_run(env, job)
    env.persona_creation.job_control.request_pause(job.id)
    env.persona_creation.shutdown()

    second = PersonaContinuum(Config(data_dir=tmp_path / "pc-data"), include_fake_agent=True)
    second.init()
    try:
        resumed = await second.persona_creation.resume_pending_jobs()
        assert job.id not in resumed
        fresh = second.persona_creation.get_job(job.id)
        assert fresh.status == "paused"
        assert second.persona_creation.job_control.pause_requested(job.id) is True
    finally:
        second.close()


# ---------------------------------------------------------------------------
# §47/§50: resume honours control state; a model switch re-resolves context
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resume_after_pause_switches_context_capability(env) -> None:
    big = PauseInjectingRuntime(
        lambda job_id: None,
        adapter_id="big_ctx_fake",
        pause_on="never",
        models=[
            ModelCapability(
                id="fake-gpt-5-big",
                display_name="Big Fake",
                provider="test",
                context_window=262_144,
                source="test_adapter",
            )
        ],
    )
    env.agent_registry.register_adapter(big)
    await env.agent_discovery.scan(force_refresh=True)

    runtime = PauseInjectingRuntime(env.persona_creation.job_control.request_pause)
    env.agent_registry.register_adapter(runtime)
    await env.agent_discovery.scan(force_refresh=True)
    job = _make_job(env, agent_id=runtime.adapter_id)
    _prepare_run(env, job)
    await env.persona_creation.resume_job(job.id)
    status = await _wait_for_status(
        env, job.id, {"paused", "completed", "completed_with_gaps", "failed", "failed_quality_gate"}
    )
    assert status == "paused"
    dimensions_before = dict(env.persona_creation.get_job(job.id).dimension_progress)

    # Resume with a different execution target: only future work changes;
    # durable evidence/dimension checkpoints stay valid and the context
    # capability is re-resolved for the new chain.
    await env.persona_creation.resume_job(
        job.id, runtime={"agent_id": big.adapter_id, "model_id": "fake-gpt-5-big"}
    )
    status = await _wait_for_status(
        env,
        job.id,
        {"completed", "completed_with_gaps", "failed", "failed_quality_gate", "paused"},
    )
    assert status in {"completed", "completed_with_gaps"}
    fresh = env.persona_creation.get_job(job.id)
    assert fresh.job_config.get("model_switched") is True
    capabilities = env.persona_creation._effective_model_capabilities(fresh)
    assert capabilities.effective_context_window == 262_144
    assert capabilities.context_capability_source != "unknown"
    # Checkpoints survived the pause + switch: no dimension lost progress.
    dimensions_after = fresh.dimension_progress
    for dimension, before in dimensions_before.items():
        assert dimensions_after.get(dimension, 0) >= before
