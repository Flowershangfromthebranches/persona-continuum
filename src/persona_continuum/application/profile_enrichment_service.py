from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from persona_continuum.agent.adapter import AgentAdapter
from persona_continuum.agent.models import (
    AgentSessionConfig,
    PermissionProfile,
)
from persona_continuum.agent.response_collector import (
    AgentRuntimeError,
    AgentSessionStartError,
    RuntimeUnavailableError,
)
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
from persona_continuum.agent.structured_output import StructuredOutputSchemaError, StructuredResult
from persona_continuum.application._utils import new_id
from persona_continuum.application.job_progress import (
    JobFailure,
    is_retryable_failure,
    percent_for_stage,
)
from persona_continuum.application.profile_library_service import ProfileLibraryService
from persona_continuum.domain.persona import PersonaType
from persona_continuum.domain.profile import (
    ActorProfile,
    EnrichmentInputMode,
    ProfileEnrichmentJob,
    ProfileEnrichmentStatus,
    ProfileStatus,
    ProfileType,
    profile_now,
)
from persona_continuum.numeric import (
    InvalidNumericFieldError,
    safe_acp_stream_limit,
    safe_int,
    safe_timeout,
)

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum


PROFILE_ENRICHMENT_SYSTEM_PROMPT = """You are Persona Continuum's Profile Enrichment Agent.
Upgrade an existing decision profile from supplied evidence and prior profile state.
Return JSON only with {"summary":"...","payload":{...},"coverage":{...},"gaps":[]}.
Do not invent facts, do not create a Persona package, and keep claims tied to the
provided evidence. For organization, institution, and collective profiles, use
the type-specific fields requested by the caller.
"""


class ProfileEnrichmentError(RuntimeError):
    pass


class ChildJobWorkerLostError(ProfileEnrichmentError):
    """A non-terminal Persona child lost its worker after one restart attempt."""

    code = "CHILD_JOB_WORKER_LOST"

    def __init__(self, child_job_id: str) -> None:
        self.child_job_id = str(child_job_id)
        super().__init__(f"{self.code}:{self.child_job_id}")


def map_child_failure_json(
    failure_json: dict[str, Any], *, child_job_id: str, parent_job_type: str = "profile_enrichment"
) -> dict[str, Any]:
    """Copy a child root failure and add lineage context without changing its code."""

    failure = dict(failure_json)
    context = dict(failure.get("context") or {})
    context.update(
        {
            "parent_job_type": parent_job_type,
            "child_job_id": str(child_job_id),
            "child_failure_code": str(failure.get("code") or "AGENT_RUNTIME_ERROR"),
        }
    )
    failure["child_job_id"] = str(child_job_id)
    failure["context"] = context
    return failure


class ProfileEnrichmentService:
    """Actor Completion/Enrichment runtime for every unified profile type."""

    WORKER_HEARTBEAT_INTERVAL_SECONDS = 5.0
    STALE_WORKER_HEARTBEAT_SECONDS = 30.0

    TERMINAL = {
        ProfileEnrichmentStatus.COMPLETED,
        ProfileEnrichmentStatus.COMPLETED_WITH_GAPS,
        ProfileEnrichmentStatus.FAILED,
        ProfileEnrichmentStatus.CANCELLED,
    }

    def __init__(self, continuum: PersonaContinuum, profiles: ProfileLibraryService) -> None:
        self.continuum = continuum
        self.profiles = profiles
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._active_bindings: dict[str, Any] = {}
        self._last_agent_audit: dict[str, Any] | None = None
        self._last_agent_audits: list[dict[str, Any]] = []
        self._last_runtime_binding_snapshot: dict[str, Any] | None = None
        self._last_runtime_activity: dict[str, dict[str, Any]] = {}
        self._last_process_alive: dict[str, bool | None] = {}
        self.runtime_executor = (
            getattr(continuum, "agent_runtime_executor", None) or AgentRuntimeExecutor()
        )

    @staticmethod
    def _worker_state_for_status(status: str) -> str:
        normalized = str(status or "").lower()
        if normalized in {"completed", "completed_with_gaps", "cancelled"}:
            return "finished"
        if normalized == "failed":
            return "failed"
        if normalized in {"paused", "paused_runtime_unavailable"}:
            return "paused"
        if normalized in {"waiting_for_materials", "waiting_io"}:
            return "waiting_io"
        if normalized in {"created", "queued"}:
            return "starting"
        return "running"

    @staticmethod
    def _worker_state_value(value: Any) -> str:
        """Read enum-backed and legacy string child worker states alike."""

        return str(getattr(value, "value", value) or "starting")

    @classmethod
    def _heartbeat_age_seconds(cls, value: Any) -> float | None:
        if not value:
            return None
        try:
            if isinstance(value, (int, float)):
                return max(0.0, time.time() - float(value))
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return max(0.0, time.time() - timestamp.timestamp())
        except (TypeError, ValueError, OverflowError):
            return None

    def _touch_worker(
        self,
        job: ProfileEnrichmentJob,
        state: str | None = None,
        *,
        finished: bool = False,
    ) -> None:
        effective = state or self._worker_state_for_status(job.status.value)
        job.touch_worker(effective, finished_at=profile_now() if finished else None)
        self.profiles.save_enrichment_job(job)

    def _capture_runtime_activity(self, profile_id: str, binding: Any) -> None:
        """Keep the latest session activity for the next durable job save."""

        process_alive = (
            self.runtime_executor._process_alive(binding.session)
            if hasattr(self.runtime_executor, "_process_alive")
            else None
        )
        self._last_runtime_activity[str(profile_id)] = dict(
            binding.session.activity_tracker.as_diagnostics()
        )
        self._last_process_alive[str(profile_id)] = process_alive

    def _apply_runtime_activity(self, job: ProfileEnrichmentJob) -> None:
        profile_id = str(job.target_profile_id)
        activity = self._last_runtime_activity.get(profile_id)
        if activity is not None:
            job.progress["activity_tracker"] = dict(activity)
            job.progress["process_alive"] = self._last_process_alive.get(profile_id)

    async def _worker_heartbeat_loop(self, job_id: str, state: str = "waiting_agent") -> None:
        """Keep parent liveness durable while a structured Agent call is pending."""

        while True:
            await asyncio.sleep(self.WORKER_HEARTBEAT_INTERVAL_SECONDS)
            try:
                current = self.profiles.get_enrichment_job(job_id)
            except Exception:
                return
            if current.status in self.TERMINAL or current.status in {
                ProfileEnrichmentStatus.PAUSED,
                ProfileEnrichmentStatus.PAUSED_RUNTIME_UNAVAILABLE,
            }:
                return
            binding = self._active_bindings.get(current.target_profile_id)
            if binding is not None:
                self._capture_runtime_activity(current.target_profile_id, binding)
                self._apply_runtime_activity(current)
            current.touch_worker(state)
            self.profiles.save_enrichment_job(current)

    def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._active_bindings.clear()

    async def resume_pending_jobs(self) -> list[str]:
        """Reattach workers to persisted jobs after a process restart.

        Explicitly paused/cancelled jobs remain paused; only jobs interrupted
        while actively researching or compiling are resumed automatically.
        """
        rows = self.continuum.database.conn.execute(
            "SELECT id FROM profile_enrichment_jobs WHERE status IN (?, ?, ?, ?)",
            (
                ProfileEnrichmentStatus.CREATED.value,
                ProfileEnrichmentStatus.RESEARCHING.value,
                ProfileEnrichmentStatus.INGESTING.value,
                ProfileEnrichmentStatus.COMPILING.value,
            ),
        ).fetchall()
        resumed: list[str] = []
        for row in rows:
            job_id = str(row["id"])
            if job_id in self._tasks and not self._tasks[job_id].done():
                continue
            self._tasks[job_id] = asyncio.create_task(
                self._run_job(job_id, [], False), name=f"profile-enrichment-{job_id}"
            )
            resumed.append(job_id)
        return resumed

    async def create_job(
        self,
        *,
        target_profile_id: str,
        job_type: str = "upgrade",
        requested_scope: str = "full_refresh",
        runtime: dict[str, Any] | None = None,
        research_policy: dict[str, Any] | None = None,
        materials: list[dict[str, Any]] | None = None,
        remote_material_consent: bool = False,
        enrichment_input_mode: str | EnrichmentInputMode | None = None,
        research_focus: str | None = None,
        visibility: str = "user",
    ) -> ProfileEnrichmentJob:
        profile = self.profiles.get_profile(target_profile_id)
        runtime = dict(runtime or {})
        runtime.setdefault("runtime_source", "local_cli")
        for key in ("turn_timeout_seconds", "acp_stream_limit_bytes"):
            raw_value = runtime.get(key)
            if raw_value is None:
                runtime.pop(key, None)
            elif key == "turn_timeout_seconds":
                runtime[key] = safe_timeout(raw_value)
            else:
                runtime[key] = safe_acp_stream_limit(raw_value)
        adapter_id = str(runtime.get("agent_id") or "")
        if not adapter_id:
            raise ProfileEnrichmentError("profile_enrichment_runtime_required")
        if self.continuum.agent_registry.get_adapter(adapter_id) is None:
            raise ProfileEnrichmentError(f"profile_enrichment_agent_not_found:{adapter_id}")
        normalized_source = str(runtime.get("runtime_source") or "local_cli").lower()
        is_remote = normalized_source in {"api", "provider", "remote", "api_provider"}
        if materials and is_remote and not remote_material_consent:
            raise ProfileEnrichmentError("remote_material_consent_required")
        material_list = list(materials or [])
        mode = (
            enrichment_input_mode
            if isinstance(enrichment_input_mode, EnrichmentInputMode)
            else EnrichmentInputMode(str(enrichment_input_mode or "local_materials"))
        )
        if mode in {EnrichmentInputMode.WEB_RESEARCH, EnrichmentInputMode.HYBRID}:
            try:
                await self.continuum.persona_creation.validate_research_runtime(
                    runtime, input_mode=mode.value
                )
            except Exception as exc:
                raise ProfileEnrichmentError(str(exc)) from exc
        if mode == EnrichmentInputMode.HYBRID and not material_list:
            raise ProfileEnrichmentError("hybrid_requires_local_materials")
        # Persist the consent decision with the runtime snapshot so a process
        # restart can resume a queued job without silently losing the user's
        # explicit privacy choice.
        runtime["remote_material_consent"] = bool(remote_material_consent)
        job = ProfileEnrichmentJob(
            id=new_id("profile_enrich"),
            target_profile_id=profile.id,
            target_profile_type=profile.profile_type,
            job_type=job_type,
            visibility="internal" if str(visibility).lower() == "internal" else "user",
            selected_runtime=runtime,
            research_policy=dict(research_policy or {}),
            requested_scope=requested_scope,
            enrichment_input_mode=mode,
            research_focus=(
                str(research_focus).strip() if str(research_focus or "").strip() else None
            ),
            input_material_ids=[
                str(item.get("id"))
                for item in material_list
                if isinstance(item, dict) and item.get("id")
            ],
            input_material_count=len(material_list),
            progress={
                "stage": "created",
                "label": "任务已创建",
                "percent": 2,
                "indeterminate": False,
                "materials": material_list,
                "events": [],
                "agent_calls": {"material": 0, "dimension": 0},
            },
        )
        job.touch_worker("starting", heartbeat_at=job.created_at)
        self._event(job, "profile_enrichment_started", mode=mode.value)
        self._event(job, "materials_attached", material_count=len(material_list))
        self.profiles.save_enrichment_job(job)
        self._tasks[job.id] = asyncio.create_task(
            self._run_job(job.id, material_list, remote_material_consent),
            name=f"profile-enrichment-{job.id}",
        )
        return self.profiles.get_enrichment_job(job.id)

    @staticmethod
    def _event(job: ProfileEnrichmentJob, event: str, **payload: Any) -> None:
        stage_map = {
            "profile_enrichment_started": "queued",
            "materials_attached": "ingesting_sources",
            "material_parsing_started": "parsing",
            "material_segmentation_completed": "segmenting",
            "material_agent_classification_completed": "material_classification",
            "semantic_relation_completed": "semantic_relation",
            "evidence_fusion_completed": "evidence_fusion",
            "dimension_extraction_started": "extracting",
            "dimension_extraction_completed": "extracting",
            "persona_compilation_started": "compiling",
            "persona_compilation_completed": "summary",
            "profile_version_created": "summary",
            "profile_enrichment_failed": "failed",
        }
        stage = str(
            payload.get("stage") or stage_map.get(event) or job.progress.get("stage") or "queued"
        )
        operation = str(
            payload.get("current_operation")
            or {
                "profile_enrichment_started": "queued",
                "material_parsing_started": "material_parsing",
                "material_segmentation_completed": "material_segmentation",
                "material_agent_classification_completed": "material_classification",
                "semantic_relation_completed": "semantic_relation",
                "evidence_fusion_completed": "evidence_fusion",
                "dimension_extraction_started": "dimension_extraction",
                "dimension_extraction_completed": "dimension_extraction",
                "persona_compilation_started": "compilation",
                "persona_compilation_completed": "summary",
                "profile_version_created": "summary",
            }.get(event)
            or job.progress.get("current_operation")
            or stage
        )
        job.progress["stage"] = stage
        if event != "profile_enrichment_failed":
            job.progress["current_operation"] = operation
        next_percent = percent_for_stage(stage)
        if stage not in {"failed", "runtime_unavailable"}:
            previous_percent = safe_int(job.progress.get("percent"), default=0, minimum=0) or 0
            next_percent = max(previous_percent, next_percent)
        job.progress["percent"] = next_percent
        job.progress["label"] = {
            "queued": "排队中",
            "ingesting_sources": "正在读取资料",
            "parsing": "正在解析资料",
            "segmenting": "正在切分资料",
            "material_classification": "正在分析材料",
            "semantic_relation": "正在建立语义关系",
            "evidence_fusion": "正在融合证据",
            "extracting": "正在提取人格维度",
            "compiling": "正在编译档案",
            "summary": "正在生成摘要",
            "failed": "任务失败",
        }.get(stage, stage)
        events = job.progress.setdefault("events", [])
        if isinstance(events, list):
            events.append({"event": event, **payload})
            del events[:-300]

    async def resume_job(self, job_id: str) -> ProfileEnrichmentJob:
        job = self.profiles.get_enrichment_job(job_id)
        if job.status in self.TERMINAL:
            return job
        if job.id not in self._tasks or self._tasks[job.id].done():
            job.worker_state = "starting"
            job.worker_finished_at = None
            job.touch_worker("starting")
            self.profiles.save_enrichment_job(job)
            self._tasks[job.id] = asyncio.create_task(
                self._run_job(job.id, [], False), name=f"profile-enrichment-{job.id}"
            )
        return self.profiles.get_enrichment_job(job.id)

    async def retry_job(self, job_id: str) -> ProfileEnrichmentJob:
        """Retry a failed enrichment using the persisted runtime and materials.

        A retry never selects a different Agent or silently discards the
        durable input snapshot.  Non-retriable failures remain visible to the
        caller so the user can correct the runtime/materials first.
        """
        job = self.profiles.get_enrichment_job(job_id)
        if job.status != ProfileEnrichmentStatus.FAILED:
            return job
        failure = job.failure_json or {}
        if not is_retryable_failure(failure):
            return job
        previous_stage = str(job.progress.get("stage") or "retry")
        if previous_stage in {"failed", "runtime_unavailable"}:
            previous_stage = str(job.progress.get("previous_stage") or "retry")
        now = profile_now()
        retry = job.model_copy(deep=True)
        retry.id = new_id("profile_enrich")
        retry.status = ProfileEnrichmentStatus.CREATED
        retry.error = None
        retry.failure_json = None
        retry.dismissed_at = None
        retry.superseded_by = None
        retry.created_at = now
        retry.updated_at = now
        retry.progress = {
            **job.progress,
            "stage": previous_stage,
            "label": "正在重试",
            "percent": percent_for_stage(previous_stage),
            "previous_stage": previous_stage,
            "retry_count": (safe_int(job.progress.get("retry_count"), default=0, minimum=0) or 0)
            + 1,
            "events": [],
        }
        retry.progress.pop("failure", None)
        retry.progress["retry_of"] = job.id
        retry.worker_state = "starting"
        retry.worker_started_at = None
        retry.worker_heartbeat_at = None
        retry.worker_finished_at = None
        retry.agent_call_count = 0
        retry.touch_worker("starting", heartbeat_at=retry.created_at)
        retry.persona_creation_job_id = None
        retry.parent_job_id = None
        retry.new_version = None
        retry.evidence_delta = {}
        retry.input_material_ids = list(job.input_material_ids)
        retry.input_material_count = job.input_material_count
        retry.selected_runtime = dict(job.selected_runtime)
        retry.superseded_by = None
        job.superseded_by = retry.id
        self._event(retry, "profile_enrichment_retry_requested", stage=previous_stage)
        self.profiles.save_enrichment_job(job)
        self.profiles.save_enrichment_job(retry)
        existing = self._tasks.get(retry.id)
        if not existing or existing.done():
            self._tasks[retry.id] = asyncio.create_task(
                self._run_job(
                    retry.id, [], bool(retry.selected_runtime.get("remote_material_consent"))
                ),
                name=f"profile-enrichment-{retry.id}",
            )
        return self.profiles.get_enrichment_job(retry.id)

    async def pause_job(self, job_id: str) -> ProfileEnrichmentJob:
        job = self.profiles.get_enrichment_job(job_id)
        job.status = ProfileEnrichmentStatus.PAUSED
        job.progress["stage"] = "paused"
        job.touch_worker("paused")
        self.profiles.save_enrichment_job(job)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self.profiles.save_enrichment_job(job)

    async def cancel_job(self, job_id: str) -> ProfileEnrichmentJob:
        job = self.profiles.get_enrichment_job(job_id)
        job.status = ProfileEnrichmentStatus.CANCELLED
        job.progress["stage"] = "cancelled"
        job.touch_worker("finished", finished_at=profile_now())
        self.profiles.save_enrichment_job(job)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self.profiles.save_enrichment_job(job)

    def dismiss_job(self, job_id: str) -> ProfileEnrichmentJob:
        return self.profiles.dismiss_enrichment_job(job_id)

    async def _run_job(
        self,
        job_id: str,
        materials: list[dict[str, Any]],
        remote_material_consent: bool,
    ) -> None:
        job = self.profiles.get_enrichment_job(job_id)
        self._touch_worker(job, "running")
        if not materials:
            persisted_materials = job.progress.get("materials")
            if isinstance(persisted_materials, list):
                materials = [item for item in persisted_materials if isinstance(item, dict)]
        effective_remote_material_consent = bool(
            remote_material_consent or job.selected_runtime.get("remote_material_consent")
        )
        try:
            self._event(job, "profile_enrichment_started", mode=job.enrichment_input_mode.value)
            job.progress.update({"stage": "queued", "percent": percent_for_stage("queued")})
            self.profiles.save_enrichment_job(job)
            if job.target_profile_type == ProfileType.PERSONA:
                await self._run_persona_enrichment(
                    job,
                    materials,
                    effective_remote_material_consent,
                )
            else:
                await self._run_structured_profile_enrichment(
                    job,
                    materials,
                    remote_material_consent=effective_remote_material_consent,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._apply_runtime_activity(job)
            message = str(exc)
            # Only the typed runtime error may move a job into the resumable
            # runtime-unavailable state.  Output, transport, structured, and
            # application errors remain failed and visible for retry/fix.
            runtime_unavailable = isinstance(exc, RuntimeUnavailableError)
            job.status = (
                ProfileEnrichmentStatus.PAUSED_RUNTIME_UNAVAILABLE
                if runtime_unavailable
                else ProfileEnrichmentStatus.FAILED
            )
            job.error = str(exc)
            if isinstance(exc, AgentRuntimeError):
                failure = JobFailure(
                    code=str((exc.diagnostics or {}).get("code") or exc.code),
                    message=str(exc),
                    phase=exc.phase,
                    runtime=dict(job.selected_runtime),
                    protocol=(exc.diagnostics or {}).get("protocol"),
                    last_event_type=(exc.diagnostics or {}).get("last_event_type"),
                    event_counts=dict((exc.diagnostics or {}).get("event_counts") or {}),
                    stderr_tail=str((exc.diagnostics or {}).get("stderr_tail") or "")[-2000:],
                    retriable=bool(exc.retriable),
                    configured_limit_bytes=(
                        safe_int(exc.diagnostics["configured_limit_bytes"], default=0, minimum=0)
                        if exc.diagnostics.get("configured_limit_bytes") is not None
                        else None
                    ),
                    observed_frame_bytes=(
                        safe_int(exc.diagnostics["observed_frame_bytes"], default=0, minimum=0)
                        if exc.diagnostics.get("observed_frame_bytes") is not None
                        else None
                    ),
                    frame_bytes=(
                        safe_int(exc.diagnostics["frame_bytes"], default=0, minimum=0)
                        if exc.diagnostics.get("frame_bytes") is not None
                        else None
                    ),
                    frame_type=exc.diagnostics.get("frame_type"),
                    event_type=exc.diagnostics.get("event_type"),
                    tool_name=exc.diagnostics.get("tool_name"),
                    diagnostics=dict(exc.diagnostics),
                )
            elif isinstance(exc, ChildJobWorkerLostError):
                failure = JobFailure(
                    code=exc.code,
                    message="Persona child worker was lost after the bounded restart attempt",
                    phase="persona_creation",
                    runtime=dict(job.selected_runtime),
                    retriable=True,
                    child_job_id=exc.child_job_id,
                    context={
                        "parent_job_type": "profile_enrichment",
                        "child_job_id": exc.child_job_id,
                        "child_failure_code": exc.code,
                    },
                )
            elif isinstance(exc, InvalidNumericFieldError):
                failure = JobFailure(
                    code=exc.code,
                    message=str(exc),
                    phase=exc.phase
                    or str(
                        job.progress.get("current_operation")
                        or job.progress.get("stage")
                        or "unknown"
                    ),
                    runtime=dict(job.selected_runtime),
                    retriable=False,
                    field=exc.field,
                    received_type=exc.received_type,
                )
            else:
                failure = JobFailure(
                    code="PROFILE_ENRICHMENT_ERROR",
                    message=message,
                    phase=str(
                        job.progress.get("current_operation")
                        or job.progress.get("stage")
                        or "unknown"
                    ),
                    runtime=dict(job.selected_runtime),
                )
            job.failure_json = failure.model_dump(mode="json")
            job.progress["failure"] = job.failure_json
            job.progress.update(
                {
                    "stage": "runtime_unavailable"
                    if job.status == ProfileEnrichmentStatus.PAUSED_RUNTIME_UNAVAILABLE
                    else "failed",
                    "error": str(exc),
                }
            )
            self._event(job, "profile_enrichment_failed", error=str(exc))
            if isinstance(exc, ChildJobWorkerLostError):
                job.touch_worker("lost", finished_at=profile_now())
            self.profiles.save_enrichment_job(job)
        finally:
            with contextlib.suppress(Exception):
                current = self.profiles.get_enrichment_job(job_id)
                final_state = (
                    "lost"
                    if current.worker_state == "lost"
                    else self._worker_state_for_status(current.status.value)
                )
                self._touch_worker(
                    current,
                    final_state,
                    finished=final_state in {"finished", "failed", "paused", "lost"},
                )
                self._tasks.pop(job_id, None)

    async def _run_persona_enrichment(
        self,
        job: ProfileEnrichmentJob,
        materials: list[dict[str, Any]],
        remote_material_consent: bool,
    ) -> None:
        profile = self.profiles.get_profile(job.target_profile_id)
        if not profile.persona_id:
            raise ProfileEnrichmentError("persona_profile_binding_missing")
        persona = self.continuum.personas.get(profile.persona_id)
        try:
            persona_type = persona.manifest.persona_type
            mode = job.enrichment_input_mode.value
            previous = self.continuum.persona_creation.list_jobs(profile.persona_id)
            existing_job = next(
                (item for item in previous if item.status in {"completed", "completed_with_gaps"}),
                None,
            )
            if existing_job is not None:
                creation_job = await self.continuum.persona_creation.create_enrichment_run(
                    parent_job_id=existing_job.id,
                    research_policy=job.research_policy or None,
                    requested_scope=job.requested_scope,
                    enrichment_input_mode=mode,
                    research_instructions=job.research_focus,
                    materials=materials,
                    runtime=job.selected_runtime,
                    base_persona_version=profile.version,
                    profile_enrichment_job_id=job.id,
                )
                job.parent_job_id = existing_job.id
                job.base_persona_version = profile.version
            else:
                creation_mode = (
                    "public_research"
                    if mode in {"web_research", "hybrid"}
                    else (
                        "fictional"
                        if persona_type == PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON
                        else "private_materials"
                    )
                )
                creation_job = await self.continuum.persona_creation.create_job(
                    display_name=persona.display_name,
                    aliases=persona.manifest.aliases,
                    persona_type=persona_type,
                    creation_mode=creation_mode,
                    runtime_source=str(job.selected_runtime.get("runtime_source") or "local_cli"),
                    agent_id=str(job.selected_runtime.get("agent_id") or ""),
                    model_id=job.selected_runtime.get("model_id"),
                    reasoning_effort=job.selected_runtime.get("reasoning_effort"),
                    auth_profile_id=job.selected_runtime.get("auth_profile_id"),
                    research_policy=job.research_policy or None,
                    research_instructions=job.research_focus,
                    existing_persona_id=profile.persona_id,
                    duplicate_action="enrich",
                    materials=materials,
                    remote_material_consent=remote_material_consent,
                    enrichment_input_mode=mode,
                    job_config={
                        "enrichment_scope": job.requested_scope,
                        "input_material_count": len(materials),
                        "parent_profile_enrichment_job_id": job.id,
                        **(
                            {
                                "acp_stream_limit_bytes": safe_acp_stream_limit(
                                    job.selected_runtime.get("acp_stream_limit_bytes")
                                )
                            }
                            if job.selected_runtime.get("acp_stream_limit_bytes") is not None
                            else {}
                        ),
                        **(
                            {
                                "turn_timeout_seconds": safe_timeout(
                                    job.selected_runtime.get("turn_timeout_seconds")
                                )
                            }
                            if job.selected_runtime.get("turn_timeout_seconds") is not None
                            else {}
                        ),
                    },
                    visibility="internal",
                )
            job.input_material_count = len(materials)
            job.input_material_ids = [
                str(item.get("id"))
                for item in materials
                if isinstance(item, dict) and item.get("id")
            ]
            job.persona_creation_job_id = creation_job.id
            job.status = ProfileEnrichmentStatus.RESEARCHING
            job.progress.update(
                {
                    "stage": "persona_creation",
                    "persona_job_id": creation_job.id,
                    "runtime": job.selected_runtime,
                    "child_worker_restart_attempted": False,
                }
            )
            self._event(
                job,
                "materials_attached",
                material_count=len(materials),
                child_job_id=creation_job.id,
            )
            self.profiles.save_enrichment_job(job)
            # A child created by create_enrichment_run is deliberately inert
            # until its complete material snapshot has been persisted.
            if existing_job is not None:
                self.continuum.persona_creation.start_job(creation_job.id)
            while True:
                current = self.continuum.persona_creation.get_job(creation_job.id)
                await self._ensure_child_worker(job, current)
                job.progress.update(
                    {
                        "stage": current.progress.stage or current.current_stage,
                        "label": current.progress.label or current.current_stage,
                        "current_operation": current.progress.current_operation
                        or current.current_stage,
                        "persona_status": current.status,
                        "source_count": current.source_count,
                        "coverage": current.coverage,
                        "material_analysis": (
                            current.job_config.get("material_analysis")
                            if isinstance(current.job_config, dict)
                            else None
                        ),
                        "material_progress": (
                            current.job_config.get("material_progress")
                            if isinstance(current.job_config, dict)
                            else None
                        ),
                        "child_worker_state": self._worker_state_value(current.worker_state),
                        "child_worker_started_at": current.worker_started_at,
                        "child_worker_heartbeat_at": current.worker_heartbeat_at,
                        "child_worker_finished_at": current.worker_finished_at,
                        "child_agent_call_count": current.agent_call_count,
                        "child_progress_percent": current.progress.percent,
                        "child_current_operation": current.progress.current_operation,
                        "child_runtime_diagnostics": self._child_runtime_diagnostics(current),
                        "runtime_binding_snapshot": (
                            current.job_config.get("runtime_binding_snapshot")
                            if isinstance(current.job_config, dict)
                            else None
                        ),
                        "agent_calls": {
                            "material": safe_int(
                                current.job_config.get("material_agent_calls"),
                                default=0,
                                minimum=0,
                            )
                            or 0,
                            "dimension": safe_int(
                                current.job_config.get("dimension_agent_calls"),
                                default=0,
                                minimum=0,
                            )
                            or 0,
                        },
                    }
                )
                # The parent is the user-facing aggregate for this path.  Its
                # durable count mirrors the child so Task Center does not
                # incorrectly report zero calls while the child is active.
                job.agent_call_count = max(0, int(current.agent_call_count or 0))
                job.progress["agent_call_count"] = job.agent_call_count
                job.progress["agent_calls"] = dict(job.progress.get("agent_calls") or {})
                existing_event_names = {
                    str(item.get("event"))
                    for item in job.progress.get("events", [])
                    if isinstance(item, dict)
                }
                child_event_map = {
                    "persona_dimension_started": "dimension_extraction_started",
                    "persona_dimension_completed": "dimension_extraction_completed",
                    "persona_compilation_started": "persona_compilation_started",
                    "persona_compilation_completed": "persona_compilation_completed",
                }
                for child_event in current.events:
                    child_name = (
                        str(child_event.get("event")) if isinstance(child_event, dict) else ""
                    )
                    mapped = child_event_map.get(child_name)
                    if mapped and mapped not in existing_event_names:
                        self._event(job, mapped)
                        existing_event_names.add(mapped)
                material_progress = current.job_config.get("material_progress") or {}
                material_status = str(material_progress.get("status") or "")
                material_event_map = {
                    "PARSING": "material_parsing_started",
                    "SEGMENTING": "material_segmentation_completed",
                    "ANALYZING": "material_agent_classification_completed",
                    "CLUSTERING": "semantic_relation_completed",
                    "FUSING": "evidence_fusion_completed",
                }
                if (
                    material_status in material_event_map
                    and material_event_map[material_status] not in existing_event_names
                ):
                    self._event(job, material_event_map[material_status])
                self._update_parent_child_progress(job, current)
                self.profiles.save_enrichment_job(job)
                job.source_count = current.source_count
                if current.status == "paused_runtime_unavailable":
                    job.status = ProfileEnrichmentStatus.PAUSED_RUNTIME_UNAVAILABLE
                    job.error = current.error or "runtime_unavailable"
                    job.progress["stage"] = "runtime_unavailable"
                    self.profiles.save_enrichment_job(job)
                    return
                if current.status in {"completed", "completed_with_gaps", "failed", "cancelled"}:
                    if current.status == "failed":
                        if current.failure_json is not None:
                            self._inherit_child_failure(job, current)
                            return
                        raise ProfileEnrichmentError(current.error or "persona_enrichment_failed")
                    if current.status == "cancelled":
                        job.status = ProfileEnrichmentStatus.CANCELLED
                        job.error = current.error
                        job.progress["stage"] = "cancelled"
                        self.profiles.save_enrichment_job(job)
                        return
                    delta = {
                        "before_source_ids": list(
                            current.job_config.get("before_source_ids") or []
                        ),
                        "new_source_ids": list(current.job_config.get("new_source_ids") or []),
                        "new_evidence_units": safe_int(
                            (current.job_config.get("material_progress") or {}).get(
                                "evidence_unit_count"
                            ),
                            default=0,
                            minimum=0,
                        )
                        or 0,
                        "new_fused_evidence": safe_int(
                            (current.job_config.get("material_progress") or {}).get("fused_count"),
                            default=0,
                            minimum=0,
                        )
                        or 0,
                        "new_contradictions": safe_int(
                            (current.job_config.get("material_progress") or {}).get(
                                "contradiction_count"
                            ),
                            default=0,
                            minimum=0,
                        )
                        or 0,
                        "new_dimension_claims": sum(
                            safe_int(value, default=0, minimum=0) or 0
                            for value in current.dimension_progress.values()
                        ),
                    }
                    job.new_source_ids = [
                        str(item) for item in current.job_config.get("new_source_ids", []) if item
                    ]
                    job.evidence_delta = dict(delta)
                    job.progress["evidence_delta"] = delta
                    has_delta = bool(delta["new_source_ids"])
                    if job.input_material_count <= 0 and not has_delta:
                        has_delta = bool(delta["new_evidence_units"] or delta["new_fused_evidence"])
                    if not has_delta and profile.persona_id:
                        persona_obj = self.continuum.personas.get(profile.persona_id)
                        manifest_data = getattr(persona_obj, "manifest", None)
                        compile_state = getattr(manifest_data, "compile_state", None)
                        uncompiled_states = {"uncompiled", "draft", "pending", "none", ""}
                        if str(compile_state).lower() in uncompiled_states:
                            has_delta = True
                    if has_delta:
                        runtime_snapshot = dict(job.selected_runtime)
                        child_binding = (
                            current.job_config.get("runtime_binding_snapshot")
                            if isinstance(current.job_config, dict)
                            else None
                        )
                        if isinstance(child_binding, dict):
                            runtime_snapshot["runtime_binding_snapshot"] = dict(child_binding)
                        profile = self.profiles.sync_persona(
                            profile.persona_id, runtime_snapshot=runtime_snapshot
                        )
                        job.new_version = profile.version
                        self._event(job, "profile_version_created", version=profile.version)
                    else:
                        job.progress.update(
                            {
                                "stage": "NO_NEW_INFORMATION",
                                "result": "NO_NEW_INFORMATION",
                                "message": "新材料与现有证据重复，本次未产生有效升级。",
                            }
                        )
                        job.new_version = None
                    job.status = (
                        ProfileEnrichmentStatus.COMPLETED_WITH_GAPS
                        if current.status == "completed_with_gaps"
                        else ProfileEnrichmentStatus.COMPLETED
                    )
                    job.progress["percent"] = 100
                    if has_delta:
                        job.progress["stage"] = "completed"
                    self._event(job, "persona_compilation_completed", version=job.new_version)
                    self.profiles.save_enrichment_job(job)
                    return
                self.profiles.save_enrichment_job(job)
                await asyncio.sleep(0.85)
        except Exception:
            raise

    async def _ensure_child_worker(self, parent: ProfileEnrichmentJob, child: Any) -> None:
        """Recover one missing child worker, then fail with a typed signal."""

        status = str(child.status or "")
        if status in {"completed", "completed_with_gaps", "failed", "cancelled"}:
            return
        if status in {"paused", "paused_runtime_unavailable"}:
            return
        tasks = getattr(self.continuum.persona_creation, "_tasks", {})
        task = tasks.get(str(child.id)) if isinstance(tasks, dict) else None
        heartbeat_age = self._heartbeat_age_seconds(
            getattr(child, "worker_heartbeat_at", None)
        )
        parent.progress["child_worker_heartbeat_age_seconds"] = heartbeat_age
        parent.progress["child_worker_heartbeat_stale"] = bool(
            heartbeat_age is not None
            and heartbeat_age >= self.STALE_WORKER_HEARTBEAT_SECONDS
        )
        live_diagnostics = self._child_runtime_diagnostics(child)
        activity_tracker = live_diagnostics.get("activity_tracker")
        activity_age = self._heartbeat_age_seconds(
            activity_tracker.get("last_transport_activity")
            if isinstance(activity_tracker, dict)
            else None
        )
        parent.progress["child_activity_age_seconds"] = activity_age
        if (
            (task is None or task.done())
            and activity_age is not None
            and activity_age < self.STALE_WORKER_HEARTBEAT_SECONDS
        ):
            parent.progress["child_worker_liveness"] = "recent_agent_activity"
            return
        if task is not None and not task.done():
            parent.progress["child_worker_liveness"] = "active_task"
            return
        parent.progress["child_worker_liveness"] = "missing_or_done_task"
        attempted = bool(parent.progress.get("child_worker_restart_attempted"))
        if not attempted:
            parent.progress["child_worker_restart_attempted"] = True
            parent.progress["child_worker_restart_at"] = profile_now()
            parent.progress["child_worker_restart_reason"] = (
                "missing_task" if task is None else "task_done_before_terminal_state"
            )
            self._touch_worker(parent, "waiting_agent")
            try:
                await self.continuum.persona_creation.resume_job(str(child.id))
            except Exception as exc:
                parent.progress["child_worker_restart_error"] = str(exc)
                parent.progress["child_worker_state"] = "lost"
                parent.touch_worker("lost", finished_at=profile_now())
                self.profiles.save_enrichment_job(parent)
                raise ChildJobWorkerLostError(str(child.id)) from exc
            return
        parent.progress["child_worker_state"] = "lost"
        parent.progress["child_worker_lost_at"] = profile_now()
        parent.touch_worker("lost", finished_at=profile_now())
        self.profiles.save_enrichment_job(parent)
        raise ChildJobWorkerLostError(str(child.id))

    @staticmethod
    def _child_runtime_diagnostics(child: Any) -> dict[str, Any]:
        audits = list(getattr(child, "agent_call_audits", []) or [])
        latest = audits[-1] if audits and isinstance(audits[-1], dict) else {}
        diagnostics = dict(latest.get("runtime_diagnostics") or {})
        progress = getattr(child, "progress", None)
        progress_model_dump = getattr(progress, "model_dump", None)
        if callable(progress_model_dump):
            progress_data = progress_model_dump(mode="json")
        elif isinstance(progress, dict):
            progress_data = progress
        else:
            progress_data = {}
        if isinstance(progress_data, dict):
            if isinstance(progress_data.get("activity_tracker"), dict):
                diagnostics["activity_tracker"] = dict(progress_data["activity_tracker"])
            if progress_data.get("process_alive") is not None:
                diagnostics["process_alive"] = progress_data["process_alive"]
        return diagnostics

    def _update_parent_child_progress(self, parent: ProfileEnrichmentJob, child: Any) -> None:
        """Map child progress to the parent without ever moving backwards."""

        child_status = str(child.status or "")
        child_percent = safe_int(
            getattr(getattr(child, "progress", None), "percent", 0),
            default=0,
            minimum=0,
            maximum=100,
        ) or 0
        if child_status in {"completed", "completed_with_gaps", "failed", "cancelled"}:
            mapped = 99
        elif child_status == "created" or self._worker_state_value(
            child.worker_state
        ) == "starting":
            mapped = 5
        else:
            mapped = max(5, min(99, int(5 + child_percent * 0.94)))
        previous = safe_int(parent.progress.get("percent"), default=0, minimum=0) or 0
        parent.progress["percent"] = max(previous, mapped)
        parent.progress["stage"] = str(
            getattr(getattr(child, "progress", None), "stage", None)
            or getattr(child, "current_stage", None)
            or "persona_creation"
        )
        parent.progress["label"] = str(
            getattr(getattr(child, "progress", None), "label", None)
            or parent.progress["stage"]
        )
        parent.progress["current_operation"] = str(
            getattr(getattr(child, "progress", None), "current_operation", None)
            or getattr(child, "current_stage", None)
            or parent.progress["stage"]
        )
        child_progress = getattr(child, "progress", None)
        child_progress_model_dump = getattr(child_progress, "model_dump", None)
        if callable(child_progress_model_dump):
            child_progress_data = child_progress_model_dump(mode="json")
        elif isinstance(child_progress, dict):
            child_progress_data = child_progress
        else:
            child_progress_data = {}
        parent.progress["child_percent_mapping"] = {
            "startup_percent": 5,
            "child_percent": child_percent,
            "mapped_percent": mapped,
            "commit_percent": 99,
        }
        parent.progress["child_job_id"] = str(child.id)
        parent.progress["child_snapshot"] = {
            "status": child_status,
            "current_stage": str(child.current_stage or ""),
            "worker_state": self._worker_state_value(child.worker_state),
            "worker_started_at": child.worker_started_at,
            "worker_heartbeat_at": child.worker_heartbeat_at,
            "worker_finished_at": child.worker_finished_at,
            "agent_call_count": child.agent_call_count,
            "progress_percent": child_percent,
            "activity_tracker": child_progress_data.get("activity_tracker")
            if isinstance(child_progress_data, dict)
            else None,
            "runtime_diagnostics": self._child_runtime_diagnostics(child),
        }
        child_state = self._worker_state_value(child.worker_state)
        parent_state = {
            "waiting_agent": "waiting_agent",
            "waiting_io": "waiting_io",
            "paused": "paused",
        }.get(child_state, "running")
        parent.touch_worker(parent_state)

    def _inherit_child_failure(self, job: ProfileEnrichmentJob, child: Any) -> None:
        """Persist the child root failure without wrapping it in a new code."""

        failure = dict(child.failure_json or {})
        if not failure:
            failure = {
                "code": "AGENT_RUNTIME_ERROR",
                "message": str(child.error or "persona_creation_failed"),
                "phase": str(child.current_stage or "persona_creation"),
                "runtime": {
                    "agent_id": str(child.agent_id or ""),
                    "model_id": str(child.model_id or ""),
                    "reasoning_effort": child.reasoning_effort,
                },
                "event_counts": {},
                "retriable": False,
            }
        child_id = str(child.id)
        failure = map_child_failure_json(failure, child_job_id=child_id)
        # Keep the child's code/phase/protocol/runtime/event_counts/retriable
        # unchanged.  Only parent lineage context is added.
        job.failure_json = failure
        job.error = str(failure.get("message") or child.error or "persona_enrichment_failed")
        job.status = ProfileEnrichmentStatus.FAILED
        job.progress["failure"] = failure
        job.progress.update(
            {
                "stage": "failed",
                "error": job.error,
                "child_job_id": child_id,
                "child_failure_code": str(failure.get("code") or "AGENT_RUNTIME_ERROR"),
            }
        )
        self._event(
            job,
            "profile_enrichment_failed",
            error=job.error,
            failure_code=failure.get("code"),
            child_job_id=child_id,
        )
        self.profiles.save_enrichment_job(job)

    async def _run_structured_profile_enrichment(
        self,
        job: ProfileEnrichmentJob,
        materials: list[dict[str, Any]],
        *,
        remote_material_consent: bool,
    ) -> None:
        profile = self.profiles.get_profile(job.target_profile_id)
        runtime = {
            **job.selected_runtime,
            # Keep the requested scope inside the immutable runtime snapshot
            # sent to the structured profile agent.  This avoids treating all
            # upgrades as an undifferentiated full refresh.
            "requested_scope": job.requested_scope,
        }
        runtime_source = str(runtime.get("runtime_source") or "local_cli").lower()
        is_remote = runtime_source in {"api", "provider", "remote", "api_provider"}
        if materials and is_remote and not remote_material_consent:
            raise ProfileEnrichmentError("remote_material_consent_required")
        adapter_id = str(runtime.get("agent_id") or "")
        adapter = self.continuum.agent_registry.get_adapter(adapter_id)
        if adapter is None:
            raise ProfileEnrichmentError(f"agent_adapter_missing:{adapter_id}")
        job.status = ProfileEnrichmentStatus.RESEARCHING
        job.progress["stage"] = "agent_reasoning"
        self._event(job, "dimension_extraction_started", profile_type=profile.profile_type.value)
        job.touch_worker("waiting_agent")
        self.profiles.save_enrichment_job(job)
        heartbeat = asyncio.create_task(
            self._worker_heartbeat_loop(job.id),
            name=f"profile-enrichment-heartbeat-{job.id}",
        )
        try:
            payload = await self._agent_json(
                adapter=adapter,
                profile=profile,
                runtime=runtime,
                materials=materials,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        self._apply_runtime_activity(job)
        audits = list(self._last_agent_audits)
        if not audits and self._last_agent_audit:
            audits = [dict(self._last_agent_audit)]
        if audits:
            job.agent_call_audits.extend(audits)
            job.agent_call_audits = job.agent_call_audits[-100:]
            job.progress["agent_calls_audit"] = list(job.agent_call_audits)
        # Count actual structured calls, including a call that returned no
        # audit payload in a legacy/fake executor.  This is a durable signal
        # for the UI and watchdog; it is never inferred from polling.
        job.agent_call_count += max(1, len(audits))
        job.progress["agent_call_count"] = job.agent_call_count
        if self._last_runtime_binding_snapshot:
            job.progress["runtime_binding_snapshot"] = dict(
                self._last_runtime_binding_snapshot
            )
        job.touch_worker("running")
        job.progress.setdefault("agent_calls", {})["material"] = (
            (
                safe_int(
                    job.progress.get("agent_calls", {}).get("material"),
                    default=0,
                    minimum=0,
                )
                or 0
            )
            + max(1, len(audits))
        )
        self._event(job, "dimension_extraction_completed")
        typed_payload = dict(payload.get("payload") or payload.get("profile") or {})
        coverage = dict(payload.get("coverage") or {})
        gaps = list(payload.get("gaps") or [])
        summary = self.profiles.summary_generator.normalize(
            payload.get("summary"), display_name=profile.display_name
        )
        if not summary:
            summary = self.profiles.summary_generator.fallback(
                profile_type=profile.profile_type,
                display_name=profile.display_name,
                compiled_profile=typed_payload,
                evidence=materials,
                source_count=profile.source_count + len(materials),
            )
        updated = self.profiles.update_profile(
            profile.id,
            summary=summary,
            payload=typed_payload,
            coverage=coverage,
            runtime_snapshot={
                **runtime,
                **(
                    {"runtime_binding_snapshot": dict(self._last_runtime_binding_snapshot)}
                    if self._last_runtime_binding_snapshot
                    else {}
                ),
            },
            status=(ProfileStatus.COMPLETED_WITH_GAPS if gaps else ProfileStatus.COMPILED),
            compile_state="completed_with_gaps" if gaps else "compiled",
            source_count=profile.source_count + len(materials),
            evidence_count=profile.evidence_count + len(materials),
            source_ids=[
                str(item.get("id"))
                for item in materials
                if isinstance(item, dict) and item.get("id")
            ],
            created_by=f"enrichment:{job.id}",
        )
        job.new_version = updated.version
        job.status = (
            ProfileEnrichmentStatus.COMPLETED_WITH_GAPS
            if gaps
            else ProfileEnrichmentStatus.COMPLETED
        )
        job.progress.update(
            {"stage": "completed", "percent": 100, "coverage": coverage, "gaps": gaps}
        )
        self._event(job, "profile_version_created", version=updated.version)
        job.source_count = updated.source_count
        self.profiles.save_enrichment_job(job)

    async def _agent_json(
        self,
        *,
        adapter: AgentAdapter,
        profile: ActorProfile,
        runtime: dict[str, Any],
        materials: list[dict[str, Any]],
    ) -> dict[str, Any]:
        session_binding = None
        self._last_agent_audit = None
        self._last_agent_audits = []
        self._last_runtime_binding_snapshot = None
        self._last_runtime_activity.pop(str(profile.id), None)
        self._last_process_alive.pop(str(profile.id), None)
        prompt_base = {
            "target_profile": profile.model_dump(mode="json"),
            "requested_scope": runtime.get("requested_scope", "full_refresh"),
            "type_specific_fields": self._type_fields(profile.profile_type),
        }
        output_schema = {"type": "object"}
        session_extra: dict[str, Any] = {}
        for key in (
            "idle_timeout_seconds",
            "hard_timeout_seconds",
            "turn_timeout_seconds",
            "acp_stream_limit_bytes",
        ):
            if runtime.get(key) is not None:
                session_extra[key] = runtime[key]
        if runtime.get("acp_stream_limit_bytes") is not None:
            session_extra["acp_stream_limit_bytes"] = safe_acp_stream_limit(
                runtime.get("acp_stream_limit_bytes")
            )
        try:
            try:
                session_binding = await self.runtime_executor.open_session(
                    adapter,
                    AgentSessionConfig(
                        session_id=f"profile_enrichment_{profile.id}",
                        room_id=f"profile_enrichment:{profile.id}",
                        participant_id=f"enrichment:{profile.id}",
                        persona_id=profile.id,
                        model_id=runtime.get("model_id"),
                        reasoning_effort=runtime.get("reasoning_effort"),
                        auth_profile_id=runtime.get("auth_profile_id"),
                        permission_profile=PermissionProfile.CHAT_SAFE,
                        allow_mcp=False,
                        tools=[],
                        system_prompt=PROFILE_ENRICHMENT_SYSTEM_PROMPT,
                        extra=session_extra,
                    )
                )
            except AgentRuntimeError:
                raise
            except Exception as exc:
                raise AgentSessionStartError(
                    f"agent_session_start_failed:{exc}",
                    phase="profile_enrichment",
                    diagnostics={"profile_id": profile.id},
                ) from exc
            self._last_runtime_binding_snapshot = session_binding.snapshot.model_dump(
                mode="json"
            )
            self._active_bindings[profile.id] = session_binding
            call_id = new_id("agent_call")
            manager = getattr(self.runtime_executor, "context_budget_manager", None)
            if manager is None:
                raise StructuredOutputSchemaError(
                    "profile_enrichment_context_budget_manager_missing",
                    phase="profile_enrichment",
                )
            batches = list(
                manager.iter_batches(
                    materials,
                    item_text=lambda item: json.dumps(item, ensure_ascii=False),
                    max_items=24,
                    phase="profile_enrichment",
                    model=session_binding.session.session_data.get("model_capability"),
                    base_text=json.dumps(prompt_base, ensure_ascii=False),
                    system_prompt=PROFILE_ENRICHMENT_SYSTEM_PROMPT,
                    expected_output=output_schema,
                )
            ) or [[]]
            results: list[dict[str, Any]] = []
            for index, batch in enumerate(batches):
                prompt = {**prompt_base, "materials": [dict(item) for item in batch]}
                result = await self.runtime_executor.execute_structured(
                    session_binding,
                    system_prompt=PROFILE_ENRICHMENT_SYSTEM_PROMPT,
                    user_message=json.dumps(prompt, ensure_ascii=False),
                    schema=output_schema,
                    phase="profile_enrichment",
                    metadata={
                        "profile_enrichment_job_id": profile.id,
                        "call_id": call_id,
                        "batch_index": index,
                        "batch_count": len(batches),
                    },
                )
                self._capture_runtime_activity(profile.id, session_binding)
                if not isinstance(result, StructuredResult) or not isinstance(result.value, dict):
                    raise StructuredOutputSchemaError(
                        "profile_enrichment_output_invalid", phase="profile_enrichment"
                    )
                value = dict(result.value)
                results.append(value)
                if result.response is not None:
                    audit = result.response.audit(
                        call_id=f"{call_id}:{index + 1}",
                        job_id=profile.id,
                        phase="profile_enrichment",
                    )
                    self._last_agent_audits.append(audit)
                    self._last_agent_audit = audit

            merged_payload: dict[str, Any] = {}
            merged_coverage: dict[str, Any] = {}
            merged_gaps: list[Any] = []
            summaries: list[str] = []
            merged: dict[str, Any] = {}
            for value in results:
                merged.update({key: child for key, child in value.items() if key not in {
                    "payload", "profile", "coverage", "gaps", "summary"
                }})
                payload = value.get("payload") or value.get("profile") or {}
                if isinstance(payload, dict):
                    merged_payload.update(payload)
                coverage = value.get("coverage") or {}
                if isinstance(coverage, dict):
                    merged_coverage.update(coverage)
                gaps = value.get("gaps") or []
                if isinstance(gaps, list):
                    merged_gaps.extend(gaps)
                summary = str(value.get("summary") or "").strip()
                if summary:
                    summaries.append(summary)
            unique_gaps: list[Any] = []
            seen_gaps: set[str] = set()
            for gap in merged_gaps:
                key = json.dumps(gap, ensure_ascii=False, sort_keys=True, default=str)
                if key not in seen_gaps:
                    seen_gaps.add(key)
                    unique_gaps.append(gap)
            merged.update(
                {
                    "summary": " ".join(summaries),
                    "payload": merged_payload,
                    "coverage": merged_coverage,
                    "gaps": unique_gaps,
                }
            )
            return merged
        finally:
            if session_binding is not None:
                self._capture_runtime_activity(profile.id, session_binding)
                self._active_bindings.pop(profile.id, None)
                with contextlib.suppress(Exception):
                    await self.runtime_executor.close(session_binding)

    @staticmethod
    def _type_fields(profile_type: ProfileType) -> list[str]:
        return {
            ProfileType.ORGANIZATION: [
                "mission",
                "culture",
                "strategy",
                "resources",
                "constraints",
                "decision_style",
                "competitive_relationships",
            ],
            ProfileType.INSTITUTION: [
                "institutional_goals",
                "policy_tools",
                "power_structure",
                "stakeholders",
                "decision_patterns",
                "historical_behavior",
            ],
            ProfileType.COLLECTIVE: [
                "group_characteristics",
                "incentives",
                "adoption_tendency",
                "common_positions",
                "internal_divisions",
            ],
            ProfileType.PERSONA: [],
        }[profile_type]


__all__ = ["ProfileEnrichmentError", "ProfileEnrichmentService", "map_child_failure_json"]
