"""Late creation failures must preserve completed work and bound follow-up calls."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.application.persona_creation_service import (
    REQUIRED_DIMENSIONS,
    PersonaCreationError,
    PersonaCreationJob,
)
from persona_continuum.compiler.schemas import ResearchArtifact
from persona_continuum.domain.persona import PersonaType


def make_job(app):
    persona = app.personas.create(
        display_name="Recovery fixture", aliases=[], persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="counterfactual_continuation",
    )
    task = app.compilation.create_task(persona.id)
    job = PersonaCreationJob(
        id="pcjob_recovery", display_name=persona.manifest.display_name,
        persona_type=PersonaType.PRIVATE_LIVING_PERSON, creation_mode="private_materials",
        runtime_source="test", agent_id="fake_agent", model_id="fake-gpt-5",
        persona_id=persona.id, compilation_task_id=task.id,
    )
    app.persona_creation._save(job)
    return job


@pytest.mark.anyio
async def test_interview_uses_one_bounded_context_and_persists_before_retry(app, monkeypatch):
    job = make_job(app)
    service = app.persona_creation
    retrieval = Mock(return_value=[{"id": f"ev_{i}", "text": "sample"} for i in range(1000)])
    material = SimpleNamespace(
        get_index=lambda _: SimpleNamespace(retrieve=retrieval),
        gap_analysis=lambda _: {"gaps": [{"dimension": "expression_dna"}]},
    )
    monkeypatch.setattr(service, "material_intelligence", material)
    agent = AsyncMock(return_value={
        "question": "哪些常用措辞尚未记录？", "dimension": "expression_dna",
    })
    monkeypatch.setattr(service, "_agent_json", agent)

    await service._request_interview_question(job)
    saved = service.get_job(job.id)
    assert len(saved.interview_questions) == 1
    retrieval.assert_called_once_with("expression_dna", top_k=24, diversity=True)
    payload = json.loads(agent.call_args.kwargs["user_message"])
    assert len(payload["existing_materials"]) <= 24
    assert payload["batch_count"] == 1

    # A later failure/retry with unchanged evidence must not regenerate it.
    await service._request_interview_question(saved)
    assert agent.await_count == 1
    assert retrieval.call_count == 1
    saved.source_ids.append("new_answer_source")
    await service._request_interview_question(saved)
    assert agent.await_count == 2


def artifact(dimension, source_id):
    return ResearchArtifact.model_validate({
        "artifact_id": "art_recovery", "schema_version": "1.1", "dimension": dimension,
        "source_ids": [source_id], "claims": [],
        "memories": [{"content": "Fixture memory", "source_id": source_id}],
        "extracted_components": {}, "uncertainty": {},
        "created_by": "test", "artifact_hash": "fixture-hash",
    })


@pytest.mark.anyio
async def test_cached_evidence_id_is_resolved_without_another_model_call(app, monkeypatch):
    job = make_job(app)
    source = app.personas.add_source_text(
        job.persona_id, title="fixture", source_type="txt", content="Fixture source",
        canonical_url=None, publisher=None, author=None, published_at=None, accessed_at=None,
    )
    job.source_ids = [source.id]
    dimension = "values_desires_contradictions"
    items = [{"evidence_id": "evf_fixture", "source_ids": [source.id], "content": "sample"}]
    service = app.persona_creation
    checkpoints = service._dimension_batch_checkpoints(job, dimension)
    fingerprint = service._batch_evidence_fingerprint(items)
    checkpoints[fingerprint] = {"artifact": artifact(dimension, "evf_fixture").model_dump()}
    agent = AsyncMock(side_effect=AssertionError("completed batch must be reused"))
    monkeypatch.setattr(service, "_agent_json", agent)
    result = await service._run_dimension_extraction(
        job, dimension=dimension, evidence_items=items, participant_id=dimension, mode="full",
        source_by_id={source.id: source}, context_manager=AgentContextBudgetManager(),
    )
    assert result.source_ids == [source.id]
    assert result.memories[0].source_id == source.id
    assert result.memories[0].source_kind == "historical_inference"
    app.compilation.submit_research_artifact(job.compilation_task_id, result.model_dump())
    assert checkpoints[fingerprint]["artifact"]["memories"][0]["source_id"] == source.id
    agent.assert_not_awaited()


@pytest.mark.parametrize("reference,sources", [
    ("unknown", ["src_a"]), ("evf_fixture", ["src_a", "src_b"]),
])
def test_unresolved_source_is_never_guessed(app, reference, sources):
    value = artifact("values_desires_contradictions", reference)
    with pytest.raises(PersonaCreationError, match="artifact_source_unresolved"):
        app.persona_creation._normalize_batch_artifact_sources(
            value, [{"evidence_id": "evf_fixture", "source_ids": sources}], set(sources),
            dimension="values_desires_contradictions",
        )
    assert value.memories[0].source_id == reference


@pytest.mark.anyio
async def test_reused_dimensions_are_not_planned_or_extracted_again(app, monkeypatch):
    job = make_job(app)
    source = app.personas.add_source_text(
        job.persona_id, title="fixture", source_type="txt", content="Fixture source",
        canonical_url=None, publisher=None, author=None, published_at=None, accessed_at=None,
    )
    job.source_ids = [source.id]
    service = app.persona_creation
    reused = {dim: artifact(dim, source.id).model_dump() for dim in REQUIRED_DIMENSIONS[:-1]}
    monkeypatch.setattr(service, "_partial_reusable_dimension_artifacts", lambda _: reused)
    monkeypatch.setattr(service, "material_intelligence", None)
    pending = REQUIRED_DIMENSIONS[-1]
    extraction = AsyncMock(return_value=artifact(pending, source.id))
    monkeypatch.setattr(service, "_run_dimension_extraction", extraction)
    await service._extract_dimensions(job)
    assert extraction.await_count == 1
    assert extraction.call_args.kwargs["dimension"] == pending
    assert len(service._latest_dimension_artifacts(job)) == 8


@pytest.mark.anyio
async def test_failed_submission_does_not_mark_sources_processed(app, monkeypatch):
    job = make_job(app)
    source = app.personas.add_source_text(
        job.persona_id, title="fixture", source_type="txt", content="Fixture source",
        canonical_url=None, publisher=None, author=None, published_at=None, accessed_at=None,
    )
    job.source_ids = [source.id]
    service = app.persona_creation
    monkeypatch.setattr(service, "material_intelligence", None)

    async def extract(*args, **kwargs):
        return artifact(kwargs["dimension"], source.id)

    persist = service._persist_dimension_result
    failing = REQUIRED_DIMENSIONS[-1]

    async def fail_one(job, dimension, *args, **kwargs):
        if dimension == failing:
            raise PersonaCreationError("source_id_not_found")
        return await persist(job, dimension, *args, **kwargs)

    monkeypatch.setattr(service, "_run_dimension_extraction", extract)
    monkeypatch.setattr(service, "_persist_dimension_result", fail_one)
    await service._extract_dimensions(job)
    assert failing not in job.job_config["dimension_processed_sources"]
    assert failing not in service._latest_dimension_artifacts(job)
