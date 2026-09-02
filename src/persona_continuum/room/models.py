from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona_continuum.room.case_state import RoomCaseState

__all__ = [
    "BindingPreflightError",
    "CrossReview",
    "DirectorConfig",
    "ParticipantSlot",
    "ProtocolTask",
    "ProtocolTaskStatus",
    "ResolvedBindingSnapshot",
    "RoomCaseState",
    "RoomMode",
    "RoomProtocolConfig",
    "RoomProtocolEvent",
    "RoomProtocolState",
    "RoomProtocolType",
    "RoomRole",
    "RoomRunStatus",
    "RoomSessionState",
    "RoomSharedContext",
    "RoomStatus",
    "RoomTemplate",
    "RoomTranscriptRecord",
    "RoomVote",
    "StructuredSubmission",
]


class DirectorMode(StrEnum):
    MANUAL = "manual"
    ROUND_ROBIN = "round_robin"
    NATURAL = "natural"
    DIRECTOR = "director"
    AI_DIRECTOR = "ai_director"


class RoomMode(StrEnum):
    MANUAL = "manual"
    AUTONOMOUS = "autonomous"


class RoomProtocolType(StrEnum):
    FREE_DISCUSSION = "free_discussion"
    HOST_MODERATED = "host_moderated"
    EXPERT_CONSULTATION = "expert_consultation"
    DEBATE = "debate"
    COMMITTEE = "committee"
    CUSTOM = "custom"


class RoomRole(StrEnum):
    HOST = "host"
    CHAIR = "chair"
    EXPERT = "expert"
    MEMBER = "member"
    PRO = "pro"
    CON = "con"
    JUDGE = "judge"
    CRITIC = "critic"
    OBSERVER = "observer"


class RoomRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # The host asked the user one indispensable question and stopped on
    # purpose.  This is a *successful* protocol outcome, not a failure: the
    # run answered, it just answered with a question.
    WAITING_CLARIFICATION = "waiting_clarification"


#: Run statuses that mean "this run is over, stop polling".
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {
        RoomRunStatus.SUCCESS.value,
        RoomRunStatus.PARTIAL_SUCCESS.value,
        RoomRunStatus.FAILED.value,
        RoomRunStatus.CANCELLED.value,
        RoomRunStatus.WAITING_CLARIFICATION.value,
    }
)


class ProtocolTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class RoomSharedContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    description: str = ""
    background: str = ""
    rules: list[str] = Field(default_factory=list)
    relationships: dict[str, Any] = Field(default_factory=dict)
    task_context: str = ""
    attached_materials: list[dict[str, Any]] = Field(default_factory=list)
    custom_instructions: str = ""


class RoomProtocolConfig(BaseModel):
    """Protocol policy only; live stage/task state belongs to RoomProtocolState."""

    model_config = ConfigDict(extra="allow")

    speaker_selection: str = "intelligent"
    max_rounds: int = Field(default=6, ge=1, le=100)
    routing_mode: str = "host_decides"
    min_experts: int = Field(default=1, ge=1, le=100)
    max_experts: int = Field(default=3, ge=1, le=100)
    independent_first: bool = True
    cross_review: bool = True
    max_review_rounds: int = Field(default=1, ge=0, le=20)
    rebuttal: bool = False
    continue_on_member_failure: bool = True
    voting_enabled: bool = True
    anonymous_voting: bool = False
    vote_method: str = "simple_majority"
    allow_abstain: bool = True
    cross_examination: bool = True
    finalizer_role: str | None = None
    stages: list[dict[str, Any]] = Field(default_factory=list)
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    completion_condition: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_expert_range(self) -> RoomProtocolConfig:
        if self.min_experts > self.max_experts:
            raise ValueError("min_experts_cannot_exceed_max_experts")
        return self


class ProtocolTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    run_id: str
    parent_task_id: str | None = None
    stage: str
    participant_id: str | None = None
    task_type: str = "participant_action"
    status: ProtocolTaskStatus = ProtocolTaskStatus.PENDING
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None


class StructuredSubmission(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    participant_id: str
    summary: str
    key_findings: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any] | str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    recommendation: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    domain_data: dict[str, Any] = Field(default_factory=dict)


class CrossReview(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    participant_id: str
    agree: list[str] = Field(default_factory=list)
    disagree: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    changed_position: bool = False
    updated_conclusion: str | None = None


class RoomVote(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    participant_id: str
    vote: str
    reason: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    weight: float = Field(default=1.0, ge=0.0)


class RoomProtocolEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    room_id: str
    run_id: str
    event_type: str
    stage: str | None = None
    actor_id: str | None = None
    target_id: str | None = None
    task_id: str | None = None
    status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RoomProtocolState(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str | None = None
    source_run_id: str | None = None
    root_task_id: str | None = None
    status: RoomRunStatus = RoomRunStatus.PENDING
    current_stage: str = "waiting_user"
    active_participant_ids: list[str] = Field(default_factory=list)
    selected_expert_ids: list[str] = Field(default_factory=list)
    tasks: list[ProtocolTask] = Field(default_factory=list)
    submissions: list[StructuredSubmission] = Field(default_factory=list)
    reviews: list[CrossReview] = Field(default_factory=list)
    votes: list[RoomVote] = Field(default_factory=list)
    final_result: dict[str, Any] | None = None
    # Ephemeral protocol bookkeeping that must never become persona memory:
    # host analysis, routing decisions, task/review metadata.
    artifacts: dict[str, Any] = Field(default_factory=dict)
    # Set when the host used its single blocking clarification.
    clarification: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RoomStatus(StrEnum):
    CREATING = "creating"
    INITIALIZING = "initializing"
    READY = "ready"
    DISCUSSING = "discussing"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    LOBBY = "creating"
    ACTIVE = "ready"
    STOPPED = "completed"


class ParticipantSlot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    participant_id: str
    persona_id: str
    role: str = RoomRole.MEMBER.value
    enabled: bool = True
    authority: float = Field(default=50.0, ge=0.0, le=100.0)
    specialties: list[str] = Field(default_factory=list)
    permissions: dict[str, bool] = Field(default_factory=dict)
    tool_permissions: list[str] = Field(default_factory=list)
    display_name: str = ""
    avatar: str | None = None
    runtime_selection: str = "default"  # specific adapter id, "default", or "random"
    model_selection: str = "default"  # specific model id, "default", or "random"
    reasoning_selection: str = "default"  # specific effort string, "default", or "random"
    auth_profile_id: str | None = None
    permission_profile: str = "chat_safe"
    allow_mcp: bool = True
    allow_dynamic_recall: bool = True
    allow_agent_tools: bool = True
    allow_director_auto_select: bool = True
    allow_manual_model_id: bool = False
    speaking_weight: float = 1.0
    min_cooldown: int = 0
    max_cooldown: int = 10
    max_consecutive_turns: int = 2
    expertise: list[str] = Field(default_factory=list)
    relationships: dict[str, float] = Field(default_factory=dict)
    runtime_pool: list[dict[str, Any]] = Field(default_factory=list)
    model_pool: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_pool: list[dict[str, Any]] = Field(default_factory=list)
    runtime_candidate_pool: list[dict[str, Any]] = Field(default_factory=list)
    model_candidate_pool: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_candidate_pool: list[dict[str, Any]] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        if self.expertise and not self.specialties:
            self.specialties = list(self.expertise)
        elif self.specialties and not self.expertise:
            self.expertise = list(self.specialties)
        if self.runtime_candidate_pool and not self.runtime_pool:
            self.runtime_pool = list(self.runtime_candidate_pool)
        if self.model_candidate_pool and not self.model_pool:
            self.model_pool = list(self.model_candidate_pool)
        if self.reasoning_candidate_pool and not self.reasoning_pool:
            self.reasoning_pool = list(self.reasoning_candidate_pool)


class ResolvedBindingSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    participant_id: str
    persona_id: str
    display_name: str
    agent_runtime_id: str
    agent_runtime_name: str = ""
    model_id: str
    model_name: str = ""
    reasoning_effort: str
    runtime_candidate_pool: list[dict[str, Any]] = Field(default_factory=list)
    model_candidate_pool: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_candidate_pool: list[dict[str, Any]] = Field(default_factory=list)
    auth_profile_id: str | None = None
    adapter_version: str | None = None
    agent_version: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DirectorConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: DirectorMode = DirectorMode.DIRECTOR
    mention_weight: float = 2.0
    question_target_weight: float = 2.5
    motivation_weight: float = 1.0
    relationship_weight: float = 1.2
    affect_weight: float = 0.8
    topic_relevance_weight: float = 1.5
    silence_bonus_weight: float = 0.5
    recent_speaking_penalty: float = 1.5
    consecutive_turn_penalty: float = 3.0
    random_jitter: float = 0.3
    ai_director_runtime_id: str | None = None
    ai_director_model_id: str | None = None


class RoomTranscriptRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    room_id: str
    turn_id: str
    participant_id: str
    persona_id: str
    speaker_name: str
    agent_runtime_id: str
    agent_session_id: str | None = None
    model_id: str | None = None
    reasoning_effort: str | None = None
    content: str
    director_reason: str | None = None
    recall_ids: list[str] = Field(default_factory=list)
    commit_status: str = "committed"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RoomSessionState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str | None = None
    description: str | None = None
    topic: str | None = None
    protocol: RoomProtocolType = RoomProtocolType.FREE_DISCUSSION
    protocol_config: RoomProtocolConfig = Field(default_factory=RoomProtocolConfig)
    shared_context: RoomSharedContext = Field(default_factory=RoomSharedContext)
    template_id: str | None = None
    # Long-lived description of what the room is about.  A follow-up must
    # never overwrite it -- follow-ups merge into ``case_state`` instead.
    case_state: RoomCaseState = Field(default_factory=RoomCaseState)
    protocol_state: RoomProtocolState = Field(default_factory=RoomProtocolState)
    protocol_events: list[RoomProtocolEvent] = Field(default_factory=list)
    mode: RoomMode = RoomMode.AUTONOMOUS
    status: RoomStatus = RoomStatus.CREATING
    participants: list[ParticipantSlot] = Field(default_factory=list)
    binding_snapshots: dict[str, ResolvedBindingSnapshot] = Field(default_factory=dict)
    turn_index: int = 0
    current_speaker_id: str | None = None
    host_participant_id: str | None = None
    initialization_stage: str = "created"
    initialization_progress: int = 0
    director_config: DirectorConfig = Field(default_factory=DirectorConfig)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    failed_turn_audits: list[dict[str, Any]] = Field(default_factory=list)
    last_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_status(cls, value: Any) -> Any:
        if isinstance(value, dict):
            normalized = dict(value)
            if "status" in normalized:
                normalized["status"] = {
                    "lobby": "creating",
                    "active": "ready",
                    "stopped": "completed",
                    "waiting_for_speaker": "ready",
                }.get(str(normalized.get("status", "")), normalized.get("status"))
            normalized.setdefault("protocol", "free_discussion")
            normalized.setdefault("protocol_config", {})
            normalized.setdefault("shared_context", {})
            normalized.setdefault("case_state", {})
            normalized.setdefault("protocol_state", {})
            normalized.setdefault("protocol_events", [])
            participants = normalized.get("participants")
            if isinstance(participants, list):
                normalized["participants"] = [
                    {"role": "member", **item} if isinstance(item, dict) else item
                    for item in participants
                ]
            return normalized
        return value


class RoomTemplate(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    description: str = ""
    protocol: RoomProtocolType = RoomProtocolType.FREE_DISCUSSION
    participants: list[ParticipantSlot] = Field(default_factory=list)
    shared_context: RoomSharedContext = Field(default_factory=RoomSharedContext)
    protocol_config: RoomProtocolConfig = Field(default_factory=RoomProtocolConfig)
    routing: dict[str, Any] = Field(default_factory=dict)
    discussion: dict[str, Any] = Field(default_factory=dict)
    completion: dict[str, Any] = Field(default_factory=dict)
    tool_permissions: dict[str, list[str]] = Field(default_factory=dict)
    built_in: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BindingPreflightError(Exception):
    def __init__(
        self,
        reason: str,
        participant_id: str = "",
        runtime_id: str = "",
        model_id: str = "",
        code: str = "room_binding_failed",
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.participant_id = participant_id
        self.runtime_id = runtime_id
        self.model_id = model_id
        self.reason = reason

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "participant_id": self.participant_id,
            "runtime_id": self.runtime_id,
            "model_id": self.model_id,
            "reason": self.reason,
        }
