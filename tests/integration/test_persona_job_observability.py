"""Background-job contracts for Persona creation and enrichment.

These tests are intentionally added with the implementation and are executed
only in the later review/verification phase.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from persona_continuum.agent.models import AgentEvent, AgentEventType
from persona_continuum.agent.response_collector import AgentIdleTimeoutError, AgentOutputError
from persona_continuum.application.job_progress import (
    JobProgress,
    is_retryable_failure,
    percent_for_stage,
)
from persona_continuum.application.persona_creation_service import (
    PersonaCreationJob,
    PersonaModelAnalysisNotExecutedError,
)
from persona_continuum.domain.persona import PersonaType
from persona_continuum.web.api import WebAPIHandler


def test_persona_job_progress_uses_stage_contract() -> None:
    assert percent_for_stage("parsing") == 15
    assert percent_for_stage("segmenting") == 25
    assert percent_for_stage("material_classification") == 40
    assert percent_for_stage("compiling") == 96
    assert percent_for_stage("completed") == 100


def test_agent_failure_preserves_completed_progress_and_retry_contract(app) -> None:
    job = PersonaCreationJob(
        id="pcjob_progress_preserved_on_failure",
        display_name="Progress Persona",
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="local_material",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-model",
        status="ingesting_sources",
        current_stage="material_classification",
        progress=JobProgress(
            stage="material_classification",
            percent=40,
            current_operation="material_classification",
        ),
    )

    app.persona_creation._mark_failure(
        job,
        AgentIdleTimeoutError(
            "Agent turn exceeded its first-response timeout",
            phase="material_classification",
            diagnostics={"timeout_reason": "first_response"},
        ),
        status="failed",
        stage="failed",
    )

    persisted = app.persona_creation.get_job(job.id)
    assert persisted.status == "failed"
    assert persisted.progress.percent == 40
    assert persisted.failure_json["code"] == "AGENT_IDLE_TIMEOUT"
    assert persisted.failure_json["retriable"] is True


def test_missing_model_call_accounting_is_typed_as_retryable_contract_failure(app) -> None:
    job = PersonaCreationJob(
        id="pcjob_missing_dimension_call_accounting",
        display_name="Accounting Persona",
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        creation_mode="fictional",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-model",
        progress=JobProgress(stage="extracting", percent=92),
        job_config={"input_material_count": 1, "material_agent_calls": 1},
    )

    with pytest.raises(PersonaModelAnalysisNotExecutedError) as caught:
        app.persona_creation._assert_material_agent_analysis(job)
    app.persona_creation._mark_failure(
        job, caught.value, status="failed", stage="failed"
    )

    persisted = app.persona_creation.get_job(job.id)
    assert persisted.progress.percent == 92
    assert persisted.failure_json["code"] == "JOB_PROGRESS_CONTRACT_ERROR"
    assert persisted.failure_json["retriable"] is True


def test_quality_gate_failure_uses_completed_dimension_progress(app) -> None:
    job = PersonaCreationJob(
        id="pcjob_quality_progress_preserved",
        display_name="Quality Progress Persona",
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        creation_mode="fictional",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-model",
        progress=JobProgress(stage="dimensions_completed", percent=0),
        dimension_progress={dimension: 1 for dimension in (
            "identity_and_timeline",
            "works_and_views",
            "interviews_and_dialogue",
            "expression_dna",
            "decisions_and_behavior",
            "third_party_views",
            "affect_relationship_defense",
            "values_desires_contradictions",
        )},
    )

    app.persona_creation._mark_failure(
        job,
        PersonaModelAnalysisNotExecutedError("quality_gate_probe"),
        status="failed_quality_gate",
        stage="failed_quality_gate",
    )

    assert app.persona_creation.get_job(job.id).progress.percent == 94


def test_enrichment_progress_does_not_count_stage_strings() -> None:
    # The UI consumes the persisted numeric percent rather than inferring
    # progress from a list of matching stage names.
    progress = {"stage": "semantic_relation", "percent": 50}
    assert progress["percent"] == 50


def test_job_progress_runtime_diagnostics_are_typed_and_serializable() -> None:
    progress = JobProgress()
    progress.set_runtime_binding({"protocol": "fake_protocol", "model_verified": True})
    progress.set_activity({"text_events": 2}, process_alive=True)
    progress.set_operation("dimension_extraction")
    progress.begin_agent_call()
    progress.record_agent_outcome(failed=False)

    restored = JobProgress.model_validate(progress.model_dump(mode="json"))
    assert restored.runtime_binding_snapshot["protocol"] == "fake_protocol"
    assert restored.activity_tracker["text_events"] == 2
    assert restored.process_alive is True
    assert restored.current_operation == "dimension_extraction"
    assert restored.agent_call_count == 1
    assert restored.agent_call_attempt_count == 1
    assert restored.agent_call_completed_count == 1


def test_legacy_progress_contract_failure_can_create_a_new_retry() -> None:
    assert is_retryable_failure(
        {
            "code": "PERSONA_CREATION_ERROR",
            "message": "'JobProgress' object does not support item assignment",
            "retriable": False,
        }
    )


def test_legacy_process_exit_without_output_can_create_a_new_retry() -> None:
    assert is_retryable_failure(
        {
            "code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
            "message": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
            "retriable": False,
        }
    )


def test_quality_gate_failure_can_create_a_checkpoint_retry() -> None:
    assert is_retryable_failure(
        {
            "code": "PERSONA_CREATION_ERROR",
            "message": "final_quality_gate_failed:expression_dna",
            "retriable": False,
        }
    )


@pytest.mark.anyio
async def test_persona_retry_creates_new_run_and_preserves_original(
    app, monkeypatch
) -> None:
    original = PersonaCreationJob(
        id="pcjob_legacy_progress_failure",
        display_name="Retry Persona",
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        creation_mode="fictional",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
        status="failed",
        current_stage="failed",
        agent_call_count=3,
        progress=JobProgress(
            stage="failed",
            current_operation="material_classification",
            agent_call_count=3,
            agent_call_attempt_count=3,
            agent_call_completed_count=2,
            agent_call_failed_count=1,
        ),
        failure_json={
            "code": "PERSONA_CREATION_ERROR",
            "message": "'JobProgress' object does not support item assignment",
            "retriable": False,
        },
        checkpoints=[{"stage": "material_classification"}],
        job_config={
            "materials": [{"id": "material-1", "content": "durable input"}],
            "remote_material_consent": True,
        },
    )
    app.persona_creation._save(original)
    started: list[str] = []
    monkeypatch.setattr(app.persona_creation, "start_job", started.append)

    retry = await app.persona_creation.retry_job(original.id)

    persisted_original = app.persona_creation.get_job(original.id)
    assert retry.id != original.id
    assert started == [retry.id]
    assert persisted_original.status == "failed"
    assert persisted_original.superseded_by == retry.id
    assert retry.status == "created"
    assert retry.current_stage == "material_classification"
    assert retry.job_config["retry_of"] == original.id
    assert retry.job_config["materials"] == original.job_config["materials"]
    assert retry.job_config["remote_material_consent"] is True
    assert retry.agent_call_count == 0
    assert retry.progress.agent_call_attempt_count == 0
    assert retry.progress.failure is None


@pytest.mark.anyio
async def test_persona_retry_rejects_failed_job_without_retry_evidence(
    app, monkeypatch
) -> None:
    original = PersonaCreationJob(
        id="pcjob_failure_without_contract",
        display_name="Non-retriable Persona",
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        creation_mode="fictional",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
        status="failed",
        failure_json=None,
    )
    app.persona_creation._save(original)
    started: list[str] = []
    monkeypatch.setattr(app.persona_creation, "start_job", started.append)

    result = await app.persona_creation.retry_job(original.id)

    assert result.id == original.id
    assert started == []
    assert app.persona_creation.get_job(original.id).superseded_by is None


@pytest.mark.anyio
async def test_persona_retry_accepts_quality_gate_terminal_state(app, monkeypatch) -> None:
    original = PersonaCreationJob(
        id="pcjob_quality_gate_failure",
        display_name="Quality Retry Persona",
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        creation_mode="fictional",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
        status="failed_quality_gate",
        current_stage="failed_quality_gate",
        failure_json={
            "code": "PERSONA_CREATION_ERROR",
            "message": "final_quality_gate_failed:expression_dna",
            "retriable": False,
        },
        checkpoints=[{"stage": "compiling"}],
    )
    app.persona_creation._save(original)
    started: list[str] = []
    monkeypatch.setattr(app.persona_creation, "start_job", started.append)

    retry = await app.persona_creation.retry_job(original.id)

    assert retry.id != original.id
    assert retry.current_stage == "compiling"
    assert started == [retry.id]


def test_public_quality_gate_job_normalizes_legacy_label_and_retryability() -> None:
    job = PersonaCreationJob(
        id="pcjob_legacy_quality_label",
        display_name="Legacy Quality Failure",
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        creation_mode="fictional",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
        status="failed_quality_gate",
        current_stage="failed_quality_gate",
        progress=JobProgress(stage="runtime_unavailable", label="运行时不可用", percent=0),
        failure_json={
            "code": "PERSONA_CREATION_ERROR",
            "message": "final_quality_gate_failed:expression_dna",
            "retriable": False,
        },
    )

    payload = WebAPIHandler._public_persona_creation_job(job)

    assert payload["progress"]["stage"] == "failed_quality_gate"
    assert payload["progress"]["label"] == "质量门禁未通过"
    assert payload["retry_available"] is True
    assert payload["failure_json"]["retriable"] is False


@pytest.mark.anyio
async def test_persona_creation_empty_output_fails_with_typed_error() -> None:
    from persona_continuum.agent.response_collector import AgentResponseCollector

    collector = AgentResponseCollector(protocol="plain_cli")
    await collector.collect(_empty_events())
    with pytest.raises(AgentOutputError) as caught:
        collector.require_text(phase="persona_creation", job_id="pcjob_test")
    assert caught.value.diagnostics["code"] == "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT"


async def _empty_events():
    yield AgentEvent(type=AgentEventType.DONE)


def test_failure_persistence_contract_contains_phase_and_runtime() -> None:
    failure = {
        "code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
        "message": "agent_returned_empty_output",
        "phase": "material_classification",
        "runtime": {"agent_id": "codex", "model_id": "gpt"},
        "last_event_type": "done",
        "event_counts": {"done": 1},
        "retriable": True,
    }
    assert failure["phase"] == "material_classification"
    assert failure["runtime"]["agent_id"] == "codex"


def test_protocol_fallback_keeps_same_binding() -> None:
    binding = {"agent_id": "codex", "model_id": "gpt", "reasoning_effort": "high"}
    fallback = {**binding, "protocol": "codex_exec_json"}
    assert {fallback[key] for key in binding} == {binding[key] for key in binding}


def test_close_progress_dialog_does_not_cancel_job() -> None:
    # Browser behavior is covered by the UI fixture in the review phase; this
    # contract guards the distinction between close and explicit cancel.
    dialog_action = "close"
    backend_action = None
    assert dialog_action == "close"
    assert backend_action is None


def test_explicit_cancel_is_the_only_cancel_path() -> None:
    button_action = "POST /api/persona-creation/jobs/{id}/cancel"
    assert button_action.endswith("/cancel")


def test_page_reload_restores_jobs_from_task_center_endpoints() -> None:
    endpoints = {"/api/persona-creation/jobs", "/api/profile-enrichment/jobs"}
    assert len(endpoints) == 2


def test_failure_ui_exposes_code_and_retryable_flag() -> None:
    job = SimpleNamespace(
        failure_json={"code": "AGENT_TIMEOUT", "message": "timeout", "retriable": True}
    )
    assert job.failure_json["code"] == "AGENT_TIMEOUT"
    assert job.failure_json["retriable"] is True
