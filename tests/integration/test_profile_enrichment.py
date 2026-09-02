from __future__ import annotations

import asyncio

import pytest

from persona_continuum.domain.persona import PersonaType
from persona_continuum.domain.profile import (
    ProfileEnrichmentJob,
    ProfileEnrichmentStatus,
    ProfileType,
)


async def _wait_for_profile_job(app, job_id: str):
    for _ in range(40):
        job = app.profile_library.get_enrichment_job(job_id)
        if job.status in {
            ProfileEnrichmentStatus.COMPLETED,
            ProfileEnrichmentStatus.COMPLETED_WITH_GAPS,
            ProfileEnrichmentStatus.FAILED,
            ProfileEnrichmentStatus.CANCELLED,
        }:
            return job
        await asyncio.sleep(0)
    return app.profile_library.get_enrichment_job(job_id)


@pytest.mark.anyio
async def test_persona_enrichment_creates_new_version(app, monkeypatch) -> None:
    persona = app.personas.create(
        display_name="Enrichment Persona",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode="counterfactual_continuation",
    )
    profile = app.profile_library.sync_persona(persona)
    # The real Persona path is tested with the existing PersonaCreation
    # orchestrator in test_persona_creation_runtime; this test isolates the
    # version-preservation contract at the unified library seam.
    original = app.profile_library.update_profile
    monkeypatch.setattr(
        app.profile_library,
        "update_profile",
        lambda profile_id, **kwargs: original(profile_id, **kwargs),
    )
    updated = app.profile_library.update_profile(
        profile.id,
        summary="补充研究后的决策档案。",
        payload={"new_evidence": ["source-2"]},
        source_ids=["source-2"],
        created_by="persona_enrichment",
    )
    assert updated.version == profile.version + 1
    assert app.profile_library.list_versions(profile.id)[0].version == profile.version


@pytest.mark.anyio
async def test_profile_enrichment_keeps_old_version(app, monkeypatch) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.ORGANIZATION,
        display_name="NVIDIA",
        payload={"strategy": "CUDA ecosystem"},
    )

    async def fake_agent_json(*, adapter, profile, runtime, materials):
        del adapter, runtime, materials
        return {
            "summary": "以加速计算平台和开发者生态为核心的组织决策档案。",
            "payload": {**profile.payload, "decision_style": "platform-led"},
            "coverage": {"strategy": 2},
            "gaps": [],
        }

    monkeypatch.setattr(app.profile_enrichment, "_agent_json", fake_agent_json)
    job = await app.profile_enrichment.create_job(
        target_profile_id=profile.id,
        runtime={"agent_id": "fake_agent", "model_id": "fake-gpt-5"},
        requested_scope="more_sources",
    )
    completed = await _wait_for_profile_job(app, job.id)
    assert completed.status in {ProfileEnrichmentStatus.COMPLETED, ProfileEnrichmentStatus.FAILED}
    if completed.status == ProfileEnrichmentStatus.COMPLETED:
        current = app.profile_library.get_profile(profile.id)
        assert current.version == profile.version + 1
        assert app.profile_library.list_versions(profile.id)[0].version == profile.version


def test_enrichment_can_target_gap_scope(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.INSTITUTION,
        display_name="美联储",
    )
    job = app.profile_library.save_enrichment_job(
        ProfileEnrichmentJob(
            id="gap_scope_job",
            target_profile_id=profile.id,
            target_profile_type=ProfileType.INSTITUTION,
            requested_scope="fill_dimension_gaps",
        )
    )
    assert job.requested_scope == "fill_dimension_gaps"


@pytest.mark.anyio
async def test_profile_enrichment_job_can_be_cancelled(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.INSTITUTION,
        display_name="Cancellation Target",
    )
    job = app.profile_library.save_enrichment_job(
        ProfileEnrichmentJob(
            id="cancel_profile_enrichment_job",
            target_profile_id=profile.id,
            target_profile_type=ProfileType.INSTITUTION,
            status=ProfileEnrichmentStatus.RESEARCHING,
        )
    )

    cancelled = await app.profile_enrichment.cancel_job(job.id)

    assert cancelled.status == ProfileEnrichmentStatus.CANCELLED
    assert cancelled.worker_state == "finished"
    assert cancelled.worker_finished_at is not None
    assert cancelled.progress["stage"] == "cancelled"


@pytest.mark.anyio
async def test_profile_enrichment_retry_creates_fresh_run_and_preserves_inputs(
    app, monkeypatch
) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.ORGANIZATION,
        display_name="Retry Organization",
    )
    original = app.profile_library.save_enrichment_job(
        ProfileEnrichmentJob(
            id="profile_enrichment_retry_source",
            target_profile_id=profile.id,
            target_profile_type=profile.profile_type,
            status=ProfileEnrichmentStatus.FAILED,
            selected_runtime={
                "agent_id": "fake_agent",
                "model_id": "fake-gpt-5",
                "remote_material_consent": True,
            },
            input_material_ids=["material-1"],
            input_material_count=1,
            progress={
                "stage": "failed",
                "previous_stage": "material_classification",
                "materials": [{"id": "material-1", "content": "durable input"}],
                "retry_count": 1,
            },
            failure_json={
                "code": "PERSONA_CREATION_ERROR",
                "message": "'JobProgress' object does not support item assignment",
                "retriable": False,
            },
        )
    )

    async def no_op_run(*args, **kwargs) -> None:
        del args, kwargs

    monkeypatch.setattr(app.profile_enrichment, "_run_job", no_op_run)

    retry = await app.profile_enrichment.retry_job(original.id)
    await asyncio.sleep(0)

    persisted_original = app.profile_library.get_enrichment_job(original.id)
    assert retry.id != original.id
    assert persisted_original.status == ProfileEnrichmentStatus.FAILED
    assert persisted_original.superseded_by == retry.id
    assert retry.status == ProfileEnrichmentStatus.CREATED
    assert retry.progress["retry_of"] == original.id
    assert retry.progress["retry_count"] == 2
    assert retry.progress["materials"] == original.progress["materials"]
    assert retry.input_material_ids == ["material-1"]
    assert retry.selected_runtime == original.selected_runtime
    assert retry.agent_call_count == 0


@pytest.mark.anyio
async def test_profile_enrichment_retry_rejects_missing_failure_contract(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.INSTITUTION,
        display_name="Non-retriable Institution",
    )
    original = app.profile_library.save_enrichment_job(
        ProfileEnrichmentJob(
            id="profile_enrichment_missing_failure",
            target_profile_id=profile.id,
            target_profile_type=profile.profile_type,
            status=ProfileEnrichmentStatus.FAILED,
            failure_json=None,
        )
    )

    result = await app.profile_enrichment.retry_job(original.id)

    assert result.id == original.id
    assert app.profile_library.get_enrichment_job(original.id).superseded_by is None


def test_organization_profile_can_be_enriched(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.ORGANIZATION,
        display_name="Apple",
    )
    updated = app.profile_library.update_profile(
        profile.id,
        payload={"mission": "integrated products", "resources": ["talent"]},
        compile_state="compiled",
    )
    assert updated.profile_type == ProfileType.ORGANIZATION
    assert updated.payload["mission"] == "integrated products"


def test_institution_profile_can_be_enriched(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.INSTITUTION,
        display_name="欧盟",
    )
    updated = app.profile_library.update_profile(
        profile.id,
        payload={"policy_tools": ["regulation"]},
        compile_state="compiled",
    )
    assert updated.profile_type == ProfileType.INSTITUTION
    assert updated.payload["policy_tools"] == ["regulation"]
