"""Regression tests for the resumable Persona Creation job architecture.

Covers the acceptance list of the performance-hot-path / resumable-job
upgrade: effective context-window resolution, batch + dimension checkpoints,
partial resume, safe pause, retry with a model switch, deterministic local
structured-output repair, least-loaded runtime placement, and Persona rename.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.models import (
    AgentEvent,
    AgentEventType,
    EffectiveModelCapabilities,
    ModelCapability,
    ReasoningCapability,
    ReasoningCapabilityMode,
)
from persona_continuum.agent.structured_output import StructuredOutputEngine
from persona_continuum.application.persona_creation_service import (
    REQUIRED_DIMENSIONS,
    PersonaCreationJob,
    _PauseRequested,
)
from persona_continuum.domain.persona import PersonaType
from persona_continuum.performance.runtime_pool import AgentRuntimePool
from persona_continuum.performance.scheduler import ExecutionClass, PrioritySemaphore
from persona_continuum.room.models import RoomTranscriptRecord
from persona_continuum.security.validation import ConflictError
from persona_continuum.storage.database import Database


class ResumableRuntime(FakeAgentAdapter):
    """Fake runtime that records prompts per participant and can fail dims."""

    def __init__(self, adapter_id: str = "res_fake", context_window: int | None = None) -> None:
        super().__init__(adapter_id=adapter_id, name="Res")
        self.participant_prompts: list[tuple[str, str]] = []
        self.fail_dimensions: set[str] = set()

    async def send(self, session, turn):  # type: ignore[no-untyped-def]
        prompt = turn.user_message
        self.participant_prompts.append((session.config.participant_id, prompt))
        if "严格最终质量门禁" in (turn.system_prompt or ""):
            dimensions = {dimension: "pass" for dimension in REQUIRED_DIMENSIONS}
            payload = {"status": "pass", "dimensions": dimensions, "issues": []}
        elif "提取维度" in prompt:
            dimension = next(
                (d for d in REQUIRED_DIMENSIONS if f"提取维度 {d}" in prompt),
                REQUIRED_DIMENSIONS[0],
            )
            if dimension in self.fail_dimensions:
                raise RuntimeError(f"SIMULATED_DIMENSION_FAILURE:{dimension}")
            src = (session.config.extra.get("source_ids") or ["s0"])[0]
            payload = {
                "artifact_id": f"a_{dimension}_{len(self.participant_prompts)}",
                "schema_version": "1.1",
                "dimension": dimension,
                "source_ids": [src],
                "claims": [
                    {
                        "content": f"c {dimension}",
                        "source_id": src,
                        "claim_type": "historical_inference",
                        "confidence": 0.6,
                    }
                ],
                "memories": [],
                "extracted_components": {},
                "conflicts": [],
                "uncertainty": {"level": 0.3, "notes": []},
                "created_by": "rec",
                "artifact_hash": f"h_{dimension}_{len(self.participant_prompts)}",
            }
        else:
            payload = {"question": "q", "dimension": REQUIRED_DIMENSIONS[0]}
        yield AgentEvent(type=AgentEventType.DONE, content=json.dumps(payload, ensure_ascii=False))


PROBE_DIM = REQUIRED_DIMENSIONS[0]


def _register(app, runtime: ResumableRuntime) -> None:
    app.agent_registry.register_adapter(runtime)


def _make_job(app, agent_id: str) -> PersonaCreationJob:
    persona = app.personas.create(
        display_name="Resume Subject",
        aliases=[],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode="counterfactual_continuation",
    )
    task = app.compilation.create_task(persona.id)
    return PersonaCreationJob(
        id="pcjob_resume",
        display_name="Resume Subject",
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        creation_mode="public_research",
        runtime_source="test",
        agent_id=agent_id,
        model_id="fake-gpt-5",
        persona_id=persona.id,
        compilation_task_id=task.id,
    )


def _add_sources(app, persona_id: str, n: int) -> list[str]:
    ids = []
    for i in range(n):
        row = app.personas.add_source_text(
            persona_id,
            title=f"RS{i}",
            source_type="web",
            canonical_url=f"https://resume.test/{i}",
            publisher="P",
            author="A",
            published_at=None,
            accessed_at=None,
            content=f"resumable evidence body {i} token R{i}",
            hash="",
            metadata={},
        )
        ids.append(row.id)
    return ids


_TERMINAL_STATUSES = {"completed", "completed_with_gaps", "failed", "failed_quality_gate"}


async def _wait_terminal(app, job_id: str, timeout: float = 90.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = app.persona_creation.get_job(job_id).status
    while loop.time() < deadline and status not in _TERMINAL_STATUSES:
        await asyncio.sleep(0.05)
        status = app.persona_creation.get_job(job_id).status
    return status


# ---------------------------------------------------------------------------
# Effective model capabilities / context-window resolution
# ---------------------------------------------------------------------------


def test_effective_model_capabilities_resolve_selected_model() -> None:
    snapshot = {
        "id": "agent_x",
        "name": "Agent X",
        "status": "ready",
        "models": [
            {
                "id": "big-model",
                "display_name": "Big",
                "context_window": 262144,
                "source": "official_capability_table",
            }
        ],
    }
    caps = EffectiveModelCapabilities.resolve(
        snapshot, requested_model="big-model", effective_model="big-model"
    )
    assert caps.effective_context_window == 262144
    # A declaratively declared window is provider metadata, not a runtime probe.
    assert caps.context_capability_source == "provider_metadata"
    assert caps.context_verified is True
    assert caps.effective_model == "big-model"

    # Unknown == Unknown: an unreported context window must never silently
    # collapse to the historical 32K fallback.
    unknown = EffectiveModelCapabilities.resolve(
        {"id": "agent_y", "models": []}, requested_model="unknown-model"
    )
    assert unknown.effective_context_window is None
    assert unknown.context_capability_source == "unknown"
    assert unknown.context_verified is False
    assert unknown.planning_context_window == 32_768
    assert unknown.planning_window == 32_768

    manager = AgentContextBudgetManager()
    assert manager.context_window(caps) == 262144


def test_batch_count_scales_with_effective_context_window() -> None:
    manager = AgentContextBudgetManager()
    items = [
        {
            "evidence_id": f"u{i}",
            "source_ids": ["s1"],
            "evidence_type": "source",
            "verbatim_samples": [],
            "intelligence": {},
            "content": "x" * 6000,
        }
        for i in range(40)
    ]

    def item_text(item: dict) -> str:
        return str(item["content"])

    small = list(
        manager.iter_batches(
            items,
            item_text=item_text,
            max_items=48,
            phase="dimension_extraction",
            model={"context_window": 32_768, "model_id": "m"},
            system_prompt="s",
            base_text="b",
        )
    )
    large = list(
        manager.iter_batches(
            items,
            item_text=item_text,
            max_items=48,
            phase="dimension_extraction",
            model={"context_window": 262_144, "model_id": "m"},
            system_prompt="s",
            base_text="b",
        )
    )
    assert len(small) >= 3
    assert len(large) < len(small)


@pytest.mark.anyio
async def test_job_resolution_uses_declared_context_window(app) -> None:
    model = ModelCapability(
        id="fake-gpt-5",
        display_name="Big Fake",
        provider="res_fake",
        context_window=262_144,
        source="official_capability_table",
        reasoning_capability=ReasoningCapability(
            mode=ReasoningCapabilityMode.NATIVE_EFFORT,
            supported_efforts=["high"],
            default_effort="high",
            verified=True,
            source="test_adapter",
        ),
    )
    runtime = FakeAgentAdapter(
        adapter_id="ctx_fake",
        models=[model],
    )
    _register(app, runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, "ctx_fake")
    probe = await runtime.probe()
    job.capability_snapshot = probe.model_dump(mode="json")
    capabilities = app.persona_creation._effective_model_capabilities(job)
    assert capabilities.effective_context_window == 262_144
    assert job.job_config["effective_model_capabilities"]["effective_context_window"] == 262_144


# ---------------------------------------------------------------------------
# Batch checkpoints
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_batch_checkpoint_reuses_finished_batches(app) -> None:
    runtime = ResumableRuntime()
    _register(app, runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.source_ids = _add_sources(app, job.persona_id or "", 1)
    job.source_count = 1
    app.persona_creation._save(job)

    source = app.personas.get_sources(job.persona_id or "")[0]
    items = [
        {
            "evidence_id": source.id,
            "source_ids": [source.id],
            "evidence_type": "source",
            "verbatim_samples": [],
            "intelligence": {},
            "content": source.content,
        }
    ]
    context_manager = AgentContextBudgetManager()
    source_by_id = {source.id: source}
    kwargs = dict(
        job=job,
        dimension=PROBE_DIM,
        evidence_items=items,
        participant_id=PROBE_DIM,
        mode="full",
        source_by_id=source_by_id,
        context_manager=context_manager,
        phase="dimension_extraction",
    )
    first = await app.persona_creation._run_dimension_extraction(**kwargs)
    calls_after_first = len(runtime.participant_prompts)
    assert calls_after_first == 1

    # Identical evidence: the batch checkpoint is reused, no new model call.
    second = await app.persona_creation._run_dimension_extraction(**kwargs)
    assert len(runtime.participant_prompts) == calls_after_first
    assert second.artifact_id == first.artifact_id

    # A failing model on top of valid checkpoints still succeeds: the batch
    # is reused instead of re-paid.
    runtime.fail_dimensions.add(PROBE_DIM)
    third = await app.persona_creation._run_dimension_extraction(**kwargs)
    assert len(runtime.participant_prompts) == calls_after_first
    assert third.artifact_id == first.artifact_id

    # Audit repair must bypass the original extraction checkpoint; otherwise
    # it would replay the same artifact that the audit already rejected.
    runtime.fail_dimensions.discard(PROBE_DIM)
    await app.persona_creation._run_dimension_extraction(
        **{
            **kwargs,
            "phase": "targeted_repair",
            "mode": "targeted_repair",
            "repair_context": {"audit_findings": [{"reason": "missing_claims"}]},
        }
    )
    assert len(runtime.participant_prompts) == calls_after_first + 1

    store = job.job_config["dimension_batch_checkpoints"]
    assert store["prompt_version"]
    assert PROBE_DIM in store["dimensions"]


# ---------------------------------------------------------------------------
# Dimension-level immediate persistence and partial reuse
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dimension_failure_preserves_completed_dimensions(app) -> None:
    runtime = ResumableRuntime()
    _register(app, runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.creation_mode = "fictional"
    job.source_ids = _add_sources(app, job.persona_id or "", 1)
    job.source_count = 1
    job.job_config["material_analysis"] = {"id": "pmjob", "status": "completed"}
    app.persona_creation._save(job)

    failing = REQUIRED_DIMENSIONS[-1]
    runtime.fail_dimensions.add(failing)
    await app.persona_creation._run_job(job.id)

    reloaded = app.persona_creation.get_job(job.id)
    assert reloaded.status in {"failed", "failed_quality_gate"}
    completed = [
        dim
        for dim in REQUIRED_DIMENSIONS
        if reloaded.job_config["dimension_processed_sources"].get(dim)
    ]
    assert set(completed) == set(REQUIRED_DIMENSIONS) - {failing}
    # Completed dimensions are durably attached to the compilation task.
    artifacts = app.persona_creation._latest_dimension_artifacts(reloaded)
    assert set(artifacts) == set(REQUIRED_DIMENSIONS) - {failing}

    # Retry: only the failed dimension goes back to the model.
    runtime.fail_dimensions.clear()
    runtime.participant_prompts.clear()
    await app.persona_creation._extract_dimensions(reloaded)
    for dim in REQUIRED_DIMENSIONS[:-1]:
        assert not any(
            participant == dim and f"提取维度 {dim}" in prompt
            for participant, prompt in runtime.participant_prompts
        )
    assert any(
        participant == failing and f"提取维度 {failing}" in prompt
        for participant, prompt in runtime.participant_prompts
    )


# ---------------------------------------------------------------------------
# Safe pause
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_safe_pause_stops_at_atomic_boundary(app) -> None:
    runtime = ResumableRuntime()
    _register(app, runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.source_ids = _add_sources(app, job.persona_id or "", 1)
    job.source_count = 1
    # Pause is control-plane state: request it through the control registry,
    # not through the job's data-plane job_config snapshot.
    app.persona_creation.job_control.request_pause(job.id)

    source = app.personas.get_sources(job.persona_id or "")[0]
    items = [
        {
            "evidence_id": source.id,
            "source_ids": [source.id],
            "evidence_type": "source",
            "verbatim_samples": [],
            "intelligence": {},
            "content": source.content,
        }
    ]
    with pytest.raises(_PauseRequested):
        await app.persona_creation._run_dimension_extraction(
            job,
            dimension=PROBE_DIM,
            evidence_items=items,
            participant_id=PROBE_DIM,
            mode="full",
            source_by_id={source.id: source},
            context_manager=AgentContextBudgetManager(),
            phase="dimension_extraction",
        )
    # No model call started after the pause request.
    assert runtime.participant_prompts == []


@pytest.mark.anyio
async def test_pause_and_resume_job_lifecycle(app) -> None:
    runtime = ResumableRuntime()
    _register(app, runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.creation_mode = "fictional"
    job.source_ids = _add_sources(app, job.persona_id or "", 1)
    job.source_count = 1
    job.job_config["material_analysis"] = {"id": "pmjob", "status": "completed"}
    job.status = "extracting"
    app.persona_creation._save(job)

    paused = await app.persona_creation.pause_job(job.id)
    assert paused.status == "paused"

    resumed = await app.persona_creation.resume_job(job.id)
    assert resumed.status in {"created", "planning", "researching", "extracting", "compiling"}
    deadline = asyncio.get_running_loop().time() + 30
    status = resumed.status
    while asyncio.get_running_loop().time() < deadline:
        status = app.persona_creation.get_job(job.id).status
        if status in {"completed", "completed_with_gaps", "failed", "failed_quality_gate"}:
            break
        await asyncio.sleep(0.05)
    assert status in {"completed", "completed_with_gaps"}


# ---------------------------------------------------------------------------
# Retry with a model switch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_retry_with_runtime_change_keeps_checkpoints(app) -> None:
    alpha = ResumableRuntime(adapter_id="alpha_fake")
    beta = ResumableRuntime(adapter_id="beta_fake")
    _register(app, alpha)
    _register(app, beta)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, alpha.adapter_id)
    job.creation_mode = "fictional"
    job.source_ids = _add_sources(app, job.persona_id or "", 1)
    job.source_count = 1
    job.status = "failed"
    job.error = "simulated"
    job.failure_json = {
        "code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
        "message": "simulated crash",
        "retriable": True,
    }
    app.persona_creation._save(job)

    retry = await app.persona_creation.retry_job(
        job.id,
        runtime={
            "runtime_source": "test",
            "agent_id": beta.adapter_id,
            "model_id": "fake-gpt-5",
            "reasoning_effort": "high",
        },
    )
    assert retry.id != job.id
    assert retry.agent_id == beta.adapter_id
    assert retry.reasoning_effort == "high"
    assert app.persona_creation.get_job(job.id).superseded_by == retry.id
    # Provenance of the switch is recorded for the job history UI.
    actions = [entry["action"] for entry in retry.job_config.get("execution_history", [])]
    assert "retry_with_runtime_change" in actions


@pytest.mark.anyio
async def test_legacy_audit_repair_failure_retries_into_final_audit(app) -> None:
    """The historical "子平先生" failure row must retry without rebuilding.

    A job that died in Audit Repair with ``unhashable type: 'list'`` kept its
    sources, evidence and all eight dimension artifacts.  After the fix the
    row is retryable even though it was persisted with ``retriable: false``,
    and the retry re-enters Final Audit directly instead of re-running
    research or the eight dimensions.
    """
    runtime = ResumableRuntime()
    _register(app, runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.creation_mode = "fictional"
    job.source_ids = _add_sources(app, job.persona_id or "", 1)
    job.source_count = 1
    job.job_config["material_analysis"] = {"id": "pmjob", "status": "completed"}
    job.status = "extracting"
    app.persona_creation._save(job)
    await app.persona_creation.resume_job(job.id)
    status = await _wait_terminal(app, job.id)
    assert status in {"completed", "completed_with_gaps"}

    dimension_calls_before = len(runtime.participant_prompts)
    # Rewrite the completed row into the historical failed shape.
    failed = app.persona_creation.get_job(job.id)
    failed.status = "failed"
    failed.error = "audit_repair_failed:identity_and_timeline:unhashable type: 'list'"
    failed.failure_json = {
        "code": "PERSONA_CREATION_ERROR",
        "message": "audit_repair_failed:identity_and_timeline:unhashable type: 'list'",
        "retriable": False,
    }
    app.persona_creation._save(failed)

    retry = await app.persona_creation.retry_job(job.id)
    assert retry.id != job.id
    assert retry.job_config.get("retry_stage") == "final_audit"
    assert app.persona_creation.get_job(job.id).superseded_by == retry.id

    await app.persona_creation.resume_job(retry.id)
    status = await _wait_terminal(app, retry.id)
    assert status in {"completed", "completed_with_gaps"}
    # No dimension-extraction model call was re-paid; only the audit phase ran.
    new_prompts = [p for _, p in runtime.participant_prompts[dimension_calls_before:]]
    assert not any("提取维度" in p for p in new_prompts)


@pytest.mark.anyio
async def test_resume_rejects_web_incompatible_runtime_before_research(app) -> None:
    runtime = ResumableRuntime()
    offline = FakeAgentAdapter(adapter_id="offline_fake")
    _register(app, runtime)
    _register(app, offline)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.creation_mode = "public_research"
    job.status = "paused"
    # Research has not produced anything yet.
    job.source_ids = []
    job.source_count = 0
    app.persona_creation._save(job)

    from persona_continuum.application.persona_creation_service import RuntimeBindingError

    with pytest.raises(RuntimeBindingError):
        await app.persona_creation.resume_job(
            job.id,
            runtime={"runtime_source": "test", "agent_id": "offline_fake"},
        )


# ---------------------------------------------------------------------------
# Deterministic structured-output repair
# ---------------------------------------------------------------------------


def test_local_repair_fixes_simple_json_damage() -> None:
    engine = StructuredOutputEngine()
    repaired = engine.local_repair('```json\n{"a": 1, "b": [1, 2,]}\n```')
    assert json.loads(repaired) == {"a": 1, "b": [1, 2]}

    truncated = engine.local_repair('{"a": 1, "items": [{"x": "v"')
    assert json.loads(truncated) == {"a": 1, "items": [{"x": "v"}]}

    # Prose-wrapped JSON is cropped, not damaged.
    wrapped = engine.local_repair('Sure! Here it is:\n{"ok": true}\nHope that helps.')
    assert engine.parse_json(wrapped) == {"ok": True}


def test_structured_engine_repairs_locally_before_model() -> None:
    engine = StructuredOutputEngine()
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "integer"}},
    }
    value = engine.parse_and_validate(
        engine.local_repair('{"a": 1,}'), schema, phase="test"
    )
    assert value.value == {"a": 1}


# ---------------------------------------------------------------------------
# Runtime pool placement
# ---------------------------------------------------------------------------


class _FakePoolTransport:
    def __init__(self) -> None:
        self.closed = False

    class _Proc:
        returncode = None

    @property
    def process(self) -> object:
        return self._Proc()

    async def close(self, *, force: bool = False) -> None:
        self.closed = True


def test_logical_sessions_place_least_loaded() -> None:
    async def scenario() -> None:
        pool = AgentRuntimePool(max_processes_per_key=4)
        counter = {"spawns": 0}

        async def make():  # type: ignore[no-untyped-def]
            counter["spawns"] += 1
            return _FakePoolTransport()

        async def noop_init() -> None:
            return None

        # Warm the pool to exactly four physical processes.
        warmups = [await pool.acquire("codex:place", make) for _ in range(4)]
        for lease in warmups:
            await lease.managed.ensure_initialized(noop_init)
            await lease.release()

        managed = []
        for _ in range(12):
            lease = await pool.acquire("codex:place", make)
            await pool.retain_logical(lease.managed)
            managed.append(lease.managed)
            await lease.release()

        distribution = sorted(item.logical_sessions for item in set(managed))
        assert len(managed) == 12
        assert distribution == [3, 3, 3, 3]
        # Affinity: a retained logical session reacquires its own process.
        target = managed[0]
        lease = await pool.acquire_managed(target)
        assert lease.managed is target
        await lease.release()
        await pool.release_logical(target)
        await pool.shutdown()

    asyncio.run(scenario())


def test_interactive_reservation_blocks_background_only() -> None:
    async def scenario() -> None:
        semaphore = PrioritySemaphore(4, reserved_units=1)
        held: list[str] = []
        for _ in range(3):
            await semaphore.acquire(ExecutionClass.BACKGROUND)
            held.append("bg")
        # One unit remains free but is reserved for interactive work.
        waiter = asyncio.create_task(semaphore.acquire(ExecutionClass.BACKGROUND))
        await asyncio.sleep(0.01)
        assert not waiter.done()
        await asyncio.wait_for(semaphore.acquire(ExecutionClass.INTERACTIVE), 1)
        held.append("interactive")
        for _ in held:
            semaphore.release()
        await asyncio.wait_for(waiter, 1)
        semaphore.release()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Persona rename
# ---------------------------------------------------------------------------


def test_persona_rename_keeps_identity_and_bindings(app) -> None:
    persona = app.personas.create(
        display_name="Alice",
        aliases=[],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode="counterfactual_continuation",
    )
    task = app.compilation.create_task(persona.id)
    source = app.personas.add_source_text(
        persona.id,
        title="RS",
        source_type="web",
        canonical_url="https://rename.test/1",
        publisher="P",
        author="A",
        published_at=None,
        accessed_at=None,
        content="rename probe body",
        hash="",
        metadata={},
    )
    app.compilation.submit_research_artifact(
        task.id,
        {
            "artifact_id": "a_identity",
            "schema_version": "1.1",
            "dimension": PROBE_DIM,
            "source_ids": [source.id],
            "claims": [
                {
                    "content": "rename probe claim",
                    "source_id": source.id,
                    "claim_type": "historical_inference",
                    "confidence": 0.6,
                }
            ],
            "memories": [],
            "extracted_components": {},
            "conflicts": [],
            "uncertainty": {"level": 0.2},
            "created_by": "test",
            "artifact_hash": "h_rename",
        },
    )

    manifest_path = Path(persona.package_path) / "manifest.yaml"
    renamed = app.personas.rename(persona.id, "乔布斯（晚年）")
    assert renamed.id == persona.id
    assert renamed.manifest.display_name == "乔布斯（晚年）"
    import yaml

    assert yaml.safe_load(manifest_path.read_text())["display_name"] == "乔布斯（晚年）"
    # Research artifacts and compilations are untouched by the rename.
    assert app.compilation.get_task(task.id).artifacts

    for bad in ("", "   ", "x" * 200, "bad\x02name"):
        with pytest.raises(ConflictError):
            app.personas.rename(persona.id, bad)


# ---------------------------------------------------------------------------
# Database transaction helper (world tick atomicity)
# ---------------------------------------------------------------------------


def test_database_transaction_commits_once_and_rolls_back(app) -> None:
    db: Database = app.database
    db.conn.execute("CREATE TABLE IF NOT EXISTS txn_probe (id INTEGER)")
    db.conn.execute("DELETE FROM txn_probe")
    db.conn.commit()

    statements: list[str] = []
    db.conn.set_trace_callback(statements.append)
    try:
        with db.transaction():
            db.conn.execute("INSERT INTO txn_probe VALUES (1)")
        with pytest.raises(RuntimeError), db.transaction():
            db.conn.execute("INSERT INTO txn_probe VALUES (2)")
            raise RuntimeError("boom")
    finally:
        db.conn.set_trace_callback(None)
    db.conn.commit()

    rows = db.conn.execute("SELECT id FROM txn_probe ORDER BY id").fetchall()
    assert [row["id"] for row in rows] == [1]
    assert statements.count("BEGIN IMMEDIATE") == 2
    assert statements.count("COMMIT") >= 1
    assert any("ROLLBACK" in statement for statement in statements)


# ---------------------------------------------------------------------------
# Room state / transcript separation
# ---------------------------------------------------------------------------


def _persist_probe_room_state(app, transcript_entries: int) -> tuple[int, dict]:
    orchestrator = app.orchestrator
    persona = app.personas.create(
        display_name="Room Probe",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode="counterfactual_continuation",
    )
    state = orchestrator.create_room(
        title="probe",
        topic="probe",
        participants=[],
        metadata={"probe": True},
    )
    state.transcript = [
        {
            "turn_id": f"turn_{i}",
            "participant_id": "host",
            "persona_id": persona.id,
            "speaker_name": "Host",
            "content": f"content {i} " + "y" * 200,
            "created_at": datetime.now(UTC).isoformat(),
        }
        for i in range(transcript_entries)
    ]
    orchestrator._persist_room_state(state)
    row = app.database.conn.execute(
        "SELECT state_json FROM rooms WHERE id = ?", (state.id,)
    ).fetchone()
    persisted = json.loads(str(row["state_json"]))
    size = len(row["state_json"])
    orchestrator.delete_room(state.id)
    return size, persisted


def test_room_state_persists_recent_window_only(app) -> None:
    small_size, small_state = _persist_probe_room_state(app, 40)
    big_size, big_state = _persist_probe_room_state(app, 400)
    window = app.orchestrator._persisted_transcript_window
    assert len(small_state["transcript"]) <= max(window, 40)
    assert len(big_state["transcript"]) == window
    assert big_state["metadata"]["transcript_truncated_in_state"] is True
    # Persistence cost no longer scales with the total transcript length.
    assert big_size < small_size * 8


def test_room_state_rehydrates_full_transcript_from_event_store(app) -> None:
    orchestrator = app.orchestrator
    state = orchestrator.create_room(
        title="rehydrate",
        topic="rehydrate",
        participants=[],
        metadata={"probe": True},
    )
    room_id = state.id
    for index in range(60):
        turn_id = f"turn_{index}"
        orchestrator._save_transcript_record(
            RoomTranscriptRecord(
                id=f"rturn_{index}",
                room_id=room_id,
                turn_id=turn_id,
                participant_id="host",
                persona_id="room_host",
                speaker_name="Host",
                agent_runtime_id="",
                content=f"content {index}",
                commit_status="host_message",
                metadata={"phase": "probe"},
                created_at=datetime.now(UTC),
            )
        )
    # Simulate a persisted truncated snapshot.
    state.transcript = list(state.transcript)[-orchestrator._persisted_transcript_window :]
    state.metadata["transcript_truncated_in_state"] = True
    data = state.model_dump(mode="json")
    orchestrator._rehydrate_transcript(data)
    assert len(data["transcript"]) == 60
    orchestrator.delete_room(room_id)


# ---------------------------------------------------------------------------
# World lazy actor memory
# ---------------------------------------------------------------------------


def test_simulation_loop_loads_memory_only_for_active_actors() -> None:
    from persona_continuum.world.director import WorldDirector
    from persona_continuum.world.models import Actor, ActorType
    from persona_continuum.world.simulation_loop import SimulationLoop

    class StubDirector(WorldDirector):
        def select_active_actors(self, actors, state, recent_events, limit=6):  # type: ignore[no-untyped-def]
            return list(actors[:limit])

    actors = [
        Actor(
            id=f"actor_{i}",
            name=f"Actor {i}",
            actor_type=ActorType.PERSONA_ACTOR,
            persona_id=f"persona_{i}",
        )
        for i in range(50)
    ]
    loaded: list[str] = []

    def loader(actor: Actor) -> list:
        loaded.append(actor.id)
        return []

    class StubTrace:
        def get_trace(self, world_id: str, branch_id: str, actor_id: str) -> dict:
            return {}

    class StubDecisionEngine:
        runtime = StubTrace()

        async def decide(self, **kwargs):  # type: ignore[no-untyped-def]
            from persona_continuum.world.models import ActionProposal, ActionType

            return ActionProposal(
                actor=kwargs["actor"].id,
                intent="wait",
                action_type=ActionType.WAIT,
                reasoning_summary="stub",
                expected_effect="none",
                confidence=0.5,
            )

    loop = SimulationLoop(
        StubDirector(policy=None),  # type: ignore[arg-type]
        StubDecisionEngine(),  # type: ignore[arg-type]
        max_active_actors=6,
    )
    batch = asyncio.run(
        loop.collect_proposals(
            world_id="w",
            branch_id="b",
            seed=None,  # type: ignore[arg-type]
            state=None,  # type: ignore[arg-type]
            actors=actors,
            recent_events=[],
            memory_loader=loader,
        )
    )
    assert len(batch.proposals) == 6
    assert sorted(loaded) == sorted(actor.id for actor in actors[:6])
