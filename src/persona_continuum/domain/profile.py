from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona_continuum.numeric import safe_int


def profile_now() -> str:
    return datetime.now(UTC).isoformat()


class ProfileType(StrEnum):
    PERSONA = "persona"
    ORGANIZATION = "organization"
    INSTITUTION = "institution"
    COLLECTIVE = "collective"


class ProfileStatus(StrEnum):
    DRAFT = "draft"
    RESEARCHING = "researching"
    READY = "ready"
    COMPILED = "compiled"
    COMPLETED_WITH_GAPS = "completed_with_gaps"
    ARCHIVED = "archived"
    ERROR = "error"


class ProfileCoverageState(StrEnum):
    UNKNOWN = "unknown"
    PARTIAL = "partial"
    COMPLETE = "complete"
    GAPS = "gaps"


class ActorProfile(BaseModel):
    """Unified library record for every entity that can become a World Agent.

    A persona profile keeps ``persona_id`` and therefore delegates evidence,
    memory and compilation to the existing Persona/Compilation services.  The
    payload is only for type-specific profile metadata; it is not a replacement
    Persona package or an unsourced system prompt.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    profile_type: ProfileType
    display_name: str
    slug: str
    aliases: list[str] = Field(default_factory=list)
    summary: str = ""
    status: ProfileStatus = ProfileStatus.DRAFT
    source_count: int = 0
    evidence_count: int = 0
    coverage_state: ProfileCoverageState = ProfileCoverageState.UNKNOWN
    compile_state: str = "draft"
    created_at: str = Field(default_factory=profile_now)
    updated_at: str = Field(default_factory=profile_now)
    version: int = 1
    persona_id: str | None = None
    runtime_snapshot: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class PersonaProfile(ActorProfile):
    profile_type: ProfileType = ProfileType.PERSONA
    persona_id: str


class OrganizationProfile(ActorProfile):
    profile_type: ProfileType = ProfileType.ORGANIZATION


class InstitutionProfile(ActorProfile):
    profile_type: ProfileType = ProfileType.INSTITUTION


class CollectiveProfile(ActorProfile):
    profile_type: ProfileType = ProfileType.COLLECTIVE


class ProfileVersion(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    profile_id: str
    version: int
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=profile_now)
    created_by: str = "profile_runtime"


class ProfileEnrichmentStatus(StrEnum):
    CREATED = "created"
    RESEARCHING = "researching"
    INGESTING = "ingesting"
    COMPILING = "compiling"
    COMPLETED = "completed"
    COMPLETED_WITH_GAPS = "completed_with_gaps"
    PAUSED = "paused"
    PAUSED_RUNTIME_UNAVAILABLE = "paused_runtime_unavailable"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EnrichmentInputMode(StrEnum):
    LOCAL_MATERIALS = "local_materials"
    WEB_RESEARCH = "web_research"
    HYBRID = "hybrid"


class ProfileEnrichmentJob(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    target_profile_id: str
    target_profile_type: ProfileType
    job_type: str = "upgrade"
    selected_runtime: dict[str, Any] = Field(default_factory=dict)
    research_policy: dict[str, Any] = Field(default_factory=dict)
    requested_scope: str = "full_refresh"
    enrichment_input_mode: EnrichmentInputMode = EnrichmentInputMode.LOCAL_MATERIALS
    # Optional user steering for web research ("着重研究她的晚年作品" etc).
    # Empty means the model plans the research direction itself.  Carried into
    # the child persona-creation job's research_instructions.
    research_focus: str | None = None
    status: ProfileEnrichmentStatus = ProfileEnrichmentStatus.CREATED
    visibility: str = "user"
    dismissed_at: str | None = None
    superseded_by: str | None = None
    worker_state: str = "starting"
    worker_started_at: str | None = None
    worker_heartbeat_at: str | None = None
    worker_finished_at: str | None = None
    agent_call_count: int = 0
    progress: dict[str, Any] = Field(default_factory=dict)
    failure_json: dict[str, Any] | None = None
    agent_call_audits: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    source_count: int = 0
    new_version: int | None = None
    persona_creation_job_id: str | None = None
    parent_job_id: str | None = None
    base_persona_version: int | None = None
    input_material_ids: list[str] = Field(default_factory=list)
    input_material_count: int = 0
    new_source_ids: list[str] = Field(default_factory=list)
    evidence_delta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=profile_now)
    updated_at: str = Field(default_factory=profile_now)

    @model_validator(mode="before")
    @classmethod
    def _normalise_persisted_numbers(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data: dict[str, Any] = dict(value)
        progress_value = data.get("progress")
        progress: dict[str, Any] = (
            progress_value if isinstance(progress_value, dict) else {}
        )
        for field in (
            "worker_state",
            "worker_started_at",
            "worker_heartbeat_at",
            "worker_finished_at",
            "agent_call_count",
        ):
            if data.get(field) is None and field in progress:
                data[field] = progress[field]
        for field in ("source_count", "input_material_count"):
            data[field] = safe_int(data.get(field), default=0, minimum=0, field=field)
        data["agent_call_count"] = (
            safe_int(data.get("agent_call_count"), default=0, minimum=0) or 0
        )
        for field in ("new_version", "base_persona_version"):
            if field in data and data[field] is not None:
                data[field] = safe_int(data[field], default=0, minimum=0, field=field)
        return data

    def touch_worker(
        self,
        state: str | None = None,
        *,
        heartbeat_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        now = heartbeat_at or profile_now()
        if state:
            self.worker_state = str(state)
        if self.worker_started_at is None:
            self.worker_started_at = now
        self.worker_heartbeat_at = now
        if finished_at is not None:
            self.worker_finished_at = finished_at
        self.progress.update(
            {
                "worker_state": self.worker_state,
                "worker_started_at": self.worker_started_at,
                "worker_heartbeat_at": self.worker_heartbeat_at,
                "worker_finished_at": self.worker_finished_at,
                "agent_call_count": self.agent_call_count,
            }
        )
        self.updated_at = now

    def worker_snapshot(self) -> dict[str, Any]:
        return {
            "worker_state": self.worker_state,
            "worker_started_at": self.worker_started_at,
            "worker_heartbeat_at": self.worker_heartbeat_at,
            "worker_finished_at": self.worker_finished_at,
            "agent_call_count": self.agent_call_count,
        }


PROFILE_TYPE_LABELS: dict[ProfileType, str] = {
    ProfileType.PERSONA: "Persona",
    ProfileType.ORGANIZATION: "Organization",
    ProfileType.INSTITUTION: "Institution",
    ProfileType.COLLECTIVE: "Collective",
}
