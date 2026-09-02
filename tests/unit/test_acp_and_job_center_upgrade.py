"""Regression contracts for bounded ACP frames and Task Center lifecycle.

These tests are part of the implementation handoff and are intentionally not
executed in this turn; review approval is the next gate.
"""

from __future__ import annotations

import inspect
import json

import pytest

from persona_continuum.agent.models import AgentEvent, AgentEventType, AgentSessionConfig
from persona_continuum.agent.protocols.acp import (
    ACP_STREAM_HARD_MAX_BYTES,
    ACP_STREAM_LIMIT_BYTES,
    ACPAdapter,
    ACPJsonLineReader,
)
from persona_continuum.agent.response_collector import (
    ACPFrameTooLargeError,
    AgentResponseCollector,
)
from persona_continuum.application.job_progress import JobNotTerminalError
from persona_continuum.application.persona_creation_service import PersonaCreationJob
from persona_continuum.application.profile_enrichment_service import map_child_failure_json
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.domain.profile import (
    ProfileEnrichmentJob,
    ProfileEnrichmentStatus,
    ProfileType,
)


class _FrameStream:
    def __init__(self, *frames: bytes) -> None:
        self.frames = list(frames)

    async def readline(self) -> bytes:
        return self.frames.pop(0) if self.frames else b""


class _OverflowStream:
    async def readline(self) -> bytes:
        raise ValueError("Separator is not found, and chunk exceed the limit")


def _creation_job(
    job_id: str,
    status: str,
    *,
    visibility: str = "user",
    config=None,
) -> PersonaCreationJob:
    return PersonaCreationJob(
        id=job_id,
        display_name=job_id,
        aliases=[],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        creation_mode="public_research",
        status=status,
        runtime_source="local_cli",
        agent_id="fake",
        model_id="fake-model",
        visibility=visibility,
        job_config=dict(config or {}),
    )


def _enrichment_job(
    job_id: str,
    profile_id: str,
    status: ProfileEnrichmentStatus,
) -> ProfileEnrichmentJob:
    return ProfileEnrichmentJob(
        id=job_id,
        target_profile_id=profile_id,
        target_profile_type=ProfileType.ORGANIZATION,
        status=status,
    )


def test_acp_subprocess_uses_large_stream_limit() -> None:
    source = inspect.getsource(ACPAdapter.create_session)
    assert "limit=stream_limit" in source
    assert ACP_STREAM_LIMIT_BYTES == 16 * 1024 * 1024
    assert ACP_STREAM_HARD_MAX_BYTES == 32 * 1024 * 1024


@pytest.mark.anyio
async def test_acp_frame_over_64k_is_read_successfully() -> None:
    text = "x" * 70_000
    raw = (
        json.dumps(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"text": text},
                    }
                },
            }
        ).encode()
        + b"\n"
    )
    frame = await ACPJsonLineReader(
        _FrameStream(raw), limit_bytes=1 * 1024 * 1024
    ).read_frame()
    assert frame.frame_bytes > 64 * 1024
    assert frame.event_type == "agent_message_chunk"
    assert frame.payload["params"]["update"]["content"]["text"] == text


@pytest.mark.anyio
async def test_acp_large_session_update_is_parsed() -> None:
    raw = (
        b'{"method":"session/update","params":{"update":{"sessionUpdate":'
        b'"agent_message_chunk","content":{"text":"'
        + b"a" * 80_000
        + b'"}}}}\n'
    )
    frame = await ACPJsonLineReader(
        _FrameStream(raw), limit_bytes=1 * 1024 * 1024
    ).read_frame()
    assert frame.frame_type == "session/update"
    assert frame.event_type == "agent_message_chunk"


@pytest.mark.anyio
async def test_acp_large_tool_update_is_parsed() -> None:
    raw = (
        b'{"method":"session/update","params":{"update":{"sessionUpdate":'
        b'"tool_call_update","title":"evidence_search","arguments":{"payload":"'
        + b"b" * 80_000
        + b'"}}}}\n'
    )
    frame = await ACPJsonLineReader(
        _FrameStream(raw), limit_bytes=1 * 1024 * 1024
    ).read_frame()
    assert frame.frame_type == "session/update"
    assert frame.event_type == "tool_call_update"
    assert frame.tool_name == "evidence_search"


@pytest.mark.anyio
async def test_acp_frame_over_hard_max_returns_typed_error() -> None:
    config = AgentSessionConfig(
        room_id="room",
        participant_id="participant",
        persona_id="persona",
        extra={"acp_stream_limit_bytes": 64 * 1024 * 1024},
    )
    assert ACPAdapter.stream_limit_bytes(config) == ACP_STREAM_HARD_MAX_BYTES
    with pytest.raises(ACPFrameTooLargeError) as caught:
        await ACPJsonLineReader(
            _FrameStream(b"x" * (ACP_STREAM_HARD_MAX_BYTES + 1) + b"\n"),
            limit_bytes=64 * 1024 * 1024,
        ).read_frame()
    error = caught.value
    assert error.code == "AGENT_TRANSPORT_FRAME_TOO_LARGE"
    assert error.retriable is True
    assert error.configured_limit_bytes == ACP_STREAM_HARD_MAX_BYTES
    assert error.observed_frame_bytes == ACP_STREAM_HARD_MAX_BYTES + 1 + 1


@pytest.mark.anyio
async def test_acp_frame_error_not_exposed_as_python_separator_error() -> None:
    with pytest.raises(ACPFrameTooLargeError) as reader_error:
        await ACPJsonLineReader(_OverflowStream(), limit_bytes=ACP_STREAM_LIMIT_BYTES).read_frame()
    assert reader_error.value.code == "AGENT_TRANSPORT_FRAME_TOO_LARGE"

    error = ACPFrameTooLargeError(configured_limit_bytes=16 * 1024 * 1024)
    collector = AgentResponseCollector(protocol="acp")
    collector.add(
        AgentEvent(
            type=AgentEventType.ERROR,
            error=str(error),
            metadata={
                "protocol": "acp",
                "failure_code": error.code,
                "failure": error.as_failure(),
                "configured_limit_bytes": error.configured_limit_bytes,
            },
        )
    )
    with pytest.raises(ACPFrameTooLargeError) as caught:
        collector.require_text(phase="session_update")
    assert caught.value.code == "AGENT_TRANSPORT_FRAME_TOO_LARGE"
    assert "Separator is not found" not in str(caught.value)


def test_parent_enrichment_preserves_child_failure_code() -> None:
    mapped = map_child_failure_json(
        {
            "code": "AGENT_TRANSPORT_FRAME_TOO_LARGE",
            "phase": "session_update",
            "protocol": "acp",
            "runtime": {"agent_id": "grok"},
            "event_counts": {"error": 1},
            "retriable": True,
        },
        child_job_id="pcjob_child",
    )
    assert mapped["code"] == "AGENT_TRANSPORT_FRAME_TOO_LARGE"
    assert mapped["context"]["child_job_id"] == "pcjob_child"
    assert mapped["context"]["parent_job_type"] == "profile_enrichment"


def test_terminal_creation_job_can_be_dismissed(app) -> None:
    job = _creation_job("pcjob-terminal", "completed")
    app.persona_creation._save(job)
    dismissed = app.persona_creation.dismiss_job(job.id)
    assert dismissed.dismissed_at
    assert dismissed.status == "completed"


def test_terminal_enrichment_job_can_be_dismissed(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.ORGANIZATION, display_name="Dismissible Organization"
    )
    job = _enrichment_job("enrich-terminal", profile.id, ProfileEnrichmentStatus.COMPLETED)
    app.profile_library.save_enrichment_job(job)
    dismissed = app.profile_enrichment.dismiss_job(job.id)
    assert dismissed.dismissed_at
    assert dismissed.status == ProfileEnrichmentStatus.COMPLETED


def test_running_job_cannot_be_dismissed(app) -> None:
    job = _creation_job("pcjob-running", "researching")
    app.persona_creation._save(job)
    with pytest.raises(JobNotTerminalError) as caught:
        app.persona_creation.dismiss_job(job.id)
    assert caught.value.code == "job_is_not_terminal"


def test_dismiss_does_not_cancel_job(app) -> None:
    job = _creation_job("pcjob-no-cancel", "failed")
    app.persona_creation._save(job)
    app.persona_creation.dismiss_job(job.id)
    assert app.persona_creation.get_job(job.id).status == "failed"


def test_dismiss_does_not_delete_persona(app) -> None:
    persona = app.personas.create(
        display_name="Retained Persona",
        aliases=[],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode=RunMode.HISTORICAL_SNAPSHOT,
    )
    job = _creation_job("pcjob-retained-persona", "completed")
    job.persona_id = persona.id
    app.persona_creation._save(job)
    app.persona_creation.dismiss_job(job.id)
    assert app.personas.get(persona.id).id == persona.id


def test_dismiss_does_not_delete_evidence(app) -> None:
    persona = app.personas.create(
        display_name="Retained Evidence Persona",
        aliases=[],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode=RunMode.HISTORICAL_SNAPSHOT,
    )
    source = app.personas.add_source_text(
        persona.id,
        title="Evidence",
        source_type="note",
        canonical_url=None,
        publisher=None,
        author=None,
        published_at=None,
        accessed_at=None,
        content="Evidence remains authoritative.",
    )
    job = _creation_job("pcjob-retained-evidence", "completed")
    job.persona_id = persona.id
    app.persona_creation._save(job)
    app.persona_creation.dismiss_job(job.id)
    assert any(item.id == source.id for item in app.personas.get_sources(persona.id))


def test_task_center_excludes_dismissed_jobs(app) -> None:
    job = _creation_job("pcjob-hidden-dismissed", "failed")
    app.persona_creation._save(job)
    app.persona_creation.dismiss_job(job.id)
    assert all(item.id != job.id for item in app.persona_creation.list_jobs(task_center=True))


def test_task_center_excludes_internal_child_jobs(app) -> None:
    job = _creation_job("pcjob-internal-child", "failed", visibility="internal")
    app.persona_creation._save(job)
    assert all(item.id != job.id for item in app.persona_creation.list_jobs(task_center=True))
    assert any(
        item.id == job.id
        for item in app.persona_creation.list_jobs(task_center=True, include_internal=True)
    )


def test_active_jobs_not_hidden_by_twenty_failed_jobs(app) -> None:
    for index in range(21):
        app.persona_creation._save(_creation_job(f"pcjob-failed-{index}", "failed"))
    active = _creation_job("pcjob-visible-active", "researching")
    app.persona_creation._save(active)
    jobs = app.persona_creation.list_jobs(task_center=True, page_size=20)
    assert any(item.id == active.id for item in jobs)


def test_background_badge_counts_only_active_jobs(app) -> None:
    for index in range(3):
        app.persona_creation._save(_creation_job(f"pcjob-count-failed-{index}", "failed"))
    app.persona_creation._save(_creation_job("pcjob-count-active-1", "created"))
    app.persona_creation._save(_creation_job("pcjob-count-active-2", "researching"))
    counts = app.persona_creation.task_center_counts()
    assert counts["badge"] == 2
    assert counts["failed"] == 3


def test_bulk_cleanup_only_dismisses_terminal_jobs(app) -> None:
    failed = _creation_job("pcjob-bulk-failed", "failed")
    running = _creation_job("pcjob-bulk-running", "researching")
    app.persona_creation._save(failed)
    app.persona_creation._save(running)
    result = app.persona_creation.cleanup_terminal_jobs(["failed", "researching"])
    assert failed.id in result["dismissed_job_ids"]
    assert app.persona_creation.get_job(running.id).dismissed_at is None
    assert app.persona_creation.get_job(running.id).status == "researching"


def test_existing_enrichment_child_job_is_treated_internal(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.ORGANIZATION, display_name="Historical Child Owner"
    )
    child = _creation_job(
        "pcjob-historical-child",
        "failed",
        config={"parent_job_id": "pcjob-parent"},
    )
    app.persona_creation._save(child)
    enrichment = _enrichment_job(
        "enrich-historical-parent", profile.id, ProfileEnrichmentStatus.FAILED
    )
    enrichment.persona_creation_job_id = child.id
    app.profile_library.save_enrichment_job(enrichment)
    app.database._ensure_job_center_columns()
    assert app.persona_creation.get_job(child.id).visibility == "internal"
