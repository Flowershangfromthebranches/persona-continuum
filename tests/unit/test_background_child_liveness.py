"""Written parent/child worker-liveness contracts.

No worker or real Agent is started here.  The tests exercise persisted model
mapping and a fake child-worker registry; execution is deferred until review.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from persona_continuum.application.persona_creation_service import PersonaCreationJob
from persona_continuum.application.profile_enrichment_service import (
    ChildJobWorkerLostError,
    ProfileEnrichmentService,
)
from persona_continuum.domain.persona import PersonaType
from persona_continuum.domain.profile import (
    ProfileEnrichmentJob,
    ProfileEnrichmentStatus,
    ProfileType,
)


def _child(job_id: str = "child-job") -> PersonaCreationJob:
    job = PersonaCreationJob(
        id=job_id,
        display_name="Child",
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        creation_mode="public_research",
        runtime_source="local_cli",
        agent_id="fake_agent",
        model_id="fake-model",
        status="researching",
        current_stage="dimension_extraction",
    )
    job.touch_worker("running")
    return job


def _parent(job_id: str = "parent-job") -> ProfileEnrichmentJob:
    return ProfileEnrichmentJob(
        id=job_id,
        target_profile_id="profile-1",
        target_profile_type=ProfileType.PERSONA,
        status=ProfileEnrichmentStatus.RESEARCHING,
    )


def _service_for_fake_child(resume_job) -> ProfileEnrichmentService:
    service = ProfileEnrichmentService.__new__(ProfileEnrichmentService)
    service.continuum = SimpleNamespace(
        persona_creation=SimpleNamespace(_tasks={}, resume_job=resume_job)
    )
    service.profiles = SimpleNamespace(save_enrichment_job=lambda job: job)
    return service


def test_profile_parent_copies_child_percent() -> None:
    service = ProfileEnrichmentService.__new__(ProfileEnrichmentService)
    parent = _parent()
    child = _child()
    child.progress.update_stage("extracting", percent=50)
    service._update_parent_child_progress(parent, child)
    assert parent.progress["child_percent_mapping"]["child_percent"] == 50
    assert parent.progress["percent"] == 52


def test_profile_parent_copies_child_operation() -> None:
    service = ProfileEnrichmentService.__new__(ProfileEnrichmentService)
    parent = _parent()
    child = _child()
    child.progress.update_stage("extracting", label="Extracting", percent=25)
    child.progress.current_operation = "dimension_extraction"
    service._update_parent_child_progress(parent, child)
    assert parent.progress["stage"] == "extracting"
    assert parent.progress["label"] == "Extracting"
    assert parent.progress["current_operation"] == "dimension_extraction"


def test_parent_progress_moves_past_8_percent() -> None:
    service = ProfileEnrichmentService.__new__(ProfileEnrichmentService)
    parent = _parent()
    child = _child()
    child.progress.update_stage("material_classification", percent=40)
    service._update_parent_child_progress(parent, child)
    assert parent.progress["percent"] > 8


def test_parent_progress_maps_child_monotonically() -> None:
    service = ProfileEnrichmentService.__new__(ProfileEnrichmentService)
    parent = _parent()
    child = _child()
    child.progress.update_stage("extracting", percent=80)
    service._update_parent_child_progress(parent, child)
    first = parent.progress["percent"]
    child.progress.update_stage("researching", percent=10)
    service._update_parent_child_progress(parent, child)
    assert parent.progress["percent"] >= first


def test_child_heartbeat_visible_in_parent() -> None:
    service = ProfileEnrichmentService.__new__(ProfileEnrichmentService)
    parent = _parent()
    child = _child()
    child.worker_started_at = "2026-08-26T00:00:00+00:00"
    child.worker_heartbeat_at = "2026-08-26T00:00:05+00:00"
    child.agent_call_count = 3
    child.progress.update_stage("extracting", percent=25)
    service._update_parent_child_progress(parent, child)
    snapshot = parent.progress["child_snapshot"]
    assert snapshot["worker_heartbeat_at"] == child.worker_heartbeat_at
    assert snapshot["agent_call_count"] == 3
    assert parent.progress["child_job_id"] == child.id


def test_enrichment_agent_call_count_visible() -> None:
    job = _parent()
    job.agent_call_count = 4
    job.touch_worker("waiting_agent")
    assert job.worker_snapshot()["agent_call_count"] == 4
    assert job.progress["agent_call_count"] == 4


@pytest.mark.anyio
async def test_child_worker_loss_is_detected() -> None:
    resumed: list[str] = []

    async def resume_job(job_id: str) -> None:
        resumed.append(job_id)

    service = _service_for_fake_child(resume_job)
    parent = _parent()
    child = _child()
    await service._ensure_child_worker(parent, child)
    assert resumed == [child.id]
    with pytest.raises(ChildJobWorkerLostError) as caught:
        await service._ensure_child_worker(parent, child)
    assert caught.value.code == "CHILD_JOB_WORKER_LOST"


@pytest.mark.anyio
async def test_child_worker_heartbeat_and_bounded_restart() -> None:
    resumed: list[str] = []

    async def resume_job(job_id: str) -> None:
        resumed.append(job_id)

    service = _service_for_fake_child(resume_job)
    parent = _parent()
    child = _child()
    await service._ensure_child_worker(parent, child)
    with pytest.raises(ChildJobWorkerLostError):
        await service._ensure_child_worker(parent, child)
    assert resumed == [child.id]
    assert parent.progress["child_worker_state"] == "lost"


def test_child_worker_loss_does_not_poll_forever() -> None:
    source = inspect.getsource(ProfileEnrichmentService._ensure_child_worker)
    assert "child_worker_restart_attempted" in source
    assert "ChildJobWorkerLostError" in source
    assert "resume_job" in source


def test_child_watchdog_records_heartbeat_age() -> None:
    source = inspect.getsource(ProfileEnrichmentService._ensure_child_worker)
    assert "child_worker_heartbeat_age_seconds" in source
    assert "STALE_WORKER_HEARTBEAT_SECONDS" in inspect.getsource(ProfileEnrichmentService)


def test_parent_child_snapshot_contains_runtime_diagnostics() -> None:
    service = ProfileEnrichmentService.__new__(ProfileEnrichmentService)
    parent = _parent()
    child = _child()
    child.agent_call_audits = [
        {"runtime_diagnostics": {"last_activity_at": "2026-08-26T00:00:01+00:00"}}
    ]
    service._update_parent_child_progress(parent, child)
    assert parent.progress["child_snapshot"]["runtime_diagnostics"]["last_activity_at"]
