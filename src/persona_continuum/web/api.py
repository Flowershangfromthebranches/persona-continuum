from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.application.job_progress import JobNotTerminalError, is_retryable_failure
from persona_continuum.application.persona_creation_service import (
    DuplicatePersonaError,
    PersonaCreationError,
    PrivateMaterialConsentRequired,
    ResearchCapabilityError,
    RuntimeBindingError,
)
from persona_continuum.application.world.entity_classification_service import (
    EntityClassificationError,
    WorldEntityClassificationResult,
)
from persona_continuum.domain.narrative import ProductionAsset
from persona_continuum.domain.persona import PersonaType
from persona_continuum.domain.profile import ProfileType
from persona_continuum.narrative.runtime import (
    NARRATIVE_PRODUCTION_CANON_REQUIRED,
    SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED,
    VIDEO_GUIDE_SOURCE_NOT_READY,
    NarrativeAgentError,
)
from persona_continuum.narrative.video_profile_registry import (
    get_profile,
    list_profiles,
    profile_capabilities_digest,
)
from persona_continuum.numeric import safe_int
from persona_continuum.room.models import (
    BindingPreflightError,
    DirectorConfig,
    ParticipantSlot,
    RoomMode,
    RoomProtocolConfig,
    RoomProtocolType,
    RoomSessionState,
    RoomSharedContext,
)
from persona_continuum.room.orchestrator import RoomBusyError, RoomProtocolConversionError
from persona_continuum.room.random_resolver import ResolverError
from persona_continuum.security.validation import ConflictError


def json_ok(data: Any = None, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data}, status_code=status_code)


def json_err(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"ok": False, "error": message}
    if details:
        payload["details"] = details
    return JSONResponse(payload, status_code=status_code)


def narrative_json_err(exc: Exception) -> JSONResponse:
    if isinstance(exc, NarrativeAgentError):
        return json_err(exc.code, status_code=502, details=exc.to_dict())
    return json_err(str(exc))


_PROTOCOL_ROLE_REQUIRED_MESSAGES: dict[str, str] = {
    "expert_consultation": "专家会诊至少需要 1 位主持人与 1 位专家。",
    "host_moderated": "主持人模式至少需要 1 位主持人。",
    "debate": "辩论模式至少需要正方与反方（并由裁判或主持人裁决）。",
    "committee": "委员会模式至少需要 1 位主席与 1 名成员或专家。",
}


def protocol_role_required_message(raw: str) -> str:
    """Map protocol_role_required:<protocol>:<groups> to a friendly message."""

    parts = raw.split(":", 2)
    protocol_id = parts[1] if len(parts) > 1 else ""
    return _PROTOCOL_ROLE_REQUIRED_MESSAGES.get(
        protocol_id,
        "所选协作模式缺少必需角色，请为席位分配合法角色后重试。",
    )


def _room_list_item(room: RoomSessionState) -> dict[str, Any]:
    data = room.model_dump(mode="json")
    data["transcript"] = []
    data["failed_turn_audits"] = []
    data["protocol_events"] = []
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("progress_events", None)
        data["metadata"] = metadata
    return data


class WebAPIHandler:
    def __init__(self, continuum: PersonaContinuum) -> None:
        self.continuum = continuum
        self.orchestrator = continuum.orchestrator
        self.discovery = continuum.agent_discovery
        self.auth = continuum.auth
        self.profiles = continuum.profile_library
        self.profile_enrichment = continuum.profile_enrichment
        self.entity_classifier = continuum.entity_classifier
        self.material_intelligence = continuum.material_intelligence

    @staticmethod
    def _query_int(
        request: Request, name: str, *, default: int, minimum: int = 0, maximum: int = 1000
    ) -> int:
        value = safe_int(
            request.query_params.get(name),
            default=default,
            minimum=minimum,
            maximum=maximum,
        )
        return int(value if value is not None else default)

    @staticmethod
    def _public_profile_job(job: Any) -> dict[str, Any]:
        """Return progress without echoing private material contents."""
        data = job.model_dump(mode="json") if hasattr(job, "model_dump") else dict(job)
        progress = data.get("progress")
        if isinstance(progress, dict) and "materials" in progress:
            materials = progress.pop("materials")
            progress["material_count"] = len(materials) if isinstance(materials, list) else 0
        data["retry_available"] = is_retryable_failure(
            data.get("failure_json") or (progress if isinstance(progress, dict) else {})
        )
        return data

    @staticmethod
    def _public_persona_creation_job(job: Any) -> dict[str, Any]:
        """Return Persona job metadata without echoing uploaded material text."""
        data = job.model_dump(mode="json") if hasattr(job, "model_dump") else dict(job)
        # Older quality-gate rows were persisted with the generic runtime
        # failure label.  Normalize only the public presentation; retain the
        # original historical diagnostics and failure payload unchanged.
        if data.get("status") == "failed_quality_gate":
            progress = data.get("progress")
            if isinstance(progress, dict):
                progress["stage"] = "failed_quality_gate"
                progress["label"] = "质量门禁未通过"
                progress["percent"] = 0
        config = dict(data.get("job_config") or {})
        data["job_config"] = config
        if isinstance(config, dict) and "materials" in config:
            materials = config.pop("materials")
            config["material_count"] = len(materials) if isinstance(materials, list) else 0
        # Batch checkpoints carry full artifact payloads (evidence text
        # included); the public payload only gets their counts.
        checkpoints = config.pop("dimension_batch_checkpoints", None)
        if isinstance(checkpoints, dict):
            dimensions = checkpoints.get("dimensions") or {}
            config["dimension_batch_checkpoint_summary"] = {
                str(dimension): len(entries)
                for dimension, entries in dimensions.items()
                if isinstance(entries, dict)
            }
        data["retry_available"] = is_retryable_failure(data.get("failure_json"))
        return data

    async def list_agents(self, request: Request) -> Response:
        probes = await self.discovery.scan(force_refresh=False)
        return json_ok([p.model_dump(mode="json") for p in probes])

    async def performance_summary(self, request: Request) -> Response:
        """Read-only observability for the performance subsystem.

        Exposes process-wide tracer counters, recent finished task summaries,
        and the current cache / runtime-pool / scheduler / context snapshots so
        a benchmark or the operator UI can compare before/after without
        changing any execution result.
        """

        from persona_continuum.performance import (
            default_execution_scheduler,
            default_runtime_pool,
            default_tracer,
        )
        from persona_continuum.performance.capability_cache import (
            default_model_capability_cache,
        )

        try:
            limit = int(request.query_params.get("limit", "20"))
        except ValueError:
            limit = 20
        recent_param = request.query_params.get("kind")
        recent = default_tracer().recent(limit=max(1, min(100, limit)))
        if recent_param:
            recent = [item for item in recent if item.get("kind") == recent_param]
        payload: dict[str, Any] = {
            "global_counters": default_tracer().global_snapshot(),
            "recent_tasks": recent,
            "model_capability_cache": default_model_capability_cache().snapshot(),
            "runtime_pool": default_runtime_pool().snapshot(),
            "scheduler": default_execution_scheduler().snapshot().as_dict(),
        }
        try:
            payload["room_context"] = self.continuum.orchestrator.context_manager.snapshot()
        except Exception:
            payload["room_context"] = {}
        try:
            loop = self.continuum.worlds.engine.simulation_loop
            payload["world_simulation"] = {
                "parallel_enabled": loop.parallel_enabled,
                "last_concurrency_used": loop.concurrency_used,
            }
        except Exception:
            payload["world_simulation"] = {}
        return json_ok(payload)

    async def rescan_agents(self, request: Request) -> Response:
        probes = await self.discovery.scan(force_refresh=True)
        return json_ok([p.model_dump(mode="json") for p in probes])

    async def inspect_agent(self, request: Request) -> Response:
        agent_id = request.path_params.get("agent_id", "")
        probe = await self.discovery.probe_adapter(agent_id)
        if not probe:
            return json_err(f"Agent not found: {agent_id}", status_code=404)
        return json_ok(probe.model_dump(mode="json"))

    async def test_agent(self, request: Request) -> Response:
        agent_id = request.path_params.get("agent_id", "")
        probe = await self.discovery.probe_adapter(agent_id)
        if not probe:
            return json_err(f"Agent not found: {agent_id}", status_code=404)
        return json_ok(
            {
                "status": probe.status.value,
                "version": probe.version,
                "models_count": len(probe.models),
                "models": [m.model_dump(mode="json") for m in probe.models[:10]],
            }
        )

    async def revalidate_agent_research(self, request: Request) -> Response:
        """Force a fresh local-CLI Web Research capability probe.

        This endpoint intentionally returns a typed BLOCKED/UNAVAILABLE
        capability as a successful diagnostic response.  A policy denial is
        useful state for the UI and must not be mistaken for a missing Agent.
        """

        agent_id = request.path_params.get("agent_id", "")
        probe = await self.discovery.probe_adapter(agent_id)
        if not probe:
            return json_err(f"Agent not found: {agent_id}", status_code=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        models = [model for model in probe.models if model.selectable]
        model_id = body.get("model_id") or (models[0].id if models else None)
        try:
            validated = await self.continuum.persona_creation.validate_research_runtime(
                {
                    "runtime_source": probe.runtime_source,
                    "agent_id": probe.id,
                    "model_id": model_id,
                    "reasoning_effort": body.get("reasoning_effort"),
                    "auth_profile_id": body.get("auth_profile_id"),
                    "force_revalidate": True,
                },
                force_revalidate=True,
            )
        except (ResearchCapabilityError, RuntimeBindingError) as exc:
            return json_err(str(exc), status_code=400)
        return json_ok(validated.model_dump(mode="json"))

    async def list_personas(self, request: Request) -> Response:
        personas = self.continuum.personas.list()
        return json_ok([p.manifest.model_dump(mode="json") for p in personas])

    async def list_profiles(self, request: Request) -> Response:
        try:
            profiles = self.profiles.list_profiles(
                profile_type=request.query_params.get("type"),
                status=request.query_params.get("status"),
                query=request.query_params.get("q") or request.query_params.get("query"),
                sort_by=request.query_params.get("sort", "updated_at"),
                descending=request.query_params.get("order", "desc") != "asc",
            )
            return json_ok([profile.model_dump(mode="json") for profile in profiles])
        except ValueError as exc:
            return json_err(str(exc), status_code=400)

    async def create_profile(self, request: Request) -> Response:
        try:
            body = await request.json()
            profile = self.profiles.create_profile(
                profile_type=ProfileType(str(body.get("profile_type") or body.get("type"))),
                display_name=str(body.get("display_name") or body.get("name") or ""),
                aliases=list(body.get("aliases") or []),
                summary=str(body.get("summary") or ""),
                payload=dict(body.get("payload") or {}),
                runtime_snapshot=dict(body.get("runtime") or {}),
            )
            job = None
            selected_runtime = dict(body.get("runtime") or {})
            if body.get("auto_enrich") or selected_runtime.get("agent_id"):
                job = await self.profile_enrichment.create_job(
                    target_profile_id=profile.id,
                    job_type=str(body.get("job_type") or "upgrade"),
                    requested_scope=str(body.get("requested_scope") or "full_refresh"),
                    runtime=selected_runtime,
                    research_policy=body.get("research_policy"),
                    materials=list(body.get("materials") or []),
                    remote_material_consent=bool(body.get("remote_material_consent")),
                    enrichment_input_mode=body.get("enrichment_input_mode"),
                )
            return json_ok(
                {
                    "profile": profile.model_dump(mode="json"),
                    "job": self._public_profile_job(job) if job else None,
                },
                status_code=202 if job else 201,
            )
        except ValueError as exc:
            if str(exc).startswith("profile_already_exists:"):
                existing_id = str(exc).split(":", 1)[1]
                return json_err(
                    "profile_already_exists",
                    status_code=409,
                    details={"profile_id": existing_id},
                )
            return json_err(str(exc), status_code=400)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def get_profile(self, request: Request) -> Response:
        try:
            return json_ok(self.profiles.get_detail(request.path_params.get("profile_id", "")))
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def list_profile_versions(self, request: Request) -> Response:
        try:
            versions = self.profiles.list_versions(request.path_params.get("profile_id", ""))
            return json_ok([version.model_dump(mode="json") for version in versions])
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def enrich_profile(self, request: Request) -> Response:
        try:
            body = await request.json()
            job = await self.profile_enrichment.create_job(
                target_profile_id=request.path_params.get("profile_id", ""),
                job_type=str(body.get("job_type") or "upgrade"),
                requested_scope=str(body.get("requested_scope") or "full_refresh"),
                runtime=dict(body.get("runtime") or {}),
                research_policy=body.get("research_policy"),
                materials=list(body.get("materials") or []),
                remote_material_consent=bool(body.get("remote_material_consent")),
                enrichment_input_mode=body.get("enrichment_input_mode"),
                research_focus=body.get("research_focus") or body.get("research_instructions"),
            )
            return json_ok(self._public_profile_job(job), status_code=202)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def archive_profile(self, request: Request) -> Response:
        try:
            profile = self.profiles.archive_profile(request.path_params.get("profile_id", ""))
            return json_ok(profile.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def list_profile_enrichment_jobs(self, request: Request) -> Response:
        profile_id = request.query_params.get("profile_id")
        include_internal = request.query_params.get("include_internal", "false").lower() == "true"
        page = self._query_int(request, "page", default=1, minimum=1, maximum=1_000_000)
        page_size = self._query_int(request, "page_size", default=20, minimum=1, maximum=100)
        jobs = self.profiles.list_enrichment_jobs(
            profile_id,
            task_center=True,
            include_internal=include_internal,
            page=page,
            page_size=page_size,
        )
        return json_ok([self._public_profile_job(job) for job in jobs])

    async def get_profile_enrichment_job(self, request: Request) -> Response:
        try:
            job = self.profiles.get_enrichment_job(request.path_params.get("job_id", ""))
            return json_ok(self._public_profile_job(job))
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def pause_profile_enrichment_job(self, request: Request) -> Response:
        try:
            job = await self.profile_enrichment.pause_job(request.path_params.get("job_id", ""))
            return json_ok(self._public_profile_job(job))
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def resume_profile_enrichment_job(self, request: Request) -> Response:
        try:
            job = await self.profile_enrichment.resume_job(request.path_params.get("job_id", ""))
            return json_ok(self._public_profile_job(job))
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def retry_profile_enrichment_job(self, request: Request) -> Response:
        try:
            requested_id = request.path_params.get("job_id", "")
            job = await self.profile_enrichment.retry_job(requested_id)
            if job.id == requested_id and str(job.status.value) == "failed":
                return json_err(
                    "job_not_retriable",
                    status_code=409,
                    details={"job_id": requested_id},
                )
            return json_ok(self._public_profile_job(job), status_code=202)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def cancel_profile_enrichment_job(self, request: Request) -> Response:
        try:
            job = await self.profile_enrichment.cancel_job(request.path_params.get("job_id", ""))
            return json_ok(self._public_profile_job(job))
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def dismiss_profile_enrichment_job(self, request: Request) -> Response:
        try:
            job = self.profiles.dismiss_enrichment_job(request.path_params.get("job_id", ""))
            return json_ok(self._public_profile_job(job))
        except JobNotTerminalError as exc:
            return json_err(
                exc.code,
                status_code=409,
                details={"job_id": exc.job_id, "status": exc.status},
            )
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def get_persona_runtime(self, request: Request) -> Response:
        persona_id = request.path_params.get("persona_id", "")
        branch_id = request.query_params.get("branch_id", "main")
        try:
            state = self.continuum.runtime_state(persona_id, branch_id)
            return json_ok(state)
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def reset_persona_runtime(self, request: Request) -> Response:
        persona_id = request.path_params.get("persona_id", "")
        try:
            body = await request.json() if request.method == "POST" else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        branch_id = (
            str(body.get("branch_id") or request.query_params.get("branch_id") or "main")
        ).strip() or "main"
        include_memories = bool(
            body.get("include_memories")
            or body.get("full_reset")
            or request.query_params.get("include_memories") in {"1", "true", "yes"}
        )
        try:
            result = self.continuum.reset_runtime_state(
                persona_id, branch_id=branch_id, include_memories=include_memories
            )
            return json_ok(result)
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def get_persona_evidence_index(self, request: Request) -> Response:
        persona_id = request.path_params.get("persona_id", "")
        try:
            index = self.material_intelligence.get_index(persona_id)
            dimension = request.query_params.get("dimension")
            limit = self._query_int(request, "limit", default=24, minimum=1, maximum=100)
            items = index.retrieve(dimension, top_k=limit, diversity=True)
            coverage = self.material_intelligence.coverage(persona_id)
            return json_ok(
                {
                    "persona_id": persona_id,
                    "coverage": coverage.model_dump(mode="json"),
                    "items": items,
                    "contradictions": [
                        item.model_dump(mode="json") for item in index.contradictions()
                    ],
                    "episodes": [item.model_dump(mode="json") for item in index.episodes()],
                    "fused_count": len(index.fused()),
                    "unit_count": len(index.units()),
                }
            )
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def list_persona_material_jobs(self, request: Request) -> Response:
        persona_id = request.query_params.get("persona_id")
        jobs = self.material_intelligence.list_jobs(persona_id)
        return json_ok([job.model_dump(mode="json") for job in jobs])

    async def get_persona_material_job(self, request: Request) -> Response:
        try:
            job = self.material_intelligence.get_job(request.path_params.get("job_id", ""))
            return json_ok(job.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def analyze_persona_materials(self, request: Request) -> Response:
        persona_id = request.path_params.get("persona_id", "")
        try:
            body = await request.json()
            source_ids = list(body.get("source_ids") or [])
            job = await asyncio.to_thread(
                self.material_intelligence.analyze_sources,
                persona_id,
                source_ids,
                runtime_snapshot=dict(body.get("runtime_snapshot") or {}),
                incremental=bool(body.get("incremental", True)),
            )
            return json_ok(job.model_dump(mode="json"), status_code=202)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def delete_persona(self, request: Request) -> Response:
        persona_id = request.path_params.get("persona_id", "")
        if not persona_id:
            return json_err("persona_id is required", status_code=400)
        try:
            success = self.continuum.personas.delete(persona_id)
            return json_ok({"deleted": success, "persona_id": persona_id})
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def rename_persona(self, request: Request) -> Response:
        """Rename a Persona's display name (identity/bindings stay stable)."""
        persona_id = request.path_params.get("persona_id", "")
        if not persona_id:
            return json_err("persona_id is required", status_code=400)
        try:
            body = await request.json()
        except Exception:
            return json_err("persona_name_required", status_code=400)
        display_name = str(
            body.get("display_name") or body.get("name") or ""
        )
        if not display_name.strip():
            return json_err("persona_name_required", status_code=400)
        try:
            aliases = body.get("aliases")
            record = self.continuum.personas.rename(
                persona_id,
                display_name,
                aliases=[str(a) for a in aliases] if isinstance(aliases, list) else None,
            )
        except ConflictError as exc:
            return json_err(str(exc), status_code=400)
        except Exception as exc:
            return json_err(str(exc), status_code=404)
        return json_ok(record.model_dump(mode="json"))

    # ------------------------------------------------------------------
    # Persona Creation Runtime API
    # ------------------------------------------------------------------

    async def create_persona_creation_job(self, request: Request) -> Response:
        try:
            body = await request.json()
            runtime = body.get("runtime") or {}
            policy = body.get("research_policy")
            job_config = dict(body.get("job_config") or {})
            for key in (
                "acp_stream_limit_bytes",
                "turn_timeout_seconds",
                "permission_profile",
                "research_tools",
                "research_tool_policy",
                "cli_flags",
            ):
                if runtime.get(key) is not None and key not in job_config:
                    job_config[key] = runtime[key]
            job = await self.continuum.persona_creation.create_job(
                display_name=str(body.get("display_name") or body.get("name") or ""),
                aliases=list(body.get("aliases") or []),
                persona_type=PersonaType(str(body.get("persona_type"))),
                creation_mode=str(body.get("creation_mode") or "public_research"),
                runtime_source=str(
                    body.get("runtime_source") or runtime.get("runtime_source") or "local_cli"
                ),
                agent_id=str(body.get("agent_id") or runtime.get("agent_id") or ""),
                model_id=body.get("model_id") or runtime.get("model_id"),
                reasoning_effort=body.get("reasoning_effort") or runtime.get("reasoning_effort"),
                auth_profile_id=body.get("auth_profile_id") or runtime.get("auth_profile_id"),
                research_policy=policy,
                birth_date=body.get("birth_date"),
                death_date=body.get("death_date"),
                data_cutoff_date=body.get("data_cutoff_date"),
                existing_persona_id=body.get("existing_persona_id"),
                # The HTTP wizard must surface the duplicate choice; direct
                # application callers can explicitly request the default
                # enrichment behavior.
                duplicate_action=str(
                    body.get("duplicate_action")
                    if body.get("duplicate_action") is not None
                    else "prompt"
                ),
                materials=list(body.get("materials") or []),
                remote_material_consent=bool(
                    body.get("remote_material_consent") or body.get("privacy_consent")
                ),
                job_config=job_config,
                subject_kind=body.get("subject_kind"),
                work_or_universe=body.get("work_or_universe"),
                life_status=body.get("life_status"),
                privacy_scope=body.get("privacy_scope"),
                identity_context=body.get("identity_context"),
                user_defined_facts=body.get("user_defined_facts"),
                research_mode=body.get("research_mode"),
                web_scope=body.get("web_scope"),
                research_instructions=body.get("research_instructions"),
            )
            payload = self._public_persona_creation_job(job)
            payload["job_id"] = job.id
            return json_ok(payload, status_code=202)
        except DuplicatePersonaError as exc:
            return json_err(
                "persona_already_exists",
                status_code=409,
                details={"persona_id": exc.persona_id, "display_name": exc.display_name},
            )
        except (
            ResearchCapabilityError,
            RuntimeBindingError,
            PrivateMaterialConsentRequired,
        ) as exc:
            return json_err(str(exc), status_code=400)
        except (PersonaCreationError, ValueError) as exc:
            return json_err(str(exc), status_code=400)
        except Exception as exc:
            return json_err(str(exc), status_code=500)

    async def list_persona_creation_jobs(self, request: Request) -> Response:
        persona_id = request.query_params.get("persona_id")
        include_internal = request.query_params.get("include_internal", "false").lower() == "true"
        page = self._query_int(request, "page", default=1, minimum=1, maximum=1_000_000)
        page_size = self._query_int(request, "page_size", default=20, minimum=1, maximum=100)
        jobs = self.continuum.persona_creation.list_jobs(
            persona_id,
            task_center=True,
            include_internal=include_internal,
            page=page,
            page_size=page_size,
        )
        return json_ok([self._public_persona_creation_job(job) for job in jobs])

    async def list_background_jobs(self, request: Request) -> Response:
        include_internal = request.query_params.get("include_internal", "false").lower() == "true"
        page = self._query_int(request, "page", default=1, minimum=1, maximum=1_000_000)
        page_size = self._query_int(request, "page_size", default=20, minimum=1, maximum=100)
        # Fetch enough terminal rows from each table to merge two independently
        # sorted histories without letting a fixed cap make deep pages empty.
        fetch_size = page * page_size
        creation = self.continuum.persona_creation.list_jobs(
            task_center=True,
            include_internal=include_internal,
            page=1,
            page_size=fetch_size,
        )
        enrichment = self.profiles.list_enrichment_jobs(
            task_center=True,
            include_internal=include_internal,
            page=1,
            page_size=fetch_size,
        )
        creation_counts = self.continuum.persona_creation.task_center_counts(
            include_internal=include_internal
        )
        enrichment_counts = self.profiles.task_center_counts(include_internal=include_internal)
        terminal_statuses = {"failed", "completed", "completed_with_gaps", "cancelled"}
        counts: dict[str, int] = {}
        for source in (creation_counts, enrichment_counts):
            for status, count in source.items():
                counts[status] = counts.get(status, 0) + (
                    safe_int(count, default=0, minimum=0) or 0
                )
        counts["active"] = (safe_int(creation_counts.get("active"), default=0, minimum=0) or 0) + (
            safe_int(enrichment_counts.get("active"), default=0, minimum=0) or 0
        )
        counts["badge"] = (safe_int(creation_counts.get("badge"), default=0, minimum=0) or 0) + (
            safe_int(enrichment_counts.get("badge"), default=0, minimum=0) or 0
        )
        counts["terminal"] = sum(counts.get(status, 0) for status in terminal_statuses)
        jobs: list[dict[str, Any]] = []
        for creation_job in creation:
            item = self._public_persona_creation_job(creation_job)
            item["job_kind"] = "persona_creation"
            jobs.append(item)
        for enrichment_job in enrichment:
            item = self._public_profile_job(enrichment_job)
            item["job_kind"] = "profile_enrichment"
            jobs.append(item)
        jobs.sort(
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )
        active_jobs = [item for item in jobs if item.get("status") not in terminal_statuses]
        terminal_jobs = [item for item in jobs if item.get("status") in terminal_statuses]
        terminal_jobs = terminal_jobs[(page - 1) * page_size : page * page_size]
        return json_ok(
            {
                "jobs": [*active_jobs, *terminal_jobs],
                "counts": counts,
                "page": page,
                "page_size": page_size,
            }
        )

    async def dismiss_persona_creation_job(self, request: Request) -> Response:
        try:
            job = self.continuum.persona_creation.dismiss_job(request.path_params.get("job_id", ""))
            return json_ok(self._public_persona_creation_job(job))
        except JobNotTerminalError as exc:
            return json_err(
                exc.code,
                status_code=409,
                details={"job_id": exc.job_id, "status": exc.status},
            )
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def cleanup_background_jobs(self, request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        raw_statuses = body.get("statuses")
        statuses = (
            [str(raw_statuses)]
            if isinstance(raw_statuses, str)
            else list(raw_statuses or ["failed", "completed", "completed_with_gaps", "cancelled"])
        )
        include_internal = bool(body.get("include_internal", False))
        creation = self.continuum.persona_creation.cleanup_terminal_jobs(
            statuses, include_internal=include_internal
        )
        enrichment = self.profiles.cleanup_enrichment_jobs(
            statuses, include_internal=include_internal
        )
        return json_ok(
            {
                "dismissed_count": (
                    safe_int(creation["dismissed_count"], default=0, minimum=0) or 0
                )
                + (safe_int(enrichment["dismissed_count"], default=0, minimum=0) or 0),
                "dismissed_job_ids": creation["dismissed_job_ids"]
                + enrichment["dismissed_job_ids"],
                "ignored_statuses": sorted(
                    set(creation["ignored_statuses"] + enrichment["ignored_statuses"])
                ),
            }
        )

    async def get_persona_creation_job(self, request: Request) -> Response:
        job_id = request.path_params.get("job_id", "")
        try:
            job = self.continuum.persona_creation.get_job(job_id)
            payload = self._public_persona_creation_job(job)
            payload["job_id"] = job.id
            return json_ok(payload)
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def pause_persona_creation_job(self, request: Request) -> Response:
        try:
            job = await self.continuum.persona_creation.pause_job(
                request.path_params.get("job_id", "")
            )
            return json_ok(self._public_persona_creation_job(job))
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def resume_persona_creation_job(self, request: Request) -> Response:
        try:
            body: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                parsed = await request.json()
                if isinstance(parsed, dict):
                    body = parsed
            runtime = (
                body.get("runtime")
                or body.get("execution_runtime")
                or body.get("agent_runtime")
            )
            job = await self.continuum.persona_creation.resume_job(
                request.path_params.get("job_id", ""),
                runtime=dict(runtime) if isinstance(runtime, dict) and runtime else None,
            )
            return json_ok(self._public_persona_creation_job(job))
        except RuntimeBindingError as exc:
            return json_err(str(exc), status_code=409)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def retry_persona_creation_job(self, request: Request) -> Response:
        try:
            requested_id = request.path_params.get("job_id", "")
            runtime: dict[str, Any] | None = None
            with contextlib.suppress(Exception):
                parsed = await request.json()
                if isinstance(parsed, dict):
                    candidate = (
                        parsed.get("runtime")
                        or parsed.get("execution_runtime")
                        or parsed.get("agent_runtime")
                    )
                    if isinstance(candidate, dict) and candidate:
                        runtime = candidate
            job = await self.continuum.persona_creation.retry_job(
                requested_id, runtime=runtime
            )
            if job.id == requested_id and job.status in {"failed", "failed_quality_gate"}:
                return json_err(
                    "job_not_retriable",
                    status_code=409,
                    details={"job_id": requested_id},
                )
            return json_ok(self._public_persona_creation_job(job), status_code=202)
        except RuntimeBindingError as exc:
            return json_err(str(exc), status_code=409)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def continue_persona_creation_job(self, request: Request) -> Response:
        try:
            body = await request.json()
            job = await self.continuum.persona_creation.continue_job(
                request.path_params.get("job_id", ""),
                research_policy=body.get("research_policy"),
            )
            return json_ok(self._public_persona_creation_job(job), status_code=202)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def cancel_persona_creation_job(self, request: Request) -> Response:
        try:
            job = await self.continuum.persona_creation.cancel_job(
                request.path_params.get("job_id", "")
            )
            return json_ok(self._public_persona_creation_job(job))
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def add_persona_creation_materials(self, request: Request) -> Response:
        try:
            body = await request.json()
            job = await self.continuum.persona_creation.add_materials(
                request.path_params.get("job_id", ""),
                list(body.get("materials") or []),
                consent=bool(body.get("remote_material_consent") or body.get("privacy_consent")),
            )
            return json_ok(self._public_persona_creation_job(job), status_code=202)
        except (PrivateMaterialConsentRequired, PersonaCreationError) as exc:
            return json_err(str(exc), status_code=400)
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def answer_persona_creation_interview(self, request: Request) -> Response:
        try:
            body = await request.json()
            job = await self.continuum.persona_creation.answer_interview(
                request.path_params.get("job_id", ""),
                str(body.get("answer") or ""),
                dimension=body.get("dimension"),
            )
            return json_ok(self._public_persona_creation_job(job), status_code=202)
        except PersonaCreationError as exc:
            return json_err(str(exc), status_code=400)
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def persona_creation_events(self, request: Request) -> Response:
        try:
            after = self._query_int(request, "after", default=0, minimum=0, maximum=1_000_000_000)
            events = self.continuum.persona_creation.get_events(
                request.path_params.get("job_id", ""), after=after
            )
            return json_ok(events)
        except Exception as exc:
            return json_err(str(exc), status_code=404)

    async def list_auth_profiles(self, request: Request) -> Response:
        profiles = self.auth.list_profiles()
        return json_ok([p.model_dump(mode="json") for p in profiles])

    async def create_auth_profile(self, request: Request) -> Response:
        try:
            body = await request.json()
            prof = self.auth.create_profile(
                name=body.get("name", "Unnamed Provider"),
                base_url=body.get("base_url", "https://api.openai.com/v1"),
                provider_type=body.get("provider_type", "openai_compatible"),
                auth_env_var=body.get("auth_env_var") or body.get("api_key_env"),
                default_model=body.get("default_model"),
                headers=body.get("headers"),
                metadata=dict(body.get("metadata") or {}),
                api_key=body.get("api_key"),
            )
            adapter_id = f"api_{prof.id}"
            from persona_continuum.agent.protocols.openai_compatible import (
                OpenAICompatibleAPIAdapter,
            )

            api_adapter = OpenAICompatibleAPIAdapter(
                adapter_id=adapter_id,
                name=f"API: {prof.name}",
                base_url=prof.base_url,
                auth_env_var=prof.auth_env_var,
                default_model=prof.default_model or "default",
                custom_headers=prof.headers,
                model_capabilities=dict(prof.metadata.get("model_capabilities") or {}),
                provider_type=prof.provider_type,
                credential_manager=self.auth.credential_manager,
                credential_id=prof.id,
            )
            self.continuum.agent_registry.register_adapter(api_adapter)
            test_res = await self.auth.test_connection(prof.id)
            await self.discovery.scan(force_refresh=True)

            prof_dict = prof.model_dump(mode="json")
            prof_dict["test_connection"] = test_res
            return json_ok(prof_dict, status_code=201)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def update_auth_profile(self, request: Request) -> Response:
        try:
            body = await request.json()
            profile_id = request.path_params.get("profile_id", "")
            existing = self.auth.get_profile(profile_id)
            if existing is None:
                return json_err("API profile not found", status_code=404)
            prof = self.auth.update_profile(
                profile_id,
                name=body.get("name") or existing.name,
                base_url=body.get("base_url") or existing.base_url,
                provider_type=body.get("provider_type") or existing.provider_type,
                auth_env_var=body.get("auth_env_var")
                if "auth_env_var" in body
                else existing.auth_env_var,
                default_model=body.get("default_model")
                if "default_model" in body
                else existing.default_model,
                headers=body.get("headers")
                if "headers" in body
                else existing.headers,
                metadata=dict(body.get("metadata") or {})
                if "metadata" in body
                else existing.metadata,
                api_key=body.get("api_key") if "api_key" in body else None,
            )
            self.continuum.agent_registry.unregister_adapter(f"api_{profile_id}")
            from persona_continuum.agent.protocols.openai_compatible import (
                OpenAICompatibleAPIAdapter,
            )

            self.continuum.agent_registry.register_adapter(
                OpenAICompatibleAPIAdapter(
                    adapter_id=f"api_{prof.id}",
                    name=f"API: {prof.name}",
                    base_url=prof.base_url,
                    auth_env_var=prof.auth_env_var,
                    default_model=prof.default_model or "default",
                    custom_headers=prof.headers,
                    model_capabilities=dict(prof.metadata.get("model_capabilities") or {}),
                    provider_type=prof.provider_type,
                    credential_manager=self.auth.credential_manager,
                    credential_id=prof.id,
                )
            )
            await self.discovery.scan(force_refresh=True)
            return json_ok(prof.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def test_auth_profile(self, request: Request) -> Response:
        profile_id = request.path_params.get("profile_id", "")
        res = await self.auth.test_connection(profile_id)
        return json_ok(res)

    async def test_provider(self, request: Request) -> Response:
        provider_id = request.path_params.get("provider_id", "")
        result = await self.continuum.credentials.test_connection(provider_id)
        return json_ok(result)

    async def delete_auth_profile(self, request: Request) -> Response:
        profile_id = request.path_params.get("profile_id", "")
        self.continuum.agent_registry.unregister_adapter(f"api_{profile_id}")
        success = self.auth.delete_profile(profile_id)
        await self.discovery.scan(force_refresh=True)
        return json_ok({"deleted": success})

    async def list_rooms(self, request: Request) -> Response:
        rooms = self.orchestrator.list_rooms()
        return json_ok([_room_list_item(r) for r in rooms])

    async def list_room_protocols(self, request: Request) -> Response:
        return json_ok(
            [
                self.orchestrator.protocol_registry.public_definition(definition)
                for definition in self.orchestrator.protocol_registry.list_definitions()
            ]
        )

    async def list_room_templates(self, request: Request) -> Response:
        templates = self.orchestrator.protocol_repository.list_templates()
        return json_ok([template.model_dump(mode="json") for template in templates])

    async def create_room_template(self, request: Request) -> Response:
        try:
            body = await request.json()
            template = self.orchestrator.protocol_repository.create_template(dict(body))
            definition = self.orchestrator.protocol_registry.get(
                template.protocol, template.protocol_config
            )
            self.orchestrator.protocol_registry.validate_participants(
                definition, template.participants
            )
            return json_ok(template.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def update_room_template(self, request: Request) -> Response:
        try:
            body = await request.json()
            template = self.orchestrator.protocol_repository.update_template(
                request.path_params.get("template_id", ""), dict(body)
            )
            definition = self.orchestrator.protocol_registry.get(
                template.protocol, template.protocol_config
            )
            self.orchestrator.protocol_registry.validate_participants(
                definition, template.participants
            )
            return json_ok(template.model_dump(mode="json"))
        except KeyError as exc:
            return json_err(str(exc), status_code=404)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def delete_room_template(self, request: Request) -> Response:
        try:
            deleted = self.orchestrator.protocol_repository.delete_template(
                request.path_params.get("template_id", "")
            )
            return json_ok({"deleted": deleted})
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def create_room(self, request: Request) -> Response:
        try:
            body = await request.json()
            template_id = body.get("template_id")
            template = (
                self.orchestrator.protocol_repository.get_template(str(template_id))
                if template_id
                else None
            )
            if template_id and template is None:
                return json_err(f"Room template not found: {template_id}", status_code=404)
            # Explicit protocol resolution: a selected template always wins
            # over a drifted client-side protocol value, and a missing
            # protocol must fail loudly instead of silently degrading to
            # free_discussion.
            protocol_missing = False
            if template is not None:
                protocol = template.protocol
            else:
                body_protocol = str(body.get("protocol") or "").strip()
                if body_protocol:
                    protocol = RoomProtocolType(body_protocol)
                else:
                    # Defer the missing-protocol rejection until persona
                    # binding preflight has run: a non-existent persona must
                    # surface its structured BindingPreflightError details
                    # instead of this generic 400. The placeholder protocol is
                    # used for preflight only; no room is persisted.
                    protocol_missing = True
                    protocol = RoomProtocolType.FREE_DISCUSSION
            participants_data = body.get("participants", [])
            if not participants_data and template is not None:
                # Templates ship unbound role slots (empty persona_id): API
                # callers must map slots to real personas explicitly.
                participants_data = [
                    participant.model_dump(mode="json")
                    for participant in template.participants
                    if participant.persona_id
                ]
                if not participants_data:
                    return json_err(
                        "模板只定义了角色槽位，尚未映射真实人物；请在请求中提供 participants。",
                        status_code=400,
                    )
            participants: list[ParticipantSlot] = []
            for p in participants_data:
                participants.append(ParticipantSlot.model_validate(p))
            if not participants:
                return json_err("请至少添加并配置一个参与者席位。", status_code=400)

            dir_cfg_data = body.get("director_config", {})
            dir_cfg = DirectorConfig.model_validate(dir_cfg_data) if dir_cfg_data else None

            # Persona binding preflight runs before the protocol rejection so
            # binding errors keep their structured details payload.
            self.orchestrator.validate_persona_bindings(participants)
            if protocol_missing:
                return json_err("请选择房间协作模式。", status_code=400)

            room = self.orchestrator.create_room(
                title=body.get("title"),
                description=body.get("description") or (template.description if template else None),
                topic=body.get("topic"),
                participants=participants,
                director_config=dir_cfg,
                mode=RoomMode(body.get("mode", "autonomous")),
                request_id=body.get("request_id"),
                host_participant_id=body.get("host_participant_id"),
                protocol=protocol,
                protocol_config=RoomProtocolConfig.model_validate(
                    body.get("protocol_config")
                    or (template.protocol_config.model_dump(mode="json") if template else {})
                ),
                shared_context=RoomSharedContext.model_validate(
                    body.get("shared_context")
                    or (template.shared_context.model_dump(mode="json") if template else {})
                ),
                template_id=str(template_id) if template_id else None,
            )
            if body.get("initialize_async", True) and room.participants:
                self.orchestrator.initialize_room_background(room.id)
            return json_ok(room.model_dump(mode="json"), status_code=201)
        except BindingPreflightError as exc:
            return json_err(exc.reason, status_code=400, details=exc.to_dict())
        except ValueError as exc:
            message = str(exc)
            if message.startswith("protocol_role_required:"):
                return json_err(protocol_role_required_message(message), status_code=400)
            return json_err(message)
        except Exception as exc:
            return json_err(str(exc))

    async def get_room(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        room = self.orchestrator.get_room(room_id)
        if not room:
            return json_err(f"Room not found: {room_id}", status_code=404)
        return json_ok(room.model_dump(mode="json"))

    async def patch_room(self, request: Request) -> Response:
        try:
            body = await request.json()
            room = self.orchestrator.update_room(
                request.path_params.get("room_id", ""), dict(body)
            )
            return json_ok(room.model_dump(mode="json"))
        except RoomProtocolConversionError as exc:
            # protocol_change_requires_convert -> 400 with a coded hint;
            # unexpected guard codes keep the generic conversion mapping.
            status = (
                400
                if exc.code == "protocol_change_requires_convert"
                else 409
                if exc.code in {"protocol_run_active", "room_status_not_convertible"}
                else 400
            )
            return json_err(exc.message, status_code=status, details={"code": exc.code})
        except KeyError as exc:
            return json_err(str(exc), status_code=404)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def update_room_bindings(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            body = await request.json()
            bindings = body.get("bindings")
            if not isinstance(bindings, dict) or not bindings:
                return json_err("bindings is required", status_code=400)
            room = await self.orchestrator.update_room_bindings(room_id, bindings)
            return json_ok(room.model_dump(mode="json"))
        except RoomBusyError as exc:
            return json_err(exc.message, status_code=409, details={"code": exc.code})
        except BindingPreflightError as exc:
            return json_err(exc.reason, status_code=400, details=exc.to_dict())
        except ResolverError as exc:
            return json_err(str(exc), status_code=400)
        except KeyError:
            return json_err(f"Room not found: {room_id}", status_code=404)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def run_room_protocol(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            body = await request.json()
            question = str(body.get("question") or body.get("message") or "")
            room = self.orchestrator.get_room(room_id)
            if room is None:
                return json_err(f"Room not found: {room_id}", status_code=404)
            # Empty-question fallback lives in run_protocol (topic fallback
            # for execution only); both entry points below forward the raw
            # question unchanged.
            if bool(body.get("background", True)):
                self.orchestrator.start_protocol_background(room_id, question)
                return json_ok({"accepted": True, "room_id": room_id}, status_code=202)
            completed = await self.orchestrator.run_protocol(room_id, question)
            return json_ok(completed.model_dump(mode="json"))
        except RoomBusyError as exc:
            return json_err(exc.message, status_code=409, details={"code": exc.code})
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def list_room_protocol_events(self, request: Request) -> Response:
        events = self.orchestrator.protocol_repository.list_events(
            request.path_params.get("room_id", ""), request.query_params.get("run_id")
        )
        return json_ok([event.model_dump(mode="json") for event in events])

    async def cancel_room_protocol(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        protocol_cancelled = await self.orchestrator.protocol_runtime.cancel(room_id)
        turn_cancelled = await self.orchestrator.cancel_turn(room_id)
        return json_ok(
            {"cancelled": protocol_cancelled or turn_cancelled, "protocol": protocol_cancelled}
        )

    async def finalize_room_protocol(self, request: Request) -> Response:
        try:
            room = await self.orchestrator.finalize_protocol(
                request.path_params.get("room_id", "")
            )
            return json_ok(room.model_dump(mode="json"))
        except RoomBusyError as exc:
            return json_err(exc.message, status_code=409, details={"code": exc.code})
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def convert_room_protocol(self, request: Request) -> Response:
        # Orchestrator guard codes map to HTTP: not found -> 404, active run
        # / bad room status -> 409, malformed target or role mapping -> 400.
        room_id = request.path_params.get("room_id", "")
        try:
            body = await request.json()
            room = await self.orchestrator.convert_room_protocol(
                room_id,
                str(body.get("target_protocol") or ""),
                dict(body.get("role_mapping") or {}),
            )
            return json_ok(room.model_dump(mode="json"))
        except RoomProtocolConversionError as exc:
            status = {"room_not_found": 404, "protocol_run_active": 409,
                      "room_status_not_convertible": 409}.get(exc.code, 400)
            return json_err(exc.message, status_code=status, details={"code": exc.code})
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def list_room_runs(self, request: Request) -> Response:
        runs = self.orchestrator.protocol_repository.list_runs(
            room_id=request.query_params.get("room_id"),
            status=request.query_params.get("status"),
            limit=self._query_int(request, "limit", default=100, minimum=1, maximum=500),
        )
        return json_ok(runs)

    async def get_room_run(self, request: Request) -> Response:
        run = self.orchestrator.protocol_repository.get_run(
            request.path_params.get("run_id", "")
        )
        if run is None:
            return json_err("Room run not found", status_code=404)
        return json_ok(run)

    async def delete_room_run(self, request: Request) -> Response:
        try:
            deleted = self.orchestrator.protocol_repository.delete_run(
                request.path_params.get("run_id", "")
            )
            return json_ok({"deleted": deleted})
        except ValueError as exc:
            return json_err(str(exc), status_code=409)

    async def resolve_preview(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        room = self.orchestrator.get_room(room_id)
        if not room:
            return json_err(f"Room not found: {room_id}", status_code=404)
        probes = await self.discovery.scan(force_refresh=False)
        snapshots = await self.orchestrator.resolver.resolve_all(room.participants, probes)
        return json_ok({k: v.model_dump(mode="json") for k, v in snapshots.items()})

    async def start_room(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            room = await self.orchestrator.wait_for_initialization(room_id)
            return json_ok(room.model_dump(mode="json"))
        except BindingPreflightError as exc:
            return json_err(exc.reason, status_code=400, details=exc.to_dict())
        except Exception as exc:
            return json_err(str(exc))

    async def run_autonomous_room(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            body = await request.json()
            events: list[dict[str, Any]] = []
            async for event in self.orchestrator.run_autonomous_discussion(
                room_id,
                max_turns=safe_int(body.get("max_turns", 6), default=6, minimum=1, maximum=100)
                or 6,
            ):
                events.append(event)
            return json_ok({"events": events})
        except Exception as exc:
            return json_err(str(exc))

    async def step_turn(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            body = (
                await request.json()
                if request.headers.get("content-type") == "application/json"
                else {}
            )
            manual_speaker_id = body.get("manual_speaker_id")
            user_message = body.get("user_message", "")

            events: list[dict[str, Any]] = []
            async for ev in self.orchestrator.step_turn(
                room_id=room_id,
                manual_speaker_id=manual_speaker_id,
                user_message=user_message,
            ):
                events.append(ev)

            return json_ok({"events": events})
        except RoomBusyError as exc:
            return json_err(exc.message, status_code=409, details={"code": exc.code})
        except Exception as exc:
            return json_err(str(exc))

    async def retry_turn(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            body = (
                await request.json()
                if request.headers.get("content-type") == "application/json"
                else {}
            )
            alt_runtime = body.get("alternate_runtime_id")
            alt_model = body.get("alternate_model_id")
            user_msg = body.get("user_message", "")

            events: list[dict[str, Any]] = []
            async for ev in self.orchestrator.retry_turn(
                room_id=room_id,
                alternate_runtime_id=alt_runtime,
                alternate_model_id=alt_model,
                user_message=user_msg,
            ):
                events.append(ev)

            return json_ok({"events": events})
        except Exception as exc:
            return json_err(str(exc))

    async def skip_turn(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            body = (
                await request.json()
                if request.headers.get("content-type") == "application/json"
                else {}
            )
            reason = body.get("reason")
            res = await self.orchestrator.skip_turn(room_id, reason=reason)
            return json_ok(res)
        except Exception as exc:
            return json_err(str(exc))

    async def restart_session(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            body = (
                await request.json()
                if request.headers.get("content-type") == "application/json"
                else {}
            )
            participant_id = body.get("participant_id")
            if not participant_id:
                return json_err("participant_id is required", status_code=400)
            success = await self.orchestrator.restart_session(room_id, participant_id)
            return json_ok({"restarted": success})
        except Exception as exc:
            return json_err(str(exc))

    async def pause_room(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            room = await self.orchestrator.pause_room(room_id)
            return json_ok(room.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def inject_room_message(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            body = await request.json()
            raw_attachments = body.get("attachments") or body.get("attachment_ids") or []
            attachment_ids = (
                [str(item) for item in raw_attachments]
                if isinstance(raw_attachments, list)
                else []
            )
            event = await self.orchestrator.inject_message(
                room_id,
                content=str(body.get("content") or body.get("message") or ""),
                injection_type=str(body.get("type") or "external_information"),
                client_message_id=str(
                    body.get("client_message_id") or body.get("request_id") or ""
                ),
                attachment_ids=attachment_ids,
            )
            return json_ok(event)
        except Exception as exc:
            return json_err(str(exc))

    async def upload_room_attachment(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            body = await request.json()
            content_base64 = body.get("content_base64") or body.get("content")
            if not isinstance(content_base64, str) or not content_base64.strip():
                return json_err("content_base64 is required", status_code=400)
            attachment = self.orchestrator.upload_room_attachment(
                room_id,
                filename=str(body.get("filename") or "attachment"),
                mime=str(body.get("mime") or body.get("content_type") or ""),
                content_base64=content_base64,
            )
            return json_ok(attachment, status_code=201)
        except KeyError:
            return json_err(f"Room not found: {room_id}", status_code=404)
        except ValueError as exc:
            return json_err(str(exc), status_code=400)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def download_room_attachment(self, request: Request) -> Response:
        from starlette.responses import FileResponse

        from persona_continuum.security.paths import ensure_child_path

        room_id = request.path_params.get("room_id", "")
        stored_name = request.path_params.get("stored_name", "")
        try:
            uploads_root = self.continuum.config.room_uploads_dir
            candidate = ensure_child_path(
                uploads_root, uploads_root / room_id / Path(stored_name).name
            )
            if not candidate.is_file():
                return json_err("Attachment not found", status_code=404)
            record = self.orchestrator.get_room_attachment_by_stored_name(
                room_id, candidate.name
            )
            filename = str((record or {}).get("filename") or candidate.name)
            media_type = str((record or {}).get("mime") or "application/octet-stream")
            return FileResponse(
                path=str(candidate),
                media_type=media_type,
                filename=filename,
            )
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def download_room_attachment_by_id(self, request: Request) -> Response:
        from starlette.responses import FileResponse

        from persona_continuum.security.paths import ensure_child_path

        attachment_id = request.path_params.get("attachment_id", "")
        try:
            record = self.orchestrator.get_room_attachment(attachment_id)
            if record is None:
                return json_err("Attachment not found", status_code=404)
            uploads_root = self.continuum.config.room_uploads_dir
            candidate = ensure_child_path(
                uploads_root,
                uploads_root / str(record.get("room_id")) / str(record.get("stored_name")),
            )
            if not candidate.is_file():
                return json_err("Attachment not found", status_code=404)
            return FileResponse(
                path=str(candidate),
                media_type=str(record.get("mime") or "application/octet-stream"),
                filename=str(record.get("filename") or candidate.name),
            )
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def resume_room(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            room = await self.orchestrator.resume_room(room_id)
            return json_ok(room.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def cancel_turn(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        success = await self.orchestrator.cancel_turn(room_id)
        return json_ok({"cancelled": success})

    async def stop_room(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        try:
            room = await self.orchestrator.stop_room(room_id)
            return json_ok(room.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def delete_room(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        success = self.orchestrator.delete_room(room_id)
        return json_ok({"deleted": success})

    async def list_transcripts(self, request: Request) -> Response:
        room_id = request.path_params.get("room_id", "")
        records = self.orchestrator.list_room_transcripts(room_id)
        return json_ok([r.model_dump(mode="json") for r in records])

    async def clear_transcripts(self, request: Request) -> Response:
        """Wipe every transcript row for a room and reset the in-memory
        window.  Intended for the UI's "clear conversation" affordance:
        the room itself, its participants, persona state and memory stay
        intact so the user can keep the room and start a fresh thread.
        """

        room_id = request.path_params.get("room_id", "")
        if not room_id:
            return json_err("room_id_required", status_code=400)
        if self.orchestrator.get_room(room_id) is None:
            return json_err(f"Room not found: {room_id}", status_code=404)
        try:
            removed = await self.orchestrator.clear_room_transcripts(room_id)
        except Exception as exc:
            return json_err(str(exc))
        return json_ok({"removed": removed})

    # ------------------------------------------------------------------
    # Parallel World API Endpoints
    # ------------------------------------------------------------------

    async def classify_world_entities(self, request: Request) -> Response:
        try:
            body = await request.json()
            description = str(body.get("description") or "")
            entities = list(body.get("entities") or body.get("candidates") or [])
            if not entities and body.get("raw_actors"):
                entities = list(body.get("raw_actors") or [])
            if not entities and body.get("seed"):
                seed = dict(body.get("seed") or {})
                metadata = dict(seed.get("metadata") or {})
                entities = list(metadata.get("entity_candidates") or [])
                if not entities:
                    entities = list(metadata.get("initial_actors") or [])
            entities = [
                item if isinstance(item, dict) else {"id": str(item), "name": str(item)}
                for item in entities
            ]
            entities = self._apply_classification_overrides(
                entities,
                body.get("classification_overrides"),
            )
            runtime = dict(
                body.get("classification_runtime")
                or body.get("builder_runtime")
                or body.get("runtime")
                or {}
            )
            result = await self.continuum.worlds.classify_entities(
                description=description,
                entities=entities,
                runtime=runtime,
                require_llm=bool(body.get("require_llm", True))
                and not bool(body.get("allow_deterministic", False)),
            )
            return json_ok(result.model_dump(mode="json"))
        except EntityClassificationError as exc:
            return json_err(str(exc), status_code=409)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    @staticmethod
    def _apply_classification_bindings(
        entities: list[dict[str, Any]],
        classification: WorldEntityClassificationResult,
    ) -> list[dict[str, Any]]:
        by_id = {
            str(item.id): item
            for item in [*classification.classified_agents, *classification.non_agent_entities]
        }
        for entity in entities:
            entity_id = str(
                entity.get("id") or entity.get("actor_id") or entity.get("name") or ""
            )
            row = by_id.get(entity_id)
            if row is None:
                continue
            entity["entity_category"] = row.category
            entity["profile_type"] = row.subtype if row.agent_capable else None
            entity["classification_confidence"] = row.confidence
            entity["classification_rationale"] = row.rationale
            if row.profile_id:
                entity["profile_id"] = row.profile_id
                if row.subtype == "person":
                    entity["persona_id"] = row.profile_id
        return entities

    @staticmethod
    def _apply_classification_overrides(
        entities: list[dict[str, Any]],
        overrides: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Carry a user correction into the next deterministic/LLM pass."""
        values = dict(overrides or {})
        for entity in entities:
            entity_id = str(
                entity.get("id") or entity.get("actor_id") or entity.get("name") or ""
            )
            override = values.get(entity_id) or values.get(str(entity.get("name") or ""))
            if override:
                entity["classification_override"] = override
        return entities

    @staticmethod
    def _ensure_agent_roster(
        entities: list[dict[str, Any]],
        classification: WorldEntityClassificationResult,
    ) -> list[dict[str, Any]]:
        """Include Agent-capable candidates not repeated in initial_actors."""
        known = {
            str(item.get("id") or item.get("actor_id") or item.get("name") or "")
            for item in entities
        }
        actor_types = {
            "person": "persona_actor",
            "organization": "organization_actor",
            "institution": "institution_actor",
            "collective": "collective_actor",
        }
        for item in classification.classified_agents:
            if str(item.id) in known:
                continue
            entities.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "actor_type": actor_types.get(item.subtype, "persona_actor"),
                    "profile_type": item.subtype,
                    "profile_id": item.profile_id,
                    "persona_id": item.profile_id if item.subtype == "person" else None,
                }
            )
        return entities

    async def confirm_world_actor_completion(self, request: Request) -> Response:
        try:
            body = await request.json()
            entities = [
                item if isinstance(item, dict) else {"id": str(item), "name": str(item)}
                for item in list(body.get("entities") or body.get("actors") or [])
            ]
            entities = self._apply_classification_overrides(
                entities,
                body.get("classification_overrides"),
            )
            runtime = dict(
                body.get("runtime")
                or body.get("actor_completion_runtime")
                or body.get("persona_creation_runtime")
                or {}
            )
            require_llm = bool(body.get("require_llm", True)) and not bool(
                body.get("allow_deterministic", False)
            )
            if require_llm and not runtime.get("agent_id"):
                return json_err("actor_completion_runtime_required", status_code=400)
            classification = await self.continuum.worlds.classify_entities(
                description=str(body.get("description") or ""),
                entities=entities,
                runtime=dict(body.get("classification_runtime") or runtime),
                require_llm=require_llm,
            )
            if classification.missing_profiles and not runtime.get("agent_id"):
                return json_err("actor_completion_runtime_required", status_code=400)
            selected = {
                str(item)
                for item in (
                    body.get("selected_entity_ids")
                    or body.get("selected_actor_ids")
                    or body.get("selected")
                    or []
                )
            }
            jobs: list[dict[str, Any]] = []
            persona_entities = [
                item
                for item in classification.missing_profiles
                if item.subtype == "person"
                and item.profile_match_status == "missing"
                and (not selected or item.id in selected)
            ]
            if persona_entities:
                confirm_personas = self.continuum.persona_creation.confirm_world_persona_completion
                persona_result = await confirm_personas(
                    actors=[
                        next(
                            (
                                entity
                                for entity in entities
                                if str(
                                    entity.get("id")
                                    or entity.get("actor_id")
                                    or entity.get("name")
                                )
                                == item.id
                            ),
                            {"id": item.id, "name": item.name},
                        )
                        for item in persona_entities
                    ],
                    selected_actor_ids=[item.id for item in persona_entities],
                    runtime=runtime,
                    research_policy=body.get("research_policy"),
                    materials_by_actor=dict(body.get("materials_by_actor") or {}),
                    remote_material_consent=bool(body.get("remote_material_consent")),
                )
                jobs.extend(
                    self._public_persona_creation_job(job)
                    for job in persona_result.jobs
                )
            for entity in classification.missing_profiles:
                if (
                    entity.subtype == "person"
                    or entity.profile_match_status != "missing"
                    or (selected and entity.id not in selected)
                ):
                    continue
                profile_type = ProfileType(entity.subtype)
                profile = self.profiles.find_by_name(entity.name, profile_type=profile_type)
                if profile is None:
                    profile = self.profiles.create_profile(
                        profile_type=profile_type,
                        display_name=entity.name,
                        aliases=[],
                        payload={
                            "classification_rationale": entity.rationale,
                            "classification_confidence": entity.confidence,
                        },
                    )
                job = await self.profile_enrichment.create_job(
                    target_profile_id=profile.id,
                    job_type="actor_completion",
                    requested_scope="full_refresh",
                    runtime=runtime,
                    research_policy=body.get("research_policy"),
                    materials=list(
                        (body.get("materials_by_actor") or {}).get(entity.id, [])
                    ),
                    remote_material_consent=bool(body.get("remote_material_consent")),
                    enrichment_input_mode=body.get("enrichment_input_mode"),
                )
                # Persist the source entity id with the async job so the UI
                # can deterministically bind the completed profile back to the
                # Actor roster after a long-running research task.
                job.progress["actor_id"] = entity.id
                self.profiles.save_enrichment_job(job)
                jobs.append(self._public_profile_job(job))
            return json_ok(
                {
                    "status": "started" if jobs else "waiting_for_materials",
                    "jobs": jobs,
                    "classification": classification.model_dump(mode="json"),
                },
                status_code=202,
            )
        except (
            ResearchCapabilityError,
            RuntimeBindingError,
            PrivateMaterialConsentRequired,
        ) as exc:
            return json_err(str(exc), status_code=400)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def match_world_personas(self, request: Request) -> Response:
        try:
            body = await request.json()
            actors = list(body.get("actors") or body.get("raw_actors") or [])
            if not actors and body.get("seed"):
                seed = body["seed"]
                actors = list((seed.get("metadata") or {}).get("initial_actors") or [])
                actors = [
                    item if isinstance(item, dict) else {"id": str(item), "name": str(item)}
                    for item in actors
                ]
            legacy_matches = self.continuum.persona_creation.match_world_actors(actors)
            return json_ok([match.model_dump(mode="json") for match in legacy_matches])
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def confirm_world_persona_completion(self, request: Request) -> Response:
        try:
            body = await request.json()
            actors = list(body.get("actors") or body.get("raw_actors") or [])
            result = await self.continuum.persona_creation.confirm_world_persona_completion(
                actors=actors,
                selected_actor_ids=list(
                    body.get("selected_actor_ids") or body.get("selected") or []
                ),
                runtime=dict(body.get("runtime") or body.get("persona_creation_runtime") or {}),
                research_policy=body.get("research_policy"),
                materials_by_actor=dict(body.get("materials_by_actor") or {}),
                remote_material_consent=bool(body.get("remote_material_consent")),
            )
            payload = result.model_dump(mode="json")
            payload["jobs"] = [
                self._public_persona_creation_job(job)
                for job in result.jobs
            ]
            return json_ok(payload, status_code=202)
        except (
            ResearchCapabilityError,
            RuntimeBindingError,
            PrivateMaterialConsentRequired,
        ) as exc:
            return json_err(str(exc), status_code=400)
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def list_worlds(self, request: Request) -> Response:
        worlds = self.continuum.worlds.list_worlds()
        return json_ok([w.model_dump(mode="json") for w in worlds])

    async def preview_world_seed(self, request: Request) -> Response:
        try:
            body = await request.json()
            description = body.get("description", "")
            if not description:
                return json_err("description is required", status_code=400)
            builder_runtime = body.get("builder_runtime") or {}
            agent_id = builder_runtime.get("agent_id")
            adapter = self.continuum.agent_registry.get_adapter(agent_id) if agent_id else None
            if not adapter and not agent_id:
                ready = await self.discovery.get_ready_agents()
                adapter = self.continuum.agent_registry.get_adapter(ready[0].id) if ready else None

            seed, raw_actors, meta = await self.continuum.worlds.llm_builder.build(
                description=description,
                adapter=adapter,
                model_id=builder_runtime.get("model_id"),
                reasoning_effort=builder_runtime.get("reasoning_effort"),
                auth_profile_id=builder_runtime.get("auth_profile_id"),
                baseline=body.get("baseline", "real_world"),
                start_date=body.get("start_date"),
                simulation_end=body.get("simulation_end", "2030"),
                allow_fallback=body.get("allow_fallback", False),
            )
            seed.metadata["initial_actors"] = raw_actors
            entities = list(meta.get("entity_candidates") or raw_actors)
            entities = [
                item if isinstance(item, dict) else {"id": str(item), "name": str(item)}
                for item in entities
            ]
            entities = self._apply_classification_overrides(
                entities,
                body.get("classification_overrides"),
            )
            classification_runtime = dict(builder_runtime)
            # Preview may resolve the first READY Builder Agent when the
            # selector was left at its default.  Carry that resolved binding
            # into the mandatory classification turn instead of accidentally
            # treating the same LLM-built roster as deterministic.
            if adapter is not None and not classification_runtime.get("agent_id"):
                classification_runtime["agent_id"] = adapter.adapter_id
            classification_runtime.setdefault("model_id", meta.get("builder_model"))
            classification = await self.continuum.worlds.classify_entities(
                description=str(description),
                entities=entities,
                runtime=classification_runtime,
                require_llm=meta.get("builder_source") == "llm",
            )
            raw_actors = self._apply_classification_bindings(raw_actors, classification)
            raw_actors = self._ensure_agent_roster(raw_actors, classification)
            seed.metadata["entity_classification"] = classification.model_dump(mode="json")
            seed.metadata["entity_candidates"] = [
                item.model_dump(mode="json") for item in classification.candidates
            ]
            matches = self.continuum.persona_creation.match_world_actors(
                [
                    item if isinstance(item, dict) else {"id": str(item), "name": str(item)}
                    for item in raw_actors
                ]
            )
            return json_ok(
                {
                    "seed": seed.model_dump(mode="json"),
                    "actors": raw_actors,
                    "persona_matches": [item.model_dump(mode="json") for item in matches],
                    "entity_classification": classification.model_dump(mode="json"),
                    "metadata": meta,
                }
            )
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def create_world(self, request: Request) -> Response:
        try:
            body = await request.json()
            description = body.get("description", "")
            title = body.get("title")
            builder_runtime = body.get("builder_runtime") or {}
            default_actor_runtime = body.get("default_actor_runtime") or {}
            actor_runtime_configs = body.get("actor_runtime_configs") or {}
            director_runtime = body.get("director_runtime") or {}
            evaluator_runtime = body.get("evaluator_runtime") or {}
            classification_result: WorldEntityClassificationResult | None = None

            # Preview and direct-create share the same Persona Match gate.  A
            # world is never persisted while human actors are still missing a
            # Persona unless the user explicitly confirms completion or elects
            # to keep a Generated Actor.
            raw_actors = list(
                body.get("raw_actors") or body.get("actors") or body.get("initial_actors") or []
            )
            raw_actors = [
                item if isinstance(item, dict) else {"id": str(item), "name": str(item)}
                for item in raw_actors
            ]
            # The deterministic Direct Create path still has to use the same
            # roster/Persona gate as Preview and LLM Builder.  Infer the
            # starting roster once from the existing WorldBuilder without
            # persisting anything; the engine receives it again as metadata.
            if (
                not raw_actors
                and not body.get("seed")
                and not body.get("use_llm_builder")
                and not builder_runtime.get("agent_id")
                and description
            ):
                inferred = self.continuum.worlds.engine.builder.build_seed(
                    description=description,
                    baseline=body.get("baseline", "real_world"),
                    start_date=body.get("start_date"),
                    simulation_end=body.get("simulation_end", "2030"),
                    rules=body.get("rules"),
                    divergence_items=body.get("divergence"),
                    immutable_facts=body.get("immutable_facts"),
                )
                raw_actors = [
                    {
                        "id": actor_id,
                        "name": actor_id.replace("_", " ").title(),
                        "actor_type": "persona_actor",
                    }
                    for actor_id in inferred.initial_actors
                ]
            if body.get("seed"):
                seed_meta = dict((body.get("seed") or {}).get("metadata") or {})
                if not raw_actors:
                    raw_actors = list(
                        seed_meta.get("initial_actors")
                        or (body.get("seed") or {}).get("initial_actors")
                        or []
                    )
                raw_actors = [
                    item if isinstance(item, dict) else {"id": str(item), "name": str(item)}
                    for item in raw_actors
                ]
            raw_actors = self._apply_classification_overrides(
                raw_actors,
                body.get("classification_overrides"),
            )
            if raw_actors:
                classification_result = await self.continuum.worlds.classify_entities(
                    description=str(description),
                    entities=raw_actors,
                    runtime=builder_runtime,
                    require_llm=bool(builder_runtime.get("agent_id"))
                    and not bool(body.get("allow_deterministic_classification", False)),
                )
                raw_actors = self._apply_classification_bindings(
                    raw_actors, classification_result
                )
                raw_actors = self._ensure_agent_roster(raw_actors, classification_result)
                unresolved_profiles = [
                    item
                    for item in classification_result.missing_profiles
                    if str(item.id)
                    not in {str(value) for value in body.get("generated_actor_ids") or []}
                ]
                if unresolved_profiles and not (
                    body.get("persona_completion_confirmed")
                    or body.get("actor_completion_confirmed")
                ):
                    return json_err(
                        "requires_actor_profile_completion_confirmation",
                        status_code=409,
                        details={
                            "classification": classification_result.model_dump(mode="json"),
                            "missing_profiles": [
                                item.model_dump(mode="json") for item in unresolved_profiles
                            ],
                            # Backward-compatible key for the existing wizard.
                            "missing_personas": [
                                item.model_dump(mode="json")
                                for item in unresolved_profiles
                                if item.subtype == "person"
                            ],
                            "raw_actors": raw_actors,
                        },
                    )
                persona_matches = self.continuum.persona_creation.match_world_actors(raw_actors)
                unresolved = [
                    item
                    for item in persona_matches
                    if item.status in {"MISSING", "AMBIGUOUS"}
                    and str(item.actor_id)
                    not in {str(value) for value in body.get("generated_actor_ids") or []}
                ]
                if unresolved and not (
                    body.get("persona_completion_confirmed")
                    or body.get("actor_completion_confirmed")
                ):
                    return json_err(
                        "requires_persona_completion_confirmation",
                        status_code=409,
                        details={
                            "matches": [item.model_dump(mode="json") for item in persona_matches],
                            "missing_personas": [
                                item.model_dump(mode="json") for item in unresolved
                            ],
                            "raw_actors": raw_actors,
                            "classification": (
                                classification_result.model_dump(mode="json")
                                if classification_result
                                else None
                            ),
                        },
                    )

                bindings = dict(
                    body.get("profile_bindings")
                    or body.get("persona_bindings")
                    or {}
                )
                if bindings:
                    for item in raw_actors:
                        actor_id = str(item.get("id") or item.get("actor_id") or "")
                        if actor_id in bindings:
                            item["profile_id"] = str(bindings[actor_id])
                            if str(item.get("profile_type") or "person") == "person":
                                item["persona_id"] = str(bindings[actor_id])
                if body.get("persona_completion_confirmed") or body.get(
                    "actor_completion_confirmed"
                ):
                    bound_matches = self.continuum.persona_creation.match_world_actors(raw_actors)
                    generated_ids = {str(value) for value in body.get("generated_actor_ids") or []}
                    still_unresolved = [
                        item
                        for item in bound_matches
                        if item.status in {"MISSING", "AMBIGUOUS"}
                        and str(item.actor_id) not in generated_ids
                    ]
                    if still_unresolved:
                        return json_err(
                            "persona_completion_incomplete",
                            status_code=409,
                            details={
                                "matches": [item.model_dump(mode="json") for item in bound_matches],
                                "missing_personas": [
                                    item.model_dump(mode="json") for item in still_unresolved
                                ],
                            },
                        )

            # If seed_data is provided directly (from preview or manual edit)
            if body.get("seed"):
                from persona_continuum.world.models import WorldSeed

                seed_payload = dict(body["seed"])
                seed_payload["metadata"] = {
                    **dict(seed_payload.get("metadata") or {}),
                    **({"initial_actors": raw_actors} if raw_actors else {}),
                    **(
                        {"entity_classification": classification_result.model_dump(mode="json")}
                        if classification_result
                        else {}
                    ),
                }
                seed = WorldSeed.model_validate(seed_payload)
                world, branch, state = self.continuum.worlds.create_world_from_seed(
                    seed=seed,
                    title=title,
                    description=description,
                    builder_runtime=builder_runtime,
                    default_actor_runtime=default_actor_runtime,
                    actor_runtime_configs=actor_runtime_configs,
                    director_runtime=director_runtime,
                    evaluator_runtime=evaluator_runtime,
                )
            elif body.get("use_llm_builder") or builder_runtime.get("agent_id"):
                agent_id = builder_runtime.get("agent_id")
                adapter = self.continuum.agent_registry.get_adapter(agent_id) if agent_id else None
                if not adapter and not agent_id:
                    ready = await self.discovery.get_ready_agents()
                    adapter = (
                        self.continuum.agent_registry.get_adapter(ready[0].id) if ready else None
                    )
                seed, raw_actors, meta = await self.continuum.worlds.llm_builder.build(
                    description=description,
                    adapter=adapter,
                    model_id=builder_runtime.get("model_id"),
                    reasoning_effort=builder_runtime.get("reasoning_effort"),
                    auth_profile_id=builder_runtime.get("auth_profile_id"),
                    baseline=body.get("baseline", "real_world"),
                    start_date=body.get("start_date"),
                    simulation_end=body.get("simulation_end", "2030"),
                    allow_fallback=body.get("allow_fallback", False),
                )
                classification_entities = list(meta.get("entity_candidates") or raw_actors)
                classification_entities = [
                    item
                    if isinstance(item, dict)
                    else {"id": str(item), "name": str(item)}
                    for item in classification_entities
                ]
                classification_entities = self._apply_classification_overrides(
                    classification_entities,
                    body.get("classification_overrides"),
                )
                classification_runtime = dict(builder_runtime)
                if adapter is not None and not classification_runtime.get("agent_id"):
                    classification_runtime["agent_id"] = adapter.adapter_id
                classification_runtime.setdefault("model_id", meta.get("builder_model"))
                classification_result = await self.continuum.worlds.classify_entities(
                    description=str(description),
                    entities=classification_entities,
                    runtime=classification_runtime,
                    require_llm=meta.get("builder_source") == "llm",
                )
                raw_actors = self._apply_classification_bindings(
                    raw_actors, classification_result
                )
                raw_actors = self._ensure_agent_roster(raw_actors, classification_result)
                built_matches = self.continuum.persona_creation.match_world_actors(
                    [
                        item if isinstance(item, dict) else {"id": str(item), "name": str(item)}
                        for item in raw_actors
                    ]
                )
                generated_ids = {str(value) for value in body.get("generated_actor_ids") or []}
                built_unresolved = [
                    item
                    for item in built_matches
                    if item.status in {"MISSING", "AMBIGUOUS"}
                    and str(item.actor_id) not in generated_ids
                ]
                profile_unresolved = [
                    item
                    for item in classification_result.missing_profiles
                    if str(item.id)
                    not in {str(value) for value in body.get("generated_actor_ids") or []}
                ]
                if profile_unresolved and not (
                    body.get("persona_completion_confirmed")
                    or body.get("actor_completion_confirmed")
                ):
                    return json_err(
                        "requires_actor_profile_completion_confirmation",
                        status_code=409,
                        details={
                            "classification": classification_result.model_dump(mode="json"),
                            "missing_profiles": [
                                item.model_dump(mode="json") for item in profile_unresolved
                            ],
                            "missing_personas": [
                                item.model_dump(mode="json")
                                for item in profile_unresolved
                                if item.subtype == "person"
                            ],
                            "seed": seed.model_dump(mode="json"),
                            "raw_actors": raw_actors,
                            "metadata": meta,
                        },
                    )
                if built_unresolved and not (
                    body.get("persona_completion_confirmed")
                    or body.get("actor_completion_confirmed")
                ):
                    return json_err(
                        "requires_persona_completion_confirmation",
                        status_code=409,
                        details={
                            "matches": [item.model_dump(mode="json") for item in built_matches],
                            "missing_personas": [
                                item.model_dump(mode="json") for item in built_unresolved
                            ],
                            "seed": seed.model_dump(mode="json"),
                            "raw_actors": raw_actors,
                            "metadata": meta,
                            "classification": classification_result.model_dump(mode="json"),
                        },
                    )
                seed.metadata["initial_actors"] = raw_actors
                seed.metadata["entity_classification"] = classification_result.model_dump(
                    mode="json"
                )
                seed.metadata["entity_candidates"] = [
                    item.model_dump(mode="json") for item in classification_result.candidates
                ]
                if (
                    body.get("persona_completion_confirmed")
                    or body.get("actor_completion_confirmed")
                ) and (body.get("persona_bindings") or body.get("profile_bindings")):
                    bindings = dict(
                        body.get("profile_bindings")
                        or body.get("persona_bindings")
                        or {}
                    )
                    for item in seed.metadata["initial_actors"]:
                        if isinstance(item, dict) and str(item.get("id")) in bindings:
                            item["profile_id"] = str(bindings[str(item.get("id"))])
                            if str(item.get("profile_type") or "person") == "person":
                                item["persona_id"] = str(bindings[str(item.get("id"))])
                if body.get("persona_completion_confirmed") or body.get(
                    "actor_completion_confirmed"
                ):
                    bound_matches = self.continuum.persona_creation.match_world_actors(
                        [
                            item if isinstance(item, dict) else {"id": str(item), "name": str(item)}
                            for item in seed.metadata.get("initial_actors", [])
                        ]
                    )
                    still_unresolved = [
                        item
                        for item in bound_matches
                        if item.status in {"MISSING", "AMBIGUOUS"}
                        and str(item.actor_id) not in generated_ids
                    ]
                    if still_unresolved:
                        return json_err(
                            "persona_completion_incomplete",
                            status_code=409,
                            details={
                                "matches": [item.model_dump(mode="json") for item in bound_matches],
                                "missing_personas": [
                                    item.model_dump(mode="json") for item in still_unresolved
                                ],
                                "seed": seed.model_dump(mode="json"),
                                "raw_actors": seed.metadata.get("initial_actors", []),
                            },
                        )
                world, branch, state = self.continuum.worlds.create_world_from_seed(
                    seed=seed,
                    title=title,
                    description=description,
                    builder_runtime=builder_runtime,
                    default_actor_runtime=default_actor_runtime,
                    actor_runtime_configs=actor_runtime_configs,
                    director_runtime=director_runtime,
                    evaluator_runtime=evaluator_runtime,
                )
            else:
                if not description:
                    return json_err("description is required", status_code=400)
                world, branch, state = self.continuum.worlds.create_world(
                    description=description,
                    title=title,
                    baseline=body.get("baseline", "real_world"),
                    start_date=body.get("start_date"),
                    simulation_end=body.get("simulation_end", "2030"),
                    rules=body.get("rules"),
                    divergence_items=body.get("divergence"),
                    immutable_facts=body.get("immutable_facts"),
                    initial_actors=body.get("initial_actors"),
                    metadata={
                        **dict(body.get("metadata") or {}),
                        **({"initial_actors": raw_actors} if raw_actors else {}),
                    },
                    builder_runtime=builder_runtime,
                    default_actor_runtime=default_actor_runtime,
                    actor_runtime_configs=actor_runtime_configs,
                    director_runtime=director_runtime,
                    evaluator_runtime=evaluator_runtime,
                )

            return json_ok(
                {
                    "world": world.model_dump(mode="json"),
                    "branch": branch.model_dump(mode="json"),
                    "state": state.model_dump(mode="json"),
                },
                status_code=201,
            )
        except Exception as exc:
            return json_err(str(exc), status_code=400)

    async def list_world_bindings(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        bindings = self.continuum.worlds.repo.list_runtime_bindings(world_id)
        return json_ok([b.model_dump(mode="json") for b in bindings])

    async def get_world(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        world = self.continuum.worlds.get_world(world_id)
        if not world:
            return json_err(f"World not found: {world_id}", status_code=404)
        branches = self.continuum.worlds.list_branches(world_id)
        return json_ok(
            {
                "world": world.model_dump(mode="json"),
                "branches": [b.model_dump(mode="json") for b in branches],
            }
        )

    async def delete_world(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        success = self.continuum.worlds.delete_world(world_id)
        return json_ok({"deleted": success})

    async def pause_world(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        try:
            world = self.continuum.worlds.pause_world(world_id)
            return json_ok(world.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def resume_world(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        try:
            world = self.continuum.worlds.resume_world(world_id)
            return json_ok(world.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def list_world_branches(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branches = self.continuum.worlds.list_branches(world_id)
        return json_ok([b.model_dump(mode="json") for b in branches])

    async def fork_world_branch(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        try:
            body = await request.json()
            source_branch_id = body.get("source_branch_id", "")
            new_name = body.get("new_name", "Forked Branch")
            snapshot_id = body.get("snapshot_id")
            if not source_branch_id:
                return json_err("source_branch_id is required", status_code=400)

            branch = self.continuum.worlds.fork_branch(
                world_id=world_id,
                source_branch_id=source_branch_id,
                new_name=new_name,
                snapshot_id=snapshot_id,
            )
            return json_ok(branch.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return json_err(str(exc))

    async def get_world_branch(self, request: Request) -> Response:
        branch_id = request.path_params.get("branch_id", "")
        branch = self.continuum.worlds.get_branch(branch_id)
        if not branch:
            return json_err(f"Branch not found: {branch_id}", status_code=404)
        return json_ok(branch.model_dump(mode="json"))

    async def step_world_branch(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branch_id = request.path_params.get("branch_id", "")
        try:
            state, events, step_unit = await self.continuum.worlds.step_branch(world_id, branch_id)
            return json_ok(
                {
                    "state": state.model_dump(mode="json"),
                    "new_events": [e.model_dump(mode="json") for e in events],
                    "step_unit": step_unit,
                }
            )
        except Exception as exc:
            return json_err(str(exc))

    async def inject_world_action(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branch_id = request.path_params.get("branch_id", "")
        try:
            from persona_continuum.world.models import ActionType, ActorAction

            body = await request.json()
            actor_id = body.get("actor_id")
            if not actor_id:
                branch_actors = self.continuum.worlds.list_actors(
                    world_id, request.path_params.get("branch_id", "")
                )
                if not branch_actors:
                    return json_err(
                        "No actor_id provided and branch has no actors", status_code=400
                    )
                actor_id = branch_actors[0].id
            act = ActorAction(
                actor_id=actor_id,
                action_type=ActionType(body.get("action_type", "decide")),
                target=body.get("target"),
                description=body.get("description", "Direct action"),
                parameters=body.get("parameters", {}),
            )
            res, state, ev = self.continuum.worlds.inject_action(world_id, branch_id, act)
            return json_ok(
                {
                    "resolution": res.model_dump(mode="json"),
                    "state": state.model_dump(mode="json"),
                    "event": ev.model_dump(mode="json"),
                }
            )
        except Exception as exc:
            return json_err(str(exc))

    async def trigger_world_scene(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branch_id = request.path_params.get("branch_id", "")
        try:
            body = await request.json()
            actor_ids = body.get("actor_ids") or []
            if not actor_ids:
                branch_actors = self.continuum.worlds.list_actors(world_id, branch_id)
                actor_ids = [a.id for a in branch_actors[:2]]
            if not actor_ids:
                return json_err("No actor_ids provided and branch has no actors", status_code=400)
            topic = body.get("topic", "Open-ended scene")

            summary, ev = await self.continuum.worlds.trigger_scene(
                world_id=world_id, branch_id=branch_id, actor_ids=actor_ids, topic=topic
            )
            return json_ok({"summary": summary, "event": ev.model_dump(mode="json")})
        except Exception as exc:
            return json_err(str(exc))

    async def list_world_events(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branch_id = request.path_params.get("branch_id", "")
        events = self.continuum.worlds.list_events(world_id, branch_id)
        return json_ok([e.model_dump(mode="json") for e in events])

    async def list_world_actors(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branch_id = request.path_params.get("branch_id", "")
        actors = self.continuum.worlds.list_actors(world_id, branch_id)
        return json_ok([a.model_dump(mode="json") for a in actors])

    async def evaluate_world(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        try:
            body = await request.json()
            question = body.get(
                "question",
                "Which simulated branch outcome leads relative to the others?",
            )
            branch_count = safe_int(
                body.get("branch_count", 3), default=3, minimum=1, maximum=100
            ) or 3
            metrics = body.get("metrics")

            evaluation = await self.continuum.worlds.evaluate_question(
                world_id=world_id,
                question=question,
                branch_count=branch_count,
                metrics=metrics,
            )
            return json_ok(evaluation.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def query_world_causal(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branch_id = request.path_params.get("branch_id", "")
        target = request.query_params.get("target", "divergence")
        try:
            chain = self.continuum.worlds.query_causal_chain(world_id, branch_id, target)
            return json_ok(chain)
        except Exception as exc:
            return json_err(str(exc))

    async def get_world_replay(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branch_id = request.path_params.get("branch_id", "")
        try:
            traj = self.continuum.worlds.get_replay_trajectory(world_id, branch_id)
            return json_ok(traj.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def list_world_organizations(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branch_id = request.path_params.get("branch_id", "")
        try:
            orgs = self.continuum.worlds.list_organizations(world_id, branch_id)
            return json_ok({oid: o.model_dump(mode="json") for oid, o in orgs.items()})
        except Exception as exc:
            return json_err(str(exc))

    async def list_world_technologies(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branch_id = request.path_params.get("branch_id", "")
        try:
            techs = self.continuum.worlds.list_technologies(world_id, branch_id)
            return json_ok({tid: t.model_dump(mode="json") for tid, t in techs.items()})
        except Exception as exc:
            return json_err(str(exc))

    async def list_world_memories(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        branch_id = request.path_params.get("branch_id", "")
        persona_id = request.query_params.get("persona_id")
        try:
            mems = self.continuum.worlds.list_memories(world_id, branch_id, persona_id)
            return json_ok([m.model_dump(mode="json") for m in mems])
        except Exception as exc:
            return json_err(str(exc))

    async def get_world_report(self, request: Request) -> Response:
        world_id = request.path_params.get("world_id", "")
        try:
            report = self.continuum.worlds.generate_report(world_id)
            return json_ok(report.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    # ------------------------------------------------------------------
    # Narrative Studio
    # ------------------------------------------------------------------
    async def list_narrative_projects(self, request: Request) -> Response:
        projects = self.continuum.narratives.list_projects()
        return json_ok([p.model_dump(mode="json") for p in projects])

    async def create_narrative_project(self, request: Request) -> Response:
        try:
            body = await request.json()
            project = self.continuum.narratives.create_project(**body)
            return json_ok(project.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return json_err(str(exc))

    async def get_narrative_project(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            project = self.continuum.narratives.get_project(project_id)
        except KeyError as exc:
            return json_err(str(exc), status_code=404)
        return json_ok(project.model_dump(mode="json"))

    async def patch_narrative_project(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            project = self.continuum.narratives.update_project(project_id, body)
            return json_ok(project.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def delete_narrative_project(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        if not self.continuum.narratives.delete_project(project_id):
            return json_err(f"Narrative project not found: {project_id}", status_code=404)
        return json_ok({"deleted": True})

    async def get_narrative_bible(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        version = self._query_int(request, "version", default=0) or None
        bible = self.continuum.narratives.get_bible(project_id, version)
        if bible is None:
            return json_err("Story bible not found", status_code=404)
        return json_ok(bible.model_dump(mode="json"))

    async def update_narrative_bible(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            bible = self.continuum.narratives.save_bible(project_id, body)
            return json_ok(bible.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def generate_narrative_bible(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = (
                await request.json()
                if request.headers.get("content-type") == "application/json"
                else {}
            )
            bible = await self.continuum.narratives.generate_story_bible(
                project_id,
                runtime=body.get("runtime"),
                generation_mode=body.get("generation_mode"),
            )
            return json_ok(bible.model_dump(mode="json"))
        except Exception as exc:
            return narrative_json_err(exc)

    async def list_narrative_bible_versions(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        return json_ok(self.continuum.narratives.list_bible_versions(project_id))

    async def list_narrative_characters(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        chars = self.continuum.narratives.list_characters(project_id)
        return json_ok([c.model_dump(mode="json") for c in chars])

    async def add_narrative_character(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            character = self.continuum.narratives.add_character(project_id, **body)
            return json_ok(character.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return json_err(str(exc))

    async def bind_narrative_character(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            binding = self.continuum.narratives.bind_character(
                project_id,
                body.get("character_id", ""),
                persona_id=body.get("persona_id"),
                world_actor_id=body.get("world_actor_id"),
                unbind=bool(body.get("unbind")),
            )
            return json_ok(binding.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def create_missing_narrative_personas(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            created = self.continuum.narratives.create_missing_personas(project_id)
            return json_ok([c.model_dump(mode="json") for c in created])
        except Exception as exc:
            return json_err(str(exc))

    async def list_narrative_facts(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        facts = self.continuum.narratives.list_facts(project_id)
        return json_ok([f.model_dump(mode="json") for f in facts])

    async def add_narrative_fact(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            fact = self.continuum.narratives.add_fact(
                project_id,
                body.get("text", ""),
                secret=bool(body.get("secret", True)),
                category=body.get("category", "story_truth"),
            )
            return json_ok(fact.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return json_err(str(exc))

    async def get_narrative_knowledge(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        episode = self._query_int(request, "episode", default=0) or None
        return json_ok(self.continuum.narratives.get_knowledge_matrix(project_id, episode))

    async def set_narrative_knowledge(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            entry = self.continuum.narratives.set_character_knowledge(
                project_id,
                body.get("character_id", ""),
                body.get("fact_id", ""),
                body.get("state", "unknown"),
                learned_episode=body.get("learned_episode"),
                confidence=float(body.get("confidence", 0.0)),
                fact_text=body.get("fact_text", ""),
            )
            return json_ok(entry.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def get_narrative_audience_knowledge(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        episode = self._query_int(request, "episode", default=0) or None
        matrix = self.continuum.narratives.get_knowledge_matrix(project_id, episode)
        return json_ok(matrix["audience_knowledge"])

    async def set_narrative_audience_knowledge(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            entry = self.continuum.narratives.set_audience_knowledge(
                project_id,
                body.get("fact_id", ""),
                body.get("state", "hidden"),
                revealed_episode=body.get("revealed_episode"),
                fact_text=body.get("fact_text", ""),
            )
            return json_ok(entry.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def generate_narrative_outline(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = (
                await request.json()
                if request.headers.get("content-type") == "application/json"
                else {}
            )
            plans = await self.continuum.narratives.generate_outline(
                project_id,
                body.get("episode_count"),
                runtime=body.get("runtime"),
                generation_mode=body.get("generation_mode"),
            )
            return json_ok([p.model_dump(mode="json") for p in plans])
        except Exception as exc:
            return narrative_json_err(exc)

    async def list_narrative_episodes(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        plans = self.continuum.narratives.list_episode_plans(project_id)
        data = []
        for plan in plans:
            row = plan.model_dump(mode="json")
            row["versions"] = [
                version.model_dump(mode="json")
                for version in self.continuum.narratives.get_episode_versions(
                    project_id, plan.episode_number
                )
            ]
            row["scenes"] = [
                scene.model_dump(mode="json")
                for scene in self.continuum.narratives.list_scenes(
                    project_id, plan.episode_number
                )
            ]
            synthesis = self.continuum.narratives.get_writer_room_synthesis(
                project_id, plan.episode_number
            )
            row["writer_room_synthesis"] = (
                synthesis.model_dump(mode="json") if synthesis else None
            )
            audits = self.continuum.narratives.list_audits(
                project_id, plan.episode_number
            )
            row["latest_audit"] = audits[0].model_dump(mode="json") if audits else None
            data.append(row)
        return json_ok(data)

    async def get_narrative_episode(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            number = int(request.path_params.get("episode_number", "0"))
            plan = self.continuum.narratives.get_episode_plan(project_id, number)
        except (KeyError, ValueError) as exc:
            return json_err(str(exc), status_code=404)
        data = plan.model_dump(mode="json")
        data["versions"] = [
            v.model_dump(mode="json")
            for v in self.continuum.narratives.get_episode_versions(project_id, number)
        ]
        return json_ok(data)

    async def prepare_narrative_episode(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            number = int(request.path_params.get("episode_number", "0"))
            return json_ok(self.continuum.narratives.prepare_episode(project_id, number))
        except Exception as exc:
            return json_err(str(exc))

    async def forecast_narrative_episode(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            number = int(request.path_params.get("episode_number", "0"))
            forecast = await self.continuum.narratives.forecast_episode_async(
                project_id,
                number,
                body.get("directions", []),
                horizon_episodes=int(body.get("horizon_episodes", 5)),
                runtime=body.get("runtime"),
                generation_mode=body.get("generation_mode"),
            )
            return json_ok(forecast.model_dump(mode="json"))
        except Exception as exc:
            return narrative_json_err(exc)

    async def generate_narrative_forecast_directions(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            number = int(request.path_params.get("episode_number", "0"))
            result = await self.continuum.narratives.generate_forecast_directions(
                project_id,
                number,
                count=int(body.get("count", 3)),
                runtime=body.get("runtime"),
                generation_mode=body.get("generation_mode"),
            )
            return json_ok(result)
        except Exception as exc:
            return narrative_json_err(exc)

    async def list_narrative_forecasts(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        forecasts = self.continuum.narratives.list_forecasts(project_id)
        return json_ok([f.model_dump(mode="json") for f in forecasts])

    async def select_narrative_forecast(self, request: Request) -> Response:
        forecast_id = request.path_params.get("forecast_id", "")
        try:
            body = await request.json()
            forecast = self.continuum.narratives.select_forecast_direction(
                forecast_id, body.get("direction_id", "")
            )
            return json_ok(forecast.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def list_narrative_scenes(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            number = int(request.path_params.get("episode_number", "0"))
        except ValueError:
            return json_err("Invalid episode number")
        scenes = self.continuum.narratives.list_scenes(project_id, number)
        return json_ok([s.model_dump(mode="json") for s in scenes])

    async def create_narrative_scene(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            body.setdefault(
                "episode_number", int(request.path_params.get("episode_number", "0") or 0)
            )
            scene = self.continuum.narratives.create_scene(project_id, body)
            return json_ok(scene.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return json_err(str(exc))

    async def simulate_narrative_scene(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            scene_id = body.get("scene_id", "")
            scene = self.continuum.narratives.repo.get_scene(scene_id)
            if not scene:
                return json_err(f"Scene not found: {scene_id}", status_code=404)
            updated = await self.continuum.narratives.simulate_scene(
                project_id,
                scene,
                branch_id=body.get("branch_id"),
                runtime=body.get("runtime"),
                generation_mode=body.get("generation_mode"),
            )
            return json_ok(updated.model_dump(mode="json"))
        except Exception as exc:
            return narrative_json_err(exc)

    async def generate_narrative_draft(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            number = int(request.path_params.get("episode_number", "0"))
            body = (
                await request.json()
                if request.headers.get("content-type") == "application/json"
                else {}
            )
            version = await self.continuum.narratives.generate_episode_draft(
                project_id,
                number,
                runtime=body.get("runtime"),
                generation_mode=body.get("generation_mode"),
            )
            return json_ok(version.model_dump(mode="json"))
        except Exception as exc:
            return narrative_json_err(exc)

    async def list_narrative_episode_versions(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            number = int(request.path_params.get("episode_number", "0"))
        except ValueError:
            return json_err("Invalid episode number")
        versions = self.continuum.narratives.get_episode_versions(project_id, number)
        return json_ok([v.model_dump(mode="json") for v in versions])

    async def audit_narrative_episode(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            report = await self.continuum.narratives.audit_episode_async(
                project_id,
                body.get("version_id", ""),
                runtime=body.get("runtime"),
                generation_mode=body.get("generation_mode"),
            )
            return json_ok(report.model_dump(mode="json"))
        except Exception as exc:
            return narrative_json_err(exc)

    async def commit_narrative_episode(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            number = int(request.path_params.get("episode_number", "0"))
            result = self.continuum.narratives.commit_episode(
                project_id,
                number,
                body.get("version_id", ""),
                force=bool(body.get("force", False)),
                override_reason=body.get("override_reason", ""),
                canon_updates=body.get("canon_updates"),
            )
            return json_ok(result)
        except Exception as exc:
            return json_err(str(exc))

    async def generate_narrative_production(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            number = int(request.path_params.get("episode_number", "0"))
            body = (
                await request.json()
                if request.headers.get("content-type") == "application/json"
                else {}
            )
            package = await self.continuum.narratives.generate_production_package_async(
                project_id,
                number,
                body.get("version_id"),
                runtime=body.get("runtime"),
                generation_mode=body.get("generation_mode"),
                is_preview=bool(body.get("is_preview", False)),
            )
            return json_ok(package.model_dump(mode="json"))
        except Exception as exc:
            return narrative_json_err(exc)

    async def list_narrative_production_packages(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        packages = self.continuum.narratives.list_production_packages(project_id)
        return json_ok([p.model_dump(mode="json") for p in packages])

    async def get_narrative_canon(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        up_to = self._query_int(request, "up_to_episode", default=0) or None
        entries = self.continuum.narratives.repo.list_canon_entries(project_id, up_to)
        return json_ok([e.model_dump(mode="json") for e in entries])

    async def list_narrative_clues(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        clues = self.continuum.narratives.list_clues(project_id)
        return json_ok([c.model_dump(mode="json") for c in clues])

    async def add_narrative_clue(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            clue = self.continuum.narratives.add_clue(project_id, **body)
            return json_ok(clue.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return json_err(str(exc))

    async def update_narrative_clue(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        clue_id = request.path_params.get("clue_id", "")
        try:
            body = await request.json()
            clues = self.continuum.narratives.list_clues(project_id)
            clue = next((c for c in clues if c.id == clue_id), None)
            if not clue:
                return json_err(f"Clue not found: {clue_id}", status_code=404)
            for key, value in body.items():
                if hasattr(clue, key):
                    setattr(clue, key, value)
            self.continuum.narratives.update_clue(clue)
            return json_ok(clue.model_dump(mode="json"))
        except Exception as exc:
            return json_err(str(exc))

    async def list_narrative_threads(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        threads = self.continuum.narratives.list_plot_threads(project_id)
        return json_ok([t.model_dump(mode="json") for t in threads])

    async def list_narrative_arcs(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        arcs = self.continuum.narratives.list_arcs(project_id)
        return json_ok([a.model_dump(mode="json") for a in arcs])

    async def add_narrative_thread(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            thread = self.continuum.narratives.add_plot_thread(project_id, **body)
            return json_ok(thread.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return json_err(str(exc))

    async def run_narrative_writer_room(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            number = int(
                request.path_params.get("episode_number")
                or body.get("episode_number", 0)
            )
            result = await self.continuum.narratives.run_writer_room(
                project_id,
                number,
                body.get("participants", []),
                cross_review=bool(body.get("cross_review", True)),
            )
            return json_ok(result)
        except Exception as exc:
            return narrative_json_err(exc)

    async def get_narrative_writer_room(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            number = int(request.path_params.get("episode_number", "0"))
            synthesis = self.continuum.narratives.get_writer_room_synthesis(
                project_id, number
            )
            return json_ok(synthesis.model_dump(mode="json") if synthesis else None)
        except Exception as exc:
            return narrative_json_err(exc)

    async def list_narrative_jobs(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        return json_ok(self.continuum.narratives.list_jobs(project_id))

    async def list_all_narrative_jobs(self, request: Request) -> Response:
        return json_ok(self.continuum.narratives.list_jobs())

    async def create_narrative_job(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            job = self.continuum.narratives.create_job(
                body.get("kind", ""), project_id, body.get("payload") or {}
            )
            return json_ok(job, status_code=202)
        except Exception as exc:
            return json_err(str(exc))

    async def get_narrative_job(self, request: Request) -> Response:
        job_id = request.path_params.get("job_id", "")
        try:
            return json_ok(self.continuum.narratives.get_job(job_id))
        except KeyError as exc:
            return json_err(str(exc), status_code=404)

    async def pause_narrative_job(self, request: Request) -> Response:
        job_id = request.path_params.get("job_id", "")
        try:
            self.continuum.narratives.pause_job(job_id)
            return json_ok(self.continuum.narratives.get_job(job_id))
        except Exception as exc:
            return json_err(str(exc))

    async def resume_narrative_job(self, request: Request) -> Response:
        job_id = request.path_params.get("job_id", "")
        try:
            return json_ok(self.continuum.narratives.resume_job(job_id))
        except Exception as exc:
            return json_err(str(exc))

    async def retry_narrative_job(self, request: Request) -> Response:
        job_id = request.path_params.get("job_id", "")
        try:
            return json_ok(self.continuum.narratives.retry_job(job_id))
        except Exception as exc:
            return json_err(str(exc))

    async def cancel_narrative_job(self, request: Request) -> Response:
        job_id = request.path_params.get("job_id", "")
        try:
            self.continuum.narratives.cancel_job(job_id)
            return json_ok({"cancel_requested": True})
        except Exception as exc:
            return json_err(str(exc))

    async def dismiss_narrative_job(self, request: Request) -> Response:
        job_id = request.path_params.get("job_id", "")
        try:
            dismissed = self.continuum.narratives.dismiss_job(job_id)
        except Exception as exc:
            return json_err(str(exc), status_code=409)
        if not dismissed:
            return json_err(f"Narrative job not found: {job_id}", status_code=404)
        return json_ok({"dismissed": True})

    # ------------------------------------------------------------------
    # Narrative Director Agent
    # ------------------------------------------------------------------
    async def create_director_session(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = (
                await request.json()
                if request.headers.get("content-type") == "application/json"
                else {}
            )
            episode = body.get("episode_number")
            session = self.continuum.narrative_director.create_session(
                project_id,
                episode_number=int(episode) if episode else None,
                mode=str(body.get("mode") or "agent"),
                runtime=body.get("runtime"),
            )
            return json_ok(session.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return narrative_json_err(exc)

    async def list_director_sessions(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            sessions = self.continuum.narrative_director.list_sessions(project_id)
            return json_ok([s.model_dump(mode="json") for s in sessions])
        except Exception as exc:
            return narrative_json_err(exc)

    async def get_director_session(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            return json_ok(self.continuum.narrative_director.session_snapshot(session_id))
        except KeyError as exc:
            return json_err(str(exc), status_code=404)
        except Exception as exc:
            return narrative_json_err(exc)

    async def send_director_message(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            body = await request.json()
            snapshot = self.continuum.narrative_director.send_message(
                session_id,
                str(body.get("content") or ""),
                runtime=body.get("runtime"),
            )
            return json_ok(snapshot, status_code=202)
        except Exception as exc:
            return narrative_json_err(exc)

    async def update_director_session(self, request: Request) -> Response:
        """PATCH-style session update: mode and/or runtime override."""
        session_id = request.path_params.get("session_id", "")
        try:
            body = await request.json()
            if body.get("mode") or body.get("runtime"):
                session = self.continuum.narrative_director.update_mode(
                    session_id,
                    str(body["mode"]) if body.get("mode") else None,
                    runtime=body.get("runtime"),
                )
                return json_ok(session.model_dump(mode="json"))
            return json_err("Nothing to update")
        except Exception as exc:
            return narrative_json_err(exc)

    async def pause_director_session(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            session = self.continuum.narrative_director.pause_session(session_id)
            return json_ok(session.model_dump(mode="json"))
        except Exception as exc:
            return narrative_json_err(exc)

    async def resume_director_session(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            session = self.continuum.narrative_director.resume_session(session_id)
            return json_ok(session.model_dump(mode="json"))
        except Exception as exc:
            return narrative_json_err(exc)

    async def cancel_director_session(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            session = self.continuum.narrative_director.cancel_session(session_id)
            return json_ok(session.model_dump(mode="json"))
        except Exception as exc:
            return narrative_json_err(exc)

    async def list_director_actions(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            self.continuum.narrative_director.get_session(session_id)
            actions = self.continuum.narrative_repo.list_director_actions(session_id)
            return json_ok([a.model_dump(mode="json") for a in actions])
        except KeyError as exc:
            return json_err(str(exc), status_code=404)
        except Exception as exc:
            return narrative_json_err(exc)

    # ------------------------------------------------------------------
    # Narrative Shooting / Video Production pipeline
    # ------------------------------------------------------------------
    async def list_video_model_profiles(self, request: Request) -> Response:
        try:
            items = [
                {
                    **profile_capabilities_digest(profile),
                    "official_sources": profile.official_sources,
                }
                for profile in list_profiles()
            ]
            return json_ok(items)
        except Exception as exc:
            return shooting_json_err(exc)

    async def get_video_model_profile(self, request: Request) -> Response:
        try:
            profile = get_profile(str(request.path_params.get("profile_id", "")))
            return json_ok(profile.model_dump(mode="json"))
        except ValueError as exc:
            # A GET on a registry resource that does not exist is 404 (the
            # 422 mapping stays for request-payload validation paths).
            return json_err(str(exc), status_code=404)
        except Exception as exc:
            return shooting_json_err(exc)

    async def list_prompt_packages(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        try:
            packages = self.continuum.narratives.repo.list_model_prompt_packages(
                project_id, package_id
            )
            return json_ok([prompt_package_payload(p) for p in packages])
        except Exception as exc:
            return shooting_json_err(exc)

    async def export_prompt_package_zip(self, request: Request) -> Response:
        """One-click export: every clip of every non-stale prompt package of
        one production package, zipped as per-clip prompt sheets + manifest.
        """
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        try:
            repo = self.continuum.narratives.repo
            production = repo.get_production_package(package_id)
            if production is None:
                return json_err(
                    f"Production package not found: {package_id}", status_code=404
                )
            # repo rows are newest-first; export folders read best in
            # creation order (oldest plan first).
            packages = list(
                reversed(
                    [
                        p
                        for p in repo.list_model_prompt_packages(project_id, package_id)
                        if not p.stale and p.clips
                    ]
                )
            )
            if not packages:
                return json_err(
                    "No clips to export for this production package", status_code=404
                )
            assets_by_id = {
                asset.id: asset
                for asset in repo.list_production_assets(project_id, package_id)
            }
            archive, filename = build_clip_export_zip(
                production, packages, assets_by_id
            )
            return Response(
                content=archive,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception as exc:
            return shooting_json_err(exc)

    def _validated_prompt_package_source(self, package_id: str) -> Any:
        """Fail fast on the canon gate / preview-source rules for one
        production package, with the same semantics as the pipeline itself,
        so callers return 409 instead of 202 + a failing background job.
        """
        production = self.continuum.narratives.repo.get_production_package(package_id)
        if production is None:
            raise KeyError(f"Production package not found: {package_id}")
        if production.is_preview:
            raise NarrativeAgentError(
                SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED,
                f"Production package {package_id} is a preview; "
                "generate the final package from the canon episode version first.",
                stage="model_prompt_package",
            )
        version = self.continuum.narratives.repo.get_episode_version(
            production.episode_version_id or ""
        )
        if version is None or not version.is_canon:
            raise NarrativeAgentError(
                NARRATIVE_PRODUCTION_CANON_REQUIRED,
                f"Production package {package_id} does not reference the "
                f"canon episode version of EP{production.episode_number}.",
                stage="model_prompt_package",
            )
        return production

    async def create_prompt_package_job(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        try:
            body = await request.json()
            payload = {"production_package_id": package_id, **(body or {})}
            profile_id = str(payload.get("profile_id") or "")
            if not profile_id:
                return json_err("profile_id is required")
            # Fail fast on an unknown profile (ValueError → 422 below)
            # instead of letting the background job fail later.
            get_profile(profile_id)
            self._validated_prompt_package_source(package_id)
            job = self.continuum.narratives.create_job(
                "model_prompt_package", project_id, payload
            )
            return json_ok(job, status_code=202)
        except Exception as exc:
            return shooting_json_err(exc)

    async def create_clip_plan_job(self, request: Request) -> Response:
        """Two-step flow, step 1: plan-only run (stop_after_plan=True).

        Persists the package at the ``clip_planned`` checkpoint for human
        review; prompt compilation happens later via compile-prompts.
        """
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        try:
            body = await request.json()
            payload = {"production_package_id": package_id, **(body or {})}
            profile_id = str(payload.get("profile_id") or "")
            if not profile_id:
                return json_err("profile_id is required")
            get_profile(profile_id)
            self._validated_prompt_package_source(package_id)
            payload["stop_after_plan"] = True
            job = self.continuum.narratives.create_job(
                "model_prompt_package", project_id, payload
            )
            return json_ok(job, status_code=202)
        except Exception as exc:
            return shooting_json_err(exc)

    async def get_narrative_clip_plan(self, request: Request) -> Response:
        """Resolve the latest non-stale clip-plan package of one production
        package; the payload shape matches GET prompt-packages/{id}.
        """
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        try:
            packages = self.continuum.narratives.repo.list_model_prompt_packages(
                project_id, package_id
            )
            # Rows come back ordered by created_at DESC: first non-stale wins.
            for package in packages:
                if not package.stale:
                    return json_ok(prompt_package_payload(package))
            return json_err(
                f"Clip plan not found for production package: {package_id}",
                status_code=404,
            )
        except Exception as exc:
            return shooting_json_err(exc)

    async def create_compile_prompts_job(self, request: Request) -> Response:
        """Two-step flow, step 2: continue a reviewed ``clip_planned``
        package from the compiling checkpoint (reuse existing package;
        status transitions compiling → ready). Option fields default to the
        package's stored values so the same package is resumed; explicit
        overrides follow the normal recompile semantics.
        """
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        try:
            package = self.continuum.narratives.repo.get_model_prompt_package(package_id)
            if package is None or package.project_id != project_id:
                return json_err(
                    f"Model prompt package not found: {package_id}", status_code=404
                )
            try:
                body = await request.json()
            except Exception:
                body = {}
            body = body or {}
            payload = {
                "production_package_id": package.production_package_id,
                "profile_id": str(body.get("profile_id") or package.target_profile_id),
                "aspect_ratio": str(body.get("aspect_ratio") or package.aspect_ratio),
                "quality_priority": str(
                    body.get("quality_priority") or package.quality_priority
                ),
                "generation_strategy": str(
                    body.get("generation_strategy") or package.generation_strategy
                ),
                "continuity_strategy": str(
                    body.get("continuity_strategy") or package.continuity_strategy
                ),
                "audio_strategy": str(body.get("audio_strategy") or package.audio_strategy),
                "prompt_language": str(body.get("prompt_language") or package.prompt_language),
            }
            get_profile(str(payload["profile_id"]))
            self._validated_prompt_package_source(str(package.production_package_id))
            job = self.continuum.narratives.create_job(
                "model_prompt_package", project_id, payload
            )
            return json_ok(job, status_code=202)
        except Exception as exc:
            return shooting_json_err(exc)

    async def get_prompt_package(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        try:
            package = self.continuum.narratives.repo.get_model_prompt_package(package_id)
            if package is None or package.project_id != project_id:
                return json_err(
                    f"Model prompt package not found: {package_id}", status_code=404
                )
            return json_ok(prompt_package_payload(package))
        except Exception as exc:
            return shooting_json_err(exc)

    async def patch_prompt_package_clip(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        clip_id = request.path_params.get("clip_id", "")
        try:
            body = await request.json()
        except Exception:
            return json_err("Request body must be JSON")
        patch_fields = (
            "prompt",
            "negative_prompt",
            "audio_prompt",
            "generation_mode",
            "reference_asset_ids",
            "duration_seconds",
        )
        patch = {key: body[key] for key in patch_fields if key in (body or {})}
        if not patch:
            return json_err("Nothing to update")
        try:
            clip = self.continuum.narrative_shooting.patch_clip(
                project_id, package_id, clip_id, patch
            )
            return json_ok(clip.model_dump(mode="json"))
        except Exception as exc:
            return shooting_json_err(exc)

    async def list_narrative_production_assets(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        package_id = request.query_params.get("production_package_id") or None
        try:
            assets = self.continuum.narratives.repo.list_production_assets(
                project_id, package_id
            )
            return json_ok([a.model_dump(mode="json") for a in assets])
        except Exception as exc:
            return shooting_json_err(exc)

    async def create_narrative_production_asset(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = await request.json()
            asset_type = str(body.get("asset_type") or "")
            name = str(body.get("name") or "")
            if not asset_type or not name:
                return json_err("asset_type and name are required")
            self.continuum.narratives.get_project(project_id)  # existence check
            metadata = dict(body.get("metadata") or {})
            if body.get("production_package_id"):
                metadata["production_package_id"] = str(body["production_package_id"])
            episode = body.get("episode_number")
            asset = ProductionAsset(
                project_id=project_id,
                episode_number=int(episode) if episode is not None else None,
                asset_type=asset_type,
                name=name,
                character_id=body.get("character_id") or None,
                location_id=body.get("location_id") or None,
                source_uri=body.get("source_uri") or None,
                local_path=body.get("local_path") or None,
                description=str(body.get("description") or ""),
                metadata=metadata,
            )
            self.continuum.narratives.repo.save_production_asset(asset)
            return json_ok(asset.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return shooting_json_err(exc)

    async def create_shooting_session(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            body = (
                await request.json()
                if request.headers.get("content-type") == "application/json"
                else {}
            )
            episode = body.get("episode_number")
            session = self.continuum.narrative_shooting.create_session(
                project_id,
                episode_number=int(episode) if episode else None,
                mode=str(body.get("mode") or "agent"),
                runtime=body.get("runtime"),
            )
            return json_ok(session.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return shooting_json_err(exc)

    async def list_shooting_sessions(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        try:
            sessions = self.continuum.narrative_shooting.list_sessions(project_id)
            return json_ok([s.model_dump(mode="json") for s in sessions])
        except Exception as exc:
            return shooting_json_err(exc)

    async def get_shooting_session(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            return json_ok(
                self.continuum.narrative_shooting.session_snapshot(session_id)
            )
        except Exception as exc:
            return shooting_json_err(exc)

    async def send_shooting_message(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            body = await request.json()
            snapshot = self.continuum.narrative_shooting.send_message(
                session_id,
                str(body.get("content") or ""),
                mode=str(body["mode"]) if body.get("mode") else None,
                runtime=body.get("runtime"),
            )
            return json_ok(snapshot, status_code=202)
        except Exception as exc:
            return shooting_json_err(exc)

    async def update_shooting_session(self, request: Request) -> Response:
        """PATCH-style session update: mode and/or runtime override."""
        session_id = request.path_params.get("session_id", "")
        try:
            body = await request.json()
            if body.get("mode") or body.get("runtime"):
                session = self.continuum.narrative_shooting.update_mode(
                    session_id,
                    str(body["mode"]) if body.get("mode") else None,
                    runtime=body.get("runtime"),
                )
                return json_ok(session.model_dump(mode="json"))
            return json_err("Nothing to update")
        except Exception as exc:
            return shooting_json_err(exc)

    async def pause_shooting_session(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            session = self.continuum.narrative_shooting.pause_session(session_id)
            return json_ok(session.model_dump(mode="json"))
        except Exception as exc:
            return shooting_json_err(exc)

    async def resume_shooting_session(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            session = self.continuum.narrative_shooting.resume_session(session_id)
            return json_ok(session.model_dump(mode="json"))
        except Exception as exc:
            return shooting_json_err(exc)

    async def cancel_shooting_session(self, request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            session = self.continuum.narrative_shooting.cancel_session(session_id)
            return json_ok(session.model_dump(mode="json"))
        except Exception as exc:
            return shooting_json_err(exc)

    async def create_production_guide_job(self, request: Request) -> Response:
        """POST production/{package_id}/production-guide → guide job (202).

        Resolves the LATEST non-stale ``ready`` prompt package of the
        production master and schedules the ``video_production_guide`` job;
        fail-fast validation returns 404/409 instead of a doomed background job.
        """
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        try:
            try:
                body = await request.json()
            except Exception:
                body = {}
            body = body or {}
            options = {
                key: str(body[key])
                for key in ("prompt_language", "title", "notes")
                if body.get(key)
            }
            self._validated_prompt_package_source(package_id)
            packages = self.continuum.narratives.repo.list_model_prompt_packages(
                project_id, package_id
            )
            # Rows come back ordered by created_at DESC: first ready wins.
            source = next(
                (p for p in packages if p.status == "ready" and not p.stale), None
            )
            if source is None:
                raise NarrativeAgentError(
                    VIDEO_GUIDE_SOURCE_NOT_READY,
                    f"No ready prompt package for production package {package_id}; "
                    "compile the video generation plan first.",
                    stage="video_production_guide",
                )
            job = self.continuum.narratives.create_job(
                "video_production_guide",
                project_id,
                {
                    "project_id": project_id,
                    "prompt_package_id": source.id,
                    "options": options,
                },
            )
            return json_ok(job, status_code=202)
        except Exception as exc:
            return shooting_json_err(exc)

    async def create_complete_video_production_job(self, request: Request) -> Response:
        """POST production/{package_id}/complete-plan → one-click job (202).

        Task #5/#6: the ONLY Simple Mode action — clip plan + prompt package +
        production guide compiled as ONE background job. Fail-fast validation
        (canon master, non-preview package, known profile) returns 404/409/422
        instead of a doomed background job.
        """
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        try:
            try:
                body = await request.json()
            except Exception:
                body = {}
            body = body or {}
            target_profile_id = str(
                body.get("target_profile_id") or body.get("profile_id") or ""
            )
            if not target_profile_id:
                return json_err("target_profile_id is required")
            get_profile(target_profile_id)
            self._validated_prompt_package_source(package_id)
            job = self.continuum.narratives.create_job(
                "complete_video_production",
                project_id,
                {
                    "production_package_id": package_id,
                    "target_profile_id": target_profile_id,
                    "aspect_ratio": str(body.get("aspect_ratio") or "16:9"),
                    "quality_priority": str(body.get("quality_priority") or "balanced"),
                    "generation_strategy": str(body.get("generation_strategy") or "auto"),
                    "continuity_strategy": str(body.get("continuity_strategy") or "auto"),
                    "audio_strategy": str(body.get("audio_strategy") or "auto"),
                    "prompt_language": str(body.get("prompt_language") or "auto"),
                },
            )
            return json_ok(job, status_code=202)
        except Exception as exc:
            return shooting_json_err(exc)

    async def get_production_guide_for_package(self, request: Request) -> Response:
        """GET production/{package_id}/production-guide → latest guide payload.

        The UI polls one lightweight lookup per production master; rows come
        back ordered by created_at DESC, so the first row is the latest guide.
        """
        project_id = request.path_params.get("project_id", "")
        package_id = request.path_params.get("package_id", "")
        try:
            guides = self.continuum.narratives.repo.list_video_production_guides(
                project_id, production_package_id=package_id
            )
            if not guides:
                return json_err(
                    f"Production guide not found for production package: {package_id}",
                    status_code=404,
                )
            return json_ok(
                production_guide_payload(guides[0], self.continuum.narratives.repo)
            )
        except Exception as exc:
            return shooting_json_err(exc)

    async def get_production_guide(self, request: Request) -> Response:
        project_id = request.path_params.get("project_id", "")
        guide_id = request.path_params.get("guide_id", "")
        try:
            guide = self.continuum.narratives.repo.get_video_production_guide(guide_id)
            if guide is None or guide.project_id != project_id:
                return json_err(
                    f"Production guide not found: {guide_id}", status_code=404
                )
            return json_ok(
                production_guide_payload(guide, self.continuum.narratives.repo)
            )
        except Exception as exc:
            return shooting_json_err(exc)

    async def export_production_guide_markdown(self, request: Request) -> Response:
        """Serve the persisted ``markdown_document`` as a download (no re-render)."""
        project_id = request.path_params.get("project_id", "")
        guide_id = request.path_params.get("guide_id", "")
        try:
            guide = self.continuum.narratives.repo.get_video_production_guide(guide_id)
            if guide is None or guide.project_id != project_id:
                return json_err(
                    f"Production guide not found: {guide_id}", status_code=404
                )
            filename = (
                f"guide-ep{guide.episode_number:02d}-"
                f"{_safe_zip_name(guide.id or guide.production_package_id)}.md"
            )
            return Response(
                content=guide.markdown_document.encode("utf-8"),
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception as exc:
            return shooting_json_err(exc)


# ---------------------------------------------------------------------
# Narrative Shooting / Video Production: error mapping and helpers
# ---------------------------------------------------------------------

# Spec'd HTTP mapping for Shooting/video-profile failures. Unmapped
# NarrativeAgentError codes fall through to narrative_json_err (502).
_SHOOTING_ERROR_STATUS: dict[str, int] = {
    "NARRATIVE_PRODUCTION_CANON_REQUIRED": 409,
    "SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED": 409,
    "SHOOTING_ACTION_NOT_ALLOWED": 403,
    "SHOOTING_SESSION_BUSY": 409,
    "SHOOTING_SESSION_NOT_FOUND": 404,
    "VIDEO_PROFILE_NOT_FOUND": 422,
    "VIDEO_PROFILE_DURATION_UNSUPPORTED": 422,
    "VIDEO_PROFILE_ASPECT_RATIO_UNSUPPORTED": 422,
    "VIDEO_PROFILE_MODE_UNSUPPORTED": 422,
    "VIDEO_GUIDE_SOURCE_NOT_READY": 409,
    "SHOOTING_LOCATION_CONTEXT_MISSING": 422,
}


def shooting_json_err(exc: Exception) -> JSONResponse:
    """Map Shooting/video-profile failures onto their spec'd HTTP statuses."""
    if isinstance(exc, NarrativeAgentError):
        status = _SHOOTING_ERROR_STATUS.get(exc.code)
        if status is not None:
            return json_err(exc.code, status_code=status, details=exc.to_dict())
        return narrative_json_err(exc)
    message = str(exc)
    for code, status in _SHOOTING_ERROR_STATUS.items():
        if code in message:
            return json_err(message, status_code=status)
    if isinstance(exc, KeyError):
        return json_err(message.strip("'\""), status_code=404)
    return narrative_json_err(exc)


def _derive_profile_update_available(package: Any) -> bool:
    """Spec §8: derive at read time from the registry, never the stored flag."""
    try:
        profile = get_profile(package.target_profile_id)
    except ValueError:
        return False
    return str(package.target_profile_version) != str(profile.profile_version)


def prompt_package_payload(package: Any) -> dict[str, Any]:
    """Serialize a ModelPromptPackage with the derived update-available flag."""
    data: dict[str, Any] = package.model_dump(mode="json")
    data["profile_update_available"] = _derive_profile_update_available(package)
    return data


def production_guide_payload(guide: Any, repo: Any) -> dict[str, Any]:
    """Serialize an ExecutableVideoProductionGuide with derived staleness.

    ``stale`` is the stored flag OR a stale source prompt package OR a stale
    production master; ``profile_update_available`` is derived at read time
    from the registry, never the stored flag (mirrors prompt_package_payload).
    """
    data: dict[str, Any] = guide.model_dump(mode="json")
    stale = bool(guide.stale)
    prompt_package = repo.get_model_prompt_package(guide.prompt_package_id)
    if prompt_package is not None and prompt_package.stale:
        stale = True
    production = repo.get_production_package(guide.production_package_id)
    if production is not None and production.stale:
        stale = True
    data["stale"] = stale
    data["profile_update_available"] = _derive_profile_update_available(guide)
    return data


def _clip_export_text(package: Any, clip: Any, assets_by_id: dict[str, Any]) -> str:
    """Human-readable prompt sheet for one clip (mirrors the UI copy)."""

    def asset_label(asset_id: str) -> str:
        asset = assets_by_id.get(asset_id)
        return f"{asset.name} ({asset_id})" if asset is not None else f"{asset_id}（未注册）"

    shots = ", ".join(str(n) for n in clip.source_shot_numbers)
    rows = [
        f"Clip {clip.clip_number} · {clip.duration_seconds}s",
        f"目标模型：{package.target_video_model_display_name or package.target_profile_id}"
        f"（profile: {package.target_profile_id} v{package.target_profile_version}）",
        f"生成模式：{clip.generation_mode}",
        f"画面比例：{clip.aspect_ratio or package.aspect_ratio}",
        f"来源 Shots：{shots or '—'}",
    ]
    if clip.purpose:
        rows.append(f"用途：{clip.purpose}")
    rows.append(
        f"参考素材：{'、'.join(asset_label(a) for a in clip.reference_asset_ids) or '无'}"
    )
    if clip.continuity_constraints:
        rows.append(f"连续性约束：{'；'.join(clip.continuity_constraints)}")
    rows.append(
        f"生成 Prompt：\n{clip.prompt or '（尚未编译 Prompt：方案仍处于 clip_planned 审阅状态）'}"
    )
    if clip.audio_prompt:
        rows.append(f"音频 Prompt：\n{clip.audio_prompt}")
    if clip.negative_prompt:
        rows.append(f"负面 Prompt：\n{clip.negative_prompt}")
    if clip.recommended_settings:
        settings = "\n".join(
            f"- {key} = {json.dumps(value, ensure_ascii=False)}"
            for key, value in clip.recommended_settings.items()
        )
        rows.append(f"推荐设置：\n{settings}")
    if clip.planning_rationale:
        rows.append(f"规划依据：{clip.planning_rationale}")
    return "\n\n".join(rows)


def _safe_zip_name(raw: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", raw).strip("-")
    return cleaned or "plan"


def build_clip_export_zip(
    production: Any,
    packages: list[Any],
    assets_by_id: dict[str, Any],
) -> tuple[bytes, str]:
    """Pack every clip of the given prompt packages into one zip.

    Layout: README.md + manifest.json + one folder per package holding one
    prompt-sheet text file per clip. Returns ``(zip bytes, filename)``; the
    filename stays ASCII so Content-Disposition needs no RFC 5987 escaping.
    """
    buffer = io.BytesIO()
    generated_at = datetime.now(UTC).isoformat()
    manifest: dict[str, Any] = {
        "generated_at": generated_at,
        "episode_number": production.episode_number,
        "production_package_id": production.id,
        "packages": [],
    }
    readme: list[str] = [
        "# Clip 导出",
        "",
        f"生成时间：{generated_at}",
        f"制作母版：{production.id}（EP{production.episode_number}）",
        "",
    ]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for plan_index, package in enumerate(packages, start=1):
            folder = f"{plan_index:02d}-{_safe_zip_name(package.target_profile_id)}"
            display = (
                package.target_video_model_display_name or package.target_profile_id
            )
            manifest["packages"].append(
                {
                    "id": package.id,
                    "folder": folder,
                    "target_profile_id": package.target_profile_id,
                    "target_profile_version": package.target_profile_version,
                    "target_video_model_display_name": display,
                    "status": package.status,
                    "aspect_ratio": package.aspect_ratio,
                    "clip_count": len(package.clips),
                    "clips": [clip.model_dump(mode="json") for clip in package.clips],
                }
            )
            readme.append(
                f"## {display}（{folder}/，{len(package.clips)} 个 Clip，"
                f"状态 {package.status}）"
            )
            for clip in package.clips:
                name = f"{folder}/clip-{clip.clip_number:02d}.txt"
                zf.writestr(name, _clip_export_text(package, clip, assets_by_id))
                readme.append(f"- Clip {clip.clip_number} → {name}")
            readme.append("")
        zf.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        zf.writestr("README.md", "\n".join(readme))
    episode = int(production.episode_number or 0)
    filename = f"clips-ep{episode:02d}-{_safe_zip_name(production.id)}.zip"
    return buffer.getvalue(), filename
