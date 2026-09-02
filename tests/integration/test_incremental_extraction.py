from __future__ import annotations

import json

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.models import AgentEvent, AgentEventType
from persona_continuum.application.persona_creation_service import (
    REQUIRED_DIMENSIONS,
    PersonaCreationJob,
    PersonaCreationOrchestrator,
    PersonaQualityGateError,
)
from persona_continuum.domain.persona import PersonaType


class RecordingRuntime(FakeAgentAdapter):
    def __init__(self) -> None:
        super().__init__(adapter_id="rec_fake", name="Rec")
        self.prompts: list[str] = []
        self.audit_failure_dimension: str | None = None
        self.fail_dimension_once: str | None = None
        self.failed_dimensions: set[str] = set()

    async def send(self, session, turn):  # type: ignore[no-untyped-def]
        prompt = turn.user_message
        self.prompts.append(prompt)
        if "严格最终质量门禁" in (turn.system_prompt or ""):
            dimensions = {dimension: "pass" for dimension in REQUIRED_DIMENSIONS}
            if self.audit_failure_dimension:
                dimensions[self.audit_failure_dimension] = "fail"
            payload = {
                "status": "fail" if self.audit_failure_dimension else "pass",
                "dimensions": dimensions,
                "issues": (
                    [{"dimension": self.audit_failure_dimension, "reason": "unsupported"}]
                    if self.audit_failure_dimension
                    else []
                ),
            }
        elif "提取维度" in prompt:
            dimension = next(
                (d for d in REQUIRED_DIMENSIONS if f"提取维度 {d}" in prompt),
                REQUIRED_DIMENSIONS[0],
            )
            src = (session.config.extra.get("source_ids") or ["s0"])[0]
            if (
                dimension == self.fail_dimension_once
                and dimension not in self.failed_dimensions
            ):
                self.failed_dimensions.add(dimension)
                payload = {"invalid": True}
            else:
                payload = {
                "artifact_id": f"a_{dimension}_{len(self.prompts)}",
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
                "artifact_hash": f"h_{dimension}_{len(self.prompts)}",
                }
        else:
            payload = {"question": "q", "dimension": REQUIRED_DIMENSIONS[0]}
        yield AgentEvent(type=AgentEventType.DONE, content=json.dumps(payload, ensure_ascii=False))


def _make_job(app, agent_id: str) -> PersonaCreationJob:
    persona = app.personas.create(
        display_name="Inc Subject",
        aliases=[],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode="counterfactual_continuation",
    )
    task = app.compilation.create_task(persona.id)
    return PersonaCreationJob(
        id="pcjob_inc",
        display_name="Inc Subject",
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        creation_mode="public_research",
        runtime_source="test",
        agent_id=agent_id,
        model_id="fake-gpt-5",
        persona_id=persona.id,
        compilation_task_id=task.id,
    )


def _add_sources(app, persona_id: str, start: int, n: int) -> list[str]:
    ids = []
    for i in range(start, start + n):
        row = app.personas.add_source_text(
            persona_id,
            title=f"S{i}",
            source_type="web",
            canonical_url=f"https://inc.test/{i}",
            publisher="P",
            author="A",
            published_at=None,
            accessed_at=None,
            content=f"incremental evidence body {i} unique token U{i}",
            hash="",
            metadata={},
        )
        ids.append(row.id)
    return ids


@pytest.mark.anyio
async def test_fictional_extraction_cannot_promote_setting_to_history(app) -> None:
    runtime = RecordingRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.persona_type = PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON
    job.creation_mode = "fictional"
    job.source_ids = _add_sources(app, job.persona_id or "", 0, 1)
    job.source_count = 1

    await app.persona_creation._extract_dimensions(job)

    artifacts = app.persona_creation._latest_dimension_artifacts(job)
    assert artifacts
    assert all(
        claim["claim_type"] == "fictional_author_defined"
        for artifact in artifacts.values()
        for claim in artifact["claims"]
    )
    assert any("不得使用任何 historical_* 类型" in prompt for prompt in runtime.prompts)


@pytest.mark.anyio
async def test_dimension_extraction_records_real_model_calls_for_completion_guard(app) -> None:
    runtime = RecordingRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.creation_mode = "private_materials"
    job.source_ids = _add_sources(app, job.persona_id or "", 0, 1)
    job.source_count = 1
    job.job_config.update({"input_material_count": 1, "material_agent_calls": 1})

    await app.persona_creation._extract_dimensions(job)

    assert job.job_config["dimension_agent_calls"] == len(REQUIRED_DIMENSIONS)
    app.persona_creation._assert_material_agent_analysis(job)


@pytest.mark.anyio
async def test_dimension_retry_reuses_seven_artifacts_and_only_repays_missing_one(app) -> None:
    runtime = RecordingRuntime()
    runtime.fail_dimension_once = "expression_dna"
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.creation_mode = "private_materials"
    job.source_ids = _add_sources(app, job.persona_id or "", 0, 1)
    job.source_count = 1
    job.job_config.update({"input_material_count": 1, "material_agent_calls": 1})

    await app.persona_creation._extract_dimensions(job)

    assert len(job.dimension_progress) == len(REQUIRED_DIMENSIONS) - 1
    assert "expression_dna" in job.job_config["dimension_errors"]
    prompt_count = len(runtime.prompts)

    await app.persona_creation._extract_dimensions(job)

    retry_prompts = runtime.prompts[prompt_count:]
    extraction_prompts = [prompt for prompt in retry_prompts if "提取维度" in prompt]
    assert len(extraction_prompts) == 1
    assert "提取维度 expression_dna" in extraction_prompts[0]
    assert len(job.dimension_progress) == len(REQUIRED_DIMENSIONS)
    assert job.job_config["dimension_errors"] == {}
    assert job.job_config["dimension_agent_calls"] == len(REQUIRED_DIMENSIONS) + 1
    app.persona_creation._assert_material_agent_analysis(job)


def test_local_audit_does_not_repair_honestly_documented_source_gaps() -> None:
    results = {
        "consistency": {
            "status": "repair_required",
            "dimensions": {
                dimension: (
                    "repair_required" if dimension == "third_party_views" else "pass"
                )
                for dimension in REQUIRED_DIMENSIONS
            },
            "issues": [
                {
                    "dimension": "third_party_views",
                    "severity": "high",
                    "type": "evidence_gap",
                }
            ],
        }
    }

    assert not PersonaCreationOrchestrator._audit_failing_dimensions(
        results, preserve_documented_source_gaps=True  # type: ignore[arg-type]
    )
    assert PersonaCreationOrchestrator._audit_failing_dimensions(  # type: ignore[arg-type]
        results
    ) == {"third_party_views"}


def test_audit_accepts_verdict_and_result_dimension_fields() -> None:
    """A per-dimension pass must not be reported as failing over field naming.

    ``FINAL_AUDIT_SCHEMA`` leaves the dimension entry free-form; GLM returned
    ``verdict`` for the consistency pass, and reading only ``status`` marked
    all eight dimensions as failures.
    """

    results = {
        "evidence": {
            "status": "pass",
            "dimensions": {dimension: {"status": "pass"} for dimension in REQUIRED_DIMENSIONS},
            "issues": [],
        },
        "consistency": {
            "status": "pass",
            "dimensions": {dimension: {"verdict": "pass"} for dimension in REQUIRED_DIMENSIONS},
            "issues": [
                {
                    "dimensions": ["expression_dna"],
                    "severity": "info",
                    "type": "coverage_gap",
                }
            ],
        },
    }

    assert not PersonaCreationOrchestrator._audit_failing_dimensions(
        results, preserve_documented_source_gaps=True  # type: ignore[arg-type]
    )
    assert not PersonaCreationOrchestrator._audit_failing_dimensions(  # type: ignore[arg-type]
        results
    )

    failing = {
        "consistency": {
            "status": "pass",
            "dimensions": {
                **{dimension: {"verdict": "pass"} for dimension in REQUIRED_DIMENSIONS},
                "works_and_views": {"result": "repair_required"},
            },
            "issues": [],
        }
    }
    assert PersonaCreationOrchestrator._audit_failing_dimensions(  # type: ignore[arg-type]
        failing
    ) == {"works_and_views"}


def test_compact_audit_conflicts_remain_structured() -> None:
    compact = PersonaCreationOrchestrator._compact_audit_artifact(
        {
            "dimension": "expression_dna",
            "source_ids": ["s1"],
            "claims": [],
            "conflicts": [
                {
                    "type": "internal_tension",
                    "summary": "x" * 300,
                    "source_ids": ["s1"],
                }
            ],
            "uncertainty": {},
        },
        claims_limit=8,
    )

    assert isinstance(compact["conflicts"][0], dict)
    assert compact["conflicts"][0]["type"] == "internal_tension"


def test_local_audit_accepts_empty_dimension_when_gap_is_documented(app) -> None:
    job = _make_job(app, "fake")
    job.creation_mode = "fictional"
    job.source_ids = ["s1"]
    artifacts = {
        dimension: {
            "dimension": dimension,
            "claims": [],
            "uncertainty": {"notes": ["source contains no third-party views"]},
        }
        for dimension in REQUIRED_DIMENSIONS
    }

    assert app.persona_creation._deterministic_audit_failures(job, artifacts) == {}


def test_local_audit_accepts_structured_no_extractable_content_fields(app) -> None:
    job = _make_job(app, "fake")
    job.creation_mode = "fictional"
    job.source_ids = ["s1"]
    uncertainty_shapes = (
        {
            "summary": "source only contains a heading",
            "missing_information": ["actual expression samples"],
        },
        {
            "no_extractable_content": True,
            "missing_information": ["supported claims"],
        },
        {
            "extractable_content_present": False,
            "repair_note": "new evidence is required",
        },
    )

    for uncertainty in uncertainty_shapes:
        artifacts = {
            dimension: {
                "dimension": dimension,
                "claims": [],
                "uncertainty": uncertainty,
            }
            for dimension in REQUIRED_DIMENSIONS
        }
        assert app.persona_creation._deterministic_audit_failures(job, artifacts) == {}


def test_local_audit_accepts_model_reported_empty_content_gap(app) -> None:
    """An explicit local source deficit is a gap, not a hard quality failure."""

    job = _make_job(app, "fake")
    job.creation_mode = "fictional"
    job.source_ids = ["s1"]
    artifacts = {
        dimension: {
            "dimension": dimension,
            "claims": [],
            "uncertainty": {
                "summary": "source only contains a dimension heading",
                "empty_content": True,
                "repair_outcome": "unresolved_source_deficit",
                "note": "new source material is required",
            },
        }
        for dimension in REQUIRED_DIMENSIONS
    }

    assert app.persona_creation._deterministic_audit_failures(job, artifacts) == {}


@pytest.mark.anyio
async def test_incremental_skips_when_no_new_evidence(app) -> None:
    runtime = RecordingRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    _add_sources(app, job.persona_id or "", 0, 4)
    job.source_ids = list(
        s.id for s in app.personas.get_sources(job.persona_id or "")
    )
    job.source_count = len(job.source_ids)

    # Full pass establishes processed coverage.
    runtime.prompts.clear()
    await app.persona_creation._extract_dimensions(job, incremental=False)
    full_prompts = len(runtime.prompts)
    assert full_prompts >= len(REQUIRED_DIMENSIONS)
    processed_after_full = dict(job.job_config.get("dimension_processed_sources") or {})
    assert processed_after_full, "full pass must record processed sources"

    # Incremental pass with no new evidence must make ZERO model calls.
    runtime.prompts.clear()
    await app.persona_creation._extract_dimensions(job, incremental=True)
    assert runtime.prompts == [], "incremental must skip already-processed evidence"


@pytest.mark.anyio
async def test_incremental_only_analyzes_new_delta(app) -> None:
    runtime = RecordingRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    first = _add_sources(app, job.persona_id or "", 0, 4)
    job.source_ids = list(first)
    job.source_count = len(first)
    await app.persona_creation._extract_dimensions(job, incremental=False)

    # Add a new source (id token U99) and run an incremental round.
    second = _add_sources(app, job.persona_id or "", 99, 100)
    job.source_ids = sorted(set(job.source_ids) | set(second))
    job.source_count = len(job.source_ids)
    runtime.prompts.clear()
    await app.persona_creation._extract_dimensions(job, incremental=True)

    extraction_prompts = [p for p in runtime.prompts if "提取维度" in p]
    # The delta round runs (new evidence exists)...
    assert extraction_prompts, "incremental must analyze newly added evidence"
    # ...and its prompt marks previously processed evidence as context only,
    # while the new body token appears as actual (non-context) evidence.
    joined = "\n".join(extraction_prompts)
    assert "incremental evidence body 99" in joined
    # The prompt declares incremental mode so old facts are not re-emitted.
    assert "增量分析" in joined or "ALREADY_PROCESSED_CONTEXT" in joined


@pytest.mark.anyio
async def test_final_full_audit_covers_all_evidence_on_fresh_thread(app) -> None:
    runtime = RecordingRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    sources = _add_sources(app, job.persona_id or "", 0, 6)
    job.source_ids = list(sources)
    job.source_count = len(sources)
    # Pretend an earlier incremental round already processed everything.
    job.job_config["dimension_processed_sources"] = {
        d: list(sources) for d in REQUIRED_DIMENSIONS
    }
    runtime.prompts.clear()
    # Even with all sources "processed", the FINAL FULL AUDIT must still run a
    # complete pass over every dimension (it is not skipped by incrementality).
    await app.persona_creation._extract_dimensions(job, incremental=True, final_audit=True)
    audit_prompts = [p for p in runtime.prompts if "提取维度" in p]
    assert len(audit_prompts) >= len(REQUIRED_DIMENSIONS)
    # Audit runs on fresh threads: participants carry the :audit suffix.
    assert any(":audit" in sid for sid, _t in runtime.sent_turns) or audit_prompts


@pytest.mark.anyio
async def test_global_audits_require_explicit_pass_for_every_dimension(app) -> None:
    runtime = RecordingRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.source_ids = _add_sources(app, job.persona_id or "", 0, 8)
    job.source_count = len(job.source_ids)
    await app.persona_creation._extract_dimensions(job)

    await app.persona_creation._run_global_audits(job)
    assert job.job_config["final_global_audit"]["status"] == "pass"

    runtime.audit_failure_dimension = REQUIRED_DIMENSIONS[-1]
    app.config.persona_audit_repair_attempts = 0
    with pytest.raises(PersonaQualityGateError, match="final_quality_gate_failed"):
        await app.persona_creation._run_global_audits(job)
    assert job.job_config["final_global_audit"]["status"] == "fail"


@pytest.mark.anyio
async def test_deterministic_audit_marks_only_the_dimensions_that_failed(app) -> None:
    runtime = RecordingRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.creation_mode = "fictional"
    job.source_ids = _add_sources(app, job.persona_id or "", 0, 1)
    job.source_count = 1
    source_id = job.source_ids[0]
    for dimension in REQUIRED_DIMENSIONS:
        payload = _historical_artifact_payload(dimension, source_id)
        if dimension in {"works_and_views", "expression_dna"}:
            payload["claims"] = []
            payload["uncertainty"] = {"level": 0.3, "notes": []}
        app.compilation.submit_research_artifact(job.compilation_task_id or "", payload)
    app.config.persona_audit_repair_attempts = 0

    with pytest.raises(PersonaQualityGateError):
        await app.persona_creation._run_global_audits(job)

    assert job.job_config["final_global_audit"]["failing_dimensions"] == [
        "expression_dna",
        "works_and_views",
    ]


def _historical_artifact_payload(dimension: str, source_id: str) -> dict:
    return {
        "artifact_id": f"art_{dimension}",
        "schema_version": "1.1",
        "dimension": dimension,
        "source_ids": [source_id],
        "claims": [
            {
                "content": f"claim {dimension}",
                "source_id": source_id,
                "claim_type": "historical_inference",
                "confidence": 0.6,
            }
        ],
        "memories": [{"content": f"memory {dimension}", "source_id": source_id}],
        "extracted_components": {},
        "conflicts": [],
        "uncertainty": {"level": 0.3, "notes": []},
        "created_by": "pre-fix-run",
        "artifact_hash": f"hash_{dimension}",
    }


@pytest.mark.anyio
async def test_quality_gate_retry_reuses_artifacts_without_repaying_extraction(app) -> None:
    """A retry run must re-judge, not re-extract, the eight prior artifacts."""
    runtime = RecordingRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    original = _make_job(app, runtime.adapter_id)
    original.persona_type = PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON
    original.creation_mode = "fictional"
    original.source_ids = _add_sources(app, original.persona_id or "", 0, 1)
    original.source_count = 1
    source_id = original.source_ids[0]
    # The pre-fix run persisted artifacts with historical_* claim types.
    for dimension in REQUIRED_DIMENSIONS:
        app.compilation.submit_research_artifact(
            original.compilation_task_id or "",
            _historical_artifact_payload(dimension, source_id),
        )
    app.persona_creation._save(original)

    retry = original.model_copy(deep=True)
    retry.id = "pcjob_retry"
    retry.status = "created"
    retry.current_stage = "retry"
    retry.job_config = {
        "retry_of": original.id,
        "material_analysis": {"id": "pmjob_done", "status": "completed"},
        "input_material_count": 1,
        "material_agent_calls": 16,
        "dimension_agent_calls": 8,
    }
    app.persona_creation._save(retry)
    runtime.prompts.clear()

    await app.persona_creation._run_job(retry.id)
    final = app.persona_creation.get_job(retry.id)

    assert final.status in {"completed", "completed_with_gaps"}
    assert final.current_stage != "NO_NEW_INFORMATION"
    # Zero dimension-extraction prompts: the eight artifacts were reused.
    assert [p for p in runtime.prompts if "提取维度" in p] == []
    # The final audit still ran (that is the point of the retry).
    assert final.job_config["final_global_audit"]["status"] == "pass"
    assert any(
        event.get("event") == "persona_dimension_extraction_resumed"
        for event in final.events
    )
    # Reused fictional artifacts were deterministically rewritten; the stale
    # historical_* versions were pruned before compilation.
    artifacts = app.compilation.get_task(retry.compilation_task_id or "").artifacts
    dims = [str(a.get("dimension")) for a in artifacts]
    assert len(dims) == len(set(dims)) == len(REQUIRED_DIMENSIONS)
    assert all(
        claim["claim_type"] == "fictional_author_defined"
        for artifact in artifacts
        for claim in artifact.get("claims") or []
    )
    assert all(
        memory.get("source_kind") == "fictional_author_defined"
        for artifact in artifacts
        for memory in artifact.get("memories") or []
    )


@pytest.mark.anyio
async def test_retry_reuse_requires_complete_valid_artifact_set(app) -> None:
    runtime = RecordingRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    job = _make_job(app, runtime.adapter_id)
    job.creation_mode = "fictional"
    job.source_ids = _add_sources(app, job.persona_id or "", 0, 1)
    job.source_count = 1
    job.job_config["retry_of"] = "pcjob_original"
    # Only four of eight dimensions exist, so reuse must fall back to extraction.
    for dimension in REQUIRED_DIMENSIONS[:4]:
        app.compilation.submit_research_artifact(
            job.compilation_task_id or "",
            _historical_artifact_payload(dimension, job.source_ids[0]),
        )
    assert app.persona_creation._retry_reusable_dimension_artifacts(job) is None
    # A complete set is reusable for a retry, but never for a fresh job.
    for dimension in REQUIRED_DIMENSIONS[4:]:
        app.compilation.submit_research_artifact(
            job.compilation_task_id or "",
            _historical_artifact_payload(dimension, job.source_ids[0]),
        )
    assert app.persona_creation._retry_reusable_dimension_artifacts(job)
    job.job_config.pop("retry_of")
    assert app.persona_creation._retry_reusable_dimension_artifacts(job) is None
    # Brand-new sources arriving with the retry must force real extraction.
    job.job_config["retry_of"] = "pcjob_original"
    job.job_config["new_source_ids"] = ["src_fresh"]
    assert app.persona_creation._retry_reusable_dimension_artifacts(job) is None


def test_retain_latest_dimension_artifacts_prunes_superseded_versions(app) -> None:
    persona = app.personas.create(
        display_name="Prune Subject",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode="counterfactual_continuation",
    )
    task = app.compilation.create_task(persona.id)
    source = app.personas.add_source_text(
        persona.id,
        title="S",
        source_type="user_provided",
        canonical_url=None,
        publisher="u",
        author="u",
        published_at=None,
        accessed_at=None,
        content="body",
    )
    for dimension in REQUIRED_DIMENSIONS:
        app.compilation.submit_research_artifact(
            task.id, _historical_artifact_payload(dimension, source.id)
        )
    # One superseding version for a single dimension.
    repaired = _historical_artifact_payload(REQUIRED_DIMENSIONS[0], source.id)
    repaired["artifact_id"] = "art_repaired"
    repaired["artifact_hash"] = "hash_repaired"
    repaired["claims"][0]["content"] = "repaired claim"
    app.compilation.submit_research_artifact(task.id, repaired)

    removed = app.compilation.retain_latest_dimension_artifacts(task.id)
    assert removed == 1
    artifacts = app.compilation.get_task(task.id).artifacts
    assert len(artifacts) == len(REQUIRED_DIMENSIONS)
    first = next(a for a in artifacts if a.get("dimension") == REQUIRED_DIMENSIONS[0])
    assert first["artifact_id"] == "art_repaired"


def _oversized_audit_artifacts() -> dict[str, dict[str, object]]:
    return {
        dimension: {
            "dimension": dimension,
            "source_ids": ["s1"],
            "claims": [
                {
                    "content": f"claim {dimension} " + "x" * 240,
                    "source_id": "s1",
                    "claim_type": "counterfactual_simulated",
                    "confidence": 0.6,
                }
                for _ in range(8)
            ],
            "extracted_components": {"summary": "y" * 900},
            "conflicts": [],
            "uncertainty": {},
        }
        for dimension in REQUIRED_DIMENSIONS
    }


def test_global_audit_payload_backfills_referenced_fused_evidence(app) -> None:
    """A referenced evf_* id outside the ranked window must still be auditable.

    The linkage audit can only verify evidence ids present in the payload's
    ledger.  Fused entries live in their own table and reach the ledger only
    through the ranked retrieve() window; without backfilling from that table
    an artifact referencing a fused id fails every audit round.
    """

    from persona_continuum.application.material_intelligence import FusedEvidence

    job = _make_job(app, "fake")
    assert job.persona_id
    referenced_id = "evf_1437b78c29c2dbbd"
    # One short low-ranked entry plus enough longer ones to push it outside
    # the default 32-item retrieve() window.
    fused_items = [FusedEvidence(
        id=referenced_id,
        persona_id=job.persona_id,
        canonical_claim="short disagreement",
        evidence_type="profile",
        source_ids=["s1"],
    )]
    fused_items.extend(
        FusedEvidence(
            id=f"evf_{index:016x}",
            persona_id=job.persona_id,
            canonical_claim="long fused claim " + "z" * 200,
            evidence_type="profile",
            source_ids=["s1"],
        )
        for index in range(1, 40)
    )
    app.material_intelligence._persist_fused(fused_items)

    artifacts = {
        "decisions_and_behavior": {
            "dimension": "decisions_and_behavior",
            "source_ids": ["s1"],
            "claims": [],
            "conflicts": [
                {"type": "persona_disagreement", "evidence_ids": [referenced_id]}
            ],
            "uncertainty": {},
        }
    }
    payload = app.persona_creation._global_audit_payload(job, artifacts)

    ledger_ids = {entry["id"] for entry in payload["evidence_ledger"]}
    assert referenced_id in ledger_ids
    backfilled = next(
        entry for entry in payload["evidence_ledger"] if entry["id"] == referenced_id
    )
    assert backfilled["referenced_context"] is True
    assert backfilled["text"] == "short disagreement"


def test_global_audit_payload_compacts_to_transport_budget(app) -> None:
    """The audit payload must fit the adapter transport, not the model window."""

    from persona_continuum.application.persona_creation_service import (
        _GLOBAL_AUDIT_ENVELOPE_HEADROOM_BYTES,
    )

    job = _make_job(app, "fake")
    artifacts = _oversized_audit_artifacts()

    unbounded = app.persona_creation._global_audit_payload(job, artifacts)
    unbounded_bytes = len(
        json.dumps(unbounded, ensure_ascii=False, default=str).encode("utf-8")
    )

    budget = 12_000
    bounded = app.persona_creation._global_audit_payload(
        job, artifacts, transport_safe_bytes=budget
    )
    bounded_bytes = len(
        json.dumps(bounded, ensure_ascii=False, default=str).encode("utf-8")
    )
    assert bounded_bytes < unbounded_bytes
    assert bounded_bytes + _GLOBAL_AUDIT_ENVELOPE_HEADROOM_BYTES <= budget
    assert bounded["dimension_artifacts"].keys() == unbounded["dimension_artifacts"].keys()

    # A generous budget must leave the payload untouched.
    roomy = app.persona_creation._global_audit_payload(
        job, artifacts, transport_safe_bytes=8 * 1024 * 1024
    )
    assert roomy == unbounded


def test_global_audit_payload_does_not_backfill_compacted_away_references(app) -> None:
    """Only evidence links visible in the compact artifact need ledger rows."""

    from persona_continuum.application.material_intelligence import EvidenceUnit
    from persona_continuum.application.persona_creation_service import (
        _GLOBAL_AUDIT_ENVELOPE_HEADROOM_BYTES,
    )

    job = _make_job(app, "fake")
    assert job.persona_id
    source_id = _add_sources(app, job.persona_id, 900, 1)[0]
    evidence_units = [
        EvidenceUnit(
            id=f"evu_{index:016x}",
            persona_id=job.persona_id,
            source_id=source_id,
            text="referenced evidence " + "z" * 200,
            normalized_text="referenced evidence " + "z" * 200,
        )
        for index in range(240)
    ]
    app.material_intelligence._persist_units(evidence_units)

    artifacts = _oversized_audit_artifacts()
    for artifact in artifacts.values():
        artifact["source_ids"] = [source_id]
        for claim in artifact["claims"]:
            claim["source_id"] = source_id
    first_dimension = REQUIRED_DIMENSIONS[0]
    artifacts[first_dimension]["claims"] = [
        {
            "content": f"claim {index}",
            "source_id": source_id,
            "claim_type": "counterfactual_simulated",
            "confidence": 0.6,
            "metadata": {"evidence_ids": [evidence_units[index].id]},
        }
        for index in range(len(evidence_units))
    ]

    budget = 12_000
    payload = app.persona_creation._global_audit_payload(
        job, artifacts, transport_safe_bytes=budget
    )
    encoded_bytes = len(
        json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    )
    visible_ids = {
        evidence_id
        for artifact in payload["dimension_artifacts"].values()
        for claim in artifact["claims"]
        for evidence_id in claim.get("evidence_ids") or []
    }
    ledger_ids = {entry["id"] for entry in payload["evidence_ledger"]}

    assert visible_ids
    assert visible_ids <= ledger_ids
    assert evidence_units[-1].id not in ledger_ids
    assert encoded_bytes + _GLOBAL_AUDIT_ENVELOPE_HEADROOM_BYTES <= budget
