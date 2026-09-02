from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import hashlib
import inspect
import json
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona_continuum.agent.adapter import AgentAdapter
from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.models import (
    AgentProbeResult,
    AgentSessionConfig,
    AgentStatus,
    EffectiveModelCapabilities,
    ReasoningCapabilityMode,
)
from persona_continuum.agent.prompt_transport import resolve_prompt_transport_capability
from persona_continuum.agent.response_collector import (
    AgentOutputError,
    AgentRuntimeError,
    AgentSessionStartError,
    AgentTransportError,
    ReasoningBindingRejectedError,
    RuntimeUnavailableError,
    sanitize_diagnostic,
)
from persona_continuum.agent.runtime_executor import (
    AgentExecutionResult,
    AgentRuntimeExecutor,
    RuntimeSessionBinding,
)
from persona_continuum.agent.structured_output import StructuredOutputSchemaError, StructuredResult
from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.application.audit_issues import (
    issues_for_dimension,
    normalize_audit_issue,
    normalize_audit_issue_dimensions,
)
from persona_continuum.application.compilation_service import REQUIRED_DIMENSIONS
from persona_continuum.application.job_control import JobControlRegistry
from persona_continuum.application.job_progress import (
    JobFailure,
    JobNotTerminalError,
    JobProgress,
    JobVisibility,
    PersonaFailureCode,
    WorkerState,
    classify_persona_failure,
    failure_code_for,
    is_retryable_failure,
    is_retryable_failure_code,
    percent_for_stage,
    progress_now,
)
from persona_continuum.application.material_intelligence import MATERIAL_AGENT_SYSTEM_PROMPTS
from persona_continuum.application.research_backend import (
    ResearchBackend,
    ResearchBackendResolver,
)
from persona_continuum.application.research_capability_cache import ResearchCapabilityCache
from persona_continuum.application.research_quality import (
    AdaptiveResearchStopGate,
    ContradictionCoverage,
    FictionalPersonaCoverage,
    InformationGainSnapshot,
    LifeStageModel,
    MarginalInformationGainTracker,
    PrivatePersonaCoverage,
    PrivatePersonaCoveragePolicy,
    ResearchCheckpoint,
    ResearchGap,
    ResearchGapAnalyzer,
    ResearchRichnessEstimator,
    SourceIndependenceAnalyzer,
)
from persona_continuum.compiler.contract import COMPILE_CONTRACT
from persona_continuum.compiler.schemas import ResearchArtifact
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.domain.provenance import (
    CHARACTER_VISIBLE,
    FICTIONAL_KINDS,
    normalise_fictional_provenance,
    normalise_material_scope,
)
from persona_continuum.numeric import (
    InvalidNumericFieldError,
    safe_acp_stream_limit,
    safe_int,
    safe_probability,
    safe_timeout,
)
from persona_continuum.performance.tracing import default_tracer
from persona_continuum.security.paths import ensure_child_path
from persona_continuum.security.validation import ConflictError, NotFoundError

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum
    from persona_continuum.application.material_intelligence import MaterialIntelligenceService
    from persona_continuum.application.profile_library_service import ProfileLibraryService


PUBLIC_RESEARCH_SYSTEM_PROMPT = """
你的目标不是写人物介绍，而是为 Persona Continuum 构建具有历史证据约束的数字人格。
必须深入研究人物长期历史，查找第一手资料和第三方资料，研究行为而不只研究观点，
研究矛盾、变化、表达方式、情感关系、失败和压力下反应。记录每条 source provenance，
区分事实、第三方描述和推断；不确定时明确 uncertainty。禁止根据常识补完人格，禁止
生成脱离 EvidenceSource 的历史事实。所有研究输出必须能回指真实来源。动态划分重要
life stages，按事件时间而不是发表年份映射证据；主动执行失败、争议、批评、观点变化和
相互矛盾描述的检索，并记录没有找到负面证据这一结果。
""".strip()

LOCAL_MATERIAL_SYSTEM_PROMPT = """
你是 Persona Continuum 的本地资料人格证据分析器。只能分析本次输入的 Evidence Ledger
内容；禁止联网、搜索、WebFetch、Browser，也禁止用模型已有知识补写人物经历。所有事实、
记忆、矛盾和推断都必须映射到输入中的真实 source_id；缺少信息时明确记录 uncertainty。
必须保留原始 provenance、反面证据、时间变化与互相矛盾的描述。不得把推断、用户设定或
虚构/模拟内容升级成历史事实。
""".strip()


class PersonaCreationError(RuntimeError):
    """Base error for fail-closed persona creation jobs."""


class PersonaModelAnalysisNotExecutedError(PersonaCreationError):
    """A durable call-accounting contract disagrees with the executed work."""

    failure_code = PersonaFailureCode.JOB_PROGRESS_CONTRACT_ERROR.value


# Bumped whenever the dimension extraction prompt or its evidence rendering
# changes shape: stale batch checkpoints must never be silently reused.
DIMENSION_EXTRACTION_PROMPT_VERSION = "3"


# Canonical component -> evidence dimensions that may support a bounded repair.
# This is schema vocabulary, not persona content: every Persona uses the same
# routing table.
TARGETED_REPAIR_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "identity_profile": ("identity_and_timeline",),
    "timeline_events": ("identity_and_timeline",),
    "self_narrative_evidence": ("identity_and_timeline",),
    "mental_models": ("works_and_views", "values_desires_contradictions"),
    "decision_heuristics": ("decisions_and_behavior",),
    "values": ("values_desires_contradictions",),
    "contradictions": ("values_desires_contradictions",),
    "failure_patterns": ("decisions_and_behavior",),
    "temperament": ("affect_relationship_defense", "expression_dna"),
    "emotional_triggers": ("affect_relationship_defense",),
    "attachment_patterns": ("affect_relationship_defense",),
    "needs_and_desires": ("values_desires_contradictions",),
    "defenses": ("affect_relationship_defense",),
    "expression_style": ("expression_dna",),
    "vocabulary": ("expression_dna",),
    "dialogue_examples": ("expression_dna", "interviews_and_dialogue"),
    "anti_patterns": ("expression_dna",),
    "relationships": ("affect_relationship_defense", "third_party_views"),
}

def classify_persona_failure_message(message: str) -> str:
    """Classify a pipeline-raised message without importing the agent layer."""

    for prefix, code in (
        ("audit_repair_failed:", PersonaFailureCode.AUDIT_REPAIR_FAILED),
        ("final_quality_gate_failed:", PersonaFailureCode.FINAL_QUALITY_GATE_FAILED),
        ("final_audit_", PersonaFailureCode.FINAL_AUDIT_FAILED),
        ("final_audit_failed", PersonaFailureCode.FINAL_AUDIT_FAILED),
    ):
        if message.startswith(prefix):
            return code.value
    if "_audit_invalid" in message:
        return PersonaFailureCode.FINAL_AUDIT_FAILED.value
    return PersonaFailureCode.PERSONA_CREATION_ERROR.value


def _context_int(value: Any) -> int | None:
    """Normalize a user-supplied context window; junk is treated as unset."""

    return safe_int(value, default=None, minimum=1)


class _PauseRequested(BaseException):
    """Cooperative safe-pause signal raised at an atomic-unit boundary.

    Derives from ``BaseException`` so a stray ``except Exception`` cannot
    swallow it: the worker only pauses between atomic units (a search, a
    fetch, one dimension batch, one audit call), never mid-call.
    """


class PersonaQualityGateError(PersonaCreationError):
    """Compilation is blocked because a required final audit did not pass.

    Carries an explicit ``failure_code`` so retry policy never has to
    string-match the message.
    """

    def __init__(
        self, message: str, *, failure_code: str | None = None
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code or classify_persona_failure_message(message)


# One audit issue.  ``dimensions`` is the canonical shape (always list[str]).
# The legacy singular ``dimension`` is still accepted because persisted
# checkpoints and older models emit it, and it may legitimately be a list when
# a finding is cross-dimension.  The Python consumer normalizes both.
FINAL_AUDIT_ISSUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dimensions": {"type": "array", "items": {"type": "string"}},
        "dimension": {"type": ["string", "array"], "items": {"type": "string"}},
        "severity": {"type": "string"},
        "type": {"type": "string"},
        "category": {"type": "string"},
        "reason": {"type": "string"},
        "detail": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
}

# Headroom for the audit envelope (audit_type + instruction + system prompt)
# that surrounds the payload when the transport guard measures the prompt.
_GLOBAL_AUDIT_ENVELOPE_HEADROOM_BYTES = 4096

FINAL_AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "dimensions", "issues"],
    "properties": {
        "status": {"type": "string", "enum": ["pass", "repair_required", "fail"]},
        "dimensions": {"type": "object"},
        # Explicitly typed: "array<any>" let downstream code silently assume a
        # field shape that a model was free to violate.
        "issues": {"type": "array", "items": FINAL_AUDIT_ISSUE_SCHEMA},
        "retrieval_source_ids": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
}


class ResearchCapabilityError(PersonaCreationError):
    pass


class RuntimeBindingError(PersonaCreationError):
    pass


class PersonaAgentOutputError(AgentOutputError, PersonaCreationError):
    """Typed output failure retained for backwards-compatible callers."""


class PersonaAgentStructuredOutputError(StructuredOutputSchemaError, PersonaCreationError):
    pass


class DuplicatePersonaError(PersonaCreationError):
    def __init__(self, persona_id: str, display_name: str) -> None:
        self.persona_id = persona_id
        self.display_name = display_name
        super().__init__(f"persona_exists:{persona_id}")


class PrivateMaterialConsentRequired(PersonaCreationError):
    pass


class PersonaCreationStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RESEARCHING = "researching"
    INGESTING_SOURCES = "ingesting_sources"
    EXTRACTING = "extracting"
    COMPILING = "compiling"
    COMPLETED = "completed"
    COMPLETED_WITH_GAPS = "completed_with_gaps"
    WAITING_FOR_MATERIALS = "waiting_for_materials"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    PAUSED_RUNTIME_UNAVAILABLE = "paused_runtime_unavailable"
    # A pause was accepted but the current atomic unit has not finished yet.
    # The status must not stay RUNNING: the UI needs to show that the worker
    # is winding down, and the scheduler must not hand it new work.
    PAUSE_REQUESTED = "pause_requested"


@runtime_checkable
class ResearchToolBroker(Protocol):
    """External research tool boundary; web access never comes from model memory."""

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...

    async def fetch(self, url: str) -> dict[str, Any] | str: ...

    def capabilities(self) -> Iterable[str]: ...


class ResearchPolicy(BaseModel):
    """Research quality contract persisted with every creation job.

    ``max_sources`` is retained as a compatibility alias for pre-upgrade job
    snapshots.  New jobs use the soft/hard budgets and the adaptive stop gate.
    """

    model_config = ConfigDict(extra="allow")

    profile: str = "deep"
    min_unique_sources: int = 30
    preferred_source_target: int = 60
    soft_max_sources: int = 100
    hard_max_sources: int = 150
    max_sources: int | None = None
    min_source_categories: int = 6
    min_sources_per_dimension: int = 4
    required_dimensions: list[str] = Field(default_factory=lambda: list(REQUIRED_DIMENSIONS))
    require_life_stage_coverage: bool = True
    min_evidence_per_life_stage: int = 2
    require_primary_secondary_balance: bool = True
    target_primary_ratio: float = 0.25
    require_negative_evidence: bool = True
    require_contradiction_search: bool = True
    require_marginal_gain_gate: bool = True
    marginal_gain_window: int = 10
    marginal_gain_stop_threshold: float = 0.08
    required_low_gain_windows: int = 2
    max_research_rounds: int = 12
    adaptive_target: bool = True
    quality_floor: float = 0.55

    @model_validator(mode="before")
    @classmethod
    def _load_legacy_policy(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data: dict[str, Any] = dict(value)
        legacy_max = data.get("max_sources")
        # Existing snapshots only had max_sources.  Preserve that exact
        # budget instead of silently adopting the new defaults on resume.
        if legacy_max is not None:
            normalized_legacy_max = safe_int(legacy_max, default=0, minimum=0)
            data.setdefault("soft_max_sources", normalized_legacy_max)
            data.setdefault("hard_max_sources", normalized_legacy_max)
            data.setdefault("require_life_stage_coverage", False)
            data.setdefault("min_evidence_per_life_stage", 1)
            data.setdefault("require_contradiction_search", False)
            data.setdefault("require_marginal_gain_gate", False)
            data.setdefault("adaptive_target", False)
            data.setdefault("quality_floor", 0.0)
        profile = str(data.get("profile") or "deep").lower()
        data["profile"] = profile
        if profile == "standard":
            if legacy_max is None:
                data.setdefault("min_unique_sources", 15)
                data.setdefault("preferred_source_target", 25)
                data.setdefault("soft_max_sources", 40)
                data.setdefault("hard_max_sources", 60)
                data.setdefault("min_source_categories", 4)
                data.setdefault("min_sources_per_dimension", 2)
            data.setdefault("require_life_stage_coverage", False)
            data.setdefault("require_contradiction_search", True)
            data.setdefault("require_marginal_gain_gate", True)
        elif profile == "exhaustive" and legacy_max is None:
            data.setdefault("min_unique_sources", 50)
            data.setdefault("preferred_source_target", 100)
            data.setdefault("soft_max_sources", 150)
            data.setdefault("hard_max_sources", 200)
            data.setdefault("min_source_categories", 7)
            data.setdefault("min_sources_per_dimension", 6)
            data.setdefault("min_evidence_per_life_stage", 3)
            data.setdefault("max_research_rounds", 24)
        integer_defaults = {
            "min_unique_sources": 30,
            "preferred_source_target": 60,
            "soft_max_sources": 100,
            "hard_max_sources": 150,
            "max_sources": None,
            "min_source_categories": 6,
            "min_sources_per_dimension": 4,
            "min_evidence_per_life_stage": 2,
            "marginal_gain_window": 10,
            "required_low_gain_windows": 2,
            "max_research_rounds": 12,
        }
        for field, default in integer_defaults.items():
            if field not in data or data[field] is None:
                if field != "max_sources":
                    data[field] = default
                continue
            data[field] = safe_int(data[field], default=default, minimum=0, field=field)
        for ratio_field, ratio_default in {
            "target_primary_ratio": 0.25,
            "marginal_gain_stop_threshold": 0.08,
            "quality_floor": 0.55,
        }.items():
            normalized = safe_probability(
                data.get(ratio_field), default=ratio_default, field=ratio_field
            )
            data[ratio_field] = ratio_default if normalized is None else normalized
        return data

    def model_post_init(self, __context: Any) -> None:
        if self.max_sources is None:
            object.__setattr__(self, "max_sources", self.hard_max_sources)

    @property
    def effective_soft_budget(self) -> int:
        return max(
            safe_int(self.min_unique_sources, default=0, minimum=0) or 0,
            safe_int(self.soft_max_sources or self.hard_max_sources, default=0, minimum=0) or 0,
        )

    @property
    def effective_hard_budget(self) -> int:
        return max(
            self.effective_soft_budget,
            safe_int(self.hard_max_sources or self.soft_max_sources, default=0, minimum=0) or 0,
        )

    def effective_target(self, subject_richness: str = "moderate") -> int:
        target = max(
            safe_int(self.min_unique_sources, default=0, minimum=0) or 0,
            safe_int(self.preferred_source_target, default=0, minimum=0) or 0,
        )
        if not self.adaptive_target:
            return target
        if self.profile == "deep":
            target += {"sparse": 0, "moderate": 0, "rich": 20, "very_rich": 40}.get(
                subject_richness, 0
            )
        elif self.profile == "exhaustive":
            target += {"sparse": 0, "moderate": 10, "rich": 30, "very_rich": 50}.get(
                subject_richness, 0
            )
        return min(target, self.effective_hard_budget)

    @classmethod
    def for_profile(cls, profile: str | None) -> ResearchPolicy:
        normalized = str(profile or "deep").lower()
        if normalized == "standard":
            return cls(
                profile="standard",
                min_unique_sources=15,
                preferred_source_target=25,
                soft_max_sources=40,
                hard_max_sources=60,
                min_source_categories=4,
                min_sources_per_dimension=2,
                require_life_stage_coverage=True,
                min_evidence_per_life_stage=1,
                max_research_rounds=8,
            )
        if normalized == "exhaustive":
            return cls(
                profile="exhaustive",
                min_unique_sources=50,
                preferred_source_target=100,
                soft_max_sources=150,
                hard_max_sources=200,
                min_source_categories=7,
                min_sources_per_dimension=6,
                require_life_stage_coverage=True,
                min_evidence_per_life_stage=3,
                max_research_rounds=24,
            )
        if normalized == "custom":
            return cls(profile="custom")
        if normalized == "deep":
            return DeepPersonaResearchPolicy()
        # Future profiles remain loadable.  Their explicit custom fields are
        # respected instead of being rejected by an enum gate.
        return cls(profile=normalized)


class DeepPersonaResearchPolicy(ResearchPolicy):
    """Named policy contract for the default public-person research gate."""

    profile: str = "deep"
    min_unique_sources: int = 30
    preferred_source_target: int = 60
    soft_max_sources: int = 100
    hard_max_sources: int = 150
    min_source_categories: int = 6
    min_sources_per_dimension: int = 4
    require_life_stage_coverage: bool = True
    min_evidence_per_life_stage: int = 2
    max_research_rounds: int = 12


class PersonaCreationJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    persona_type: PersonaType
    creation_mode: str
    status: str = "created"
    runtime_source: str
    agent_id: str
    model_id: str
    reasoning_effort: str | None = None
    auth_profile_id: str | None = None
    agent_version: str | None = None
    capability_snapshot: dict[str, Any] = Field(default_factory=dict)
    persona_id: str | None = None
    compilation_task_id: str | None = None
    research_policy: ResearchPolicy = Field(default_factory=ResearchPolicy)
    source_count: int = 0
    source_ids: list[str] = Field(default_factory=list)
    dimension_progress: dict[str, int] = Field(default_factory=dict)
    current_stage: str = "created"
    coverage: dict[str, Any] = Field(default_factory=dict)
    life_stage_progress: dict[str, int] = Field(default_factory=dict)
    life_stages: list[dict[str, Any]] = Field(default_factory=list)
    information_gain: list[dict[str, Any]] = Field(default_factory=list)
    research_gaps: list[dict[str, Any]] = Field(default_factory=list)
    research_checkpoints: list[dict[str, Any]] = Field(default_factory=list)
    research_stop_reason: str | None = None
    query_history: dict[str, dict[str, Any]] = Field(default_factory=dict)
    private_coverage: dict[str, Any] = Field(default_factory=dict)
    progress: JobProgress = Field(default_factory=JobProgress)
    worker_state: WorkerState = WorkerState.STARTING
    worker_started_at: str | None = None
    worker_heartbeat_at: str | None = None
    worker_finished_at: str | None = None
    agent_call_count: int = 0
    failure_json: dict[str, Any] | None = None
    visibility: str = "user"
    dismissed_at: str | None = None
    superseded_by: str | None = None
    agent_call_audits: list[dict[str, Any]] = Field(default_factory=list)
    checkpoints: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    job_config: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    interview_questions: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="before")
    @classmethod
    def _normalise_persisted_numbers(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data: dict[str, Any] = dict(value)
        progress_value = data.get("progress")
        progress: dict[str, Any] = progress_value if isinstance(progress_value, dict) else {}
        for field in (
            "worker_state",
            "worker_started_at",
            "worker_heartbeat_at",
            "worker_finished_at",
            "agent_call_count",
        ):
            if data.get(field) is None and field in progress:
                data[field] = progress[field]
        data["source_count"] = safe_int(data.get("source_count"), default=0, minimum=0) or 0
        data["agent_call_count"] = safe_int(data.get("agent_call_count"), default=0, minimum=0) or 0
        for field in ("dimension_progress", "life_stage_progress"):
            raw = data.get(field)
            if isinstance(raw, dict):
                data[field] = {
                    str(key): safe_int(item, default=0, minimum=0) or 0 for key, item in raw.items()
                }
        return data

    def touch_worker(
        self,
        state: WorkerState | str | None = None,
        *,
        heartbeat_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        """Persist worker liveness alongside the user-facing job status."""

        self.progress.touch_worker(
            state,
            heartbeat_at=heartbeat_at,
            finished_at=finished_at,
            agent_call_count=self.agent_call_count,
        )
        self.worker_state = self.progress.worker_state
        self.worker_started_at = self.progress.worker_started_at
        self.worker_heartbeat_at = self.progress.worker_heartbeat_at
        self.worker_finished_at = self.progress.worker_finished_at


class PersonaMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    actor_id: str
    display_name: str
    actor_type: str
    status: str
    persona_id: str | None = None
    confidence: float = 0.0
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalise_confidence(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        normalized = safe_probability(data.get("confidence"), default=0.0)
        data["confidence"] = normalized if normalized is not None else 0.0
        return data


class WorldPersonaCompletionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    matches: list[PersonaMatch] = Field(default_factory=list)


class PersonaCreationOrchestrator:
    WORKER_HEARTBEAT_INTERVAL_SECONDS = 5.0

    """Application-layer runtime for all Persona creation and enrichment paths.

    The orchestrator deliberately delegates persona persistence, evidence ingestion and
    compilation to the existing services. It never writes a replacement persona format
    and never accepts an unsourced system prompt as a compiled persona.
    """

    TERMINAL_STATUSES = {
        "completed",
        "completed_with_gaps",
        "failed",
        "failed_quality_gate",
        "cancelled",
    }
    ACTIVE_STATUSES = {
        "created",
        "planning",
        "researching",
        "ingesting_sources",
        "extracting",
        "compiling",
        "paused",
        "paused_runtime_unavailable",
        "pause_requested",
        "waiting_for_materials",
    }
    # Statuses that represent "work is being performed right now".  Pause is
    # honoured before entering any of them.
    WORK_STATUSES = frozenset(
        {
            "planning",
            "researching",
            "ingesting_sources",
            "extracting",
            "compiling",
        }
    )
    # Statuses that mean the run is stopped and must not be auto-resumed.
    PAUSED_STATUSES = frozenset(
        {
            "paused",
            "paused_runtime_unavailable",
            "pause_requested",
            "waiting_for_materials",
        }
    )

    def __init__(
        self,
        continuum: PersonaContinuum,
        *,
        research_broker: ResearchToolBroker | None = None,
        max_parallel_persona_research: int | None = None,
        profile_library: ProfileLibraryService | None = None,
        material_intelligence: MaterialIntelligenceService | None = None,
        research_capability_cache: ResearchCapabilityCache | None = None,
    ) -> None:
        self.continuum = continuum
        self.research_broker = research_broker
        self.profile_library = profile_library
        self.material_intelligence = material_intelligence
        self.research_capability_cache = research_capability_cache or ResearchCapabilityCache(
            continuum.database
        )
        from persona_continuum.performance.scheduler import AdaptiveConcurrencyController

        cfg = continuum.config
        initial_research_concurrency = (
            safe_int(max_parallel_persona_research, default=None, minimum=1)
            if max_parallel_persona_research is not None
            else safe_int(
                getattr(cfg, "persona_research_initial_concurrency", 3),
                default=3,
                minimum=1,
            )
        ) or 3
        self._persona_research_controller = AdaptiveConcurrencyController(
            minimum=safe_int(
                getattr(cfg, "persona_research_min_concurrency", 1), default=1, minimum=1
            )
            or 1,
            initial=initial_research_concurrency,
            maximum=safe_int(
                getattr(cfg, "persona_research_max_concurrency", 6), default=6, minimum=1
            )
            or 6,
            recovery_successes=safe_int(
                getattr(cfg, "persona_research_recovery_successes", 4),
                default=4,
                minimum=1,
            )
            or 4,
        )
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._active_bindings: dict[str, RuntimeSessionBinding] = {}
        # Logical-session reuse: (job_id, participant_id) -> live binding.
        # One logical session per job participant; the physical runtime behind
        # it is pooled.  This is what removes the per-call spawn/close churn
        # without ever sharing threads between different personas/dimensions.
        self._job_sessions: dict[tuple[str, str], RuntimeSessionBinding] = {}
        self._job_session_keys: dict[str, set[tuple[str, str]]] = {}
        # Research worker session pools are closed when the job run ends.
        self._open_research_backends: dict[str, set[Any]] = {}
        # Debounced snapshot persistence state (job_id -> gate).
        self._save_gates: dict[str, dict[str, Any]] = {}
        self._gate_lock = asyncio.Lock()
        # Cross-job research caches: parallel worlds researching related
        # public figures share fetched pages (never per-persona judgments).
        from persona_continuum.performance.research_cache import (
            ResearchQueryCache,
            ResearchSourceCache,
        )

        self.research_source_cache = (
            ResearchSourceCache()
            if bool(getattr(cfg, "research_source_cache_enabled", True))
            else None
        )
        self.research_query_cache = (
            ResearchQueryCache(
                background_ttl_seconds=getattr(
                    cfg, "research_query_background_ttl_seconds", 12 * 60 * 60
                ),
                news_ttl_seconds=getattr(cfg, "research_query_news_ttl_seconds", 30 * 60),
            )
            if bool(getattr(cfg, "research_query_cache_enabled", True))
            else None
        )
        self._search_semaphore = asyncio.Semaphore(
            max(1, safe_int(getattr(cfg, "max_search_concurrency", 3), default=3, minimum=1) or 3)
        )
        self._fetch_semaphore = asyncio.Semaphore(
            max(1, safe_int(getattr(cfg, "max_fetch_concurrency", 4), default=4, minimum=1) or 4)
        )
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._event_callbacks: list[Callable[[dict[str, Any]], Any]] = []
        # Control-plane state (pause/cancel) is stored separately from job
        # data so two Job objects can never overwrite each other's flags.
        self.job_control = JobControlRegistry(continuum.database)
        self._gain_trackers: dict[str, MarginalInformationGainTracker] = {}
        self._source_analyzer = SourceIndependenceAnalyzer()
        self._gap_analyzer = ResearchGapAnalyzer()
        self._stop_gate = AdaptiveResearchStopGate()
        self.runtime_executor = (
            getattr(continuum, "agent_runtime_executor", None) or AgentRuntimeExecutor()
        )
        # Shared scheduler: background research yields model/search/fetch
        # slots to interactive room turns instead of monopolizing them.
        self.execution_scheduler = getattr(continuum, "execution_scheduler", None)

    @contextlib.asynccontextmanager
    async def _research_search_slot(self) -> AsyncIterator[None]:
        if self.execution_scheduler is not None:
            from persona_continuum.performance.scheduler import ExecutionClass

            async with self.execution_scheduler.search_slot(priority=ExecutionClass.FOREGROUND):
                yield
            return
        async with self._search_semaphore:
            yield

    @contextlib.asynccontextmanager
    async def _research_fetch_slot(self) -> AsyncIterator[None]:
        if self.execution_scheduler is not None:
            from persona_continuum.performance.scheduler import ExecutionClass

            async with self.execution_scheduler.fetch_slot(priority=ExecutionClass.FOREGROUND):
                yield
            return
        async with self._fetch_semaphore:
            yield

    def _effective_model_capabilities(
        self, job: PersonaCreationJob
    ) -> EffectiveModelCapabilities:
        """Resolve the model this job actually runs, once, from one place.

        Every stage reads this object instead of guessing: the raw probe
        snapshot does not name the selected model, so per-stage guesses used
        to fall back to a 32K context window even for 256K+ models.
        """
        binding = job.job_config.get("runtime_binding_snapshot") or {}
        effective = str(binding.get("effective_model") or "").strip() or None
        snapshot = dict(job.capability_snapshot or {})
        # The live binding is the highest-trust context source: use it for the
        # whole chain instead of letting each stage re-guess from the snapshot.
        if isinstance(binding, dict) and binding:
            merged = dict(snapshot)
            merged["runtime_binding_snapshot"] = binding
            for key in ("context_window_mode", "adapter_context_limit", "id", "agent_id"):
                if binding.get(key) is not None:
                    merged[key] = binding[key]
            snapshot = merged
        capabilities = EffectiveModelCapabilities.resolve(
            snapshot,
            requested_model=job.model_id,
            effective_model=effective,
            agent_id=job.agent_id,
            adapter_id=job.agent_id,
            reasoning_level=job.reasoning_effort,
            requested_context_window=_context_int(
                job.job_config.get("requested_context_window")
            ),
            planning_context_window=_context_int(
                job.job_config.get("planning_context_window")
            ),
        )
        job.job_config["effective_model_capabilities"] = capabilities.model_dump(mode="json")
        return capabilities

    def _job_id_of(self, job: PersonaCreationJob | str) -> str:
        return str(job if isinstance(job, str) else job.id)

    def pause_requested(self, job: PersonaCreationJob | str) -> bool:
        """Authoritative pause check.

        Reads the control plane (in-process signal + durable state), never the
        caller's possibly-stale ``job.job_config`` snapshot.
        """

        return bool(self.job_control.pause_requested(self._job_id_of(job)))

    def cancel_requested(self, job: PersonaCreationJob | str) -> bool:
        return bool(self.job_control.cancel_requested(self._job_id_of(job)))

    def _raise_if_pause_requested(self, job: PersonaCreationJob | str) -> None:
        """Safe-pause boundary: only checked between atomic work units.

        The request is deliberately not consumed here: every concurrent
        dimension worker hits its own boundary and raises, and the request is
        cleared only once the job has really reached PAUSED (or the user
        resumes/cancels).
        """

        job_id = self._job_id_of(job)
        if not self.job_control.pause_requested(job_id):
            return
        stage = "pause_requested"
        if not isinstance(job, str):
            stage = str(job.current_stage or job.status or "pause_requested")
        raise _PauseRequested(stage)

    def _cancel_or_pause_requested(self, job: PersonaCreationJob | str) -> bool:
        job_id = self._job_id_of(job)
        if self.job_control.cancel_requested(job_id):
            return True
        return self.job_control.pause_requested(job_id)

    def _record_execution_history(
        self, job: PersonaCreationJob, action: str, **details: Any
    ) -> None:
        """Append a durable execution-target timeline entry (per-stage provenance)."""
        history = job.job_config.setdefault("execution_history", [])
        history.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "action": action,
                "agent_id": job.agent_id,
                "model_id": job.model_id,
                "reasoning_effort": job.reasoning_effort,
                "auth_profile_id": job.auth_profile_id,
                "status": job.status,
                "stage": job.current_stage,
                **details,
            }
        )
        job.job_config["execution_history"] = history[-100:]

    def _extraction_percent(self, job: PersonaCreationJob) -> int:
        """Map eight-dimension progress onto the 75-94% extraction band."""
        completed = sum(
            1
            for dimension in REQUIRED_DIMENSIONS
            if (safe_int(job.dimension_progress.get(dimension, 0), default=0, minimum=0) or 0) > 0
        )
        return 75 + min(19, round(19 * completed / len(REQUIRED_DIMENSIONS)))

    async def _emit_extraction_progress(
        self, job: PersonaCreationJob, *, current_dimension: str | None = None, **extra: Any
    ) -> None:
        completed_dimensions = [
            dimension
            for dimension in REQUIRED_DIMENSIONS
            if (safe_int(job.dimension_progress.get(dimension, 0), default=0, minimum=0) or 0) > 0
        ]
        percent = self._extraction_percent(job)
        job.progress.update_stage(
            "extracting",
            label="正在生成八维 ResearchArtifact",
            percent=percent,
            current_item=current_dimension,
            completed=len(completed_dimensions),
            total=len(REQUIRED_DIMENSIONS),
        )
        await self._emit(
            job,
            "persona_extraction_progress",
            percent=percent,
            completed_dimensions=len(completed_dimensions),
            total_dimensions=len(REQUIRED_DIMENSIONS),
            completed=completed_dimensions,
            current_dimension=current_dimension,
            **extra,
        )

    def _split_oversized_evidence(
        self,
        items: list[dict[str, Any]],
        *,
        render: Callable[[dict[str, Any]], str],
        context_manager: AgentContextBudgetManager,
        capabilities: EffectiveModelCapabilities,
        phase: str,
        system_prompt: str,
        base_text: str,
        expected_output: Any,
        max_chunks: int = 8,
    ) -> list[dict[str, Any]]:
        """Semantic-chunk evidence units that alone exceed a batch budget.

        Chunking splits on paragraph boundaries and preserves the full
        provenance chain: every sub-unit keeps the original source ids and the
        original evidence id, and carries an explicit CHUNK range marker so no
        evidence link is lost.  Content that cannot fit within ``max_chunks``
        keeps failing closed exactly as before.
        """
        budget = context_manager.budget_for(
            model=capabilities, phase=phase, expected_output=expected_output
        )
        base_tokens = context_manager.estimate_tokens(base_text) + context_manager.estimate_tokens(
            system_prompt
        )
        raw_available = budget.evidence_token_budget - base_tokens
        if raw_available <= 0:
            return list(items)
        available = max(1, int(raw_available * 0.9))
        result: list[dict[str, Any]] = []
        for item in items:
            if context_manager.estimate_tokens(render(item)) <= available:
                result.append(item)
                continue
            content = str(item.get("content") or "")
            paragraphs = [p for p in re.split(r"\n\s*\n", content) if p.strip()]
            if not paragraphs:
                paragraphs = [content]
            chunks: list[str] = []
            current: list[str] = []
            current_tokens = 0
            for paragraph in paragraphs:
                paragraph_tokens = context_manager.estimate_tokens(paragraph)
                if paragraph_tokens > available:
                    if current:
                        chunks.append("\n\n".join(current))
                        current, current_tokens = [], 0
                    hard_size = max(200, available * 4)
                    pieces = [
                        paragraph[i : i + hard_size]
                        for i in range(0, len(paragraph), hard_size)
                    ]
                    chunks.extend(pieces)
                    continue
                if current and current_tokens + paragraph_tokens > available:
                    chunks.append("\n\n".join(current))
                    current, current_tokens = [], 0
                current.append(paragraph)
                current_tokens += paragraph_tokens
            if current:
                chunks.append("\n\n".join(current))
            if len(chunks) > max_chunks:
                # Splitting cannot faithfully preserve a chain this large under
                # the current budget; keep the historical fail-closed behavior.
                result.append(item)
                continue
            total = len(chunks)
            for index, chunk in enumerate(chunks, start=1):
                sub = dict(item)
                sub["content"] = f"[CHUNK {index}/{total}]\n{chunk}"
                sub["chunk_range"] = f"{index}/{total}"
                result.append(sub)
        return result

    @staticmethod
    def _batch_evidence_fingerprint(batch: list[dict[str, Any]]) -> str:
        """Hash the real model input of one batch (ids + source ids + content)."""
        material = [
            {
                "evidence_id": str(item.get("evidence_id") or ""),
                "source_ids": sorted(str(value) for value in item.get("source_ids") or []),
                "content": str(item.get("content") or ""),
            }
            for item in batch
        ]
        return hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _persona_source_revision(self, job: PersonaCreationJob) -> str:
        return hashlib.sha256(
            json.dumps(sorted(job.source_ids), ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]

    def _dimension_batch_checkpoints(
        self, job: PersonaCreationJob, dimension: str
    ) -> dict[str, dict[str, Any]]:
        """Per-dimension checkpoint map keyed by evidence fingerprint.

        Cleared whenever the persona source set changed, so evidence edits
        can never resurrect a stale extraction result.
        """
        config = job.job_config
        store = config.get("dimension_batch_checkpoints")
        if not isinstance(store, dict):
            store = {}
        revision = self._persona_source_revision(job)
        if store.get("source_revision") != revision:
            store = {
                "source_revision": revision,
                "prompt_version": DIMENSION_EXTRACTION_PROMPT_VERSION,
                "dimensions": {},
            }
        elif store.get("prompt_version") != DIMENSION_EXTRACTION_PROMPT_VERSION:
            store["prompt_version"] = DIMENSION_EXTRACTION_PROMPT_VERSION
            store["dimensions"] = {}
        dimensions: dict[str, Any] = store.setdefault("dimensions", {})
        entries = dimensions.get(dimension)
        if not isinstance(entries, dict):
            entries = {}
            dimensions[dimension] = entries
        config["dimension_batch_checkpoints"] = store
        return entries

    def _record_dimension_provenance(
        self, job: PersonaCreationJob, dimension: str
    ) -> None:
        binding = job.job_config.get("runtime_binding_snapshot") or {}
        provenance = job.job_config.setdefault("dimension_provenance", {})
        provenance[dimension] = {
            "job_id": job.id,
            "stage": "dimension_extraction",
            "agent_id": job.agent_id,
            "requested_model": job.model_id,
            "effective_model": binding.get("effective_model") or job.model_id,
            "reasoning": job.reasoning_effort,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _worker_state_for_status(status: str) -> WorkerState:
        normalized = str(status or "").lower()
        if normalized in {"completed", "completed_with_gaps", "cancelled"}:
            return WorkerState.FINISHED
        if normalized in {"failed", "failed_quality_gate"}:
            return WorkerState.FAILED
        if normalized in {"paused", "paused_runtime_unavailable"}:
            return WorkerState.PAUSED
        if normalized in {"waiting_for_materials", "waiting_io"}:
            return WorkerState.WAITING_IO
        if normalized in {"created", "queued"}:
            return WorkerState.STARTING
        return WorkerState.RUNNING

    def _touch_worker(
        self,
        job: PersonaCreationJob,
        state: WorkerState | str | None = None,
        *,
        finished: bool = False,
    ) -> None:
        effective_state = state or self._worker_state_for_status(job.status)
        finished_at = datetime.now(UTC).isoformat() if finished else None
        job.touch_worker(effective_state, finished_at=finished_at)
        self._save(job)

    def _sync_runtime_activity(
        self, job: PersonaCreationJob, binding: RuntimeSessionBinding
    ) -> None:
        """Merge the live session activity before the main job save.

        The heartbeat persists a freshly loaded job while an Agent turn is
        pending.  The main coroutine may still hold an older in-memory copy,
        so it must copy the session diagnostics back before saving the result
        or failure; otherwise the final save would erase the heartbeat update.
        """

        process_alive = (
            self.runtime_executor._process_alive(binding.session)
            if hasattr(self.runtime_executor, "_process_alive")
            else None
        )
        job.progress.set_activity(
            binding.session.activity_tracker.as_diagnostics(),
            process_alive=process_alive,
        )
        job.job_config["agent_process_alive"] = process_alive

    async def _worker_heartbeat_loop(self, job_id: str) -> None:
        """Keep a durable worker heartbeat alive during long Agent I/O."""

        while True:
            await asyncio.sleep(self.WORKER_HEARTBEAT_INTERVAL_SECONDS)
            try:
                current = self.get_job(job_id)
            except Exception:
                return
            if current.status in self.TERMINAL_STATUSES or current.status in {
                "paused",
                "paused_runtime_unavailable",
            }:
                return
            binding = self._active_bindings.get(job_id)
            if binding is not None:
                self._sync_runtime_activity(current, binding)
            current.touch_worker(WorkerState.WAITING_AGENT)
            self._save(current)

    # ------------------------------------------------------------------
    # Persistent job lifecycle
    # ------------------------------------------------------------------

    async def create_job(
        self,
        *,
        display_name: str,
        aliases: list[str] | None = None,
        persona_type: PersonaType | str,
        creation_mode: str,
        runtime_source: str,
        agent_id: str,
        model_id: str | None = None,
        reasoning_effort: str | None = None,
        auth_profile_id: str | None = None,
        research_policy: ResearchPolicy | dict[str, Any] | None = None,
        birth_date: str | None = None,
        death_date: str | None = None,
        data_cutoff_date: str | None = None,
        existing_persona_id: str | None = None,
        duplicate_action: str = "enrich",
        materials: list[dict[str, Any]] | None = None,
        remote_material_consent: bool = False,
        job_config: dict[str, Any] | None = None,
        enrichment_input_mode: str | None = None,
        visibility: str | JobVisibility = JobVisibility.USER,
        start_worker: bool = True,
    ) -> PersonaCreationJob:
        name = str(display_name or "").strip()
        if not name:
            raise PersonaCreationError("display_name_required")
        p_type = PersonaType(persona_type)
        mode = str(creation_mode).lower()
        mode = {
            "public": "public_research",
            "deep_research": "public_research",
            "private": "private_materials",
            "interview": "guided_interview",
            "synthetic": "fictional",
        }.get(mode, mode)
        runtime_source = str(runtime_source or "").lower()
        runtime_source = {
            "cli": "local_cli",
            "local": "local_cli",
            "local-cli": "local_cli",
            "provider": "api",
            "api_provider": "api",
            "remote": "api",
        }.get(runtime_source, runtime_source)
        input_mode = str(enrichment_input_mode or "").lower() or None
        persisted_job_config = dict(job_config or {})
        for key in ("turn_timeout_seconds", "acp_stream_limit_bytes"):
            raw_value = persisted_job_config.get(key)
            if raw_value is None:
                # A null control value means "use the runtime default" and
                # must not be written as an explicit job override.
                persisted_job_config.pop(key, None)
            elif key == "turn_timeout_seconds":
                persisted_job_config[key] = safe_timeout(raw_value)
            else:
                persisted_job_config[key] = safe_acp_stream_limit(raw_value)
        job_config = persisted_job_config
        self._validate_mode(p_type, mode, input_mode)

        duplicate = self._find_duplicate(name, aliases or [])
        if existing_persona_id:
            self.continuum.personas.get(existing_persona_id)
        elif duplicate is not None:
            if duplicate_action in {"cancel", "reject"}:
                raise DuplicatePersonaError(duplicate.id, duplicate.display_name)
            if duplicate_action in {"new_version", "re_research"}:
                # Explicit user choice: PersonaService will allocate a unique
                # package id while preserving the existing persona untouched.
                pass
            elif duplicate_action in {"enrich", "continue", "update"}:
                existing_persona_id = duplicate.id
            else:
                raise DuplicatePersonaError(duplicate.id, duplicate.display_name)

        probe, adapter, resolved_model, resolved_reasoning = await self._resolve_runtime(
            runtime_source=runtime_source,
            agent_id=agent_id,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
        )
        auth_profile_id = auth_profile_id or getattr(adapter, "credential_id", None)
        if self._requires_web_research(mode, input_mode):
            await self._resolve_or_verify_research_runtime(
                probe=probe,
                adapter=adapter,
                runtime=self._research_runtime_payload(
                    {
                        "runtime_source": runtime_source,
                        "agent_id": adapter.adapter_id,
                        "agent_name": probe.name,
                        "agent_version": probe.version,
                        "runtime_status": probe.status.value,
                        "model_id": resolved_model,
                        "reasoning_effort": resolved_reasoning,
                        "auth_profile_id": auth_profile_id,
                    },
                    job_config,
                ),
                job_id=f"preflight:{adapter.adapter_id}:{resolved_model}",
            )
        if (
            mode in {"private_materials", "guided_interview"}
            and runtime_source == "api"
            and not remote_material_consent
            and materials
        ):
            raise PrivateMaterialConsentRequired("remote_private_material_consent_required")

        persona = (
            self.continuum.personas.get(existing_persona_id)
            if existing_persona_id
            else self.continuum.personas.create(
                display_name=name,
                aliases=list(aliases or []),
                persona_type=p_type,
                run_mode=self._run_mode_for(p_type),
                birth_date=birth_date,
                death_date=death_date,
                data_cutoff_date=data_cutoff_date,
                sensitivity="private" if p_type.value.startswith("private_") else "normal",
            )
        )
        if self.profile_library is not None:
            self.profile_library.sync_persona(persona)
        task = self.continuum.compilation.create_task(persona.id)
        policy = self._coerce_research_policy(
            research_policy,
            default_profile="deep" if mode == "public_research" else "standard",
        )
        snapshot = probe.model_dump(mode="json")
        job = PersonaCreationJob(
            id=new_id("pcjob"),
            display_name=name,
            aliases=list(aliases or []),
            persona_type=p_type,
            creation_mode=mode,
            runtime_source=runtime_source,
            agent_id=adapter.adapter_id,
            model_id=resolved_model,
            reasoning_effort=resolved_reasoning,
            auth_profile_id=auth_profile_id,
            agent_version=probe.version,
            capability_snapshot=snapshot,
            persona_id=persona.id,
            compilation_task_id=task.id,
            research_policy=policy,
            visibility=(
                JobVisibility.INTERNAL.value
                if str(visibility).lower() == JobVisibility.INTERNAL.value
                else JobVisibility.USER.value
            ),
            job_config={
                **(job_config or {}),
                "materials": list(materials or []),
                "existing_persona_id": existing_persona_id,
                "remote_material_consent": bool(remote_material_consent),
                "runtime_binding_snapshot": {
                    "runtime_source": runtime_source,
                    "agent_id": adapter.adapter_id,
                    "agent_name": probe.name,
                    "model_id": resolved_model,
                    "reasoning_effort": resolved_reasoning,
                    "auth_profile_id": auth_profile_id,
                    "agent_version": probe.version,
                    "capabilities": snapshot.get("capabilities", {}),
                    "research": snapshot.get("research", {}),
                },
                "enrichment_input_mode": input_mode,
                "input_material_count": len(materials or []),
            },
        )
        job.progress.update_stage(
            "created",
            label="任务已创建",
            percent=percent_for_stage("created"),
            message="等待后台 Agent 任务启动",
        )
        job.touch_worker(WorkerState.STARTING, heartbeat_at=job.created_at)
        if existing_persona_id:
            existing_sources = self.continuum.personas.get_sources(persona.id)
            job.source_ids = [source.id for source in existing_sources]
            job.source_count = len(job.source_ids)
        self._save(job)
        if start_worker:
            self.start_job(job.id)
        return job

    async def validate_research_runtime(
        self,
        runtime: dict[str, Any],
        *,
        input_mode: str | None = None,
        force_revalidate: bool = False,
    ) -> AgentProbeResult:
        """Validate a selected runtime without creating a Persona job."""
        source = str(runtime.get("runtime_source") or "local_cli").lower()
        agent_id = str(runtime.get("agent_id") or "")
        if not agent_id:
            raise ResearchCapabilityError("research_runtime_required")
        probe, adapter, resolved_model, resolved_reasoning = await self._resolve_runtime(
            runtime_source=source,
            agent_id=agent_id,
            model_id=runtime.get("model_id"),
            reasoning_effort=runtime.get("reasoning_effort"),
        )
        if str(input_mode or "").lower() != "local_materials":
            try:
                await self._resolve_or_verify_research_runtime(
                    probe=probe,
                    adapter=adapter,
                    runtime=self._research_runtime_payload(
                        {
                            "runtime_source": source,
                            "agent_id": adapter.adapter_id,
                            "agent_name": probe.name,
                            "agent_version": probe.version,
                            "runtime_status": probe.status.value,
                            "model_id": resolved_model,
                            "reasoning_effort": resolved_reasoning,
                            "auth_profile_id": runtime.get("auth_profile_id"),
                        },
                        runtime,
                    ),
                    job_id=f"runtime-validation:{adapter.adapter_id}:{resolved_model}",
                    force_revalidate=force_revalidate or bool(runtime.get("force_revalidate")),
                )
            except ResearchCapabilityError:
                # A manual revalidation is itself a diagnostic operation.  Let
                # the UI display a typed BLOCKED/UNAVAILABLE result instead of
                # reducing it to a generic HTTP error; creation preflight still
                # raises because force_revalidate is false there.
                if force_revalidate and probe.research.verification_status.value in {
                    "blocked",
                    "unavailable",
                }:
                    return probe
                raise
        return probe

    @staticmethod
    def _research_runtime_payload(
        base: dict[str, Any], source: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Merge only safe research-policy dimensions into a runtime binding."""

        result = dict(base)
        source = source or {}
        for key in (
            "permission_profile",
            "research_tools",
            "research_tool_policy",
            "cli_flags",
            "turn_timeout_seconds",
            "acp_stream_limit_bytes",
        ):
            if source.get(key) is not None:
                result[key] = source[key]
        return result

    def _coerce_research_policy(
        self,
        value: ResearchPolicy | dict[str, Any] | None,
        *,
        default_profile: str,
    ) -> ResearchPolicy:
        if isinstance(value, ResearchPolicy):
            return value
        data = dict(value or {})
        profile = str(data.get("profile") or default_profile).lower()
        base = ResearchPolicy.for_profile(profile)
        if data:
            return ResearchPolicy.model_validate({**base.model_dump(mode="json"), **data})
        return base

    # A running job whose worker heartbeat is older than this and whose worker
    # task no longer exists is unrecoverable by waiting; it is marked failed
    # with the typed retryable code JOB_STALLED instead of freezing forever.
    STALL_WORKER_HEARTBEAT_SECONDS = 300

    @staticmethod
    def _heartbeat_age_seconds(value: Any) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - parsed).total_seconds())

    def reap_stalled_jobs(self) -> list[str]:
        """Typed-liveness sweep for jobs whose worker disappeared mid-run.

        "status running but no worker task" can never finish on its own: the
        future died (exception swallowed, event loop recycled) and the row
        keeps showing the same stage percent (the classic stuck-at-40%).  A
        job with a live task, a fresh heartbeat, or an in-flight model call is
        never touched, so a long MODEL_RUNNING turn is not misread as a stall.
        """

        reaped: list[str] = []
        placeholders = ", ".join("?" for _ in self.WORK_STATUSES)
        rows = self.continuum.database.conn.execute(
            f"SELECT id FROM persona_creation_jobs WHERE status IN ({placeholders})",
            tuple(sorted(self.WORK_STATUSES)),
        ).fetchall()
        for row in rows:
            job_id = str(row["id"])
            task = self._tasks.get(job_id)
            if task is not None and not task.done():
                continue
            try:
                job = self.get_job(job_id)
            except Exception:
                continue
            if job.job_config.get("agent_call_in_flight"):
                # The runtime timeout policy owns an in-flight model call.
                continue
            heartbeat_age = self._heartbeat_age_seconds(job.progress.worker_heartbeat_at)
            if heartbeat_age is not None and heartbeat_age < self.STALL_WORKER_HEARTBEAT_SECONDS:
                continue
            failure = JobFailure(
                code=PersonaFailureCode.JOB_STALLED.value,
                message=(
                    "job_stalled: worker task missing before a terminal state "
                    f"(last heartbeat {job.progress.worker_heartbeat_at})"
                ),
                phase=str(job.current_stage or ""),
                retriable=True,
                diagnostics={
                    "last_prompt_state": job.progress.prompt_state,
                    "last_percent": job.progress.percent,
                    "worker_heartbeat_at": job.progress.worker_heartbeat_at,
                },
            )
            job.progress.failure = failure
            job.failure_json = failure.model_dump(mode="json")
            job.status = "failed"
            job.current_stage = "job_stalled"
            job.touch_worker(WorkerState.LOST, finished_at=progress_now())
            self._save(job)
            reaped.append(job_id)
        return reaped

    def start_job(self, job_id: str) -> None:
        self.reap_stalled_jobs()
        existing = self._tasks.get(job_id)
        if existing and not existing.done():
            return
        # Register the pause signal *before* the worker can reach a boundary,
        # and seed it from durable state so a pause requested by another
        # process is honoured from the very first check.
        self.job_control.pause_event(job_id)
        self._tasks[job_id] = asyncio.create_task(
            self._run_job(job_id), name=f"persona-creation-{job_id}"
        )

    def shutdown(self) -> None:
        """Cancel in-process workers before the owning database is closed."""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        for job_id in list(self._tasks):
            # Durable control rows are intentionally kept: a pause survives a
            # restart.  Only the in-process signal is dropped.
            self.job_control.release(job_id)
        self._tasks.clear()
        self._active_bindings.clear()
        # Drop cached logical sessions; closing needs a loop, which may already
        # be gone during interpreter shutdown, so leak references gracefully.
        self._job_sessions.clear()
        self._job_session_keys.clear()

    async def resume_pending_jobs(self) -> list[str]:
        """Restart interrupted jobs -- never one the user paused.

        A durable pause request outlives the process, so a restart must
        restore the job to PAUSED and wait for a manual Resume instead of
        silently continuing to burn model calls.
        """

        rows = self.continuum.database.conn.execute(
            "SELECT id FROM persona_creation_jobs WHERE status IN (?, ?, ?, ?, ?, ?)",
            (
                "created",
                "planning",
                "researching",
                "ingesting_sources",
                "extracting",
                "compiling",
            ),
        ).fetchall()
        resumed: list[str] = []
        for row in rows:
            job_id = str(row["id"])
            try:
                job = self.get_job(job_id)
                if self.job_control.pause_requested(job_id):
                    # Honour the durable pause: do not start a worker at all.
                    job.status = "paused"
                    job.current_stage = job.current_stage or "paused"
                    job.touch_worker(
                        WorkerState.PAUSED, finished_at=datetime.now(UTC).isoformat()
                    )
                    self._save(job)
                    with contextlib.suppress(Exception):
                        await self._emit(job, "persona_creation_paused", stage=job.current_stage)
                    continue
                await self._assert_snapshot_available(job)
            except Exception as exc:
                job = self.get_job(job_id)
                job.status = "paused_runtime_unavailable"
                job.current_stage = "runtime_unavailable"
                job.error = str(exc)
                self._save(job)
                continue
            self.start_job(job_id)
            resumed.append(job_id)
        return resumed

    def get_job(self, job_id: str) -> PersonaCreationJob:
        row = self.continuum.database.conn.execute(
            "SELECT * FROM persona_creation_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(job_id)
        return self._row_to_job(row)

    def list_jobs(
        self,
        persona_id: str | None = None,
        *,
        task_center: bool = False,
        include_internal: bool = False,
        include_dismissed: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> list[PersonaCreationJob]:
        """List jobs without letting terminal history hide active work.

        The normal application query remains backwards compatible.  Task
        Center queries explicitly return every non-terminal user job first,
        then one paginated terminal-history page.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if persona_id:
            conditions.append("persona_id = ?")
            params.append(persona_id)
        if task_center:
            conditions.append("dismissed_at IS NULL")
            # A retry creates a new visible run while the original failure is
            # retained in durable history.  Keep superseded rows out of the
            # primary Task Center so the fresh run is the highlighted card.
            conditions.append("(superseded_by IS NULL OR superseded_by = '')")
            if not include_internal:
                conditions.append("COALESCE(visibility, 'user') = 'user'")
            where = " AND ".join(conditions) or "1 = 1"
            terminal_statuses = sorted(self.TERMINAL_STATUSES)
            terminal_placeholders = ", ".join("?" for _ in terminal_statuses)
            active_rows = self.continuum.database.conn.execute(
                f"SELECT * FROM persona_creation_jobs WHERE {where} "
                f"AND status NOT IN ({terminal_placeholders}) "
                "ORDER BY updated_at DESC, created_at DESC",
                (*params, *terminal_statuses),
            ).fetchall()
            terminal_params = [*params, *terminal_statuses]
            # The unified Task Center may over-fetch terminal rows from page 1
            # before merging the two job tables. Do not cap that look-ahead at
            # 1000, otherwise older history pages become falsely empty.
            size = max(1, safe_int(page_size, default=20, minimum=1) or 20)
            offset = max(0, (safe_int(page, default=1, minimum=1) or 1) - 1) * size
            terminal_rows = self.continuum.database.conn.execute(
                f"SELECT * FROM persona_creation_jobs WHERE {where} "
                f"AND status IN ({terminal_placeholders}) "
                "ORDER BY updated_at DESC, created_at DESC "
                "LIMIT ? OFFSET ?",
                (*terminal_params, size, offset),
            ).fetchall()
            return [self._row_to_job(row) for row in [*active_rows, *terminal_rows]]

        where = " AND ".join(conditions) or "1 = 1"
        rows = self.continuum.database.conn.execute(
            f"SELECT * FROM persona_creation_jobs WHERE {where} ORDER BY created_at DESC",
            tuple(params),
        ).fetchall()
        if not include_dismissed:
            # Preserve the old unrestricted behavior for internal callers,
            # while allowing explicit history queries to opt into dismissed rows.
            rows = [row for row in rows if row["dismissed_at"] is None]
        return [self._row_to_job(row) for row in rows]

    def task_center_counts(self, *, include_internal: bool = False) -> dict[str, int]:
        conditions = [
            "dismissed_at IS NULL",
            "(superseded_by IS NULL OR superseded_by = '')",
        ]
        params: list[Any] = []
        if not include_internal:
            conditions.append("COALESCE(visibility, 'user') = 'user'")
        where = " AND ".join(conditions)
        rows = self.continuum.database.conn.execute(
            "SELECT status, COUNT(*) AS count FROM persona_creation_jobs "
            f"WHERE {where} GROUP BY status",
            tuple(params),
        ).fetchall()
        counts = {
            str(row["status"]): safe_int(row["count"], default=0, minimum=0) or 0 for row in rows
        }
        terminal = self.TERMINAL_STATUSES
        active = sum(count for status, count in counts.items() if status not in terminal)
        badge_statuses = (
            active - counts.get("paused", 0) - counts.get("paused_runtime_unavailable", 0)
        )
        return {
            **counts,
            "active": active,
            "badge": max(0, badge_statuses),
            "terminal": sum(counts.get(status, 0) for status in terminal),
        }

    def dismiss_job(self, job_id: str) -> PersonaCreationJob:
        job = self.get_job(job_id)
        if job.status not in self.TERMINAL_STATUSES:
            raise JobNotTerminalError(job.id, job.status)
        job.dismissed_at = datetime.now(UTC).isoformat()
        self._save(job)
        return self.get_job(job.id)

    def cleanup_terminal_jobs(
        self, statuses: Iterable[str], *, include_internal: bool = False
    ) -> dict[str, Any]:
        requested = [str(status) for status in statuses]
        selected = sorted(set(requested) & self.TERMINAL_STATUSES)
        if not selected:
            return {"dismissed_job_ids": [], "dismissed_count": 0, "ignored_statuses": requested}
        conditions = [
            "dismissed_at IS NULL",
            f"status IN ({','.join('?' for _ in selected)})",
        ]
        params: list[Any] = list(selected)
        if not include_internal:
            conditions.append("COALESCE(visibility, 'user') = 'user'")
        where = " AND ".join(conditions)
        rows = self.continuum.database.conn.execute(
            f"SELECT id FROM persona_creation_jobs WHERE {where}", tuple(params)
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        if ids:
            now = datetime.now(UTC).isoformat()
            placeholders = ",".join("?" for _ in ids)
            self.continuum.database.conn.execute(
                "UPDATE persona_creation_jobs SET dismissed_at = ?, updated_at = ? "
                f"WHERE id IN ({placeholders})",
                (now, now, *ids),
            )
            self.continuum.database.conn.commit()
        return {
            "dismissed_job_ids": ids,
            "dismissed_count": len(ids),
            "ignored_statuses": [status for status in requested if status not in selected],
        }

    def get_events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        job = self.get_job(job_id)
        return job.events[max(0, safe_int(after, default=0, minimum=0) or 0) :]

    def subscribe_events(self, job_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.setdefault(job_id, set()).add(queue)
        return queue

    def unsubscribe_events(self, job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(job_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(job_id, None)

    async def pause_job(self, job_id: str) -> PersonaCreationJob:
        """Request a safe pause that takes effect at the next atomic boundary.

        The worker keeps running the current atomic unit (one search, one
        fetch, one dimension batch, one audit call) so no half-finished model
        output is lost, checkpoints it, and only then enters PAUSED.  A job
        without a live worker pauses immediately.

        The request is written to the control plane first, then signalled to
        the in-process worker.  It is never stored only in a ``job_config``
        snapshot that a running worker could overwrite.
        """
        job = self.get_job(job_id)
        if job.status in self.TERMINAL_STATUSES:
            return job
        task = self._tasks.get(job_id)
        if task is None or task.done():
            # No worker to wind down: pause is immediate.
            self.job_control.request_pause(job_id)
            job.status = "paused"
            job.current_stage = "paused"
            job.touch_worker(
                WorkerState.PAUSED, finished_at=datetime.now(UTC).isoformat()
            )
            self._save(job)
            # The request is already honoured; clear it so the next resume does
            # not start from a stale pending-pause state.
            self.job_control.clear_pause(job_id)
            self.job_control.release(job_id)
            self._save(job)
            await self._emit(job, "persona_creation_paused")
            return self.get_job(job_id)
        self.job_control.request_pause(job_id)
        job.job_config["pause_requested"] = True
        job.status = "pause_requested"
        job.progress.set_operation(job.current_stage)
        self._save(job)
        await self._emit(job, "persona_creation_pause_requested", stage=job.current_stage)
        return self.get_job(job_id)

    async def _enter_paused(self, job_id: str, stage: str) -> PersonaCreationJob:
        """Transition a worker that honoured a pause request into PAUSED.

        Only here -- after the atomic unit completed and checkpointed -- may
        the pause request be cleared.
        """

        job = self.get_job(job_id)
        self.job_control.clear_pause(job_id)
        self.job_control.release(job_id)
        job.status = "paused"
        job.current_stage = stage or job.current_stage or "paused"
        job.error = None
        job.touch_worker(WorkerState.PAUSED, finished_at=datetime.now(UTC).isoformat())
        self._save(job)
        await self._emit(
            job,
            "persona_creation_paused",
            stage=job.current_stage,
            checkpoint=job.job_config.get("last_checkpoint") or {},
        )
        return job

    async def _apply_runtime_override(
        self, job: PersonaCreationJob, runtime: dict[str, Any] | None, *, action: str
    ) -> None:
        """Switch the execution target (Agent/Model/Reasoning) before continuing.

        Only future work is affected: persisted evidence, batch checkpoints,
        and completed dimension artifacts stay valid, and the job's logical
        sessions are dropped so the new target never inherits the old one's
        threads or runtime affinity.
        """
        selected = dict(runtime or {})
        agent_id = str(selected.get("agent_id") or "").strip()
        runtime_source = str(selected.get("runtime_source") or "").strip()
        if not agent_id and not runtime_source:
            return
        model_id = str(selected.get("model_id") or "").strip() or None
        reasoning_effort = str(selected.get("reasoning_effort") or "").strip() or None
        auth_profile_id = selected.get("auth_profile_id")
        probe, adapter, resolved_model, resolved_reasoning = await self._resolve_runtime(
            runtime_source=runtime_source or job.runtime_source,
            agent_id=agent_id or job.agent_id,
            model_id=model_id or job.model_id,
            reasoning_effort=reasoning_effort or job.reasoning_effort,
        )
        # Compatibility gate for the work that still has to run: structured
        # output must exist, and web research only when research is unfinished.
        flags = probe.capabilities
        if str(getattr(flags, "structured_output_mode", "") or "") in {
            "unavailable",
        }:
            raise RuntimeBindingError(
                f"runtime_override_structured_output_unavailable:{adapter.adapter_id}"
            )
        if (
            self._requires_web_research(job.creation_mode, str(
                job.job_config.get("enrichment_input_mode") or "") or None)
            and not job.research_stop_reason
            and not flags.web_search
            and not probe.research.discovers_sources
        ):
            raise RuntimeBindingError(
                f"runtime_override_web_research_required:{adapter.adapter_id}"
            )
        job.runtime_source = runtime_source or job.runtime_source
        job.agent_id = adapter.adapter_id
        job.model_id = resolved_model
        job.reasoning_effort = resolved_reasoning
        job.auth_profile_id = auth_profile_id or getattr(adapter, "credential_id", None)
        job.agent_version = probe.version
        job.capability_snapshot = probe.model_dump(mode="json")
        job.job_config["runtime_binding_snapshot"] = {
            "runtime_source": job.runtime_source,
            "agent_id": adapter.adapter_id,
            "agent_name": probe.name,
            "model_id": resolved_model,
            "reasoning_effort": resolved_reasoning,
            "auth_profile_id": job.auth_profile_id,
            "agent_version": probe.version,
            "capabilities": job.capability_snapshot.get("capabilities", {}),
            "research": job.capability_snapshot.get("research", {}),
        }
        job.job_config["model_switched"] = True
        self._record_execution_history(
            job,
            action,
            agent_name=probe.name,
            requested_model=model_id,
            effective_model=resolved_model,
            reasoning=resolved_reasoning,
        )
        await self.close_job_sessions(job.id)
        self._save(job)

    async def resume_job(
        self, job_id: str, *, runtime: dict[str, Any] | None = None
    ) -> PersonaCreationJob:
        job = self.get_job(job_id)
        try:
            await self._assert_snapshot_available(job)
        except Exception as exc:
            job.status = "paused_runtime_unavailable"
            job.current_stage = "runtime_unavailable"
            job.error = str(exc)
            self._save(job)
            await self._emit(job, "persona_creation_runtime_unavailable", error=str(exc))
            return job
        if job.status not in self.TERMINAL_STATUSES:
            if runtime:
                try:
                    await self._apply_runtime_override(
                        job, runtime, action="resume_with_runtime_change"
                    )
                    await self._assert_snapshot_available(job)
                except RuntimeBindingError:
                    raise
                except Exception as exc:
                    job.status = "paused_runtime_unavailable"
                    job.current_stage = "runtime_unavailable"
                    job.error = str(exc)
                    self._save(job)
                    await self._emit(
                        job, "persona_creation_runtime_unavailable", error=str(exc)
                    )
                    return job
            # Resume clears the control-plane request before any work restarts
            # (durable + in-process signal), then continues from the checkpoint.
            self.job_control.clear_pause(job_id)
            self.job_control.clear_cancel(job_id)
            self.job_control.release(job_id)
            job.job_config.pop("pause_requested", None)
            job.job_config.pop("pause_requested_at", None)
            job.job_config.pop("cancel_requested", None)
            job.status = "created"
            job.current_stage = "resume"
            job.error = None
            job.worker_state = WorkerState.STARTING
            job.worker_finished_at = None
            job.touch_worker(WorkerState.STARTING)
            self._save(job)
            self.start_job(job_id)
        return self.get_job(job_id)

    async def retry_job(
        self, job_id: str, *, runtime: dict[str, Any] | None = None
    ) -> PersonaCreationJob:
        """Create a new retry run and retain the original failed history.

        A retry never rebuilds the persona from zero.  Sources, research,
        evidence and completed dimensions are durable; the new run re-enters
        the phase that failed.  An audit/quality-gate failure with all eight
        dimensions already present skips research and re-extraction entirely
        and goes straight back to Final Audit / Audit Repair.
        """
        job = self.get_job(job_id)
        if job.status not in {"failed", "failed_quality_gate", "paused_runtime_unavailable"}:
            return job
        failure_code = failure_code_for(job.failure_json)
        retryable = is_retryable_failure(job.failure_json)
        if not retryable and not runtime:
            return job
        checkpoint = job.checkpoints[-1] if job.checkpoints else {}
        audit_phase_failure = failure_code in {
            PersonaFailureCode.AUDIT_REPAIR_FAILED.value,
            PersonaFailureCode.FINAL_AUDIT_FAILED.value,
            PersonaFailureCode.FINAL_QUALITY_GATE_FAILED.value,
        } or str((job.failure_json or {}).get("message") or "").startswith(
            ("audit_repair_failed:", "final_quality_gate_failed:", "final_audit_failed:")
        )
        completed_dimensions = [
            dimension
            for dimension in REQUIRED_DIMENSIONS
            if (safe_int(job.dimension_progress.get(dimension, 0), default=0, minimum=0) or 0) > 0
        ]
        dimensions_complete = len(completed_dimensions) >= len(REQUIRED_DIMENSIONS)
        # Skip research/re-extraction only when the audit is the phase that
        # failed and every dimension artifact is already durable.
        retry_stage = (
            "final_audit"
            if audit_phase_failure and dimensions_complete
            else str(checkpoint.get("stage") or "retry")
        )
        now = datetime.now(UTC).isoformat()
        retry = job.model_copy(deep=True)
        retry.id = new_id("pcjob")
        retry.status = "created"
        retry.current_stage = retry_stage
        retry.error = None
        retry.failure_json = None
        retry.dismissed_at = None
        retry.superseded_by = None
        retry.created_at = now
        retry.updated_at = now
        retry.events = []
        retry.agent_call_audits = []
        retry.job_config = {**job.job_config, "retry_of": job.id}
        # Control-plane flags belong to the old run, never to the retry.
        for key in (
            "pause_requested",
            "pause_requested_at",
            "cancel_requested",
            "control_version",
        ):
            retry.job_config.pop(key, None)
        retry.job_config["retry_stage"] = retry_stage
        retry.job_config["retry_failure_code"] = failure_code
        if retry_stage == "final_audit":
            # Re-run the audit/repair decisions with the current code; the
            # evidence and dimension artifacts are reused as-is.
            retry.job_config.pop("final_global_audit", None)
        if runtime:
            # Persisted evidence/batch checkpoints/artifacts stay valid; the
            # runtime override only retargets the work that still has to run.
            await self._apply_runtime_override(retry, runtime, action="retry_with_runtime_change")
        retry.progress = JobProgress.model_validate(
            {
                **retry.progress.model_dump(mode="json"),
                "failure": None,
                "stage": retry.current_stage,
                "label": "正在重试",
                "percent": percent_for_stage(retry.current_stage),
                "message": "从上一次失败检查点重新启动",
            }
        )
        retry.worker_state = WorkerState.STARTING
        retry.worker_started_at = None
        retry.worker_heartbeat_at = None
        retry.worker_finished_at = None
        retry.agent_call_count = 0
        retry.progress.worker_state = WorkerState.STARTING
        retry.progress.worker_started_at = None
        retry.progress.worker_heartbeat_at = None
        retry.progress.worker_finished_at = None
        retry.progress.agent_call_count = 0
        retry.progress.agent_call_attempt_count = 0
        retry.progress.agent_call_completed_count = 0
        retry.progress.agent_call_failed_count = 0
        retry.progress.activity_tracker = {}
        retry.progress.process_alive = None
        retry.progress.runtime_binding_snapshot = {}
        retry.progress.set_operation(retry.current_stage)
        job.superseded_by = retry.id
        self._save(job)
        self._save(retry)
        self.start_job(retry.id)
        return self.get_job(retry.id)

    async def continue_job(
        self,
        job_id: str,
        *,
        research_policy: ResearchPolicy | dict[str, Any] | None = None,
        requested_scope: str | None = None,
        materials: list[dict[str, Any]] | None = None,
        runtime: dict[str, Any] | None = None,
        enrichment_input_mode: str | None = None,
    ) -> PersonaCreationJob:
        """Start a new enrichment task while preserving old evidence/artifacts."""
        job = self.get_job(job_id)
        if job.status not in {"completed", "completed_with_gaps"}:
            return await self.resume_job(job_id)
        child = await self.create_enrichment_run(
            parent_job_id=job.id,
            research_policy=research_policy,
            requested_scope=requested_scope,
            enrichment_input_mode=str(
                enrichment_input_mode
                or job.job_config.get("enrichment_input_mode")
                or ("web_research" if job.creation_mode == "public_research" else "local_materials")
            ),
            materials=list(materials or []),
            runtime=runtime,
        )
        self.start_job(child.id)
        return self.get_job(child.id)

    async def create_enrichment_run(
        self,
        *,
        parent_job_id: str,
        research_policy: ResearchPolicy | dict[str, Any] | None = None,
        requested_scope: str | None = None,
        enrichment_input_mode: str = "local_materials",
        materials: list[dict[str, Any]] | None = None,
        runtime: dict[str, Any] | None = None,
        base_persona_version: int | None = None,
        profile_enrichment_job_id: str | None = None,
    ) -> PersonaCreationJob:
        """Create an immutable child run for enrichment.

        The completed parent is never reset or mutated.  Existing artifacts
        are copied into a fresh compilation task and all new input is saved in
        the child snapshot before a worker is allowed to start.
        """
        parent = self.get_job(parent_job_id)
        if parent.status not in {"completed", "completed_with_gaps"}:
            raise PersonaCreationError("enrichment_parent_not_terminal")
        if not parent.persona_id:
            raise PersonaCreationError("persona_not_initialized")
        previous_task = (
            self.continuum.compilation.get_task(parent.compilation_task_id)
            if parent.compilation_task_id
            else None
        )
        task = self.continuum.compilation.create_task(parent.persona_id)
        for artifact in list(previous_task.artifacts if previous_task else []):
            with contextlib.suppress(Exception):
                self.continuum.compilation.submit_research_artifact(task.id, artifact)
        child_config = {
            **parent.job_config,
            "parent_job_id": parent.id,
            "parent_profile_enrichment_job_id": profile_enrichment_job_id,
            "base_persona_version": safe_int(
                base_persona_version or parent.job_config.get("base_persona_version"),
                default=1,
                minimum=1,
            )
            or 1,
            "materials": list(materials or []),
            "input_material_ids": [
                str(item.get("id"))
                for item in (materials or [])
                if isinstance(item, dict) and item.get("id")
            ],
            "input_material_count": len(materials or []),
            "new_source_ids": [],
            "enrichment_input_mode": enrichment_input_mode,
            "enrichment_scope": requested_scope
            or parent.job_config.get("enrichment_scope", "full_refresh"),
        }
        runtime_snapshot = dict(parent.capability_snapshot)
        selected_runtime = dict(runtime or {})
        for key in ("acp_stream_limit_bytes", "turn_timeout_seconds"):
            if selected_runtime.get(key) is not None:
                child_config.setdefault(key, selected_runtime[key])
        for key in ("turn_timeout_seconds", "acp_stream_limit_bytes"):
            raw_value = child_config.get(key)
            if raw_value is None:
                child_config.pop(key, None)
            elif key == "turn_timeout_seconds":
                child_config[key] = safe_timeout(raw_value)
            else:
                child_config[key] = safe_acp_stream_limit(raw_value)
        selected_agent = str(selected_runtime.get("agent_id") or parent.agent_id)
        selected_source = str(selected_runtime.get("runtime_source") or parent.runtime_source)
        selected_model = selected_runtime.get("model_id") or parent.model_id
        selected_reasoning = selected_runtime.get("reasoning_effort") or parent.reasoning_effort
        selected_auth = selected_runtime.get("auth_profile_id") or parent.auth_profile_id
        if runtime:
            probe, adapter, selected_model, selected_reasoning = await self._resolve_runtime(
                runtime_source=selected_source,
                agent_id=selected_agent,
                model_id=selected_model,
                reasoning_effort=selected_reasoning,
            )
            runtime_snapshot = probe.model_dump(mode="json")
            selected_agent = adapter.adapter_id
            selected_auth = selected_auth or getattr(adapter, "credential_id", None)
        if enrichment_input_mode in {"web_research", "hybrid"}:
            verified_probe = await self.validate_research_runtime(
                {
                    "runtime_source": selected_source,
                    "agent_id": selected_agent,
                    "model_id": selected_model,
                    "reasoning_effort": selected_reasoning,
                    "auth_profile_id": selected_auth,
                },
                input_mode=enrichment_input_mode,
            )
            runtime_snapshot = verified_probe.model_dump(mode="json")
        child_config["runtime_binding_snapshot"] = {
            "runtime_source": selected_source,
            "agent_id": selected_agent,
            "agent_name": str(runtime_snapshot.get("name") or selected_agent),
            "model_id": selected_model,
            "reasoning_effort": selected_reasoning,
            "auth_profile_id": selected_auth,
            "agent_version": runtime_snapshot.get("version") or parent.agent_version,
            "capabilities": runtime_snapshot.get("capabilities", {}),
            "research": runtime_snapshot.get("research", {}),
        }
        child = PersonaCreationJob(
            id=new_id("pcjob"),
            display_name=parent.display_name,
            aliases=list(parent.aliases),
            persona_type=parent.persona_type,
            creation_mode=(
                "public_research"
                if enrichment_input_mode in {"web_research", "hybrid"}
                else "private_materials"
            ),
            runtime_source=selected_source,
            agent_id=selected_agent,
            model_id=selected_model,
            reasoning_effort=selected_reasoning,
            auth_profile_id=selected_auth,
            agent_version=(runtime_snapshot.get("version") if runtime else parent.agent_version),
            capability_snapshot=runtime_snapshot,
            persona_id=parent.persona_id,
            compilation_task_id=task.id,
            research_policy=self._coerce_research_policy(
                research_policy,
                default_profile=parent.research_policy.profile,
            )
            if research_policy is not None
            else parent.research_policy,
            source_ids=list(parent.source_ids),
            source_count=parent.source_count,
            dimension_progress=dict(parent.dimension_progress),
            coverage=dict(parent.coverage),
            current_stage="created",
            visibility=(
                JobVisibility.INTERNAL.value
                if profile_enrichment_job_id
                else JobVisibility.USER.value
            ),
            job_config=child_config,
        )
        child.touch_worker(WorkerState.STARTING, heartbeat_at=child.created_at)
        self._save(child)
        return child

    async def cancel_job(self, job_id: str) -> PersonaCreationJob:
        job = self.get_job(job_id)
        if job.status in self.TERMINAL_STATUSES:
            return job
        # A pending safe-pause request must not survive a hard cancel.
        self.job_control.request_cancel(job_id)
        job.job_config.pop("pause_requested", None)
        job.status = "cancelled"
        job.current_stage = "cancelled"
        job.touch_worker(WorkerState.FINISHED, finished_at=datetime.now(UTC).isoformat())
        self._save(job)
        self.job_control.clear_all(job_id)
        self.job_control.release(job_id)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(Exception):
            await self.close_job_sessions(job_id)
        with contextlib.suppress(Exception):
            await self.close_research_backends(job_id)
        await self.close_job_persistence(job_id)
        await self._emit(job, "persona_creation_cancelled")
        return job

    # ------------------------------------------------------------------
    # Materials and guided interview
    # ------------------------------------------------------------------

    async def add_materials(
        self, job_id: str, materials: list[dict[str, Any]], *, consent: bool = False
    ) -> PersonaCreationJob:
        job = self.get_job(job_id)
        if job.persona_id is None:
            raise PersonaCreationError("persona_not_initialized")
        if (
            job.runtime_source == "api"
            and not consent
            and job.persona_type.value.startswith("private_")
        ):
            raise PrivateMaterialConsentRequired("remote_private_material_consent_required")
        config_materials = list(job.job_config.get("materials", []))
        config_materials.extend(materials)
        job.job_config["materials"] = config_materials
        if consent:
            job.job_config["remote_material_consent"] = True
        self._save(job)
        if job.status in {"waiting_for_materials", "paused"}:
            await self.resume_job(job.id)
        return self.get_job(job.id)

    async def answer_interview(
        self, job_id: str, answer: str, *, dimension: str | None = None
    ) -> PersonaCreationJob:
        job = self.get_job(job_id)
        if not str(answer).strip():
            raise PersonaCreationError("interview_answer_required")
        if job.persona_id is None:
            raise PersonaCreationError("persona_not_initialized")
        if (
            job.persona_type.value.startswith("private_")
            and job.runtime_source == "api"
            and not bool(job.job_config.get("remote_material_consent"))
        ):
            raise PrivateMaterialConsentRequired("remote_private_material_consent_required")
        source = self.continuum.personas.add_source_text(
            job.persona_id,
            title=f"Guided interview answer{f' · {dimension}' if dimension else ''}",
            source_type="guided_interview",
            canonical_url=None,
            publisher="Persona Continuum guided interview",
            author="user",
            published_at=None,
            accessed_at=datetime.now(UTC).isoformat(),
            content=str(answer).strip(),
            metadata={
                "provenance": "user_provided",
                "source_kind": "guided_interview",
                "material_kind": "guided_interview",
                "dimension": dimension,
                "privacy": "private_material",
            },
        )
        job.source_ids = sorted(set([*job.source_ids, source.id]))
        job.source_count = len(job.source_ids)
        job.job_config.setdefault("interview_answers", []).append(
            {"source_id": source.id, "dimension": dimension}
        )
        job.status = "created"
        job.current_stage = "interview_answer_ingested"
        if self.material_intelligence is not None:
            material_job = await self._analyze_private_materials(job, [source.id], incremental=True)
            job.job_config["material_analysis"] = material_job.model_dump(mode="json")
            await self._emit(
                job,
                "persona_material_analysis_completed",
                material_job_id=material_job.id,
                material_status=material_job.status.value,
                coverage=material_job.coverage,
            )
        self._save(job)
        self.start_job(job.id)
        return self.get_job(job.id)

    # ------------------------------------------------------------------
    # World actor matching and completion
    # ------------------------------------------------------------------

    def match_world_actors(self, actors: list[dict[str, Any]]) -> list[PersonaMatch]:
        personas = self.continuum.personas.list()
        exact: dict[str, list[Any]] = {}
        for persona in personas:
            values = [persona.id, persona.display_name, *persona.manifest.aliases]
            for value in values:
                key = self._normalise_name(str(value))
                if key:
                    exact.setdefault(key, []).append(persona)
        matches: list[PersonaMatch] = []
        for raw_actor in actors:
            raw = (
                raw_actor.model_dump(mode="json")
                if hasattr(raw_actor, "model_dump")
                else dict(raw_actor)
                if isinstance(raw_actor, dict)
                else {"id": str(raw_actor), "name": str(raw_actor)}
            )
            actor_id = str(raw.get("id") or raw.get("actor_id") or raw.get("name") or "actor")
            name = str(raw.get("name") or raw.get("display_name") or actor_id)
            actor_type = str(raw.get("actor_type") or "persona_actor").lower()
            profile_type = str(raw.get("profile_type") or "").lower()
            if (
                actor_type
                in {
                    "organization_actor",
                    "organization",
                    "institution_actor",
                    "collective_actor",
                    "environment_actor",
                    "environment",
                }
                or profile_type in {"organization", "institution", "collective"}
                or raw.get("entity_category") == "non_agent"
            ):
                matches.append(
                    PersonaMatch(
                        actor_id=actor_id,
                        display_name=name,
                        actor_type=actor_type,
                        status="NOT_PERSON",
                        reason="organization_or_environment_actor",
                    )
                )
                continue
            persona_id = raw.get("persona_id")
            direct = self._persona_by_id(personas, str(persona_id)) if persona_id else None
            candidates = exact.get(self._normalise_name(str(persona_id or actor_id)), [])
            if not candidates:
                candidates = exact.get(self._normalise_name(name), [])
            if direct:
                candidates = [direct]
            else:
                # A persona id and display name can normalise to the same key
                # (for example ``steve_jobs`` and ``Steve Jobs``).  Treat those
                # aliases as one exact candidate rather than an ambiguity.
                candidates = list({candidate.id: candidate for candidate in candidates}.values())
            if len(candidates) == 1:
                p = candidates[0]
                matches.append(
                    PersonaMatch(
                        actor_id=actor_id,
                        display_name=name,
                        actor_type=actor_type,
                        status="MATCHED",
                        persona_id=p.id,
                        confidence=1.0,
                        reason="exact_persona_id_or_name",
                    )
                )
                continue
            if len(candidates) > 1:
                matches.append(
                    PersonaMatch(
                        actor_id=actor_id,
                        display_name=name,
                        actor_type=actor_type,
                        status="AMBIGUOUS",
                        confidence=0.0,
                        candidates=[
                            {"id": p.id, "display_name": p.display_name} for p in candidates
                        ],
                        reason="multiple_exact_persona_candidates",
                    )
                )
                continue
            fuzzy: list[tuple[float, Any]] = []
            needle = self._normalise_name(name)
            for p in personas:
                for value in [p.id, p.display_name, *p.manifest.aliases]:
                    score = difflib.SequenceMatcher(
                        None, needle, self._normalise_name(str(value))
                    ).ratio()
                    fuzzy.append((score, p))
            fuzzy.sort(key=lambda item: item[0], reverse=True)
            if fuzzy and fuzzy[0][0] >= 0.86:
                top = [item for item in fuzzy if item[0] >= fuzzy[0][0] - 0.02]
                unique = {item[1].id: item for item in top}
                if len(unique) == 1:
                    p = next(iter(unique.values()))[1]
                    matches.append(
                        PersonaMatch(
                            actor_id=actor_id,
                            display_name=name,
                            actor_type=actor_type,
                            status="AMBIGUOUS",
                            confidence=safe_probability(fuzzy[0][0], default=0.0) or 0.0,
                            candidates=[{"id": p.id, "display_name": p.display_name}],
                            reason="fuzzy_match_requires_confirmation",
                        )
                    )
                    continue
            matches.append(
                PersonaMatch(
                    actor_id=actor_id,
                    display_name=name,
                    actor_type=actor_type,
                    status="MISSING",
                    reason="no_safe_persona_match",
                )
            )
        return matches

    async def confirm_world_persona_completion(
        self,
        *,
        actors: list[dict[str, Any]],
        selected_actor_ids: list[str],
        runtime: dict[str, Any],
        research_policy: dict[str, Any] | None = None,
        materials_by_actor: dict[str, list[dict[str, Any]]] | None = None,
        remote_material_consent: bool = False,
    ) -> WorldPersonaCompletionResult:
        normalized_actors = [
            item.model_dump(mode="json")
            if hasattr(item, "model_dump")
            else dict(item)
            if isinstance(item, dict)
            else {"id": str(item), "name": str(item)}
            for item in actors
        ]
        matches = self.match_world_actors(normalized_actors)
        selected = {str(actor_id) for actor_id in selected_actor_ids}
        jobs: list[dict[str, Any]] = []
        materials_by_actor = materials_by_actor or {}
        for match in matches:
            if match.actor_id not in selected or match.status not in {"MISSING", "AMBIGUOUS"}:
                continue
            actor = next(
                (
                    item
                    for item in normalized_actors
                    if str(item.get("id") or item.get("actor_id") or item.get("name"))
                    == match.actor_id
                ),
                {},
            )
            if match.status == "AMBIGUOUS" and not actor.get("force_create_persona"):
                match.reason = "ambiguous_match_requires_manual_binding"
                continue
            raw_type = str(actor.get("persona_type") or "public_living_person")
            try:
                p_type = PersonaType(raw_type)
            except ValueError:
                p_type = (
                    PersonaType.PRIVATE_LIVING_PERSON
                    if actor.get("private")
                    else PersonaType.PUBLIC_LIVING_PERSON
                )
            mode = "private_materials" if p_type.value.startswith("private_") else "public_research"
            actor_materials = materials_by_actor.get(match.actor_id, [])
            if mode == "private_materials" and not actor_materials:
                match.reason = "private_materials_or_guided_interview_required"
                continue
            job = await self.create_job(
                display_name=match.display_name,
                aliases=list(actor.get("aliases") or []),
                persona_type=p_type,
                creation_mode=mode,
                runtime_source=str(runtime.get("runtime_source") or "local_cli"),
                agent_id=str(runtime.get("agent_id") or ""),
                model_id=runtime.get("model_id"),
                reasoning_effort=runtime.get("reasoning_effort"),
                auth_profile_id=runtime.get("auth_profile_id"),
                research_policy=research_policy,
                materials=actor_materials,
                remote_material_consent=remote_material_consent,
                duplicate_action="enrich",
                job_config={"world_actor_id": match.actor_id},
            )
            jobs.append(job.model_dump(mode="json"))
            await self._emit(
                job,
                "world_missing_personas_detected",
                actor_id=match.actor_id,
                display_name=match.display_name,
            )
            await self._emit(job, "world_persona_completion_started", actor_id=match.actor_id)
        status = (
            "waiting_for_materials"
            if any(
                match.reason == "private_materials_or_guided_interview_required"
                for match in matches
            )
            and not jobs
            else "started"
        )
        return WorldPersonaCompletionResult(status=status, jobs=jobs, matches=matches)

    # ------------------------------------------------------------------
    # Internal orchestration
    # ------------------------------------------------------------------

    async def _run_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        from persona_continuum.performance.tracing import default_tracer

        trace_id = f"job:{job_id}"
        tracer = default_tracer()
        tracer.start_task("persona_creation", trace_id)
        tracer.record(
            trace_id,
            persona_id=job.persona_id,
            model_id=job.model_id,
            reasoning_effort=job.reasoning_effort,
            research_profile=job.research_policy.profile,
        )
        self._touch_worker(job, WorkerState.RUNNING)
        try:
            # A retry that failed inside the final audit already has every
            # source, evidence row and dimension artifact; re-running research
            # or the eight dimensions would burn model calls for nothing.
            retry_stage = str(job.job_config.get("retry_stage") or "")
            before_sources = set(job.source_ids)
            job.job_config.setdefault("before_source_ids", sorted(before_sources))
            await self._emit(job, "persona_creation_started")
            await self._set_stage(job, "planning", "planning")
            await self._assert_snapshot_available(job)
            await self._ingest_configured_materials(job)
            if (
                self.material_intelligence is not None
                and job.persona_id
                and job.source_ids
                and (
                    job.creation_mode in {"private_materials", "guided_interview", "fictional"}
                    or job.job_config.get("enrichment_input_mode") in {"local_materials", "hybrid"}
                )
                and not job.job_config.get("material_analysis")
            ):
                # Atomic boundary: a material analysis window is a batch of
                # model calls and must not start once a pause is pending.
                self._raise_if_pause_requested(job.id)
                material_job = await self._analyze_private_materials(
                    job, job.source_ids, incremental=True
                )
                job.job_config["material_analysis"] = material_job.model_dump(mode="json")
                job.job_config["material_progress"] = dict(material_job.progress)
                self._save(job)
                await self._emit(
                    job,
                    "persona_material_analysis_completed",
                    material_job_id=material_job.id,
                    material_status=material_job.status.value,
                    coverage=material_job.coverage,
                )

            job.job_config["new_source_ids"] = sorted(set(job.source_ids) - before_sources)
            job.job_config["input_material_count"] = (
                safe_int(job.job_config.get("input_material_count"), default=0, minimum=0) or 0
            )
            self._save(job)

            uses_web_research = self._requires_web_research(
                job.creation_mode,
                str(job.job_config.get("enrichment_input_mode") or "") or None,
            )
            if uses_web_research and retry_stage == "final_audit":
                # Retry of an audit-phase failure: research already converged,
                # so skip straight to re-judging the existing evidence.
                uses_web_research = False
                await self._emit(
                    job,
                    "persona_research_skipped",
                    reason="audit_retry_reuses_completed_research",
                    source_count=job.source_count,
                )
            if uses_web_research:
                research_started = time.perf_counter()
                try:
                    async with self._persona_research_controller.slot():
                        with tracer.span(trace_id, "research"):
                            await self._research_public(job)
                except BaseException as exc:
                    await self._persona_research_controller.record_failure(exc)
                    raise
                else:
                    await self._persona_research_controller.record_success(
                        latency_ms=(time.perf_counter() - research_started) * 1000.0
                    )
                job.job_config["new_source_ids"] = sorted(set(job.source_ids) - before_sources)
                job.job_config["adaptive_persona_scheduler"] = (
                    self._persona_research_controller.snapshot()
                )
                self._save(job)
            elif job.source_count == 0:
                if job.creation_mode in {"private_materials", "guided_interview"}:
                    await self._request_interview_question(job)
                job.status = "waiting_for_materials"
                job.current_stage = "waiting_for_materials"
                self._save(job)
                return

            # Research rounds build one incremental cross-dimension Evidence
            # Ledger.  The eight dimension artifacts are synthesized once from
            # that ledger after research converges.
            should_synthesize_dimensions = bool(
                not uses_web_research
                or getattr(self.continuum.config, "persona_evidence_intelligence", True)
                or len(job.dimension_progress) < len(REQUIRED_DIMENSIONS)
            )
            # A retry run may reuse the prior run's validated dimension
            # artifacts instead of re-paying for extraction; the global audit
            # (with the fixed rules) still re-judges them below.  For an
            # audit-phase retry this is what keeps the retry from rebuilding
            # the eight dimensions from scratch.
            reusable_artifacts = (
                self._retry_reusable_dimension_artifacts(job)
                if should_synthesize_dimensions or retry_stage == "final_audit"
                else None
            )
            if retry_stage == "final_audit" and reusable_artifacts is None:
                # Coverage no longer matches the settled evidence, so the
                # artifacts cannot be trusted: fall back to a real extraction
                # pass in ``_extract_dimensions`` below.
                retry_stage = ""
                job.job_config["retry_stage"] = ""
            if reusable_artifacts is not None:
                await self._set_stage(job, "extracting", "retry_dimension_reuse")
                restored = self._restore_reused_artifact_provenance(job, reusable_artifacts)
                await self._emit(
                    job,
                    "persona_dimension_extraction_resumed",
                    dimensions=len(reusable_artifacts),
                    provenance_restored=restored,
                )
            elif should_synthesize_dimensions:
                with tracer.span(trace_id, "dimension_synthesis"):
                    await self._extract_dimensions(job)
                completed_dimensions = [
                    dim
                    for dim in REQUIRED_DIMENSIONS
                    if (safe_int(job.dimension_progress.get(dim, 0), default=0, minimum=0) or 0) > 0
                ]
                self._checkpoint(job, "dimensions_completed", dimensions=completed_dimensions)
            coverage = self._coverage(job)
            job.coverage = coverage
            await self._emit(job, "persona_coverage_updated", coverage=coverage)

            # A private identity assembled from one short note is not promoted
            # to a high-confidence digital persona.  It may still be compiled
            # as an explicitly visible draft so the user can continue adding
            # materials/interview answers without losing provenance.
            if (
                job.persona_type.value.startswith("private_")
                and job.source_count < 3
                and job.creation_mode in {"private_materials", "guided_interview"}
            ):
                await self._request_interview_question(job)

            if job.creation_mode in {"private_materials", "guided_interview"} and not coverage.get(
                "dimensions_complete"
            ):
                await self._request_interview_question(job)

            if not job.source_ids:
                if uses_web_research:
                    raise PersonaCreationError("public_research_produced_no_sources")
                job.status = "waiting_for_materials"
                job.current_stage = "waiting_for_materials"
                self._save(job)
                return

            self._assert_material_agent_analysis(job)

            # Duplicate-only uploads still pass through Material Intelligence
            # and the eight-dimension Agent extraction, but must not trigger a
            # new Persona compilation/version.
            if (
                (safe_int(job.job_config.get("input_material_count"), default=0, minimum=0) or 0)
                > 0
                or uses_web_research
            ) and not job.job_config.get("new_source_ids") and not job.job_config.get("retry_of"):
                # An explicit retry must still run the final audit and compile;
                # the duplicate-input shortcut only applies to fresh uploads.
                job.status = "completed"
                job.current_stage = "NO_NEW_INFORMATION"
                job.error = None
                self._save(job)
                await self._emit(
                    job,
                    "persona_creation_completed",
                    result="NO_NEW_INFORMATION",
                    persona_id=job.persona_id,
                )
                return

            if bool(getattr(self.continuum.config, "persona_final_full_audit", True)):
                with tracer.span(trace_id, "global_audit"):
                    await self._run_global_audits(job)
                self._checkpoint(job, "audit_completed")

            # ``_set_stage`` is itself a pause boundary: a pause requested
            # while the audit finished must never walk into compilation.
            await self._set_stage(job, "compiling", "compiling")
            if not job.persona_id or not job.compilation_task_id:
                raise PersonaCreationError("compilation_binding_missing")
            # Compilation merges every attached artifact, so superseded
            # versions (e.g. a pre-fix historical_* artifact replaced during
            # retry reuse or audit repair) must not leak stale claims.
            with tracer.span(trace_id, "compile"):
                if hasattr(self.continuum.compilation, "retain_latest_dimension_artifacts"):
                    self.continuum.compilation.retain_latest_dimension_artifacts(
                        job.compilation_task_id
                    )
                compiled = self.continuum.compilation.compile_persona(
                    job.persona_id, job.compilation_task_id
                )
                validation = self.continuum.compilation.validate_persona(job.persona_id)
            task = self.continuum.compilation.get_task(job.compilation_task_id)
            has_gaps = bool(
                task.status == "completed_with_gaps"
                or not coverage.get("passed")
                or validation.failure_reason
            )
            manifest = self.continuum.personas.get(job.persona_id).manifest
            manifest.compile_state = (
                "draft"
                if job.persona_type.value.startswith("private_") and has_gaps
                else "completed_with_gaps"
                if has_gaps
                else "compiled"
            )
            manifest.active = not has_gaps
            manifest.confidence["creation_coverage"] = (
                safe_probability(coverage.get("score"), default=0.0) or 0.0
            )
            self.continuum.personas.update_manifest(manifest)
            if self.profile_library is not None:
                # Atomic boundary: the summary is another model call, so a
                # pause requested after compile must stop before it starts.
                self._raise_if_pause_requested(job.id)
                job.progress.update_stage("summary", label="正在生成 Profile Summary", percent=99)
                self._save(job)
                adapter = self.continuum.agent_registry.get_adapter(job.agent_id)
                await self.profile_library.generate_persona_summary(
                    job.persona_id,
                    adapter=adapter,
                    runtime={
                        "runtime_source": job.runtime_source,
                        "agent_id": job.agent_id,
                        "model_id": job.model_id,
                        "reasoning_effort": job.reasoning_effort,
                        "auth_profile_id": job.auth_profile_id,
                    },
                )
            job.status = "completed_with_gaps" if has_gaps else "completed"
            job.current_stage = "completed_with_gaps" if has_gaps else "completed"
            job.error = None
            job.failure_json = None
            job.progress.failure = None
            job.progress.update_stage(
                job.current_stage,
                label="完成（存在资料缺口）" if has_gaps else "Persona 创建完成",
                percent=100,
                completed=job.source_count,
                total=job.research_policy.preferred_source_target,
            )
            self._save(job)
            await self._emit(
                job,
                "persona_compilation_completed",
                persona_id=compiled.persona.id,
                validation=validation.model_dump(mode="json"),
            )
            await self._emit(
                job,
                "persona_creation_completed",
                persona_id=compiled.persona.id,
                status=job.status,
            )
            world_actor_id = job.job_config.get("world_actor_id")
            if world_actor_id:
                await self._emit(
                    job,
                    "world_persona_completed",
                    actor_id=world_actor_id,
                    persona_id=compiled.persona.id,
                )
                await self._emit(
                    job,
                    "world_actor_persona_bound",
                    actor_id=world_actor_id,
                    persona_id=compiled.persona.id,
                )
        except PersonaQualityGateError as exc:
            job = self.get_job(job_id)
            self._mark_failure(job, exc, status="failed_quality_gate", stage="failed_quality_gate")
            await self._emit(job, "persona_quality_gate_failed", error=str(exc))
        except RuntimeBindingError as exc:
            # A runtime disappearing mid-job is resumable, but must never be
            # silently replaced by another Agent/Model.
            job = self.get_job(job_id)
            self._mark_failure(
                job, exc, status="paused_runtime_unavailable", stage="runtime_unavailable"
            )
            await self._emit(job, "persona_creation_runtime_unavailable", error=str(exc))
        except RuntimeUnavailableError as exc:
            job = self.get_job(job_id)
            self._mark_failure(
                job, exc, status="paused_runtime_unavailable", stage="runtime_unavailable"
            )
            await self._emit(job, "persona_creation_runtime_unavailable", error=str(exc))
        except _PauseRequested as exc:
            # Safe pause: the atomic unit completed and was checkpointed, so
            # resume only has to redo unfinished work.  Only here is the
            # control-plane request cleared.
            await self._enter_paused(job_id, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            job = self.get_job(job_id)
            self._mark_failure(job, exc, status="failed", stage="failed")
            await self._emit(job, "persona_creation_failed", error=str(exc))
        finally:
            summary = tracer.finish_task(trace_id)
            if summary is not None:
                with contextlib.suppress(Exception):
                    traced_job = self.get_job(job_id)
                    traced_job.job_config["performance_trace"] = summary
                    self._save(traced_job)
            with contextlib.suppress(Exception):
                await self.close_job_persistence(job_id)
            with contextlib.suppress(Exception):
                current = self.get_job(job_id)
                final_state = self._worker_state_for_status(current.status)
                self._touch_worker(
                    current,
                    final_state,
                    finished=final_state
                    in {WorkerState.FINISHED, WorkerState.FAILED, WorkerState.PAUSED},
                )
            # Terminal for this run: release logical sessions back to the pool.
            with contextlib.suppress(Exception):
                await self.close_job_sessions(job_id)
            with contextlib.suppress(Exception):
                await self.close_research_backends(job_id)
            # The run is over, so drop its in-process pause signal.  A durable
            # pause request is left in place when the job did not reach
            # PAUSED (for example a crash) so a restart still honours it.
            with contextlib.suppress(Exception):
                final = self.get_job(job_id)
                if final.status in self.TERMINAL_STATUSES or final.status == "paused":
                    self.job_control.release(job_id)
            self._tasks.pop(job_id, None)

    async def _research_public(self, job: PersonaCreationJob) -> None:
        await self._set_stage(job, "researching", "researching")
        backend = await self._resolve_research_backend_from_job(job)
        job.job_config["research_backend"] = getattr(backend, "name", "unknown")
        self._save(job)
        plan = await self._research_plan(job)
        life_stage_model = LifeStageModel.from_plan(plan)
        job.life_stages = [stage.model_dump(mode="json") for stage in life_stage_model.life_stages]
        job.job_config["life_stage_model"] = job.life_stages
        job.job_config.setdefault(
            "contradiction_coverage", ContradictionCoverage().model_dump(mode="json")
        )
        await self._emit(
            job,
            "persona_research_plan_created",
            plan=plan,
            life_stages=job.life_stages,
            profile=job.research_policy.profile,
            effective_target=job.research_policy.effective_target(
                ResearchRichnessEstimator.estimate(
                    self.continuum.personas.get_sources(job.persona_id or "")
                )
            ),
        )
        seen_urls = {
            str(source.metadata.get("canonical_url") or source.path)
            for source in self.continuum.personas.get_sources(job.persona_id or "")
        }
        seen_hashes = {
            source.hash for source in self.continuum.personas.get_sources(job.persona_id or "")
        }
        queries = list(plan.get("queries") or [])
        queries.extend(list(plan.get("contradiction_search_queries") or []))
        queries.extend(list(plan.get("negative_evidence_queries") or []))
        if not queries:
            raise PersonaCreationError("research_plan_has_no_queries")
        tracker = self._gain_trackers.setdefault(
            job.id,
            MarginalInformationGainTracker(
                window=job.research_policy.marginal_gain_window,
                threshold=job.research_policy.marginal_gain_stop_threshold,
            ),
        )
        for snapshot in job.information_gain:
            with contextlib.suppress(Exception):
                tracker.snapshots.append(InformationGainSnapshot.model_validate(snapshot))
        query_history = job.query_history
        max_rounds = max(
            1, safe_int(job.research_policy.max_research_rounds, default=12, minimum=1) or 12
        )
        for round_index in range(max_rounds):
            # Atomic boundary: a round's searches are one dispatch group.  A
            # pause requested during round N must not let round N+1 fire a new
            # batch of remote requests.
            self._raise_if_pause_requested(job.id)
            sources_before = list(self.continuum.personas.get_sources(job.persona_id or ""))
            task_before = (
                self.continuum.compilation.get_task(job.compilation_task_id)
                if job.compilation_task_id
                else None
            )
            artifacts_before = list(task_before.artifacts if task_before else [])
            dimension_before = dict(job.dimension_progress)
            life_before = dict(job.life_stage_progress)
            current_coverage = self._coverage(job, life_stage_model)
            gaps = self._gap_analyzer.analyze(
                sources=sources_before,
                required_dimensions=job.research_policy.required_dimensions,
                min_sources_per_dimension=job.research_policy.min_sources_per_dimension,
                dimension_progress={
                    dimension: safe_int(
                        (row or {}).get(
                            "independent_evidence_count",
                            job.dimension_progress.get(dimension, 0),
                        ),
                        default=0,
                        minimum=0,
                    )
                    or 0
                    for dimension, row in (current_coverage.get("dimension_coverage") or {}).items()
                }
                or job.dimension_progress,
                life_stage_model=life_stage_model,
                life_stage_progress=job.life_stage_progress,
                min_evidence_per_life_stage=job.research_policy.min_evidence_per_life_stage,
                category_count=safe_int(
                    current_coverage.get("source_category_count"), default=0, minimum=0
                )
                or 0,
                min_source_categories=job.research_policy.min_source_categories,
                primary_count=safe_int(
                    current_coverage.get("primary_source_count"), default=0, minimum=0
                )
                or 0,
                secondary_count=safe_int(
                    current_coverage.get("secondary_source_count"), default=0, minimum=0
                )
                or 0,
                contradiction_coverage=job.job_config.get("contradiction_coverage"),
                relationship_evidence=self._artifact_signal_count(
                    artifacts_before, ("relationship", "family", "rival", "colleague")
                ),
                behavior_evidence=self._artifact_signal_count(
                    artifacts_before, ("decision", "behavior", "pressure", "failure")
                ),
                expression_evidence=self._artifact_signal_count(
                    artifacts_before, ("expression", "phrase", "voice", "said")
                ),
                decision_evidence=self._artifact_signal_count(
                    artifacts_before, ("decision", "chose", "tradeoff", "outcome")
                ),
            )
            job.research_gaps = [gap.model_dump(mode="json") for gap in gaps]
            if round_index:
                gap_queries = self._gap_analyzer.build_gap_queries(
                    job.display_name, gaps, query_history=query_history, limit=16
                )
                if gap_queries:
                    queries = gap_queries
            if not queries:
                decision = self._stop_gate.evaluate(
                    policy=job.research_policy,
                    coverage=current_coverage,
                    marginal_gain=tracker,
                    source_count=job.source_count,
                    round_index=round_index,
                    high_priority_gaps=gaps,
                    subject_richness=ResearchRichnessEstimator.estimate(sources_before),
                    source_space_exhausted=True,
                )
                job.research_stop_reason = decision.stop_reason or "source_space_exhausted"
                job.coverage = current_coverage
                self._record_research_checkpoint(
                    job,
                    round_index,
                    InformationGainSnapshot(round_index=round_index),
                    gaps,
                    decision,
                )
                break
            await self._set_stage(job, "researching", "researching")
            added_this_round = 0
            added_this_round = 0
            # ---------------- Stage 1: bounded-parallel searches --------------
            # Every planned query is searched exactly once (the serial plan's
            # should_repeat filter is applied identically); concurrency is
            # capped by max_search_concurrency.  Failures degrade a single
            # query instead of aborting the round.
            planned_queries: list[str] = []
            attempt_updates: list[tuple[str, dict[str, Any]]] = []
            for raw_query in list(queries):
                query_text = str(raw_query).strip()
                if not query_text:
                    continue
                history_entry = query_history.setdefault(
                    query_text,
                    {
                        "attempts": 0,
                        "result_count": 0,
                        "duplicate_count": 0,
                        "information_gain": 0.0,
                    },
                )
                if not self._gap_analyzer.should_repeat(query_text, query_history):
                    continue
                planned_queries.append(query_text)
                attempt_updates.append((query_text, history_entry))
            for _q, entry in attempt_updates:
                entry["attempts"] = (safe_int(entry.get("attempts"), default=0, minimum=0) or 0) + 1

            round_no = round_index + 1

            async def _search_one(
                q: str, _round_no: int = round_no
            ) -> tuple[str, list[dict[str, Any]], str | None]:
                await self._emit(job, "persona_search_started", query=q, round=_round_no)
                search_started = time.perf_counter()
                default_tracer().count(f"job:{job.id}", "search_query_count", 1)
                async with self._research_search_slot():
                    try:
                        found = await backend.search(q, limit=10)
                    except Exception as exc:
                        return q, [], f"{type(exc).__name__}: {exc}"[:300]
                    finally:
                        default_tracer().observe_value(
                            f"job:{job.id}",
                            "search_latency_ms",
                            (time.perf_counter() - search_started) * 1000.0,
                        )
                return q, found, None

            search_results_by_query: dict[str, list[dict[str, Any]]] = {}
            search_errors: dict[str, str] = {}
            if planned_queries:
                # Atomic boundary before dispatching this search group.  A
                # group that already started completes; the next one never
                # starts once a pause is pending.
                self._raise_if_pause_requested(job.id)
                batch_search = getattr(backend, "batch_search", None)
                if callable(batch_search):
                    batch_started = time.perf_counter()
                    for query in planned_queries:
                        await self._emit(job, "persona_search_started", query=query, round=round_no)
                        default_tracer().count(f"job:{job.id}", "search_query_count", 1)
                    try:
                        async with self._research_search_slot():
                            batch_results = await batch_search(planned_queries, limit=10)
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"[:300]
                        search_errors.update({query: error for query in planned_queries})
                        batch_results = {}
                    elapsed = (time.perf_counter() - batch_started) * 1000.0
                    default_tracer().observe_value(
                        f"job:{job.id}", "search_batch_latency_ms", elapsed
                    )
                    for query in planned_queries:
                        search_results_by_query[query] = [
                            dict(item)
                            for item in batch_results.get(query, []) or []
                            if isinstance(item, dict)
                        ]
                else:
                    gathered = await asyncio.gather(
                        *(_search_one(q) for q in planned_queries), return_exceptions=False
                    )
                    for returned_q, found, err in gathered:
                        search_results_by_query[returned_q] = found
                        if err:
                            search_errors[returned_q] = err
            for q in planned_queries:
                results_for_q = search_results_by_query.get(q) or []
                history = query_history[q]
                history["result_count"] = (
                    safe_int(history.get("result_count"), default=0, minimum=0) or 0
                ) + len(results_for_q)
                if any(
                    token in q.casefold()
                    for token in (
                        "contradict",
                        "controvers",
                        "criticism",
                        "failure",
                        "opposing",
                        "争议",
                        "失败",
                        "批评",
                        "变化",
                    )
                ):
                    contradiction = ContradictionCoverage.model_validate(
                        job.job_config.get("contradiction_coverage") or {}
                    )
                    contradiction.search_executed = True
                    contradiction.queries_executed = sorted(
                        set([*contradiction.queries_executed, q])
                    )
                    job.job_config["contradiction_coverage"] = contradiction.model_dump(mode="json")

            # ------------- Stage 2: prefetch page contents concurrently -------
            # Warm the shared ResearchSourceCache for every URL lacking inline
            # content.  Bounded, best-effort, order-independent; deterministic
            # behaviour comes from Stage 3 which reads back in canonical order.
            async def _warm(url_value: str) -> None:
                fetch_started = time.perf_counter()
                default_tracer().count(f"job:{job.id}", "fetch_url_count", 1)
                try:
                    async with self._research_fetch_slot():
                        await backend.fetch(url_value)
                except Exception:
                    # Same tolerance as the historical inline fetch path.
                    return
                finally:
                    default_tracer().observe_value(
                        f"job:{job.id}",
                        "fetch_latency_ms",
                        (time.perf_counter() - fetch_started) * 1000.0,
                    )

            warm_urls: list[str] = []
            warm_seen: set[str] = set()
            for q in planned_queries:
                for result in search_results_by_query.get(q) or []:
                    source = await self._normalise_research_result(job, result)
                    if source is None:
                        continue
                    u = str(source.get("canonical_url") or "")
                    if not u or u in seen_urls or u in warm_seen:
                        continue
                    if str(source.get("content") or "").strip():
                        continue
                    warm_seen.add(u)
                    warm_urls.append(u)
            warmed_payloads: dict[str, dict[str, Any] | str] = {}
            if warm_urls:
                # Atomic boundary before dispatching this fetch group.
                self._raise_if_pause_requested(job.id)
                batch_fetch = getattr(backend, "batch_fetch", None)
                if callable(batch_fetch):
                    fetch_started = time.perf_counter()
                    default_tracer().count(f"job:{job.id}", "fetch_url_count", len(warm_urls))
                    try:
                        async with self._research_fetch_slot():
                            warmed_payloads = await batch_fetch(warm_urls)
                    except Exception:
                        warmed_payloads = {}
                    default_tracer().observe_value(
                        f"job:{job.id}",
                        "fetch_batch_latency_ms",
                        (time.perf_counter() - fetch_started) * 1000.0,
                    )
                else:
                    await asyncio.gather(*(_warm(u) for u in warm_urls))

            # --------- Stage 3: deterministic ingestion (serial semantics) ----
            # Query order / result rank / dedupe state evolve exactly like the
            # old inline loop; only IO overlapped above and DB writes are
            # batched here.
            pending_entries: list[dict[str, Any]] = []
            found_events: list[dict[str, Any]] = []
            ingestion_stopped = False

            def _build_entry(
                source: dict[str, Any], u: str, q: str, _round_no: int = round_no
            ) -> dict[str, Any]:
                return {
                    "title": str(source.get("title") or u or job.display_name),
                    "source_type": str(source.get("source_type") or "web"),
                    "canonical_url": u or None,
                    "publisher": str(source.get("publisher") or "Unknown publisher"),
                    "author": str(source.get("author") or "Unknown author"),
                    "published_at": source.get("published_at"),
                    "accessed_at": datetime.now(UTC).isoformat(),
                    "content": str(source.get("content") or "").strip(),
                    # PersonaService computes the canonical content hash; a
                    # broker supplied hash stays provenance-only.
                    "hash": "",
                    "metadata": {
                        "provenance": "web_research",
                        "category": source.get("category") or source.get("source_type") or "web",
                        "research_round": _round_no,
                        "query": q,
                        "canonical_url": u,
                        "upstream_hash": source.get("hash") or None,
                        "event_time": source.get("event_time") or source.get("occurred_at"),
                        "origin_identifier": source.get("origin_identifier")
                        or source.get("original_url"),
                        "citation": source.get("citation") or source.get("source_identity"),
                        "publisher": source.get("publisher"),
                        "author": source.get("author"),
                        "is_primary": bool(source.get("is_primary") or source.get("first_person")),
                    },
                }

            for q in planned_queries:
                if job.source_count >= job.research_policy.effective_hard_budget:
                    break
                for result in search_results_by_query.get(q) or []:
                    source = await self._normalise_research_result(job, result)
                    if source is None:
                        continue
                    url = str(source.get("canonical_url") or "")
                    # Public research provenance requires a stable web identity;
                    # snippets without a canonical URL never become Evidence.
                    if not url:
                        continue
                    content_hash = hashlib.sha256(
                        str(source.get("content") or "").encode()
                    ).hexdigest()
                    if url in seen_urls or content_hash in seen_hashes:
                        continue
                    if not source.get("content") and url:
                        fetched = warmed_payloads.get(url)
                        if fetched is None:
                            fetch_started = time.perf_counter()
                            default_tracer().count(f"job:{job.id}", "fetch_url_count", 1)
                            async with self._research_fetch_slot():
                                fetched = await backend.fetch(url)
                            default_tracer().observe_value(
                                f"job:{job.id}",
                                "fetch_latency_ms",
                                (time.perf_counter() - fetch_started) * 1000.0,
                            )
                        source = self._merge_fetched_source(source, fetched)
                    content = str(source.get("content") or "").strip()
                    if not content:
                        continue
                    if any(
                        token in (str(source.get("title") or "") + " " + content).casefold()
                        for token in (
                            "controvers",
                            "critic",
                            "failure",
                            "dispute",
                            "争议",
                            "批评",
                            "失败",
                        )
                    ):
                        contradiction = ContradictionCoverage.model_validate(
                            job.job_config.get("contradiction_coverage") or {}
                        )
                        contradiction.negative_evidence_found = True
                        job.job_config["contradiction_coverage"] = contradiction.model_dump(
                            mode="json"
                        )
                    content_hash = hashlib.sha256(content.encode()).hexdigest()
                    if content_hash in seen_hashes:
                        history = query_history[q]
                        history["duplicate_count"] = (
                            safe_int(history.get("duplicate_count"), default=0, minimum=0) or 0
                        ) + 1
                        continue
                    await self._emit(
                        job,
                        "persona_source_found",
                        title=source.get("title") or url,
                        canonical_url=url,
                    )
                    pending_entries.append(_build_entry(source, url, q))
                    found_events.append({"title": source.get("title") or url, "url": url})
                    # Budget accounting counts accepted candidates immediately;
                    # a rare concurrent duplicate simply overcounts until the
                    # next coverage refresh reconciles it.
                    provisional_count = job.source_count + len(pending_entries)
                    if provisional_count >= job.research_policy.effective_hard_budget:
                        ingestion_stopped = True
                        break
                if ingestion_stopped:
                    break

            inserted_errors: list[str] = []
            evidence_rows: list[Any] = []
            if pending_entries:
                try:
                    evidence_rows = self.continuum.personas.add_source_texts(
                        job.persona_id or "", entries=pending_entries
                    )
                except Exception as exc:
                    inserted_errors.append(str(exc)[:300])
                    evidence_rows = []
                accepted_iter = iter(evidence_rows)
                for _entry_info, entry in zip(found_events, pending_entries, strict=False):
                    row = next(accepted_iter, None)
                    if row is None:
                        history = query_history[entry["metadata"]["query"]]
                        history["duplicate_count"] = (
                            safe_int(history.get("duplicate_count"), default=0, minimum=0) or 0
                        ) + 1
                        continue
                    url = str(entry["metadata"]["canonical_url"] or "")
                    job.source_ids.append(row.id)
                    job.source_ids = sorted(set(job.source_ids))
                    job.source_count = len(job.source_ids)
                    seen_urls.add(url)
                    seen_hashes.add(row.hash)
                    added_this_round += 1
                    await self._emit(
                        job,
                        "persona_source_ingested",
                        source_id=row.id,
                        source_count=job.source_count,
                        raw_source_count=job.source_count,
                    )
                self._save(job)
            job.query_history = query_history
            if (
                added_this_round
                and self.material_intelligence is not None
                and bool(getattr(self.continuum.config, "persona_evidence_intelligence", True))
            ):
                new_source_ids = [str(row.id) for row in evidence_rows]
                with default_tracer().span(f"job:{job.id}", "evidence_intelligence"):
                    intelligence_job = await self._analyze_private_materials(
                        job,
                        new_source_ids,
                        incremental=True,
                        agent_phases=("classify",),
                        shared_factual_cache=True,
                    )
                coverage_snapshot = dict(intelligence_job.coverage or {})
                dimension_coverage = dict(coverage_snapshot.get("dimension_coverage") or {})
                for dimension in REQUIRED_DIMENSIONS:
                    job.dimension_progress[dimension] = (
                        safe_int(dimension_coverage.get(dimension), default=0, minimum=0) or 0
                    )
                job.job_config["evidence_intelligence"] = {
                    "job_id": intelligence_job.id,
                    "status": intelligence_job.status.value,
                    "incremental": True,
                    "source_ids": new_source_ids,
                    "coverage": coverage_snapshot,
                }
                self._save(job)
            elif added_this_round:
                # Legacy benchmark path: preserve the former raw-evidence ×
                # dimension topology so before/after measurements remain real.
                await self._extract_dimensions(job, incremental=True)
            sources_after = list(self.continuum.personas.get_sources(job.persona_id or ""))
            self._update_life_stage_progress(job, life_stage_model, sources_after)
            task_after = (
                self.continuum.compilation.get_task(job.compilation_task_id)
                if job.compilation_task_id
                else None
            )
            artifacts_after = list(task_after.artifacts if task_after else [])
            gain = tracker.calculate(
                round_index=round_index + 1,
                sources=sources_after,
                previous_sources=sources_before,
                artifacts=artifacts_after,
                previous_artifacts=artifacts_before,
                dimension_progress=job.dimension_progress,
                previous_dimension_progress=dimension_before,
                life_stage_progress=job.life_stage_progress,
                previous_life_stage_progress=life_before,
            )
            job.information_gain = [
                item.model_dump(mode="json") for item in tracker.snapshots[-50:]
            ]
            for history_data in query_history.values():
                history_data["information_gain"] = gain.marginal_gain_score
                duplicate_count = (
                    safe_int(history_data.get("duplicate_count"), default=0, minimum=0) or 0
                )
                result_count = safe_int(history_data.get("result_count"), default=0, minimum=0) or 0
                history_data["duplicate_rate"] = (
                    safe_probability(duplicate_count / max(1, result_count), default=0.0) or 0.0
                )
            coverage = self._coverage(job, life_stage_model)
            for dimension, row in (coverage.get("dimension_coverage") or {}).items():
                job.dimension_progress[dimension] = (
                    safe_int(
                        (row or {}).get(
                            "independent_evidence_count", job.dimension_progress.get(dimension, 0)
                        ),
                        default=0,
                        minimum=0,
                    )
                    or 0
                )
            # The stop gate must evaluate the evidence produced in this round.
            # Reusing the pre-search gap list can keep already-filled gaps alive
            # and incorrectly drive a rich subject all the way to the hard cap.
            gaps = self._gap_analyzer.analyze(
                sources=sources_after,
                required_dimensions=job.research_policy.required_dimensions,
                min_sources_per_dimension=job.research_policy.min_sources_per_dimension,
                dimension_progress=job.dimension_progress,
                life_stage_model=life_stage_model,
                life_stage_progress=job.life_stage_progress,
                min_evidence_per_life_stage=job.research_policy.min_evidence_per_life_stage,
                category_count=safe_int(
                    coverage.get("source_category_count", 0), default=0, minimum=0
                ),
                min_source_categories=job.research_policy.min_source_categories,
                primary_count=safe_int(
                    coverage.get("primary_source_count", 0), default=0, minimum=0
                ),
                secondary_count=safe_int(
                    coverage.get("secondary_source_count", 0), default=0, minimum=0
                ),
                contradiction_coverage=job.job_config.get("contradiction_coverage"),
                relationship_evidence=self._artifact_signal_count(
                    artifacts_after, ("relationship", "family", "rival", "colleague")
                ),
                behavior_evidence=self._artifact_signal_count(
                    artifacts_after, ("decision", "behavior", "pressure", "failure")
                ),
                expression_evidence=self._artifact_signal_count(
                    artifacts_after, ("expression", "phrase", "voice", "said")
                ),
                decision_evidence=self._artifact_signal_count(
                    artifacts_after, ("decision", "chose", "tradeoff", "outcome")
                ),
            )
            job.research_gaps = [gap.model_dump(mode="json") for gap in gaps]
            job.coverage = coverage
            await self._emit(
                job,
                "persona_coverage_updated",
                coverage=coverage,
                information_gain=gain.model_dump(mode="json"),
                research_gaps=job.research_gaps,
                stop_reason=job.research_stop_reason,
            )
            decision = self._stop_gate.evaluate(
                policy=job.research_policy,
                coverage=coverage,
                marginal_gain=tracker,
                source_count=job.source_count,
                round_index=round_index + 1,
                high_priority_gaps=gaps,
                subject_richness=ResearchRichnessEstimator.estimate(sources_after),
                source_space_exhausted=added_this_round == 0,
            )
            self._record_research_checkpoint(job, round_index + 1, gain, gaps, decision)
            if not decision.continue_research:
                job.research_stop_reason = decision.stop_reason
                break
            if not added_this_round and decision.continue_research:
                job.research_stop_reason = "insufficient_public_record"
                break
            # Keep the initial plan only until the first gap audit.  Subsequent
            # rounds are driven by missing dimensions/life stages/contradictions.
            queries = []
            self._save(job)
        job.coverage = self._coverage(job, life_stage_model)
        if (
            not job.research_stop_reason
            and job.source_count >= job.research_policy.effective_hard_budget
        ):
            job.research_stop_reason = "hard_budget_exhausted"
        if not job.research_stop_reason:
            job.research_stop_reason = "insufficient_public_record"
        default_tracer().observe(f"job:{job.id}", "unique_source_count", job.source_count)
        default_tracer().record(
            f"job:{job.id}",
            research_stop_reason=job.research_stop_reason,
            adaptive_persona_scheduler=self._persona_research_controller.snapshot(),
        )
        self._save(job)

    RESEARCH_PLAN_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of primary search queries for researching this persona",
            },
            "life_stages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "significance": {"type": "string"},
                        "required_evidence": {"type": "integer"},
                    },
                    "required": ["id", "title"],
                },
            },
            "contradiction_search_queries": {
                "type": "array",
                "items": {"type": "string"},
            },
            "negative_evidence_queries": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["queries"],
    }

    @classmethod
    def _normalize_research_plan(cls, raw: Any) -> dict[str, Any]:
        if isinstance(raw, list):
            queries = [
                str(item.get("query") if isinstance(item, dict) else item).strip()
                for item in raw
                if item
            ]
            return {
                "queries": [q for q in queries if q],
                "life_stages": [],
                "contradiction_search_queries": [],
                "negative_evidence_queries": [],
            }
        if not isinstance(raw, dict):
            raise PersonaCreationError("research_plan_invalid")
        plan = dict(raw)
        if "plan" in plan and isinstance(plan["plan"], dict):
            nested = plan["plan"]
            for k, v in nested.items():
                plan.setdefault(k, v)
        raw_queries = (
            plan.get("queries")
            or plan.get("search_queries")
            or plan.get("research_queries")
            or plan.get("search_plan")
            or plan.get("query_list")
        )
        if not raw_queries and isinstance(plan.get("life_stages"), list):
            raw_queries = [
                q
                for stage in plan["life_stages"]
                if isinstance(stage, dict)
                for q in (stage.get("queries") or stage.get("search_queries") or [])
            ]
        if isinstance(raw_queries, str):
            raw_queries = [raw_queries]
        elif not isinstance(raw_queries, list):
            raw_queries = []
        normalized_queries: list[str] = []
        for item in raw_queries:
            if isinstance(item, str) and item.strip():
                normalized_queries.append(item.strip())
            elif isinstance(item, dict):
                q = str(
                    item.get("query") or item.get("text") or item.get("search_query") or ""
                ).strip()
                if q:
                    normalized_queries.append(q)
        if not normalized_queries:
            raise PersonaCreationError("research_plan_invalid")
        plan["queries"] = normalized_queries
        plan.setdefault("life_stages", [])
        plan.setdefault("contradiction_search_queries", [])
        plan.setdefault("negative_evidence_queries", [])
        for key in ("contradiction_search_queries", "negative_evidence_queries"):
            items = plan.get(key)
            if isinstance(items, list):
                plan[key] = [
                    str(it.get("query") if isinstance(it, dict) else it).strip()
                    for it in items
                    if it
                ]
            else:
                plan[key] = []
        return plan

    async def _research_plan(self, job: PersonaCreationJob) -> dict[str, Any]:
        prompt = {
            "display_name": job.display_name,
            "persona_type": job.persona_type.value,
            "enrichment_scope": job.job_config.get("enrichment_scope", "full_refresh"),
            "policy": job.research_policy.model_dump(mode="json"),
            "required_dimensions": REQUIRED_DIMENSIONS,
            "instructions": [
                "跨越人物生命和事业阶段生成研究查询",
                "包含第一人称、官方、传记、长期报道、批评、失败、争议和观点变化",
                "不要把转载同源新闻作为独立来源",
                "动态识别人物的重要 life_stages；每个阶段返回 "
                "id、title、start、end、significance、required_evidence",
                "以事件发生时间映射 life stage，不要用来源发表年份代替事件时间",
                "主动规划 contradiction_search_queries 和 negative_evidence_queries",
            ],
        }
        request = json.dumps(prompt, ensure_ascii=False)
        try:
            result = await self._agent_json(
                job,
                user_message=request,
                system_prompt=PUBLIC_RESEARCH_SYSTEM_PROMPT,
                phase="public_research",
                schema=self.RESEARCH_PLAN_SCHEMA,
            )
            return self._normalize_research_plan(result)
        except Exception as exc:
            repair_message = (
                f"研究计划格式不符合要求：{exc}。请返回 JSON：\n"
                '{"queries": ["..."], "life_stages": [...], '
                '"contradiction_search_queries": ["..."], "negative_evidence_queries": ["..."]}\n'
                + request
            )
            result = await self._agent_json(
                job,
                user_message=repair_message,
                system_prompt=PUBLIC_RESEARCH_SYSTEM_PROMPT,
                phase="public_research",
                schema=self.RESEARCH_PLAN_SCHEMA,
            )
            return self._normalize_research_plan(result)

    def _update_life_stage_progress(
        self, job: PersonaCreationJob, life_stage_model: LifeStageModel, sources: list[Any]
    ) -> None:
        if not life_stage_model.life_stages:
            job.life_stage_progress = {}
            return
        clusters = self._source_analyzer.cluster_sources(sources)
        source_to_cluster = {
            source_id: cluster.cluster_id
            for cluster in clusters
            for source_id in cluster.member_source_ids
        }
        progress: dict[str, set[str]] = {stage.id: set() for stage in life_stage_model.life_stages}
        for source in sources:
            for stage_id in life_stage_model.map_source(source):
                source_id = str(
                    source.get("id") if isinstance(source, dict) else getattr(source, "id", "")
                )
                cluster_id = source_to_cluster.get(source_id)
                progress.setdefault(stage_id, set()).add(cluster_id or source_id)
        job.life_stage_progress = {stage_id: len(values) for stage_id, values in progress.items()}

    def _artifact_signal_count(
        self, artifacts: list[dict[str, Any]], terms: tuple[str, ...]
    ) -> int:
        count = 0
        for artifact in artifacts:
            for claim in artifact.get("claims") or []:
                content = str(claim.get("content") if isinstance(claim, dict) else claim).casefold()
                if any(term in content for term in terms):
                    count += 1
        return count

    def _record_research_checkpoint(
        self,
        job: PersonaCreationJob,
        round_index: int,
        gain: InformationGainSnapshot,
        gaps: list[ResearchGap],
        decision: Any,
    ) -> None:
        coverage = dict(job.coverage)
        checkpoint = ResearchCheckpoint(
            id=new_id("pcchk"),
            job_id=job.id,
            round_index=round_index,
            raw_source_count=safe_int(
                coverage.get("raw_source_count", job.source_count), default=0, minimum=0
            )
            or 0,
            independent_source_count=safe_int(
                coverage.get("independent_sources", coverage.get("unique_sources", 0)),
                default=0,
                minimum=0,
            )
            or 0,
            quality_source_count=safe_int(
                coverage.get("quality_source_count", 0), default=0, minimum=0
            )
            or 0,
            dimension_coverage=dict(coverage.get("dimension_coverage") or {}),
            life_stage_coverage=dict(coverage.get("life_stages") or {}),
            primary_secondary_balance={
                "primary": coverage.get("primary_source_count", 0),
                "secondary": coverage.get("secondary_source_count", 0),
                "balanced": coverage.get("primary_secondary_balanced", False),
            },
            contradiction_coverage=dict(coverage.get("contradiction_coverage") or {}),
            information_gain=gain.model_dump(mode="json"),
            research_gaps=[gap.model_dump(mode="json") for gap in gaps],
            continue_reason=(decision.details or {}).get("continue_reason")
            if decision.continue_research
            else None,
            stop_reason=decision.stop_reason,
            created_at=datetime.now(UTC).isoformat(),
        )
        job.research_checkpoints.append(checkpoint.model_dump(mode="json"))
        job.research_checkpoints = job.research_checkpoints[-50:]
        if job.persona_id:
            with contextlib.suppress(Exception):
                checkpoint_id = checkpoint.id or new_id("pcchk")
                self.continuum.database.conn.execute(
                    """
                    INSERT OR REPLACE INTO persona_research_checkpoints (
                      id, job_id, round_index, raw_source_count,
                      independent_source_count, quality_source_count,
                      coverage_json, information_gain_json, gaps_json,
                      stop_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        job.id,
                        checkpoint.round_index,
                        checkpoint.raw_source_count,
                        checkpoint.independent_source_count,
                        checkpoint.quality_source_count,
                        dumps(coverage),
                        dumps(checkpoint.information_gain),
                        dumps(checkpoint.research_gaps),
                        checkpoint.stop_reason,
                        checkpoint.created_at,
                    ),
                )
                self.continuum.database.conn.commit()
        self._save(job)

    async def _extract_dimensions(
        self,
        job: PersonaCreationJob,
        *,
        incremental: bool = False,
        final_audit: bool = False,
    ) -> None:
        """Extract the eight required dimensions.

        Three execution modes share one code path:

        - ``final_audit`` (default path before compile): a FULL pass over the
          complete evidence set on fresh audit threads, guaranteeing nothing
          was missed by incremental rounds.
        - ``incremental`` (called per research round): analyses only evidence
          whose source ids were not processed for that dimension yet, plus a
          bounded set of possibly-conflicting related old units.  Processed
          source ids are tracked per dimension in ``job_config``.
        - plain/full: whole-evidence extraction (private/first runs).

        Dimensions run under a bounded semaphore.  Each dimension uses its own
        logical Agent session (own thread), so concurrency never shares
        conversational state; a single-dimension failure is recorded and does
        not destroy already-extracted dimensions.
        """

        stage_label = "final_full_audit" if final_audit else "extracting"
        await self._set_stage(job, "extracting", stage_label)
        sources = list(self.continuum.personas.get_sources(job.persona_id or ""))
        if not sources:
            return
        persona_id = job.persona_id
        if not persona_id:
            raise PersonaCreationError("persona_id_required")
        context_manager = getattr(
            self.runtime_executor, "context_budget_manager", AgentContextBudgetManager()
        )
        source_by_id = {source.id: source for source in sources}
        all_source_ids = sorted(source_by_id)
        incremental_enabled = bool(
            incremental
            and not final_audit
            and getattr(self.continuum.config, "persona_incremental_extraction", True)
        )
        concurrency_limit = max(
            1,
            safe_int(
                getattr(self.continuum.config, "persona_dimension_concurrency", 3),
                default=3,
                minimum=1,
            )
            or 3,
        )
        semaphore = asyncio.Semaphore(concurrency_limit)

        def processed_map() -> dict[str, list[str]]:
            raw = job.job_config.get("dimension_processed_sources")
            return {str(k): [str(v) for v in (val or [])] for k, val in (raw or {}).items()}

        processed = processed_map()

        def plan_dimension(dimension: str) -> tuple[list[dict[str, Any]], list[str], str]:
            """Return (evidence_items, delta_source_ids, mode)."""

            retrieved: list[dict[str, Any]] = []
            if self.material_intelligence is not None:
                retrieval_limit = (
                    safe_int(
                        getattr(self.continuum.config, "persona_dimension_retrieval_items", 48),
                        default=48,
                        minimum=8,
                    )
                    or 48
                )
                retrieved = self.material_intelligence.get_index(persona_id).retrieve(
                    dimension, top_k=retrieval_limit, diversity=True
                )
            units: list[dict[str, Any]] = []
            seen_unit_ids: set[str] = set()
            for item in retrieved:
                unit_id = str(item.get("id") or "")
                if unit_id and unit_id in seen_unit_ids:
                    continue
                seen_unit_ids.add(unit_id)
                ids = [str(value) for value in item.get("source_ids", [])]
                units.append(
                    {
                        "evidence_id": unit_id,
                        "source_ids": [value for value in ids if value in source_by_id],
                        "evidence_type": item.get("kind"),
                        "verbatim_samples": list(item.get("verbatim_samples") or []),
                        "intelligence": item.get("intelligence") or {},
                        "content": str(item.get("text") or ""),
                    }
                )
            if not units:
                # Legacy personas may predate the index.  Use every source with
                # a budget-fitting batch rather than a last-N slice.
                units = [
                    {
                        "evidence_id": source.id,
                        "source_ids": [source.id],
                        "evidence_type": "source",
                        "verbatim_samples": [],
                        "title": source.title,
                        "provenance": source.metadata,
                        "content": source.content,
                    }
                    for source in sources
                ]
            done_ids = set(processed.get(dimension) or [])
            covered_new = [
                unit
                for unit in units
                if any(source_id not in done_ids for source_id in unit["source_ids"])
            ]
            if incremental_enabled and done_ids:
                delta_ids = sorted(
                    {
                        source_id
                        for unit in covered_new
                        for source_id in unit["source_ids"]
                        if source_id not in done_ids
                    }
                )
                if not delta_ids:
                    return [], [], "skipped_no_delta"
                if not covered_new:
                    return [], delta_ids, "skipped_no_delta"
                old_units = [unit for unit in units if unit not in covered_new]
                related_old = self._related_context_units(covered_new, old_units, limit=4)
                items = []
                for unit in covered_new:
                    entry = dict(unit)
                    entry["processing_mode"] = "delta"
                    items.append(entry)
                for unit in related_old:
                    entry = dict(unit)
                    entry["processing_mode"] = "related_context"
                    items.append(entry)
                return items, delta_ids, "incremental"
            return list(units), list(all_source_ids), "full"

        # A resume/retry keeps every dimension whose persisted artifact still
        # covers all currently-known evidence; only the missing or stale ones
        # go back to the model.  The final audit pass always re-judges.
        reused_dimensions: dict[str, dict[str, Any]] = {}
        if not final_audit:
            reused_dimensions = self._partial_reusable_dimension_artifacts(job)
        for dimension in REQUIRED_DIMENSIONS:
            artifact = reused_dimensions.get(dimension)
            if artifact is None:
                continue
            await self._persist_dimension_result(
                job, dimension, dict(artifact), mode="reused", final_audit=False
            )
        if reused_dimensions:
            await self._emit_extraction_progress(job, reused=len(reused_dimensions))
            if len(reused_dimensions) >= len(REQUIRED_DIMENSIONS):
                await self._emit(
                    job,
                    "persona_dimension_extraction_resumed",
                    dimensions=len(reused_dimensions),
                    provenance_restored=len(reused_dimensions),
                )

        async def run_dimension(dimension: str) -> dict[str, Any] | None:
            async with semaphore:
                participant = f"{dimension}:audit" if final_audit else str(dimension)
                try:
                    self._raise_if_pause_requested(job)
                    items, delta_ids, mode = plan_dimension(dimension)
                    if mode == "skipped_no_delta":
                        return None
                    await self._emit(
                        job,
                        "persona_dimension_started",
                        dimension=dimension,
                        mode=mode,
                        evidence_units=len(items),
                        final_audit=final_audit,
                    )
                    if not final_audit:
                        await self._emit_extraction_progress(job, current_dimension=dimension)
                    from persona_continuum.performance.tracing import default_tracer

                    with default_tracer().span(f"job:{job.id}", f"dimension_{dimension}"):
                        validated = await self._run_dimension_extraction(
                            job,
                            dimension=dimension,
                            evidence_items=items,
                            participant_id=participant,
                            mode=mode,
                            source_by_id=source_by_id,
                            context_manager=context_manager,
                            phase="dimension_extraction",
                        )
                    # Provenance bookkeeping stays at source granularity so a
                    # later round can compute an exact unprocessed delta.
                    current_done = set(processed.get(dimension) or [])
                    if final_audit or mode == "full":
                        current_done = set(all_source_ids)
                    else:
                        current_done.update(delta_ids)
                    processed[dimension] = sorted(current_done)
                    # A completed dimension is durable immediately: validated,
                    # compiled, persisted, and announced before the gather
                    # continues, so a later failure never re-pays for it.
                    await self._persist_dimension_result(
                        job,
                        dimension,
                        validated.model_dump(mode="json"),
                        mode=mode,
                        final_audit=final_audit,
                    )
                    return {
                        "dimension": dimension,
                        "artifact": validated,
                        "mode": mode,
                        "delta_ids": delta_ids,
                        "mode_participant": participant,
                    }
                except Exception as exc:
                    # Single-dimension failure must not destroy task state;
                    # other dimensions continue and the next round retries.
                    errors = job.job_config.setdefault("dimension_errors", {})
                    errors[dimension] = str(exc)[:500]
                    self._save(job)
                    await self._emit(
                        job,
                        "persona_dimension_failed",
                        dimension=dimension,
                        error=str(exc)[:400],
                        final_audit=final_audit,
                    )
                    return None
                finally:
                    await self._drop_job_session(job.id, participant)

        tasks = [asyncio.create_task(run_dimension(d)) for d in REQUIRED_DIMENSIONS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in results:
            if isinstance(outcome, _PauseRequested):
                # Every worker stops at its own boundary first; raise only
                # after all siblings finished their current atomic unit.
                raise outcome
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
        job.job_config["dimension_processed_sources"] = {dim: ids for dim, ids in processed.items()}
        self._save(job)
        by_dim = {
            payload["dimension"]: payload
            for payload in results
            if isinstance(payload, dict)
        }
        succeeded = set(by_dim) | set(reused_dimensions)
        new_failures = job.job_config.get("dimension_errors") or {}
        if final_audit:
            # The audit pass is quality-critical: it may only pass forward when
            # every dimension either succeeded now or already has coverage.
            still_failing = {dim: err for dim, err in new_failures.items()}
            incomplete = [
                dim
                for dim in REQUIRED_DIMENSIONS
                if dim not in by_dim
                and safe_int(job.dimension_progress.get(dim, 0), default=0, minimum=0) == 0
            ]
            if len(incomplete) >= len(REQUIRED_DIMENSIONS):
                first_error = next(iter(still_failing.values()), "dimension_extraction_failed")
                raise PersonaCreationError(first_error)
        elif not succeeded and all(
            safe_int(job.dimension_progress.get(dim, 0), default=0, minimum=0) == 0
            for dim in REQUIRED_DIMENSIONS
        ):
            first_error = next(iter(new_failures.values()), "dimension_extraction_failed")
            raise PersonaCreationError(first_error)
        # Failures recorded this pass that later succeeded are cleared.
        if succeeded:
            cleared = {dim: err for dim, err in new_failures.items() if dim in succeeded}
            remaining = {dim: err for dim, err in new_failures.items() if dim not in succeeded}
            job.job_config["dimension_errors"] = remaining
            if cleared:
                self._save(job)
        if not final_audit:
            await self._emit_extraction_progress(job)

    async def _persist_dimension_result(
        self,
        job: PersonaCreationJob,
        dimension: str,
        artifact_payload: dict[str, Any],
        *,
        mode: str,
        final_audit: bool,
    ) -> None:
        """Make one dimension's ResearchArtifact durably reusable right now.

        Validates provenance boundaries, submits the canonical payload to the
        compilation task, restores progress/processed bookkeeping, records the
        execution provenance, and emits the completion event.  Submission is
        hash-deduplicated, so re-persisting a reused artifact is a no-op.
        """
        compilation_service = self.continuum.compilation
        if job.persona_type == PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON:
            # The provenance boundary is deterministic even across retries:
            # a reused fictional artifact can never carry historical_* types.
            # It is normalised into the *fictional* family rather than
            # counterfactual_simulated -- canon is author-defined fact about the
            # character, not a simulated divergence, and only the fictional
            # family is visible to a fictional persona at turn time.
            normalise_fictional_provenance(artifact_payload)
        if hasattr(compilation_service, "_canonical_artifact_sha256"):
            artifact_payload["artifact_hash"] = compilation_service._canonical_artifact_sha256(
                artifact_payload
            )
        compilation_service.submit_research_artifact(
            job.compilation_task_id or "", artifact_payload
        )
        source_count = len(set(artifact_payload.get("source_ids") or []))
        job.dimension_progress[dimension] = max(
            safe_int(job.dimension_progress.get(dimension, 0), default=0, minimum=0) or 0,
            source_count,
        )
        processed = job.job_config.setdefault("dimension_processed_sources", {})
        current_done = {str(value) for value in (processed.get(dimension) or [])}
        processed[dimension] = sorted(current_done | set(artifact_payload.get("source_ids") or []))
        if not final_audit:
            self._record_dimension_provenance(job, dimension)
        self._save(job)
        if not final_audit:
            await self._emit(
                job,
                "persona_dimension_completed",
                dimension=dimension,
                source_count=source_count,
                artifact_id=str(artifact_payload.get("artifact_id") or ""),
                mode=mode,
                reused=mode == "reused",
            )

    def _partial_reusable_dimension_artifacts(
        self, job: PersonaCreationJob
    ) -> dict[str, dict[str, Any]]:
        """Prior-run artifacts that still cover a dimension's current evidence.

        A dimension is reusable when its latest artifact validates, references
        only sources the job knows, and the processed-source ledger proves the
        dimension already saw every source currently known.  New evidence for
        a dimension forces real (incremental) extraction instead.
        """
        known_sources = set(job.source_ids)
        if not known_sources or not job.compilation_task_id:
            return {}
        processed_ledger = {
            str(dim): {str(value) for value in ids}
            for dim, ids in (job.job_config.get("dimension_processed_sources") or {}).items()
        }
        if not processed_ledger:
            return {}
        latest = self._latest_dimension_artifacts(job)
        reusable: dict[str, dict[str, Any]] = {}
        for dimension in REQUIRED_DIMENSIONS:
            artifact = latest.get(dimension)
            if artifact is None:
                continue
            covered = processed_ledger.get(dimension) or set()
            if not known_sources.issubset(covered):
                continue
            try:
                validated = ResearchArtifact.model_validate(artifact)
            except Exception:
                continue
            if validated.dimension != dimension:
                continue
            if not validated.source_ids or not set(validated.source_ids) <= known_sources:
                continue
            reusable[dimension] = dict(artifact)
        return reusable

    def _latest_dimension_artifacts(self, job: PersonaCreationJob) -> dict[str, dict[str, Any]]:
        if not job.compilation_task_id:
            return {}
        artifacts = self.continuum.compilation.get_task(job.compilation_task_id).artifacts
        latest: dict[str, dict[str, Any]] = {}
        for artifact in reversed(artifacts):
            if not isinstance(artifact, dict):
                continue
            dimension = str(artifact.get("dimension") or "")
            if dimension in REQUIRED_DIMENSIONS and dimension not in latest:
                latest[dimension] = dict(artifact)
        return latest

    def _retry_reusable_dimension_artifacts(
        self, job: PersonaCreationJob
    ) -> dict[str, dict[str, Any]] | None:
        """Return a complete prior-run artifact set when a retry can skip re-extraction.

        A quality-gate retry copies the failed run's compilation task, so the
        eight dimension artifacts already exist and were already persisted with
        provenance.  Re-running extraction would re-pay the model for identical
        evidence.  Reuse is allowed only when every dimension's artifact still
        validates against the ResearchArtifact schema and references only
        sources the job knows about; otherwise the caller falls back to a full
        extraction pass.
        """

        if not job.job_config.get("retry_of"):
            return None
        # Any source that arrived after the failed run must go through real
        # extraction; reuse is only for an identical-evidence retry.
        if job.job_config.get("new_source_ids"):
            return None
        known_sources = set(job.source_ids)
        if not known_sources or not job.compilation_task_id:
            return None
        latest = self._latest_dimension_artifacts(job)
        if len(latest) < len(REQUIRED_DIMENSIONS):
            return None
        reusable: dict[str, dict[str, Any]] = {}
        for dimension in REQUIRED_DIMENSIONS:
            artifact = latest.get(dimension)
            if artifact is None:
                return None
            try:
                validated = ResearchArtifact.model_validate(artifact)
            except Exception:
                return None
            if validated.dimension != dimension:
                return None
            if not validated.source_ids or not set(validated.source_ids) <= known_sources:
                return None
            reusable[dimension] = dict(artifact)
        return reusable

    def _restore_reused_artifact_provenance(
        self, job: PersonaCreationJob, artifacts: dict[str, dict[str, Any]]
    ) -> int:
        """Re-submit reused artifacts so the compile step sees this task's set.

        The retry shares the copied compilation task, so the artifacts are
        normally already attached; submitting the canonical payload is
        idempotent (duplicate-hash submissions return unchanged) and repairs
        the case where the copied task lost its artifact list.  Also restores
        per-dimension progress and processed-source bookkeeping so coverage and
        later incremental rounds behave as if extraction had just run.
        """

        restored = 0
        task = self.continuum.compilation.get_task(job.compilation_task_id or "")
        attached = {
            str(artifact.get("dimension"))
            for artifact in task.artifacts
            if isinstance(artifact, dict)
        }
        for dimension in REQUIRED_DIMENSIONS:
            artifact = artifacts.get(dimension)
            if artifact is None:
                continue
            validated = ResearchArtifact.model_validate(artifact)
            payload = validated.model_dump(mode="json")
            rewritten = False
            if job.persona_type == PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON:
                # Same boundary as _persist_dimension_result, applied on restore.
                rewritten = normalise_fictional_provenance(payload) or rewritten
            if dimension not in attached or rewritten:
                compilation_service = self.continuum.compilation
                if hasattr(compilation_service, "_canonical_artifact_sha256"):
                    payload["artifact_hash"] = compilation_service._canonical_artifact_sha256(
                        payload
                    )
                compilation_service.submit_research_artifact(
                    job.compilation_task_id or "", payload
                )
                restored += 1
            job.dimension_progress[dimension] = max(
                safe_int(job.dimension_progress.get(dimension, 0), default=0, minimum=0) or 0,
                len(set(validated.source_ids)),
            )
            processed = job.job_config.setdefault("dimension_processed_sources", {})
            current_done = {str(value) for value in (processed.get(dimension) or [])}
            processed[dimension] = sorted(current_done | set(validated.source_ids))
        self._save(job)
        return restored

    async def _run_global_audits(self, job: PersonaCreationJob) -> None:
        """Run fact/provenance and cross-dimension audits over the ledger.

        Raw webpages are not reread eight times.  Both audits receive the
        compact Evidence Ledger plus the latest eight artifacts.  A bounded
        targeted repair may re-synthesize only dimensions that failed, after
        which both gates must explicitly pass.
        """

        if not bool(getattr(self.continuum.config, "persona_global_audit", True)):
            return
        if not job.persona_id:
            raise PersonaQualityGateError(
                "final_audit_persona_missing",
                failure_code=PersonaFailureCode.FINAL_AUDIT_FAILED.value,
            )

        attempts = (
            safe_int(
                getattr(self.continuum.config, "persona_audit_repair_attempts", 1),
                default=1,
                minimum=0,
                maximum=2,
            )
            or 0
        )
        audit_history: list[dict[str, Any]] = []
        for attempt in range(attempts + 1):
            # Atomic boundary: the first audit pass and every later repair
            # re-audit is new model work.  A pause requested after the
            # dimensions settled must stop the pipeline here, not after the
            # audit has already burned a call.
            self._raise_if_pause_requested(job.id)
            artifacts = self._latest_dimension_artifacts(job)
            deterministic_failures = self._deterministic_audit_failures(job, artifacts)
            if deterministic_failures:
                dimension_status = {
                    dimension: (
                        "repair_required" if dimension in deterministic_failures else "pass"
                    )
                    for dimension in REQUIRED_DIMENSIONS
                }
                results: dict[str, dict[str, Any]] = {
                    "evidence": {
                        "status": "repair_required",
                        "dimensions": dimension_status,
                        "issues": [
                            {"dimension": dimension, "reason": reason}
                            for dimension, reason in deterministic_failures.items()
                        ],
                    },
                    "consistency": {
                        "status": "repair_required",
                        "dimensions": dimension_status,
                        "issues": [],
                    },
                }
            else:
                # Bound the payload by the agent's transport ceiling so the
                # audit never dispatches a prompt the transport guard must
                # reject (model context is a separate, much larger budget).
                adapter = self.continuum.agent_registry.get_adapter(job.agent_id)
                transport = (
                    resolve_prompt_transport_capability(adapter)
                    if adapter is not None
                    else None
                )
                payload = self._global_audit_payload(
                    job,
                    artifacts,
                    transport_safe_bytes=(
                        transport.safe_prompt_bytes if transport is not None else None
                    ),
                )
                evidence_result, consistency_result = await asyncio.gather(
                    self._run_final_audit_pass(job, "evidence", payload, attempt=attempt),
                    self._run_final_audit_pass(job, "consistency", payload, attempt=attempt),
                )
                results = {
                    "evidence": evidence_result,
                    "consistency": consistency_result,
                }
            audit_history.append({"attempt": attempt, **results})
            local_material_mode = job.creation_mode != "public_research" or str(
                job.job_config.get("enrichment_input_mode") or ""
            ) in {"local_materials", "hybrid"}
            failing_dimensions = self._audit_failing_dimensions(
                results,
                preserve_documented_source_gaps=local_material_mode,
            )
            passed = (
                (
                    local_material_mode
                    or all(
                        str(result.get("status") or "").casefold() == "pass"
                        for result in results.values()
                    )
                )
                and not failing_dimensions
            )
            if passed:
                job.job_config["final_global_audit"] = {
                    "status": "pass",
                    "attempts": audit_history,
                }
                self._save(job)
                await self._emit(job, "persona_final_audit_passed", attempts=attempt + 1)
                return
            if attempt >= attempts:
                job.job_config["final_global_audit"] = {
                    "status": "fail",
                    "attempts": audit_history,
                    "failing_dimensions": sorted(failing_dimensions),
                }
                self._save(job)
                raise PersonaQualityGateError(
                    "final_quality_gate_failed:" + ",".join(sorted(failing_dimensions)),
                    failure_code=PersonaFailureCode.FINAL_QUALITY_GATE_FAILED.value,
                )
            # Another atomic boundary before the bounded repair pass.
            self._raise_if_pause_requested(job.id)
            await self._repair_audit_dimensions(
                job,
                failing_dimensions,
                attempt=attempt + 1,
                audit_results=results,
            )

    def _deterministic_audit_failures(
        self, job: PersonaCreationJob, artifacts: dict[str, dict[str, Any]]
    ) -> dict[str, str]:
        known_sources = set(job.source_ids)
        local_material_mode = job.creation_mode != "public_research" or str(
            job.job_config.get("enrichment_input_mode") or ""
        ) in {"local_materials", "hybrid"}
        failures: dict[str, str] = {}
        for dimension in REQUIRED_DIMENSIONS:
            artifact = artifacts.get(dimension)
            if artifact is None:
                failures[dimension] = "missing_artifact"
                continue
            claims = [item for item in artifact.get("claims") or [] if isinstance(item, dict)]
            if not claims:
                uncertainty = artifact.get("uncertainty") or {}
                documented_gap = isinstance(uncertainty, dict) and bool(
                    uncertainty.get("notes")
                    or uncertainty.get("gaps")
                    or uncertainty.get("reason")
                    or uncertainty.get("missing_evidence")
                    or uncertainty.get("no_extractable_content") is True
                    or uncertainty.get("extractable_content_present") is False
                    or (
                        uncertainty.get("empty_content") is True
                        and (uncertainty.get("summary") or uncertainty.get("note"))
                    )
                    or (
                        uncertainty.get("repair_outcome") == "unresolved_source_deficit"
                        and (uncertainty.get("summary") or uncertainty.get("note"))
                    )
                    or (
                        uncertainty.get("missing_information")
                        and (
                            uncertainty.get("summary")
                            or uncertainty.get("extraction_notes")
                            or uncertainty.get("repair_note")
                        )
                    )
                )
                if not (local_material_mode and documented_gap):
                    failures[dimension] = "missing_claims"
                continue
            if any(
                not str(claim.get("source_id") or "")
                or str(claim.get("source_id")) not in known_sources
                for claim in claims
            ):
                failures[dimension] = "invalid_claim_source_link"
        return failures

    def _global_audit_payload(
        self,
        job: PersonaCreationJob,
        artifacts: dict[str, dict[str, Any]],
        *,
        transport_safe_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Build the final-audit payload, compacting to the transport budget.

        ``transport_safe_bytes`` is the adapter's transport ceiling, not the
        model context window: an ARGV-only CLI may only carry ~57KB even when
        the model holds 1M tokens.  When the first-pass payload exceeds it the
        payload is rebuilt at progressively tighter compaction levels instead
        of dispatching a prompt the transport guard must reject.
        """

        config_ledger_limit = (
            safe_int(
                getattr(self.continuum.config, "persona_global_audit_ledger_items", 32),
                default=32,
                minimum=8,
            )
            or 32
        )
        config_claims_limit = (
            safe_int(
                getattr(
                    self.continuum.config,
                    "persona_global_audit_claims_per_dimension",
                    8,
                ),
                default=8,
                minimum=2,
            )
            or 8
        )
        # Every evidence id retained in the compact artifact is a verification
        # target of the linkage audit.  References belonging to claims removed
        # by compaction are intentionally omitted as well: backfilling those
        # hidden references cannot help the model verify a claim and can exceed
        # an ARGV transport ceiling by tens of kilobytes.
        compaction_levels = (
            (config_ledger_limit, config_claims_limit, 1.0),
            (max(8, config_ledger_limit // 2), max(2, config_claims_limit // 2), 0.5),
            (8, 2, 0.3),
            (4, 1, 0.2),
        )
        payload: dict[str, Any] = {}
        for level, (ledger_limit, claims_limit, text_scale) in enumerate(compaction_levels):
            payload = self._build_audit_payload(
                job,
                artifacts,
                ledger_limit=ledger_limit,
                claims_limit=claims_limit,
                text_scale=text_scale,
            )
            if transport_safe_bytes is None or level == len(compaction_levels) - 1:
                break
            encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            if (
                len(encoded) + _GLOBAL_AUDIT_ENVELOPE_HEADROOM_BYTES
                <= int(transport_safe_bytes)
            ):
                break
        return payload

    def _build_audit_payload(
        self,
        job: PersonaCreationJob,
        artifacts: dict[str, dict[str, Any]],
        *,
        ledger_limit: int,
        claims_limit: int,
        text_scale: float,
    ) -> dict[str, Any]:
        ledger_text_limit = self._scaled_audit_limit(280, text_scale)
        ledger: list[dict[str, Any]] = []
        contradictions: list[dict[str, Any]] = []
        compact_artifacts = {
            dimension: self._compact_audit_artifact(
                artifact, claims_limit=claims_limit, text_scale=text_scale
            )
            for dimension, artifact in artifacts.items()
        }
        if self.material_intelligence is not None and job.persona_id:
            index = self.material_intelligence.get_index(job.persona_id)
            ledger = [
                {
                    "id": item.get("id"),
                    "text": self._audit_text(item.get("text"), ledger_text_limit),
                    "source_ids": item.get("source_ids") or [],
                    "evidence_ids": item.get("evidence_ids") or [],
                    "dimension_scores": item.get("dimension_scores") or {},
                    "intelligence": self._compact_audit_intelligence(
                        item.get("intelligence"), text_scale
                    ),
                    "verbatim_samples": [
                        self._audit_text(value, self._scaled_audit_limit(180, text_scale))
                        for value in (item.get("verbatim_samples") or [])[:2]
                    ],
                }
                for item in index.retrieve(top_k=ledger_limit, diversity=True)
            ]
            contradictions = [
                {
                    "type": item.contradiction_type,
                    "evidence_ids": item.evidence_ids,
                    "summary": self._audit_text(
                        item.summary, self._scaled_audit_limit(320, text_scale)
                    ),
                }
                for item in index.get_contradictions()[:16]
            ]
            referenced_ids = {
                match
                for value in (compact_artifacts, contradictions)
                for match in re.findall(
                    r"\b(?:evu|evf)_[0-9a-f]+\b",
                    json.dumps(value, ensure_ascii=False, default=str),
                )
            }
            included_ids = {
                str(item.get("id") or "") for item in ledger
            } | {
                str(evidence_id)
                for item in ledger
                for evidence_id in item.get("evidence_ids") or []
            }
            missing_ids = referenced_ids - included_ids
            if missing_ids:
                unit_by_id = {item.id: item for item in index.units()}
                fused_by_id = {item.id: item for item in index.fused()}
                for evidence_id in sorted(missing_ids):
                    unit = unit_by_id.get(evidence_id)
                    if unit is not None:
                        ledger.append(
                            {
                                "id": unit.id,
                                "text": self._audit_text(unit.text, ledger_text_limit),
                                "source_ids": [unit.source_id],
                                "dimension_scores": unit.dimension_scores,
                                "referenced_context": True,
                            }
                        )
                        continue
                    # Fused entries live in their own table and only reach the
                    # ledger through the ranked retrieve() window.  A referenced
                    # evf_* id outside that window must still be backfilled, or
                    # the linkage audit can never verify it and the dimension
                    # fails every repair round.
                    fused_item = fused_by_id.get(evidence_id)
                    if fused_item is not None:
                        ledger.append(
                            {
                                "id": fused_item.id,
                                "text": self._audit_text(
                                    fused_item.canonical_claim, ledger_text_limit
                                ),
                                "source_ids": fused_item.source_ids,
                                "dimension_scores": fused_item.dimension_scores,
                                "referenced_context": True,
                            }
                        )
        return {
            "persona_id": job.persona_id,
            "source_ids": sorted(set(job.source_ids)),
            "evidence_ledger": ledger,
            "contradiction_graph": contradictions,
            "dimension_artifacts": compact_artifacts,
            "required_dimensions": list(REQUIRED_DIMENSIONS),
        }

    @staticmethod
    def _scaled_audit_limit(base: int, scale: float) -> int:
        return max(40, int(base * scale))

    @staticmethod
    def _audit_text(value: Any, limit: int) -> str:
        text = str(value or "").strip()
        return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"

    @classmethod
    def _compact_audit_intelligence(
        cls, value: Any, text_scale: float = 1.0
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        compact: dict[str, Any] = {}
        limit = cls._scaled_audit_limit(180, text_scale)
        for key in (
            "claims",
            "events",
            "dates",
            "entities",
            "quotes",
            "contradictions",
            "negative_evidence",
            "uncertainties",
        ):
            raw = value.get(key)
            items = raw if isinstance(raw, list) else [raw] if raw else []
            compact[key] = [
                cls._audit_text(
                    json.dumps(item, ensure_ascii=False, default=str)
                    if isinstance(item, (dict, list))
                    else item,
                    limit,
                )
                for item in items[:2]
            ]
        return {key: items for key, items in compact.items() if items}

    @classmethod
    def _compact_audit_artifact(
        cls,
        artifact: dict[str, Any],
        *,
        claims_limit: int,
        text_scale: float = 1.0,
    ) -> dict[str, Any]:
        claims = []
        claim_limit = cls._scaled_audit_limit(220, text_scale)
        evidence_link_limit = max(1, min(4, int(4 * text_scale)))
        for raw in artifact.get("claims") or []:
            if not isinstance(raw, dict):
                continue
            raw_metadata = raw.get("metadata")
            metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
            raw_evidence_ids = raw.get("evidence_ids") or metadata.get("evidence_ids") or []
            if isinstance(raw_evidence_ids, str):
                raw_evidence_ids = [raw_evidence_ids]
            compact_claim = {
                "content": cls._audit_text(raw.get("content"), claim_limit),
                "source_id": raw.get("source_id"),
                "claim_type": raw.get("claim_type"),
                "confidence": raw.get("confidence"),
            }
            evidence_ids = [
                str(value)
                for value in raw_evidence_ids
                if str(value).strip()
            ][:evidence_link_limit]
            if evidence_ids:
                compact_claim["evidence_ids"] = evidence_ids
            claims.append(compact_claim)
            if len(claims) >= claims_limit:
                break
        components = json.dumps(
            artifact.get("extracted_components") or {},
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
        conflict_limit = cls._scaled_audit_limit(220, text_scale)
        nested_item_limit = max(2, min(8, int(8 * text_scale)))
        return {
            "dimension": artifact.get("dimension"),
            "source_ids": artifact.get("source_ids") or [],
            "claims": claims,
            "conflicts": [
                cls._compact_audit_value(
                    item, conflict_limit, max_items=nested_item_limit
                )
                for item in (artifact.get("conflicts") or [])[:4]
            ],
            "uncertainty": artifact.get("uncertainty") or {},
            "component_summary": cls._audit_text(
                components, cls._scaled_audit_limit(800, text_scale)
            ),
        }

    @classmethod
    def _compact_audit_value(
        cls, value: Any, limit: int, *, max_items: int = 8
    ) -> Any:
        """Keep structured audit fields structured while bounding their text."""

        if isinstance(value, dict):
            return {
                str(key): cls._compact_audit_value(
                    item, limit, max_items=max_items
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                cls._compact_audit_value(item, limit, max_items=max_items)
                for item in value[:max_items]
            ]
        return cls._audit_text(value, limit) if isinstance(value, str) else value

    async def _run_final_audit_pass(
        self,
        job: PersonaCreationJob,
        audit_type: str,
        payload: dict[str, Any],
        *,
        attempt: int,
    ) -> dict[str, Any]:
        if audit_type == "evidence":
            instruction = (
                "检查事实准确性、claim/source linkage、时间线、冲突、negative evidence、"
                "uncertainty 与错误归因。每个 required dimension 必须给出 "
                "pass/repair_required/fail。资料未覆盖某维度时，只要 artifact 如实留空或明确"
                "记录 uncertainty 且没有补写，就应判 pass 并把缺口记为 warning/info；只有"
                "artifact 自身可修复的错误才判 repair_required。"
            )
        else:
            instruction = (
                "检查八维完整性、跨维矛盾、价值观与行为、语言风格证据、动机推断边界和关系一致性。"
                "每个 required dimension 必须给出 pass/repair_required/fail。最终门禁评估的是"
                "产物是否忠实于现有资料，不以编造内容换取表面完整：资料本身缺少第三方评价、"
                "生平或逐字样本时，若 artifact 已诚实保留缺口，应判 pass 并记录 coverage gap；"
                "只有错误归因、无来源断言、结构损坏或未处理的真实矛盾才需返工。"
            )
        participant_id = f"final_{audit_type}_audit:{attempt}"
        try:
            result = await self._agent_json(
                job,
                user_message=json.dumps(
                    {"audit_type": audit_type, "instruction": instruction, **payload},
                    ensure_ascii=False,
                    default=str,
                ),
                system_prompt=(
                    "你是 Persona Continuum 严格最终质量门禁。只能依据输入 Ledger 与 artifacts；"
                    "不得把推断或模拟延续当成历史事实。返回符合 schema 的单个 JSON 对象。"
                ),
                participant_id=participant_id,
                phase=f"final_{audit_type}_audit",
                schema=FINAL_AUDIT_SCHEMA,
            )
        finally:
            await self._drop_job_session(job.id, participant_id)
        if not isinstance(result, dict):
            raise PersonaQualityGateError(
                f"final_{audit_type}_audit_invalid",
                failure_code=PersonaFailureCode.FINAL_AUDIT_FAILED.value,
            )
        return dict(result)

    @staticmethod
    def _audit_failing_dimensions(
        results: dict[str, dict[str, Any]], *, preserve_documented_source_gaps: bool = False
    ) -> set[str]:
        failing: set[str] = set()
        issue_types_by_dimension: dict[str, set[str]] = {}
        for result in results.values():
            dimensions = result.get("dimensions") or {}
            if isinstance(dimensions, dict):
                for dimension in REQUIRED_DIMENSIONS:
                    raw = dimensions.get(dimension)
                    # ``FINAL_AUDIT_SCHEMA`` keeps the per-dimension entry
                    # free-form, so models return ``status``, ``verdict``,
                    # ``result`` or a bare string.  Reading only ``status``
                    # turned an all-pass audit into eight false failures.
                    if isinstance(raw, dict):
                        status = next(
                            (
                                raw[key]
                                for key in ("status", "verdict", "result", "state")
                                if raw.get(key) is not None
                            ),
                            None,
                        )
                    else:
                        status = raw
                    if str(status or "").casefold() != "pass":
                        failing.add(dimension)
            else:
                failing.update(REQUIRED_DIMENSIONS)
            for issue in result.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                # A finding may name one dimension, several, or none
                # (cross-cutting).  Never test an untrusted field directly for
                # set/list membership: that is what raised
                # "unhashable type: 'list'".
                issue_dimensions = [
                    candidate
                    for candidate in normalize_audit_issue_dimensions(issue)
                    if candidate in REQUIRED_DIMENSIONS
                ]
                if not issue_dimensions:
                    # Global findings are attributed to the whole artifact and
                    # only escalate through explicit severity below.
                    continue
                issue_type = str(issue.get("type") or "").casefold()
                severity = str(issue.get("severity") or "").casefold()
                for dimension in issue_dimensions:
                    issue_types_by_dimension.setdefault(dimension, set()).add(issue_type)
                    if severity in {
                        "repair_required",
                        "high",
                        "critical",
                        "fail",
                        "error",
                    }:
                        failing.add(dimension)
        if preserve_documented_source_gaps:
            source_gap_types = {
                "content_thinness",
                "documented_gap",
                "evidence_gap",
                "missing_core_sections",
                "missing_dimension_content",
                "no_verbatim_samples",
                "single_source_no_cross_validation",
            }
            for dimension, issue_types in issue_types_by_dimension.items():
                if issue_types and issue_types <= source_gap_types:
                    failing.discard(dimension)
        return failing

    async def _repair_audit_dimensions(
        self,
        job: PersonaCreationJob,
        dimensions: set[str],
        *,
        attempt: int,
        audit_results: dict[str, dict[str, Any]] | None = None,
        target_components_by_dimension: dict[str, list[str]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not job.persona_id or not job.compilation_task_id:
            raise PersonaQualityGateError(
                "audit_repair_binding_missing",
                failure_code=PersonaFailureCode.AUDIT_REPAIR_FAILED.value,
            )
        sources = list(self.continuum.personas.get_sources(job.persona_id))
        source_by_id = {source.id: source for source in sources}
        context_manager = getattr(
            self.runtime_executor, "context_budget_manager", AgentContextBudgetManager()
        )
        current_artifacts = self._latest_dimension_artifacts(job)
        pending_dimensions = [dim for dim in REQUIRED_DIMENSIONS if dim in dimensions]
        concurrency_limit = max(
            1,
            safe_int(
                getattr(self.continuum.config, "persona_dimension_concurrency", 3),
                default=3,
                minimum=1,
            )
            or 3,
        )
        semaphore = asyncio.Semaphore(concurrency_limit)

        async def repair_one(dimension: str) -> dict[str, Any]:
            # Each repair consumes immutable inputs (audit findings plus a
            # snapshot of the current artifact) and returns its own artifact;
            # the deterministic canonical-order merge happens after the gather.
            async with semaphore:
                # Workers still queued behind the semaphore must not start a
                # repair model call once a pause has been requested.
                self._raise_if_pause_requested(job.id)
                target_components = list(
                    (target_components_by_dimension or {}).get(dimension, [])
                )
                current_artifact = current_artifacts.get(dimension, {})
                if target_components:
                    evidence_items = self._targeted_repair_evidence_items(
                        current_artifact,
                        known_source_ids=set(source_by_id),
                    )
                else:
                    retrieved = (
                        self.material_intelligence.get_index(job.persona_id or "").retrieve(
                            dimension, top_k=32, diversity=True
                        )
                        if self.material_intelligence is not None
                        else []
                    )
                    evidence_items = [
                        {
                            "evidence_id": item.get("id"),
                            "source_ids": [
                                str(value)
                                for value in item.get("source_ids") or []
                                if str(value) in source_by_id
                            ],
                            "evidence_type": item.get("kind"),
                            "verbatim_samples": item.get("verbatim_samples") or [],
                            "intelligence": item.get("intelligence") or {},
                            "content": item.get("text") or "",
                        }
                        for item in retrieved
                    ]
                if not evidence_items:
                    if target_components:
                        return {
                            "dimension": dimension,
                            "artifact": None,
                            "outcomes": {
                                component: "gap_no_evidence"
                                for component in target_components
                            },
                        }
                    evidence_items = [
                        {
                            "evidence_id": source.id,
                            "source_ids": [source.id],
                            "evidence_type": "source",
                            "verbatim_samples": [],
                            "content": source.content,
                        }
                        for source in sources
                    ]
                participant_id = f"{dimension}:repair:{attempt}"
                # A cross-dimension finding reaches every dimension it names;
                # ``_global`` / ``cross_cutting`` findings are broadcast.  The
                # model may send ``dimension`` as a string, a list, or omit it,
                # so every shape is normalized before matching.
                dimension_findings = [
                    normalize_audit_issue(issue)
                    for result in (audit_results or {}).values()
                    for issue in issues_for_dimension(result.get("issues"), dimension)
                ]
                artifact_context = (
                    self._targeted_repair_artifact_slice(
                        current_artifact,
                        target_components,
                    )
                    if target_components
                    else self._compact_audit_artifact(current_artifact, claims_limit=16)
                )
                repair_context = {
                    "instruction": (
                        "仅修复审计指出的产物缺陷；保留当前 artifact 中仍有来源支持的内容。"
                        "不得把资料缺失当作需要补写的内容，也不得改变已正确的 claim 类型。"
                    ),
                    "audit_findings": dimension_findings[:12],
                    "target_components": target_components,
                    "evidence_gate": (
                        "只能为 target_components 输出同名 canonical component；"
                        "每项内容必须能由本次 EVIDENCE 或 current_artifact_slice 直接支持。"
                        "证据不足时保持空值，并在 uncertainty.notes 写 KEEP GAP。"
                    )
                    if target_components
                    else None,
                    "current_artifact_slice": artifact_context,
                }
                try:
                    repaired = await self._run_dimension_extraction(
                        job,
                        dimension=dimension,
                        evidence_items=evidence_items,
                        participant_id=participant_id,
                        mode="targeted_repair",
                        source_by_id=source_by_id,
                        context_manager=context_manager,
                        phase="targeted_repair",
                        repair_context=repair_context,
                    )
                finally:
                    await self._drop_job_session(job.id, participant_id)
                if not target_components:
                    return {"dimension": dimension, "artifact": repaired, "outcomes": {}}
                merged, outcomes = self._merge_targeted_repair_artifact(
                    current_artifact,
                    repaired.model_dump(mode="json"),
                    target_components=target_components,
                    allowed_source_ids={
                        str(source_id)
                        for item in evidence_items
                        for source_id in item.get("source_ids") or []
                    },
                )
                return {"dimension": dimension, "artifact": merged, "outcomes": outcomes}

        outcomes = await asyncio.gather(
            *(repair_one(dimension) for dimension in pending_dimensions),
            return_exceptions=True,
        )
        by_dimension: dict[str, Any] = {}
        repair_outcomes: dict[str, dict[str, Any]] = {}
        for dimension, outcome in zip(pending_dimensions, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if isinstance(outcome, _PauseRequested):
                    # A pause is control-plane state, not an audit failure:
                    # surface it so the worker enters PAUSED instead of being
                    # marked failed_quality_gate.
                    raise outcome
                raise PersonaQualityGateError(
                    f"audit_repair_failed:{dimension}:{sanitize_diagnostic(outcome)}",
                    failure_code=PersonaFailureCode.AUDIT_REPAIR_FAILED.value,
                ) from outcome
            repaired_result: dict[str, Any] = outcome
            by_dimension[repaired_result["dimension"]] = repaired_result["artifact"]
            repair_outcomes[repaired_result["dimension"]] = dict(
                repaired_result.get("outcomes") or {}
            )
        compilation_service = self.continuum.compilation
        for dimension in pending_dimensions:
            repaired = by_dimension.get(dimension)
            if repaired is None:
                continue
            artifact_payload = (
                repaired.model_dump(mode="json")
                if hasattr(repaired, "model_dump")
                else dict(repaired)
            )
            # Pydantic may normalize legacy memory types while validating the
            # full settled artifact. Hash the validated payload, exactly as
            # submit_research_artifact will, or the safety check correctly
            # rejects the repair as an artifact_hash_mismatch.
            artifact_payload = ResearchArtifact.model_validate(artifact_payload).model_dump(
                mode="json"
            )
            artifact_payload["artifact_hash"] = (
                compilation_service._canonical_artifact_sha256(artifact_payload)
            )
            compilation_service.submit_research_artifact(job.compilation_task_id, artifact_payload)
            await self._emit(
                job,
                "persona_audit_dimension_repaired",
                dimension=dimension,
                attempt=attempt,
            )
        return repair_outcomes

    @staticmethod
    def _targeted_repair_evidence_items(
        artifact: dict[str, Any], *, known_source_ids: set[str]
    ) -> list[dict[str, Any]]:
        """Build a fail-closed evidence slice from character-visible claims."""

        items: list[dict[str, Any]] = []
        for index, claim in enumerate(artifact.get("claims") or []):
            if not isinstance(claim, dict):
                continue
            raw_metadata = claim.get("metadata")
            metadata: dict[str, Any] = (
                raw_metadata if isinstance(raw_metadata, dict) else {}
            )
            if normalise_material_scope(metadata.get("material_scope")) != CHARACTER_VISIBLE:
                continue
            source_id = str(claim.get("source_id") or "")
            if not source_id or source_id not in known_source_ids:
                continue
            content = str(claim.get("content") or "").strip()
            if not content:
                continue
            items.append(
                {
                    "evidence_id": f"claim_slice_{index}",
                    "source_ids": [source_id],
                    "evidence_type": "character_visible_claim",
                    "verbatim_samples": [],
                    "intelligence": {"material_scope": CHARACTER_VISIBLE},
                    "content": content,
                }
            )
        return items[:16]

    @staticmethod
    def _targeted_repair_artifact_slice(
        artifact: dict[str, Any], target_components: list[str]
    ) -> dict[str, Any]:
        def character_visible(claim: Any) -> bool:
            if not isinstance(claim, dict):
                return False
            raw_metadata = claim.get("metadata")
            if not isinstance(raw_metadata, dict):
                return False
            return (
                normalise_material_scope(raw_metadata.get("material_scope"))
                == CHARACTER_VISIBLE
            )

        claim_refs: list[str] = []
        for claim in artifact.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            raw_metadata = claim.get("metadata")
            metadata: dict[str, Any] = (
                raw_metadata if isinstance(raw_metadata, dict) else {}
            )
            if character_visible(claim):
                claim_refs.extend(str(value) for value in metadata.get("evidence_ids") or [])
        return {
            "dimension": artifact.get("dimension"),
            "target_components": list(target_components),
            "character_visible_claim_count": sum(
                1
                for cls in artifact.get("claims") or []
                if character_visible(cls)
            ),
            "evidence_refs": sorted(set(claim_refs))[:32],
            "extracted_components": {},
            "conflicts": [],
            "uncertainty": {
                "level": 0.5,
                "notes": ["bounded character-visible targeted repair slice"],
            },
        }

    @staticmethod
    def _merge_targeted_repair_artifact(
        current: dict[str, Any],
        repaired: dict[str, Any],
        *,
        target_components: list[str],
        allowed_source_ids: set[str],
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Keep settled fields and accept only evidence-gated target slots."""

        merged = json.loads(json.dumps(current, ensure_ascii=False, default=str))
        merged_components = dict(merged.get("extracted_components") or {})
        repaired_components = dict(repaired.get("extracted_components") or {})
        outcomes: dict[str, str] = {}
        accepted = False
        for component in target_components:
            value = repaired_components.get(component)
            if value in (None, "", [], {}) or not allowed_source_ids:
                outcomes[component] = "gap_insufficient_evidence"
                continue
            merged_components[component] = value
            outcomes[component] = "repaired"
            accepted = True
        if not accepted:
            return None, outcomes

        merged["extracted_components"] = merged_components
        existing_claim_keys = {
            (str(item.get("content") or ""), str(item.get("source_id") or ""))
            for item in merged.get("claims") or []
            if isinstance(item, dict)
        }
        for claim in repaired.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            source_id = str(claim.get("source_id") or "")
            content = str(claim.get("content") or "").strip()
            if not content or source_id not in allowed_source_ids:
                continue
            metadata = dict(claim.get("metadata") or {})
            metadata["material_scope"] = CHARACTER_VISIBLE
            claim["metadata"] = metadata
            key = (content, source_id)
            if key not in existing_claim_keys:
                merged.setdefault("claims", []).append(claim)
                existing_claim_keys.add(key)
        merged["artifact_id"] = new_id("art")
        merged["artifact_hash"] = hashlib.sha256(
            json.dumps(merged, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        merged.pop("artifact_canonical_sha256", None)
        return merged, outcomes

    async def repair_missing_components(
        self,
        job_id: str,
        components: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run bounded component repair and recompile through production services."""

        job = self.get_job(job_id)
        if not job.persona_id or not job.compilation_task_id:
            raise PersonaQualityGateError("targeted_repair_binding_missing")
        task = self.continuum.compilation.get_task(job.compilation_task_id)
        coverage_before = dict(task.plan.get("compile_coverage") or {})
        missing = {
            str(value)
            for key in ("missing_core_components", "missing_optional_components")
            for value in coverage_before.get(key, [])
        }
        requested = set(components or missing)
        unknown = requested - set(COMPILE_CONTRACT)
        if unknown:
            raise PersonaCreationError("unknown_target_components:" + ",".join(sorted(unknown)))
        selected = sorted(requested & missing)
        if not selected:
            self._finalize_targeted_repair_job(job, task, coverage_before)
            repair_state = dict(job.job_config.get("targeted_repair") or {})
            repair_state.update(
                {
                    "status": "completed",
                    "coverage_after": coverage_before,
                    "completed_at": datetime.now(UTC).isoformat(),
                }
            )
            job.job_config["targeted_repair"] = repair_state
            self._save(job)
            return {
                "status": "no_missing_components",
                "components": {},
                "coverage_before": coverage_before,
                "coverage_after": coverage_before,
            }

        by_dimension: dict[str, list[str]] = {}
        for component in selected:
            for dimension in TARGETED_REPAIR_DIMENSIONS[component]:
                by_dimension.setdefault(dimension, []).append(component)

        outcomes = await self._repair_audit_dimensions(
            job,
            set(by_dimension),
            attempt=1,
            audit_results={},
            target_components_by_dimension=by_dimension,
        )

        task = self.continuum.compilation.get_task(job.compilation_task_id)
        if job.persona_type == PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON:
            changed = False
            for artifact in task.artifacts:
                if normalise_fictional_provenance(artifact):
                    artifact.pop("artifact_canonical_sha256", None)
                    artifact["artifact_hash"] = (
                        self.continuum.compilation._canonical_artifact_sha256(artifact)
                    )
                    changed = True
            if changed:
                self.continuum.compilation._save_task(task)

        self.continuum.compilation.retain_latest_dimension_artifacts(
            job.compilation_task_id
        )
        compiled = self.continuum.compilation.compile_persona(
            job.persona_id, job.compilation_task_id
        )
        task = self.continuum.compilation.get_task(job.compilation_task_id)
        coverage_after = dict(task.plan.get("compile_coverage") or {})
        flat_outcomes = {
            component: result
            for dimension_outcomes in outcomes.values()
            for component, result in dimension_outcomes.items()
        }
        job.job_config["targeted_repair"] = {
            "status": "completed",
            "components": flat_outcomes,
            "coverage_before": coverage_before,
            "coverage_after": coverage_after,
            "compiled_manifest_version": compiled.manifest.version,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        self._finalize_targeted_repair_job(job, task, coverage_after)
        self._save(job)
        return dict(job.job_config["targeted_repair"])

    def _finalize_targeted_repair_job(
        self,
        job: PersonaCreationJob,
        task: Any,
        coverage: dict[str, Any],
    ) -> None:
        """Synchronize production state after bounded repair and compilation."""

        complete = bool(task.status == "completed" and coverage.get("ok"))
        job.status = "completed" if complete else "completed_with_gaps"
        job.current_stage = job.status
        job.error = None
        job.failure_json = None
        job.progress.failure = None
        job.progress.update_stage(
            job.current_stage,
            label="Persona 创建完成" if complete else "完成（存在资料缺口）",
            percent=100,
            completed=job.source_count,
            total=job.research_policy.preferred_source_target,
        )
        job.touch_worker(WorkerState.FINISHED, finished_at=datetime.now(UTC).isoformat())
        if not job.persona_id:
            return
        manifest = self.continuum.personas.get(job.persona_id).manifest
        manifest.compile_state = "compiled" if complete else "completed_with_gaps"
        manifest.active = complete
        record = self.continuum.personas.update_manifest(manifest)
        if self.profile_library is not None:
            self.profile_library.sync_persona(record)

    @staticmethod
    def _related_context_units(
        new_units: list[dict[str, Any]],
        old_units: list[dict[str, Any]],
        *,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        """Deterministically pick old units likely related to new evidence.

        Keyword overlap approximates "possibly conflicting / highly relevant"
        without an extra model call; a stable sort keeps runs reproducible.
        """

        def keywords(text: str) -> set[str]:
            return {token.casefold() for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", text or "")}

        new_texts = [keywords(str(unit.get("content") or "")) for unit in new_units]
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for unit in old_units:
            tokens = keywords(str(unit.get("content") or ""))
            overlap = sum(len(tokens & text_set) for text_set in new_texts)
            if overlap <= 0:
                continue
            scored.append((overlap, str(unit.get("evidence_id") or ""), unit))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [unit for _, _, unit in scored[:limit]]

    async def _run_dimension_extraction(
        self,
        job: PersonaCreationJob,
        *,
        dimension: str,
        evidence_items: list[dict[str, Any]],
        participant_id: str,
        mode: str,
        source_by_id: dict[str, Any],
        context_manager: Any,
        phase: str = "dimension_extraction",
        repair_context: dict[str, Any] | None = None,
    ) -> ResearchArtifact:
        local_material_mode = job.creation_mode != "public_research" or str(
            job.job_config.get("enrichment_input_mode") or ""
        ) in {"local_materials", "hybrid"}
        prompt_intro = f"""
为 Persona Continuum 提取维度 {dimension} 的 ResearchArtifact。
只使用下方 SOURCE_ID 对应的内容；每个事实 claim 和 memory 都必须携带真实 source_id，
区分 historical_self_report、historical_third_party_report、historical_inference，
记录 conflicts 与 uncertainty。不要补写资料中没有的事实。必须返回单个 JSON 对象，字段
严格包括 artifact_id, schema_version(固定为 1.1), dimension, source_ids, claims, memories,
extracted_components, conflicts, uncertainty, created_by, artifact_hash。当前八维是：
{", ".join(REQUIRED_DIMENSIONS)}。
""".strip()
        if job.persona_type == PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON:
            prompt_intro += (
                "\n\n这是虚构/合成人格。作者对该人物的设定、以及该人物在故事中经历过的事，"
                "claim_type 必须使用 fictional_author_defined（作者设定）或 fictional_canon"
                "（人物在故事中的经历），memory.source_kind 同理；"
                "不得使用任何 historical_* 类型，也不得使用 counterfactual_simulated"
                "（那只用于分支模拟，不是人物正典）。"
                "\n同时必须区分材料作用域："
                "character_visible（人物本人知道的信息）可进入 claims/memories；"
                "author_only（作者塑造人物用的幕后设定、未来人物弧、隐藏动机）"
                "与 evaluation_only（验收测试题、预期答案）不得写入 claims/memories，"
                "只能在 material_scope 中标记后排除。"
            )
        if mode == "incremental":
            prompt_intro += (
                "\n\n本轮为增量分析：只针对新加入的 EVIDENCE 单元提取新的 claims/memories；"
                "标记为 ALREADY_PROCESSED_CONTEXT 的单元仅作背景参考，用于识别矛盾与时间线，"
                "不要重复输出其中已提取的事实。"
            )
        if repair_context:
            prompt_intro += "\n\nTARGETED_REPAIR_CONTEXT:\n" + json.dumps(
                repair_context,
                ensure_ascii=False,
                default=str,
            )

        def render_dimension_evidence(item: dict[str, Any]) -> str:
            prefix = ""
            if item.get("processing_mode") == "related_context":
                prefix = "ALREADY_PROCESSED_CONTEXT\n"
            return (
                prefix
                + "EVIDENCE_ID: {evidence_id}\n"
                "SOURCE_IDS: {source_ids}\n"
                "EVIDENCE_TYPE: {evidence_type}\n"
                "VERBATIM: {verbatim_samples}\n"
                "INTELLIGENCE: {intelligence}\n"
                "CONTENT:\n{content}".format(
                    evidence_id=item.get("evidence_id"),
                    source_ids=json.dumps(item.get("source_ids") or [], ensure_ascii=False),
                    evidence_type=item.get("evidence_type"),
                    verbatim_samples=json.dumps(
                        item.get("verbatim_samples") or [], ensure_ascii=False
                    ),
                    intelligence=json.dumps(
                        item.get("intelligence") or {}, ensure_ascii=False, default=str
                    ),
                    content=item.get("content") or "",
                )
            )

        batch_artifacts: list[ResearchArtifact] = []
        dimension_system_prompt = (
            LOCAL_MATERIAL_SYSTEM_PROMPT if local_material_mode else PUBLIC_RESEARCH_SYSTEM_PROMPT
        )
        batch_item_limit = (
            safe_int(
                getattr(self.continuum.config, "persona_dimension_retrieval_items", 48),
                default=48,
                minimum=8,
            )
            or 48
        )
        capabilities = self._effective_model_capabilities(job)
        batchable_items = self._split_oversized_evidence(
            evidence_items,
            render=render_dimension_evidence,
            context_manager=context_manager,
            capabilities=capabilities,
            phase=phase,
            system_prompt=dimension_system_prompt,
            base_text=prompt_intro,
            expected_output=ResearchArtifact.model_json_schema(),
        )
        # A targeted repair has different instructions and acceptance criteria
        # from the original extraction. Reusing an evidence-only checkpoint
        # here would replay the exact artifact the audit just rejected.
        batch_checkpoints = (
            self._dimension_batch_checkpoints(job, dimension)
            if phase == "dimension_extraction" and not repair_context
            else {}
        )
        for batch in context_manager.iter_batches(
            batchable_items,
            item_text=render_dimension_evidence,
            max_items=batch_item_limit,
            phase="dimension_extraction",
            model=capabilities,
            system_prompt=dimension_system_prompt,
            expected_output=ResearchArtifact.model_json_schema(),
            base_text=prompt_intro,
            # Hard capacity and the best single-pass workload are different:
            # a 1M model is not forced to swallow ~950K in one turn, but an
            # unknown window must never collapse back into dozens of 32K-shaped
            # batches (Unknown keeps the planning fallback, not 32K-capable
            # batching).
            preferred_working_context=capabilities.preferred_working_context,
        ):
            batch_index = len(batch_artifacts) + 1
            fingerprint = self._batch_evidence_fingerprint(batch)
            checkpoint = batch_checkpoints.get(fingerprint)
            if checkpoint is not None:
                try:
                    validated_batch = ResearchArtifact.model_validate(checkpoint["artifact"])
                except Exception:
                    batch_checkpoints.pop(fingerprint, None)
                else:
                    job.job_config["dimension_checkpoint_hit_count"] = (
                        safe_int(
                            job.job_config.get("dimension_checkpoint_hit_count"),
                            default=0,
                            minimum=0,
                        )
                        or 0
                    ) + 1
                    from persona_continuum.performance.tracing import default_tracer

                    default_tracer().incr_global("dimension_checkpoint_hit_count", 1)
                    batch_artifacts.append(validated_batch)
                    await self._emit(
                        job,
                        "persona_dimension_batch_completed",
                        dimension=dimension,
                        batch_index=batch_index,
                        reused=True,
                    )
                    continue
            # Safe-pause boundary: an in-flight batch always finishes and is
            # checkpointed, but no new model call starts once pause is requested.
            self._raise_if_pause_requested(job)
            context = "\n\n".join(render_dimension_evidence(item) for item in batch)
            job.job_config["dimension_agent_calls"] = (
                safe_int(job.job_config.get("dimension_agent_calls"), default=0, minimum=0) or 0
            ) + 1
            job.job_config["last_dimension_agent_phase"] = phase
            job.job_config["last_dimension"] = dimension
            self._save(job)
            artifact = await self._agent_json(
                job,
                user_message=f"{prompt_intro}\n\n{context}",
                system_prompt=dimension_system_prompt,
                participant_id=participant_id,
                phase=phase,
                schema=ResearchArtifact,
            )
            validated_batch = ResearchArtifact.model_validate(artifact)
            if validated_batch.dimension != dimension:
                raise PersonaCreationError(f"artifact_dimension_mismatch:{dimension}")
            batch_source_ids = {
                source_id
                for item in batch
                for source_id in item.get("source_ids") or []
                if isinstance(source_id, str) and source_id in source_by_id
            }
            if batch_source_ids:
                validated_batch.source_ids = sorted(
                    set(validated_batch.source_ids) & batch_source_ids or batch_source_ids
                )
            primary_batch_source = (
                validated_batch.source_ids[0]
                if validated_batch.source_ids
                else (next(iter(batch_source_ids), None) if batch_source_ids else None)
            )
            if primary_batch_source:
                for claim_entry in validated_batch.claims:
                    if not claim_entry.source_id:
                        claim_entry.source_id = primary_batch_source
                for memory_entry in validated_batch.memories:
                    if not memory_entry.source_id:
                        memory_entry.source_id = primary_batch_source
            batch_artifacts.append(validated_batch)
            # Every successful batch is checkpointed before the next one runs,
            # so a later failure re-pays only for the unfinished batches.
            from persona_continuum.performance.tracing import default_tracer

            default_tracer().incr_global("dimension_batch_model_call_count", 1)
            binding = job.job_config.get("runtime_binding_snapshot") or {}
            batch_checkpoints[fingerprint] = {
                "fingerprint": fingerprint,
                "batch_index": batch_index,
                "artifact": validated_batch.model_dump(mode="json"),
                "provenance": {
                    "agent_id": job.agent_id,
                    "requested_model": job.model_id,
                    "effective_model": binding.get("effective_model") or job.model_id,
                    "reasoning": job.reasoning_effort,
                    "job_id": job.id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            }
            self._save(job)
            await self._emit(
                job,
                "persona_dimension_batch_completed",
                dimension=dimension,
                batch_index=batch_index,
                reused=False,
            )
        if not batch_artifacts:
            raise PersonaCreationError(f"dimension_context_empty:{dimension}")
        validated = self._merge_dimension_artifacts(batch_artifacts, dimension=dimension)
        artifact_source_ids = {
            value for item in evidence_items for value in item.get("source_ids") or []
        }
        if validated.dimension != dimension:
            raise PersonaCreationError(f"artifact_dimension_mismatch:{dimension}")
        if artifact_source_ids:
            # Preserve the compiler's formal source provenance even when a
            # structured response omitted a valid retrieved source id.
            validated.source_ids = sorted(
                set(validated.source_ids) & artifact_source_ids or artifact_source_ids
            )
        if job.persona_type == PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON:
            # The provenance boundary is deterministic: a model response cannot
            # promote fictional setting material into a historical assertion.
            for claim_entry in validated.claims:
                if claim_entry.claim_type not in FICTIONAL_KINDS:
                    claim_entry.claim_type = "fictional_author_defined"
            for memory_entry in validated.memories:
                if memory_entry.source_kind not in FICTIONAL_KINDS:
                    memory_entry.source_kind = "fictional_author_defined"
        if job.creation_mode == "public_research" and mode != "incremental":
            default_source = (
                validated.source_ids[0]
                if validated.source_ids
                else (next(iter(artifact_source_ids), None) if artifact_source_ids else None)
            )
            if default_source:
                for claim_entry in validated.claims:
                    if not claim_entry.source_id:
                        claim_entry.source_id = default_source
                for memory_entry in validated.memories:
                    if not memory_entry.source_id:
                        memory_entry.source_id = default_source
            unsourced = [
                entry
                for entry in [*validated.claims, *validated.memories]
                if not getattr(entry, "source_id", None)
            ]
            if unsourced:
                raise PersonaCreationError(f"artifact_unsourced_evidence:{dimension}")
        return validated

    @staticmethod
    def _merge_dimension_artifacts(
        artifacts: list[ResearchArtifact], *, dimension: str
    ) -> ResearchArtifact:
        """Merge budget-sized artifact results without losing batch provenance."""

        if len(artifacts) == 1:
            return artifacts[0]
        first = artifacts[0].model_dump(mode="json")
        claims: list[dict[str, Any]] = []
        memories: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        seen_claims: set[str] = set()
        seen_memories: set[str] = set()
        seen_conflicts: set[str] = set()
        for artifact in artifacts:
            payload = artifact.model_dump(mode="json")
            for target, key in (
                (claims, "claims"),
                (memories, "memories"),
                (conflicts, "conflicts"),
            ):
                for value in payload.get(key) or []:
                    marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                    seen = (
                        seen_claims
                        if key == "claims"
                        else seen_memories
                        if key == "memories"
                        else seen_conflicts
                    )
                    if marker not in seen:
                        seen.add(marker)
                        target.append(value)
        source_ids = sorted(
            {source_id for artifact in artifacts for source_id in artifact.source_ids}
        )
        uncertainty_levels = [
            safe_probability(artifact.uncertainty.get("level"), default=0.5) or 0.5
            for artifact in artifacts
        ]
        merged: dict[str, Any] = {
            **first,
            "artifact_id": first.get("artifact_id") or new_id("artifact"),
            "dimension": dimension,
            "source_ids": source_ids,
            "claims": claims,
            "memories": memories,
            "extracted_components": {
                "batch_components": [artifact.extracted_components for artifact in artifacts]
            },
            "conflicts": conflicts,
            "uncertainty": {
                "level": max(uncertainty_levels, default=0.5),
                "batch_levels": uncertainty_levels,
            },
            "created_by": "agent_runtime_batched",
        }
        merged["artifact_hash"] = hashlib.sha256(
            json.dumps(merged, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return ResearchArtifact.model_validate(merged)

    async def _request_interview_question(self, job: PersonaCreationJob) -> None:
        missing = [
            dimension
            for dimension in REQUIRED_DIMENSIONS
            if job.dimension_progress.get(dimension, 0)
            < job.research_policy.min_sources_per_dimension
        ]
        material_gap_payload: dict[str, Any] = {}
        if self.material_intelligence is not None and job.persona_id:
            with contextlib.suppress(Exception):
                material_gap_payload = self.material_intelligence.gap_analysis(job.persona_id)
                missing = [
                    str(item.get("dimension"))
                    for item in material_gap_payload.get("gaps", [])
                    if item.get("dimension") in REQUIRED_DIMENSIONS
                ] or missing
        interview_system_prompt = (
            "你是 Persona Continuum 的 Guided Persona Interview interviewer。"
            "只生成问题，不编造答案。"
        )
        prompt_base = {
            "display_name": job.display_name,
            "missing_dimensions": missing,
            "evidence_gaps": material_gap_payload.get("gaps", []),
            "instruction": "动态生成一个最能补齐缺口的问题；允许用户回答不清楚，不得替用户填写。",
        }
        existing_materials: list[dict[str, Any]] = []
        if self.material_intelligence is not None and job.persona_id:
            with contextlib.suppress(Exception):
                existing_materials = [
                    dict(item)
                    for item in self.material_intelligence.get_index(job.persona_id).retrieve(
                        top_k=None, diversity=True
                    )
                    if isinstance(item, dict)
                ]
        context_manager = getattr(
            self.runtime_executor, "context_budget_manager", AgentContextBudgetManager()
        )
        question_schema = {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "dimension": {"type": "string"},
                "why": {"type": "string"},
            },
        }
        batches = list(
            context_manager.iter_batches(
                existing_materials,
                item_text=lambda item: json.dumps(
                    {
                        "id": item.get("id"),
                        "content": item.get("text") or item.get("content") or "",
                    },
                    ensure_ascii=False,
                ),
                max_items=24,
                phase="guided_interview",
                model=self._effective_model_capabilities(job),
                base_text=json.dumps(prompt_base, ensure_ascii=False),
                system_prompt=interview_system_prompt,
                expected_output=question_schema,
            )
        ) or [[]]
        candidates: list[dict[str, Any]] = []
        for batch_index, batch in enumerate(batches):
            result = await self._agent_json(
                job,
                user_message=json.dumps(
                    {
                        **prompt_base,
                        "batch_index": batch_index,
                        "batch_count": len(batches),
                        "existing_materials": [
                            {
                                "id": item.get("id"),
                                "content": item.get("text") or item.get("content") or "",
                            }
                            for item in batch
                        ],
                    },
                    ensure_ascii=False,
                ),
                system_prompt=interview_system_prompt,
                participant_id="guided_interview",
            )
            if isinstance(result, dict) and str(result.get("question") or "").strip():
                candidates.append(dict(result))
        result = candidates[0] if candidates else {}
        if not isinstance(result, dict) or not str(result.get("question") or "").strip():
            raise PersonaCreationError("interview_question_invalid")
        question = {
            "question": str(result["question"]),
            "dimension": result.get("dimension") or (missing[0] if missing else None),
            "why": str(result.get("why") or "补齐当前证据缺口"),
        }
        job.interview_questions.append(question)
        self._save(job)
        await self._emit(job, "persona_interview_question", **question)

    async def _ingest_configured_materials(self, job: PersonaCreationJob) -> None:
        configured = list(job.job_config.get("materials", []))
        if not configured or not job.persona_id:
            return
        await self._set_stage(job, "ingesting_sources", "ingesting_sources")
        already = set(job.source_ids)
        pending: list[Any] = []
        for material in configured:
            if not isinstance(material, dict):
                continue
            if material.get("content_base64") is not None:
                try:
                    raw_bytes = base64.b64decode(str(material.get("content_base64")), validate=True)
                except Exception as exc:
                    raise PersonaCreationError("material_base64_invalid") from exc
                filename = Path(str(material.get("filename") or "material.txt")).name
                upload_dir = self.continuum.config.sources_dir / "persona_creation" / job.id
                upload_dir.mkdir(parents=True, exist_ok=True)
                upload_path = ensure_child_path(upload_dir, upload_dir / filename)
                upload_path.write_bytes(raw_bytes)
                with contextlib.suppress(ConflictError):
                    pending.extend(
                        self.continuum.personas.add_sources(job.persona_id, [upload_path])
                    )
            elif material.get("content") is not None:
                content = str(material.get("content") or "").strip()
                if not content:
                    continue
                try:
                    source = self.continuum.personas.add_source_text(
                        job.persona_id,
                        title=str(material.get("title") or "User provided material"),
                        source_type=str(material.get("source_type") or "user_provided"),
                        canonical_url=None,
                        publisher="user",
                        author=str(material.get("author") or "user"),
                        published_at=None,
                        accessed_at=datetime.now(UTC).isoformat(),
                        content=content,
                        metadata={
                            **dict(material.get("metadata") or {}),
                            "provenance": "user_provided",
                            "privacy": "private_material",
                        },
                    )
                except ConflictError:
                    continue
                pending.append(source)
            elif material.get("path"):
                path = Path(str(material["path"])).expanduser()
                path = ensure_child_path(self.continuum.config.data_dir, path)
                if not path.exists() or not path.is_file():
                    raise PersonaCreationError(f"material_not_found:{path}")
                with contextlib.suppress(ConflictError):
                    pending.extend(self.continuum.personas.add_sources(job.persona_id, [path]))
        for source in pending:
            if source.id not in already:
                job.source_ids.append(source.id)
                await self._emit(job, "persona_source_ingested", source_id=source.id)
        job.source_ids = sorted(set(job.source_ids))
        job.source_count = len(job.source_ids)
        if self.material_intelligence is not None and job.source_ids:
            material_job = await self._analyze_private_materials(
                job, job.source_ids, incremental=True
            )
            job.job_config["material_analysis"] = material_job.model_dump(mode="json")
            job.job_config["material_progress"] = dict(material_job.progress)
            await self._emit(
                job,
                "persona_material_analysis_completed",
                material_job_id=material_job.id,
                material_status=material_job.status.value,
                coverage=material_job.coverage,
            )
        job.job_config["materials"] = []
        self._save(job)

    async def _analyze_private_materials(
        self,
        job: PersonaCreationJob,
        source_ids: list[str],
        *,
        incremental: bool,
        agent_phases: tuple[str, ...] | None = None,
        shared_factual_cache: bool = False,
    ) -> Any:
        if self.material_intelligence is None or not job.persona_id:
            raise PersonaCreationError("material_intelligence_unavailable")

        async def analyze_with_runtime(phase: str, payload: dict[str, Any]) -> Any:
            payload = dict(payload)
            participant_id = str(payload.pop("_participant_id", f"persona_material_{phase}"))
            job.job_config["material_agent_calls"] = (
                safe_int(job.job_config.get("material_agent_calls"), default=0, minimum=0) or 0
            ) + 1
            job.job_config["last_material_agent_phase"] = phase
            self._save(job)
            phase_names = {
                "classify": "material_agent_classification",
                "relate": "semantic_relation",
                "fuse": "evidence_fusion",
            }
            event_prefix = phase_names.get(phase, f"material_agent_{phase}")
            await self._emit(job, f"{event_prefix}_started")
            system_prompt = MATERIAL_AGENT_SYSTEM_PROMPTS.get(phase)
            if system_prompt is None:
                raise PersonaCreationError(f"material_agent_phase_unsupported:{phase}")
            result = await self._agent_json(
                job,
                user_message=json.dumps(payload, ensure_ascii=False),
                system_prompt=system_prompt,
                participant_id=participant_id,
                phase={
                    "classify": "material_classification",
                    "relate": "semantic_relation",
                    "fuse": "evidence_fusion",
                }.get(phase, f"material_{phase}"),
            )
            await self._emit(job, f"{event_prefix}_completed")
            return result

        async def report_progress(material_job: Any) -> None:
            job.job_config["material_progress"] = dict(material_job.progress)
            job.job_config["material_progress"]["status"] = str(material_job.status.value)
            material_stage = {
                "PARSING": "parsing",
                "SEGMENTING": "segmenting",
                "ANALYZING": "material_classification",
                "CLUSTERING": "semantic_relation",
                "FUSING": "evidence_fusion",
                "INDEXING": "indexing",
                "GAP_ANALYSIS": "gap_analysis",
                "READY_FOR_COMPILATION": "extracting",
            }.get(str(material_job.status.value), str(material_job.status.value).lower())
            material_percent = (
                job.progress.percent
                if material_stage == "failed"
                else percent_for_stage(material_stage)
            )
            # Dynamic stage progress: "material_classification" covers a whole
            # windowed Agent phase and must not freeze at its floor percent.
            # Window k/N maps into the 40%~49% band; the next stage starts 50%.
            windows_total = (
                safe_int(material_job.progress.get("windows_total"), default=0, minimum=0) or 0
            )
            windows_completed = (
                safe_int(material_job.progress.get("windows_completed"), default=0, minimum=0)
                or 0
            )
            if material_stage == "material_classification" and windows_total > 0:
                ratio = min(windows_completed, windows_total) / windows_total
                material_percent = max(40, min(49, 40 + round(9 * ratio)))
            stage_updates: dict[str, Any] = {
                "label": "本地资料分析失败" if material_stage == "failed" else material_stage,
                "percent": material_percent,
                "completed": safe_int(
                    material_job.progress.get("evidence_unit_count"), default=0, minimum=0
                )
                or 0,
                "total": job.source_count or None,
                "current_item": material_job.progress.get("stage"),
            }
            if windows_total > 0:
                stage_updates["current_subtask"] = "material_classification_windows"
                stage_updates["windows_completed"] = windows_completed
                stage_updates["windows_total"] = windows_total
                stage_updates["current_window"] = (
                    safe_int(material_job.progress.get("current_window"), default=None, minimum=1)
                    or windows_completed
                )
            prompt_state = material_job.progress.get("prompt_state")
            if prompt_state:
                stage_updates["prompt_state"] = str(prompt_state)
            job.progress.update_stage(material_stage, **stage_updates)
            if material_stage in {"parsing", "segmenting", "material_classification"}:
                self._checkpoint(job, material_stage, material_job_id=material_job.id)
            self._save(job)
            status = str(material_job.status.value)
            event_map = {
                "PARSING": "material_parsing_started",
                "SEGMENTING": "material_segmentation_completed",
                "ANALYZING": "material_agent_classification_started",
                "CLUSTERING": "semantic_relation_started",
                "FUSING": "evidence_fusion_started",
                "INDEXING": "evidence_fusion_completed",
                "GAP_ANALYSIS": "dimension_extraction_started",
            }
            if status in event_map:
                await self._emit(job, event_map[status], progress=dict(material_job.progress))
            await self._emit(
                job,
                "persona_material_progress",
                material_job_id=material_job.id,
                material_status=material_job.status.value,
                progress=dict(material_job.progress),
            )

        runtime_profile_snapshot = dict(job.capability_snapshot or {})
        runtime_profile_snapshot.update(dict(job.job_config.get("runtime_binding_snapshot") or {}))
        runtime_profile_snapshot.setdefault("effective_model", job.model_id)
        runtime_profile_snapshot.setdefault("effective_reasoning", job.reasoning_effort)
        # The Adapter/Protocol transport capability is declared once by the
        # adapter and read by the material planner; the business layer never
        # branches on an adapter id or tool name.
        adapter = self.continuum.agent_registry.get_adapter(job.agent_id)
        if adapter is not None:
            runtime_profile_snapshot["prompt_transport"] = (
                resolve_prompt_transport_capability(adapter).as_dict()
            )
        return await self.material_intelligence.analyze_sources_async(
            job.persona_id,
            source_ids,
            runtime_snapshot=runtime_profile_snapshot,
            incremental=incremental,
            agent_analyzer=analyze_with_runtime,
            agent_phases=agent_phases,
            shared_factual_cache=shared_factual_cache,
            progress_callback=report_progress,
        )

    def _assert_material_agent_analysis(self, job: PersonaCreationJob) -> None:
        count = safe_int(job.job_config.get("input_material_count"), default=0, minimum=0) or 0
        if count <= 0 or bool(job.job_config.get("deterministic_only")):
            return
        material_calls = (
            safe_int(job.job_config.get("material_agent_calls"), default=0, minimum=0) or 0
        )
        dimension_calls = (
            safe_int(job.job_config.get("dimension_agent_calls"), default=0, minimum=0) or 0
        )
        if material_calls <= 0 or dimension_calls <= 0:
            raise PersonaModelAnalysisNotExecutedError(
                "enrichment_model_analysis_not_executed"
            )

    def _coverage(
        self, job: PersonaCreationJob, life_stage_model: LifeStageModel | None = None
    ) -> dict[str, Any]:
        sources = list(self.continuum.personas.get_sources(job.persona_id or ""))
        selected = [source for source in sources if source.id in set(job.source_ids)]
        clusters = self._source_analyzer.cluster_sources(selected)
        self._persist_source_clusters(job, clusters)
        cluster_by_source = {
            source_id: cluster for cluster in clusters for source_id in cluster.member_source_ids
        }
        independent_sources = len(clusters)
        canonical_sources = []
        for cluster in clusters:
            source = next(
                (item for item in selected if item.id == cluster.canonical_source_id), None
            )
            if source is not None:
                canonical_sources.append(source)
        categories = {
            str(source.metadata.get("category") or source.source_type or "unknown").lower()
            for source in canonical_sources
        }
        event_years = {
            match.group(1)[:4]
            for source in selected
            for match in [re.search(r"(\d{4})", str(source.metadata.get("event_time") or ""))]
            if match
        }
        primary_count = sum(
            1
            for cluster in clusters
            if any(
                self._source_analyzer._is_primary(source)
                for source in selected
                if source.id in cluster.member_source_ids
            )
        )
        secondary_count = max(0, independent_sources - primary_count)
        primary_ratio = primary_count / max(1, independent_sources)
        primary_secondary_balanced = (
            primary_count > 0
            and secondary_count > 0
            and primary_ratio
            >= (safe_probability(job.research_policy.target_primary_ratio, default=0.25) or 0.25)
        )
        dimension_coverage: dict[str, dict[str, Any]] = {}
        artifacts: list[dict[str, Any]] = []
        if job.compilation_task_id:
            with contextlib.suppress(Exception):
                artifacts = self.continuum.compilation.get_task(job.compilation_task_id).artifacts
        for dimension in job.research_policy.required_dimensions:
            matching = [item for item in artifacts if str(item.get("dimension")) == dimension]
            source_ids = {
                str(source_id) for item in matching for source_id in item.get("source_ids", [])
            }
            independent_ids = {
                cluster_by_source[source_id].cluster_id
                for source_id in source_ids
                if source_id in cluster_by_source
            }
            claim_count = sum(len(item.get("claims") or []) for item in matching)
            contradiction_count = sum(len(item.get("conflicts") or []) for item in matching)
            evidence_count = len(source_ids)
            independent_evidence = len(independent_ids)
            dimension_coverage[dimension] = {
                "evidence_count": evidence_count,
                "independent_evidence_count": independent_evidence,
                "claim_count": claim_count,
                "contradiction_count": contradiction_count,
                "confidence": min(
                    1.0,
                    independent_evidence / max(1, job.research_policy.min_sources_per_dimension),
                ),
                "remaining_gaps": []
                if independent_evidence >= job.research_policy.min_sources_per_dimension
                else ["independent_evidence"],
            }
        dimensions_complete = all(
            safe_int(
                item.get("independent_evidence_count", job.dimension_progress.get(dimension, 0)),
                default=0,
                minimum=0,
            )
            or job.research_policy.min_sources_per_dimension <= 0
            for dimension, item in dimension_coverage.items()
        )
        if not dimension_coverage:
            dimensions_complete = all(
                job.dimension_progress.get(dimension, 0)
                >= job.research_policy.min_sources_per_dimension
                for dimension in job.research_policy.required_dimensions
            )
        if life_stage_model is None and job.life_stages:
            life_stage_model = LifeStageModel.model_validate({"life_stages": job.life_stages})
        if life_stage_model:
            self._update_life_stage_progress(job, life_stage_model, selected)
        life_stage_requirements = {
            stage.id: max(job.research_policy.min_evidence_per_life_stage, stage.required_evidence)
            for stage in (life_stage_model.life_stages if life_stage_model else [])
        }
        life_stages_complete = (
            all(
                job.life_stage_progress.get(stage_id, 0) >= required
                for stage_id, required in life_stage_requirements.items()
            )
            if life_stage_requirements
            else not job.research_policy.require_life_stage_coverage
        )
        contradiction_data = ContradictionCoverage.model_validate(
            job.job_config.get("contradiction_coverage") or {}
        )
        negative_evidence = bool(contradiction_data.negative_evidence_found) or any(
            bool(item.get("conflicts")) or bool((item.get("uncertainty") or {}).get("notes"))
            for item in artifacts
        )
        if job.creation_mode in {"private_materials", "guided_interview"}:
            private = self._private_coverage(selected, job)
            material_layer: dict[str, Any] = {}
            if self.material_intelligence is not None and job.persona_id:
                with contextlib.suppress(Exception):
                    material = self.material_intelligence.coverage(job.persona_id)
                    material_layer = material.model_dump(mode="json")
                    private.conversation_message_count = max(
                        private.conversation_message_count, material.message_count
                    )
                    private.conversation_time_span_days = max(
                        private.conversation_time_span_days,
                        material.conversation_time_span_days,
                    )
                    private.behavioral_episode_count = max(
                        private.behavioral_episode_count, material.episode_count
                    )
                    private.distinct_relationship_contexts = max(
                        private.distinct_relationship_contexts,
                        material.relationship_context_count,
                    )
                    private.dimension_coverage = {
                        dimension: max(
                            safe_int(
                                private.dimension_coverage.get(dimension, 0),
                                default=0,
                                minimum=0,
                            )
                            or 0,
                            safe_int(
                                material.dimension_coverage.get(dimension, 0),
                                default=0,
                                minimum=0,
                            )
                            or 0,
                        )
                        for dimension in REQUIRED_DIMENSIONS
                    }
                    private.high_priority_gaps = sorted(
                        set(private.high_priority_gaps) | set(material.high_priority_gaps)
                    )
            job.private_coverage = private.model_dump(mode="json")
            private_complete = not private.high_priority_gaps
            passed = private_complete and dimensions_complete
            score = sum(bool(value) for value in (private_complete, dimensions_complete)) / 2
            return {
                "raw_source_count": len(selected),
                "source_count": len(selected),
                "unique_sources": independent_sources,
                "independent_sources": independent_sources,
                "independent_source_count": independent_sources,
                "source_categories": sorted(categories),
                "source_category_count": len(categories),
                "dimensions_complete": dimensions_complete,
                "dimensions": {
                    dimension: job.dimension_progress.get(dimension, 0)
                    for dimension in REQUIRED_DIMENSIONS
                },
                "dimension_coverage": dimension_coverage,
                "private_coverage": private.model_dump(mode="json"),
                "material_layer": material_layer,
                "negative_evidence": negative_evidence,
                "passed": passed,
                "score": score,
            }
        if job.creation_mode == "fictional":
            fictional = self._fictional_coverage(selected, job)
            job.private_coverage = fictional.model_dump(mode="json")
            complete = not fictional.high_priority_gaps
            return {
                "raw_source_count": len(selected),
                "source_count": len(selected),
                "unique_sources": independent_sources,
                "independent_sources": independent_sources,
                "independent_source_count": independent_sources,
                "dimensions_complete": complete,
                "dimensions": dict(job.dimension_progress),
                "dimension_coverage": dimension_coverage,
                "fictional_coverage": fictional.model_dump(mode="json"),
                "passed": complete,
                "score": 1.0 if complete else 0.5,
            }
        source_score = min(
            1.0, independent_sources / max(1, job.research_policy.min_unique_sources)
        )
        category_score = min(
            1.0,
            len(categories) / max(1, job.research_policy.min_source_categories),
        )
        dimension_score = sum(
            1
            for item in dimension_coverage.values()
            if (safe_int(item.get("independent_evidence_count", 0), default=0, minimum=0) or 0)
            >= job.research_policy.min_sources_per_dimension
        ) / max(1, len(job.research_policy.required_dimensions))
        quality_count = sum(
            self._source_analyzer.assess_quality(
                source, cluster_by_source.get(source.id)
            ).quality_score
            >= job.research_policy.quality_floor
            for source in canonical_sources
        )
        quality_assessments = [
            self._source_analyzer.assess_quality(
                source, cluster_by_source.get(source.id)
            ).model_dump(mode="json")
            for source in canonical_sources
        ]
        high_priority_gap = any(
            (safe_int(gap.get("priority", 0), default=0, minimum=0) or 0) >= 80
            for gap in job.research_gaps
            if isinstance(gap, dict)
        )
        passed = (
            dimensions_complete
            and independent_sources >= job.research_policy.min_unique_sources
            and len(categories) >= job.research_policy.min_source_categories
            and (life_stages_complete or not job.research_policy.require_life_stage_coverage)
            and (
                primary_secondary_balanced
                or not job.research_policy.require_primary_secondary_balance
            )
            and (
                contradiction_data.search_executed
                or not job.research_policy.require_contradiction_search
            )
            and not high_priority_gap
        )
        return {
            "raw_source_count": len(selected),
            "source_count": len(selected),
            "unique_sources": independent_sources,
            "independent_sources": independent_sources,
            "independent_source_count": independent_sources,
            "quality_source_count": quality_count,
            "source_quality": quality_assessments,
            "source_clusters": [cluster.model_dump(mode="json") for cluster in clusters],
            "source_categories": sorted(categories),
            "source_category_count": len(categories),
            "time_periods": sorted(event_years),
            "time_period_count": len(event_years),
            "primary_category_count": primary_count,
            "secondary_category_count": secondary_count,
            "primary_source_count": primary_count,
            "secondary_source_count": secondary_count,
            "primary_ratio": primary_ratio,
            "primary_secondary_balanced": primary_secondary_balanced,
            "dimensions_complete": dimensions_complete,
            "dimensions": {
                dimension: job.dimension_progress.get(dimension, 0)
                for dimension in REQUIRED_DIMENSIONS
            },
            "dimension_coverage": dimension_coverage,
            "life_stages": {
                stage_id: {
                    "evidence_count": job.life_stage_progress.get(stage_id, 0),
                    "required_evidence": required,
                    "complete": job.life_stage_progress.get(stage_id, 0) >= required,
                }
                for stage_id, required in life_stage_requirements.items()
            },
            "life_stage_coverage": dict(job.life_stage_progress),
            "life_stages_complete": life_stages_complete,
            "contradiction_coverage": contradiction_data.model_dump(mode="json"),
            "contradiction_search_executed": contradiction_data.search_executed,
            "negative_evidence": negative_evidence,
            "passed": passed,
            "score": (source_score + category_score + dimension_score) / 3,
        }

    def _persist_source_clusters(self, job: PersonaCreationJob, clusters: list[Any]) -> None:
        if not job.persona_id:
            return
        now = datetime.now(UTC).isoformat()
        conn = self.continuum.database.conn
        try:
            for cluster in clusters:
                conn.execute(
                    """
                INSERT INTO persona_source_clusters (
                  cluster_id, persona_id, origin_type, origin_identifier,
                  member_source_ids_json, canonical_source_id,
                  independence_confidence, reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, cluster_id) DO UPDATE SET
                  origin_type=excluded.origin_type,
                  origin_identifier=excluded.origin_identifier,
                  member_source_ids_json=excluded.member_source_ids_json,
                  canonical_source_id=excluded.canonical_source_id,
                  independence_confidence=excluded.independence_confidence,
                  reason=excluded.reason,
                  updated_at=excluded.updated_at
                """,
                    (
                        cluster.cluster_id,
                        job.persona_id,
                        cluster.origin_type,
                        cluster.origin_identifier,
                        dumps(cluster.member_source_ids),
                        cluster.canonical_source_id,
                        cluster.independence_confidence,
                        cluster.reason,
                        now,
                        now,
                    ),
                )
            conn.commit()
        except Exception:
            # Quality accounting remains usable for read-only/test stores that
            # predate the optional cluster table.
            with contextlib.suppress(Exception):
                conn.rollback()

    def _private_coverage(
        self, sources: list[Any], job: PersonaCreationJob
    ) -> PrivatePersonaCoverage:
        contents = [str(source.content or "") for source in sources]
        message_count = sum(
            (
                safe_int(
                    source.metadata.get("message_count") or source.metadata.get("messages"),
                    default=0,
                    minimum=0,
                )
                or 0
            )
            or max(1, len(re.findall(r"(?:^|\n)\s*[^:\n]{1,40}:", content)))
            for source, content in zip(sources, contents, strict=True)
        )
        dates = [
            match.group(0)
            for content in contents
            for match in re.finditer(r"\b(?:19|20)\d{2}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?\b", content)
        ]
        contexts = {
            str(source.metadata.get("context") or source.metadata.get("interaction_context") or "")
            for source in sources
            if source.metadata.get("context") or source.metadata.get("interaction_context")
        }
        relationships = {
            str(source.metadata.get("relationship") or source.metadata.get("participant") or "")
            for source in sources
            if source.metadata.get("relationship") or source.metadata.get("participant")
        }
        episodes = sum(
            safe_int(
                source.metadata.get("behavioral_episode_count") or source.metadata.get("episodes"),
                default=0,
                minimum=0,
            )
            or 0
            for source in sources
        )
        if not episodes:
            episodes = sum(
                len(
                    re.findall(
                        r"\b(?:when|during|after|before|遇到|面对|因为|于是)\b",
                        content,
                        flags=re.IGNORECASE,
                    )
                )
                for content in contents
            )
        answered = len(job.job_config.get("interview_answers") or [])
        span_days = 0
        if len(dates) >= 2:
            years = [safe_int(value[:4], default=0, minimum=0) or 0 for value in dates]
            span_days = max(0, (max(years) - min(years)) * 365)
        dimension_coverage = dict(job.dimension_progress)
        policy = PrivatePersonaCoveragePolicy(
            required_dimensions=job.research_policy.required_dimensions
        )
        gaps: list[str] = []
        if message_count < policy.message_volume_target:
            gaps.append("message_volume")
        if span_days < policy.min_time_span_days:
            gaps.append("conversation_time_span")
        if len(contexts) < policy.min_interaction_contexts:
            gaps.append("interaction_contexts")
        if len(relationships) < policy.min_relationship_contexts:
            gaps.append("relationship_contexts")
        if episodes < policy.min_behavioral_episodes:
            gaps.append("behavioral_episodes")
        return PrivatePersonaCoverage(
            conversation_message_count=message_count,
            conversation_time_span_days=span_days,
            distinct_context_count=len(contexts),
            distinct_relationship_contexts=len(relationships),
            behavioral_episode_count=episodes,
            guided_questions_answered=answered,
            dimension_coverage=dimension_coverage,
            high_priority_gaps=gaps,
        )

    def _fictional_coverage(
        self, sources: list[Any], job: PersonaCreationJob
    ) -> FictionalPersonaCoverage:
        contents = [str(source.content or "") for source in sources]
        text = "\n".join(contents)
        scene_count = sum(
            safe_int(
                source.metadata.get("scene_count") or source.metadata.get("scenes"),
                default=0,
                minimum=0,
            )
            or 0
            for source in sources
        ) or len(re.findall(r"(?:^|\n)\s*(?:场景|SCENE|INT\.|EXT\.)", text, flags=re.IGNORECASE))
        dialogue_count = sum(
            safe_int(
                source.metadata.get("dialogue_count") or source.metadata.get("dialogues"),
                default=0,
                minimum=0,
            )
            or 0
            for source in sources
        ) or len(re.findall(r"(?:^|\n)\s*[^:\n]{1,40}:", text))
        behavioral_events = sum(
            safe_int(
                source.metadata.get("behavioral_event_count") or source.metadata.get("events"),
                default=0,
                minimum=0,
            )
            or 0
            for source in sources
        ) or len(
            re.findall(
                r"\b(?:decides|refuses|chooses|fails|逃避|决定|拒绝|选择|失败)\b",
                text,
                flags=re.IGNORECASE,
            )
        )
        relationships = {
            str(source.metadata.get("relationship") or source.metadata.get("participant") or "")
            for source in sources
            if source.metadata.get("relationship") or source.metadata.get("participant")
        }
        settings = sum(
            safe_int(
                source.metadata.get("user_setting_count") or source.metadata.get("settings"),
                default=0,
                minimum=0,
            )
            or 0
            for source in sources
        )
        gaps: list[str] = []
        if len(text) < 2000:
            gaps.append("work_text_volume")
        if scene_count < 3:
            gaps.append("scenes")
        if dialogue_count < 6:
            gaps.append("dialogue")
        if behavioral_events < 4:
            gaps.append("behavioral_events")
        if len(relationships) < 2:
            gaps.append("relationships")
        return FictionalPersonaCoverage(
            work_text_characters=len(text),
            scene_count=scene_count,
            dialogue_count=dialogue_count,
            behavioral_event_count=behavioral_events,
            relationship_count=len(relationships),
            user_setting_count=settings,
            dimension_coverage=dict(job.dimension_progress),
            high_priority_gaps=gaps,
        )

    # ------------------------------------------------------------------
    # Agent/runtime helpers
    # ------------------------------------------------------------------

    async def _resolve_runtime(
        self,
        *,
        runtime_source: str,
        agent_id: str,
        model_id: str | None,
        reasoning_effort: str | None,
    ) -> tuple[AgentProbeResult, AgentAdapter, str, str | None]:
        if not agent_id:
            raise RuntimeBindingError("agent_id_required")
        probes = await self.continuum.agent_discovery.scan(force_refresh=False)
        probe = next((p for p in probes if p.id == agent_id), None)
        if probe is None or probe.status != AgentStatus.READY:
            raise RuntimeBindingError(f"runtime_not_ready:{agent_id}")
        if runtime_source and probe.runtime_source != runtime_source:
            raise RuntimeBindingError(
                f"runtime_source_mismatch:{runtime_source}:{probe.runtime_source}"
            )
        adapter = self.continuum.agent_registry.get_adapter(agent_id)
        if adapter is None:
            raise RuntimeBindingError(f"agent_adapter_missing:{agent_id}")
        models = [model for model in probe.models if model.selectable]
        if not models:
            raise RuntimeBindingError(f"model_capability_missing:{agent_id}")
        selected = (
            next((model for model in models if model.id == model_id), None)
            if model_id
            else models[0]
        )
        if selected is None:
            raise RuntimeBindingError(f"model_not_reported_by_agent:{model_id}")
        effort = reasoning_effort
        if str(effort or "").strip().casefold() in {"", "none", "default", "auto"}:
            effort = None
        capability = selected.reasoning_capability
        supported = {str(item).casefold() for item in capability.supported_efforts}
        if effort:
            normalized_effort = str(effort).strip().casefold()
            can_bind = (
                capability.mode
                in {
                    ReasoningCapabilityMode.NATIVE_EFFORT,
                    ReasoningCapabilityMode.MANUAL_CONFIG,
                }
                and normalized_effort in supported
                and (
                    capability.mode == ReasoningCapabilityMode.MANUAL_CONFIG or capability.verified
                )
            )
            if not can_bind:
                raise ReasoningBindingRejectedError(
                    "Selected reasoning effort has no verified runtime binding",
                    phase="session_binding",
                    diagnostics={
                        "protocol": str(probe.protocols[0] if probe.protocols else "unknown"),
                        "agent": adapter.adapter_id,
                        "requested_reasoning": effort,
                        "capability_mode": capability.mode.value,
                        "supported_efforts": list(capability.supported_efforts),
                        "binding_strategy": capability.binding_strategy,
                        "verified": capability.verified,
                    },
                )
        if effort is None and capability.mode in {
            ReasoningCapabilityMode.NATIVE_EFFORT,
            ReasoningCapabilityMode.MANUAL_CONFIG,
            ReasoningCapabilityMode.DEFAULT_ONLY,
        }:
            effort = capability.default_effort or selected.default_reasoning_effort
        if effort in {"none", "default", "auto", ""}:
            effort = None
        return probe, adapter, selected.id, effort

    async def _assert_snapshot_available(self, job: PersonaCreationJob) -> None:
        probe, _, _, _ = await self._resolve_runtime(
            runtime_source=job.runtime_source,
            agent_id=job.agent_id,
            model_id=job.model_id,
            reasoning_effort=job.reasoning_effort,
        )
        if job.agent_version and probe.version and job.agent_version != probe.version:
            raise RuntimeBindingError(f"agent_version_changed:{job.agent_version}:{probe.version}")

    @staticmethod
    def _requires_web_research(creation_mode: str, input_mode: str | None) -> bool:
        """Return whether this job is allowed to touch a web research backend."""

        return creation_mode == "public_research" and str(input_mode or "").lower() not in {
            "local_materials",
            "local_material",
        }

    async def _resolve_or_verify_research_runtime(
        self,
        *,
        probe: AgentProbeResult,
        adapter: AgentAdapter,
        runtime: dict[str, Any],
        job_id: str,
        force_revalidate: bool = False,
    ) -> ResearchBackend:
        resolver = ResearchBackendResolver(
            adapter=adapter,
            research=probe.research.model_dump(mode="json"),
            broker=self.research_broker,
            mcp=getattr(self.continuum, "research_mcp", None),
            capability_cache=self.research_capability_cache,
        )
        try:
            backend = await resolver.resolve(
                runtime=runtime,
                job_id=job_id,
                force_revalidate=force_revalidate or bool(runtime.get("force_revalidate")),
            )
        except RuntimeError as exc:
            # Resolver errors are intentionally specific (session start,
            # missing Web execution, or URL validation), never a generic
            # "not reported" message.
            verified = resolver.last_capability
            if verified is not None:
                probe.research = verified
            raise ResearchCapabilityError(str(exc)) from exc
        verified = resolver.last_capability
        if verified is not None:
            probe.research = verified
        return backend

    async def _resolve_research_backend(self, job: PersonaCreationJob) -> ResearchBackend:
        research = dict(job.capability_snapshot.get("research") or {})
        adapter = self.continuum.agent_registry.get_adapter(job.agent_id)
        resolver = ResearchBackendResolver(
            adapter=adapter,
            research=research,
            broker=self.research_broker,
            mcp=getattr(self.continuum, "research_mcp", None),
            capability_cache=self.research_capability_cache,
        )
        try:
            backend_runtime = self._research_runtime_payload(
                {
                    "runtime_source": job.runtime_source,
                    "agent_id": job.agent_id,
                    "agent_name": str(job.job_config.get("agent_name") or job.agent_id),
                    "agent_version": job.agent_version,
                    "runtime_status": "ready",
                    "model_id": job.model_id,
                    "reasoning_effort": job.reasoning_effort,
                    "auth_profile_id": job.auth_profile_id,
                },
                job.job_config,
            )
            backend = await resolver.resolve(
                runtime=backend_runtime,
                job_id=job.id,
                force_revalidate=bool(job.job_config.get("force_revalidate")),
            )
            if resolver.last_capability is not None:
                job.capability_snapshot["research"] = resolver.last_capability.model_dump(
                    mode="json"
                )
                job.job_config["research_capability_verification"] = {
                    "verification_status": resolver.last_capability.verification_status.value,
                    "verification_method": resolver.last_capability.verification_method,
                    "verified_at": resolver.last_capability.verified_at,
                    "error": resolver.last_capability.verification_error,
                }
                self._save(job)
            return backend
        except RuntimeError as exc:
            raise ResearchCapabilityError(str(exc)) from exc

    async def _resolve_research_backend_from_job(self, job: PersonaCreationJob) -> ResearchBackend:
        from persona_continuum.application.research_backend import CachingResearchBackend

        backend = await self._resolve_research_backend(job)
        # Shared caches + bounded fetch concurrency; cache misses fall through
        # to the resolved backend untouched, so verification semantics and
        # provenance behavior are identical.
        backend = CachingResearchBackend(
            backend,
            source_cache=self.research_source_cache,
            query_cache=self.research_query_cache,
            fetch_semaphore=None,
        )
        # Track pooled research worker sessions so the whole pool is released
        # when the job run terminates (complete, fail, or cancel).
        closer = getattr(backend, "aclose", None)
        if callable(closer):
            self._open_research_backends.setdefault(job.id, set()).add(backend)
        return backend

    async def close_research_backends(self, job_id: str) -> None:
        backends = self._open_research_backends.pop(job_id, set())
        for backend in backends:
            closer = getattr(backend, "aclose", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    await closer()

    async def _agent_json(
        self,
        job: PersonaCreationJob,
        *,
        user_message: str,
        system_prompt: str,
        participant_id: str = "persona_creation",
        phase: str | None = None,
        schema: Any | None = None,
    ) -> Any:
        effective_phase = str(phase or participant_id)
        result = await self._run_agent_with_heartbeat(
            job,
            user_message=user_message,
            system_prompt=system_prompt,
            participant_id=participant_id,
            phase=effective_phase,
            schema=schema or {"type": "object"},
        )
        if not isinstance(result, StructuredResult):
            raise PersonaAgentStructuredOutputError(
                "structured_output_result_missing",
                phase=effective_phase,
            )
        return result.value

    async def _agent_text(
        self,
        job: PersonaCreationJob,
        *,
        user_message: str,
        system_prompt: str,
        participant_id: str,
        phase: str | None = None,
    ) -> str:
        effective_phase = str(phase or participant_id)
        job.progress.set_operation(effective_phase)
        self._save(job)
        result = await self._run_agent_with_heartbeat(
            job,
            user_message=user_message,
            system_prompt=system_prompt,
            participant_id=participant_id,
            phase=effective_phase,
        )
        if not isinstance(result, AgentExecutionResult):
            raise PersonaAgentOutputError("text_execution_result_missing", phase=effective_phase)
        return result.text

    async def _run_agent_with_heartbeat(
        self,
        job: PersonaCreationJob,
        *,
        user_message: str,
        system_prompt: str,
        participant_id: str,
        phase: str,
        schema: Any | None = None,
    ) -> AgentExecutionResult | StructuredResult:
        """Expose WAITING_AGENT while the executor is inside a long call."""

        job.touch_worker(WorkerState.WAITING_AGENT)
        self._save(job)
        heartbeat = asyncio.create_task(
            self._worker_heartbeat_loop(job.id),
            name=f"persona-worker-heartbeat-{job.id}",
        )
        try:
            return await self._run_agent(
                job,
                user_message=user_message,
                system_prompt=system_prompt,
                participant_id=participant_id,
                phase=phase,
                schema=schema,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _run_agent(
        self,
        job: PersonaCreationJob,
        *,
        user_message: str,
        system_prompt: str,
        participant_id: str,
        phase: str | None = None,
        schema: Any | None = None,
    ) -> AgentExecutionResult | StructuredResult:
        effective_phase = str(phase or participant_id)
        job.progress.set_operation(effective_phase)
        self._save(job)
        await self._assert_snapshot_available(job)
        # Safe-pause boundary: never start a new Agent turn once a pause has
        # been requested; the current turn always finishes and checkpoints.
        self._raise_if_pause_requested(job)
        adapter = self.continuum.agent_registry.get_adapter(job.agent_id)
        if adapter is None:
            raise RuntimeBindingError(f"agent_adapter_missing:{job.agent_id}")
        # Prompt diagnostics: expose what is about to be dispatched so a
        # "model quota not moving" symptom can be attributed immediately
        # (waiting on scheduler vs. transport limit vs. model actually running).
        transport = resolve_prompt_transport_capability(adapter)
        estimated_prompt_bytes = len(
            ((system_prompt or "") + (user_message or "")).encode("utf-8")
        )
        binding_snapshot = job.job_config.get("runtime_binding_snapshot") or {}
        model_context_limit = safe_int(
            binding_snapshot.get("context_window"), default=None, minimum=1
        )
        if model_context_limit is None:
            model_context_limit = safe_int(
                (job.capability_snapshot.get("capabilities") or {}).get("context_window"),
                default=None,
                minimum=1,
            )
        job.progress.prompt_info = {
            "estimated_prompt_chars": len(system_prompt or "") + len(user_message or ""),
            "estimated_prompt_bytes": estimated_prompt_bytes,
            "model_context_limit": model_context_limit,
            "transport_mode": transport.transport_mode,
            "transport_max_prompt_bytes": transport.max_prompt_bytes,
            "transport_safe_prompt_bytes": transport.safe_prompt_bytes,
        }
        session_binding: RuntimeSessionBinding | None = None
        # Logical sessions live for the whole job per participant instead of
        # being re-spawned per model call.  The physical runtime underneath is
        # pooled; threads are never shared between participants/dimensions.
        session_key = (job.id, participant_id)
        reused = False
        try:
            cached_binding = self._job_sessions.get(session_key)
            if (
                cached_binding is not None
                and cached_binding.session.is_active
                and cached_binding.session.config.model_id == (job.model_id or None)
                and cached_binding.session.config.auth_profile_id == job.auth_profile_id
            ):
                session_binding = cached_binding
                reused = True
            if session_binding is None:
                job.progress.set_prompt_state("STARTING_SESSION")
                session_extra: dict[str, Any] = {"source_ids": list(job.source_ids)}
                for key in (
                    "idle_timeout_seconds",
                    "hard_timeout_seconds",
                    "turn_timeout_seconds",
                    "acp_stream_limit_bytes",
                ):
                    if job.job_config.get(key) is not None:
                        session_extra[key] = job.job_config[key]
                stream_limit = job.job_config.get("acp_stream_limit_bytes")
                if stream_limit is not None:
                    session_extra["acp_stream_limit_bytes"] = safe_acp_stream_limit(stream_limit)
                try:
                    session_binding = await self.runtime_executor.open_session(
                        adapter,
                        AgentSessionConfig(
                            session_id=f"persona_creation_{job.id}_{participant_id}",
                            room_id=f"persona_creation:{job.id}",
                            participant_id=participant_id,
                            persona_id=job.persona_id or job.id,
                            model_id=job.model_id,
                            reasoning_effort=job.reasoning_effort,
                            auth_profile_id=job.auth_profile_id,
                            allow_mcp=False,
                            tools=[],
                            system_prompt=system_prompt,
                            extra=session_extra,
                        ),
                    )
                except AgentRuntimeError:
                    raise
                except Exception as exc:
                    raise AgentSessionStartError(
                        f"agent_session_start_failed:{sanitize_diagnostic(exc)}",
                        phase=effective_phase,
                        diagnostics={"agent_id": job.agent_id, "model_id": job.model_id},
                    ) from exc
                self._job_sessions[session_key] = session_binding
                self._job_session_keys.setdefault(job.id, set()).add(session_key)
            assert session_binding is not None
            job.job_config["runtime_binding_snapshot"] = session_binding.snapshot.model_dump(
                mode="json"
            )
            job.progress.set_runtime_binding(job.job_config["runtime_binding_snapshot"])
            if not reused:
                self._save(job)
            self._active_bindings[job.id] = session_binding
            call_id = new_id("agent_call")
            job.progress.set_prompt_state("WAITING_RUNTIME")
            job.progress.begin_agent_call()
            job.agent_call_count = job.progress.agent_call_attempt_count
            job.job_config["agent_call_in_flight"] = {
                "call_id": call_id,
                "phase": effective_phase,
                "started_at": job.progress.updated_at,
            }
            self._touch_worker(job, WorkerState.WAITING_AGENT)

            def on_runtime_state(state: str) -> None:
                # Mirrors the executor's dispatch lifecycle onto the durable
                # job progress: WAITING_SCHEDULER -> SENDING_PROMPT ->
                # MODEL_RUNNING.  A job can only show MODEL_RUNNING after the
                # Adapter dispatch actually completed.
                job.progress.set_prompt_state(state)
                if state == "MODEL_RUNNING":
                    job.progress.dispatch_metrics = {
                        **job.progress.dispatch_metrics,
                        "model_started_at": progress_now(),
                    }
                    self._save(job)

            try:
                call_metadata = {
                    "persona_creation_job_id": job.id,
                    "participant_id": participant_id,
                    "stage": effective_phase,
                    "phase": effective_phase,
                    "call_id": call_id,
                }
                if schema is None:
                    result = await self.runtime_executor.execute_text(
                        session_binding,
                        system_prompt=system_prompt,
                        user_message=user_message,
                        expected_output=None,
                        phase=effective_phase,
                        stream=False,
                        metadata=call_metadata,
                        state_callback=on_runtime_state,
                    )
                else:
                    result = await self.runtime_executor.execute_structured(
                        session_binding,
                        system_prompt=system_prompt,
                        user_message=user_message,
                        schema=schema,
                        phase=effective_phase,
                        metadata=call_metadata,
                        state_callback=on_runtime_state,
                    )
                self._sync_runtime_activity(job, session_binding)
                response = result.response
                if response is not None:
                    self._record_agent_call(job, response, call_id=call_id, phase=effective_phase)
                else:
                    job.progress.record_agent_outcome(failed=False)
                    job.job_config.pop("agent_call_in_flight", None)
                    self._save(job)
                # The dispatch finished; the state is stale until the next
                # call starts building its prompt.
                job.progress.set_prompt_state(None)
                return result
            except AgentRuntimeError as exc:
                self._sync_runtime_activity(job, session_binding)
                response = getattr(exc, "response", None)
                if response is not None:
                    self._record_agent_call(
                        job,
                        response,
                        call_id=call_id,
                        phase=effective_phase,
                        failure=exc,
                    )
                else:
                    self._record_agent_failure_without_response(
                        job, exc, call_id=call_id, phase=effective_phase
                    )
                # A transport/runtime failure invalidates the cached logical
                # session; the next attempt gets a fresh thread on a healthy
                # runtime (crash recovery instead of poisoning the pool).
                await self._drop_job_session(job.id, participant_id)
                raise
            except Exception as exc:
                self._sync_runtime_activity(job, session_binding)
                wrapped = AgentTransportError(
                    "agent_transport_failed",
                    phase=effective_phase,
                    diagnostics={
                        "protocol": session_binding.snapshot.protocol
                        if session_binding is not None
                        else "unknown",
                        "exception_type": type(exc).__name__,
                        "diagnostic": sanitize_diagnostic(exc),
                    },
                )
                self._record_agent_failure_without_response(
                    job, wrapped, call_id=call_id, phase=effective_phase
                )
                await self._drop_job_session(job.id, participant_id)
                raise wrapped from exc
        finally:
            if session_binding is not None:
                self._active_bindings.pop(job.id, None)

    async def _drop_job_session(self, job_id: str, participant_id: str) -> None:
        """Close and forget one cached job session (used after failures)."""

        session_key = (job_id, participant_id)
        binding = self._job_sessions.pop(session_key, None)
        keys = self._job_session_keys.get(job_id)
        if keys is not None:
            keys.discard(session_key)
        if binding is not None:
            with contextlib.suppress(Exception):
                await self.runtime_executor.close(binding)

    async def close_job_sessions(self, job_id: str) -> None:
        """Release every logical session held by a finished/cancelled job."""

        keys = list(self._job_session_keys.pop(job_id, set()))
        for session_key in keys:
            binding = self._job_sessions.pop(session_key, None)
            if binding is not None:
                with contextlib.suppress(Exception):
                    await self.runtime_executor.close(binding)

    async def _broker_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        if self.research_broker is None:
            raise ResearchCapabilityError("research_broker_not_configured")
        pending_result: object = self.research_broker.search(query, limit=limit)
        if inspect.isawaitable(pending_result):
            result = await pending_result
        else:
            result = pending_result
        return [dict(item) for item in (result or []) if isinstance(item, dict)]

    async def _broker_fetch(self, url: str) -> dict[str, Any] | str:
        if self.research_broker is None:
            return ""
        pending_result: object = self.research_broker.fetch(url)
        if inspect.isawaitable(pending_result):
            result = await pending_result
        else:
            result = pending_result
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return dict(result)
        raise PersonaCreationError("research_broker_fetch_invalid")

    async def _normalise_research_result(
        self, job: PersonaCreationJob, result: dict[str, Any]
    ) -> dict[str, Any] | None:
        url = str(
            result.get("canonical_url") or result.get("url") or result.get("link") or ""
        ).strip()
        title = str(result.get("title") or result.get("name") or url).strip()
        if not url and not result.get("content"):
            return None
        return {
            "canonical_url": url,
            "title": title,
            "publisher": result.get("publisher") or result.get("source") or "",
            "author": result.get("author") or "",
            "published_at": result.get("published_at") or result.get("date"),
            "content": result.get("content") or result.get("text") or "",
            "snippet": result.get("snippet") or "",
            "source_type": result.get("source_type") or "web",
            "category": result.get("category") or result.get("source_category") or "web",
            "hash": result.get("hash") or "",
            "event_time": result.get("event_time")
            or result.get("event_date")
            or result.get("occurred_at"),
            "origin_identifier": result.get("origin_identifier")
            or result.get("original_url")
            or result.get("source_chain"),
            "citation": result.get("citation") or result.get("source_identity"),
            "is_primary": result.get("is_primary") or result.get("first_person"),
        }

    def _merge_fetched_source(
        self, source: dict[str, Any], fetched: dict[str, Any] | str
    ) -> dict[str, Any]:
        if isinstance(fetched, str):
            return {**source, "content": fetched}
        return {
            **source,
            **fetched,
            "content": fetched.get("content") or fetched.get("text") or source.get("content"),
        }

    def _record_agent_call(
        self,
        job: PersonaCreationJob,
        response: Any,
        *,
        call_id: str,
        phase: str,
        failure: AgentRuntimeError | None = None,
    ) -> None:
        """Persist bounded Agent Call Audit data, never the full prompt."""

        audit = response.audit(call_id=call_id, job_id=job.id, phase=phase)
        audit["agent_id"] = job.agent_id
        audit["model_id"] = job.model_id
        audit["reasoning_effort"] = job.reasoning_effort
        audit["status"] = "failed" if failure else audit.get("status", "ok")
        if failure:
            audit["failure_code"] = getattr(failure, "code", "AGENT_RUNTIME_ERROR")
            audit["failure"] = failure.as_failure()
        job.agent_call_audits.append(audit)
        job.agent_call_audits = job.agent_call_audits[-100:]
        job.progress.record_agent_outcome(failed=failure is not None)
        job.progress.agent_call_count = job.agent_call_count
        job.job_config.pop("agent_call_in_flight", None)
        job.job_config["agent_call_audits"] = list(job.agent_call_audits)
        job.job_config["agent_last_event"] = response.last_event_type
        job.job_config["agent_protocol"] = response.protocol
        self._touch_worker(job, WorkerState.RUNNING)
        self._save(job)

    def _record_agent_failure_without_response(
        self,
        job: PersonaCreationJob,
        failure: AgentRuntimeError,
        *,
        call_id: str,
        phase: str,
    ) -> None:
        """Persist a bounded failed attempt when no response audit exists."""

        diagnostics = dict(failure.diagnostics)
        audit = {
            "call_id": call_id,
            "job_id": job.id,
            "phase": phase,
            "agent_id": job.agent_id,
            "model_id": job.model_id,
            "reasoning_effort": job.reasoning_effort,
            "protocol": diagnostics.get("protocol"),
            "last_event_type": diagnostics.get("last_event_type"),
            "status": "failed",
            "failure_code": getattr(failure, "code", "AGENT_RUNTIME_ERROR"),
            "failure": failure.as_failure(),
            # Where the dispatch was when it failed: WAITING_SCHEDULER and
            # MODEL_RUNNING failures have completely different root causes.
            "prompt_state": job.progress.prompt_state,
            "prompt_info": dict(job.progress.prompt_info),
        }
        job.agent_call_audits.append(audit)
        job.agent_call_audits = job.agent_call_audits[-100:]
        job.progress.record_agent_outcome(failed=True)
        job.progress.agent_call_count = job.agent_call_count
        job.job_config.pop("agent_call_in_flight", None)
        job.job_config["agent_call_audits"] = list(job.agent_call_audits)
        if diagnostics.get("last_event_type") is not None:
            job.job_config["agent_last_event"] = diagnostics["last_event_type"]
        if diagnostics.get("protocol") is not None:
            job.job_config["agent_protocol"] = diagnostics["protocol"]
        self._touch_worker(job, WorkerState.RUNNING)
        self._save(job)

    def _mark_failure(
        self, job: PersonaCreationJob, exc: BaseException, *, status: str, stage: str
    ) -> None:
        completed_percent = max(
            job.progress.percent,
            self._extraction_percent(job) if job.dimension_progress else 0,
        )
        if isinstance(exc, AgentRuntimeError):
            diagnostics = dict(exc.diagnostics)
            failure = JobFailure(
                code=str(diagnostics.get("code") or exc.code),
                message=str(exc),
                phase=exc.phase,
                runtime={
                    "runtime_source": job.runtime_source,
                    "agent_id": job.agent_id,
                    "model_id": job.model_id,
                    "reasoning_effort": job.reasoning_effort,
                },
                protocol=diagnostics.get("protocol") or job.job_config.get("agent_protocol"),
                last_event_type=diagnostics.get("last_event_type")
                or job.job_config.get("agent_last_event"),
                event_counts=dict(diagnostics.get("event_counts") or {}),
                stderr_tail=str(diagnostics.get("stderr_tail") or "")[-2000:],
                retriable=bool(exc.retriable),
                configured_limit_bytes=(
                    safe_int(diagnostics["configured_limit_bytes"], default=0, minimum=0)
                    if diagnostics.get("configured_limit_bytes") is not None
                    else None
                ),
                observed_frame_bytes=(
                    safe_int(diagnostics["observed_frame_bytes"], default=0, minimum=0)
                    if diagnostics.get("observed_frame_bytes") is not None
                    else None
                ),
                frame_bytes=(
                    safe_int(diagnostics["frame_bytes"], default=0, minimum=0)
                    if diagnostics.get("frame_bytes") is not None
                    else None
                ),
                frame_type=diagnostics.get("frame_type"),
                event_type=diagnostics.get("event_type"),
                tool_name=diagnostics.get("tool_name"),
                diagnostics=diagnostics,
            )
            job.job_config["failure"] = exc.as_failure()
        elif isinstance(exc, InvalidNumericFieldError):
            failure = JobFailure(
                code=exc.code,
                message=str(exc),
                phase=exc.phase
                or str(job.progress.current_operation or job.current_stage or stage),
                runtime={
                    "runtime_source": job.runtime_source,
                    "agent_id": job.agent_id,
                    "model_id": job.model_id,
                    "reasoning_effort": job.reasoning_effort,
                },
                retriable=False,
                field=exc.field,
                received_type=exc.received_type,
            )
        else:
            progress_contract_error = (
                isinstance(exc, TypeError)
                and "JobProgress" in str(exc)
                and "item assignment" in str(exc)
            )
            explicit_code = getattr(exc, "failure_code", None)
            resolved_code = (
                "JOB_PROGRESS_CONTRACT_ERROR"
                if progress_contract_error
                else str(explicit_code or classify_persona_failure(exc))
            )
            failure = JobFailure(
                code=resolved_code,
                message=sanitize_diagnostic(exc),
                phase=str(job.progress.current_operation or stage),
                runtime={"runtime_source": job.runtime_source, "agent_id": job.agent_id},
                retriable=bool(
                    progress_contract_error
                    or is_retryable_failure_code(resolved_code)
                    or is_retryable_failure(
                        {"code": resolved_code, "message": sanitize_diagnostic(exc)}
                    )
                ),
                diagnostics={"exception_type": type(exc).__name__},
            )
        failure_phase = str(failure.phase or job.progress.current_operation or stage)
        if failure.phase is None:
            failure.phase = failure_phase
        job.failure_json = failure.model_dump(mode="json")
        job.progress.failure = failure
        job.status = status
        job.current_stage = stage
        job.progress.set_operation(failure_phase)
        job.touch_worker(
            WorkerState.PAUSED if status == "paused_runtime_unavailable" else WorkerState.FAILED,
            finished_at=datetime.now(UTC).isoformat(),
        )
        failure_label = {
            "failed": "任务失败",
            "failed_quality_gate": "质量门禁未通过",
            "paused_runtime_unavailable": "运行时不可用",
        }.get(status, "任务失败")
        job.progress.update_stage(
            stage,
            label=failure_label,
            percent=completed_percent,
            message=str(exc),
        )
        job.error = str(exc)
        self._save(job)

    def _checkpoint(self, job: PersonaCreationJob, stage: str, **payload: Any) -> None:
        checkpoint = {
            "stage": stage,
            "timestamp": datetime.now(UTC).isoformat(),
            "source_count": job.source_count,
            "dimension_progress": dict(job.dimension_progress),
            **payload,
        }
        job.checkpoints.append(checkpoint)
        job.checkpoints = job.checkpoints[-50:]
        job.job_config["last_checkpoint"] = checkpoint
        job.touch_worker(WorkerState.RUNNING)
        self._save(job)

    async def _set_stage(self, job: PersonaCreationJob, status: str, stage: str) -> None:
        """Enter a new pipeline stage; also a hard safe-pause boundary.

        A stage transition always means new work is about to start, so a pause
        requested while the previous stage was finishing must stop the pipeline
        here instead of letting it walk into research / dimensions / audit /
        compile.
        """

        if status in self.WORK_STATUSES:
            self._raise_if_pause_requested(job.id)
        job.status = status
        job.current_stage = stage
        job.progress.update_stage(
            stage,
            label={
                "planning": "正在规划研究",
                "researching": "正在搜索公开资料",
                "ingesting_sources": "正在读取资料",
                "extracting": "正在生成八维 ResearchArtifact",
                "retry_dimension_reuse": "正在复用上次已完成的八维提取结果",
                "compiling": "正在编译 Persona",
            }.get(stage, stage),
            percent=percent_for_stage(
                stage,
                completed=job.source_count,
                total=job.research_policy.preferred_source_target,
                web=status == "researching",
            ),
            completed=job.source_count,
            total=job.research_policy.preferred_source_target if status == "researching" else None,
        )
        job.touch_worker(
            WorkerState.WAITING_IO
            if status in {"waiting_for_materials", "waiting_io"}
            else WorkerState.RUNNING
        )
        self._save(job)
        event_name = {
            "planning": "persona_research_plan_created",
            "researching": "persona_search_started",
            "ingesting_sources": "persona_source_ingested",
            "extracting": "persona_dimension_started",
            "compiling": "persona_compilation_started",
        }.get(status)
        if event_name:
            await self._emit(job, event_name, stage=stage)

    # Events whose loss would break resume/observability invariants force an
    # immediate full-row snapshot instead of waiting out the debounce window.
    _FORCE_SAVE_EVENTS = frozenset(
        {
            "persona_creation_started",
            "persona_creation_completed",
            "persona_creation_failed",
            "persona_creation_cancelled",
            "persona_creation_runtime_unavailable",
            "persona_compilation_completed",
            "persona_coverage_updated",
            "world_persona_completed",
            "world_actor_persona_bound",
            "persona_dimension_failed",
        }
    )

    async def _debounced_save(self, job: PersonaCreationJob, *, force: bool = False) -> None:
        """Full-row snapshots are debounced; events themselves stay realtime.

        ``_save`` rewrites ~20 JSON columns per call and historically ran once
        (or more) per ingested source/event, dominating IO during deep
        research.  Subscribers still receive every event instantly; the
        durable row converges within ``job_snapshot_debounce_seconds``.
        """

        raw_interval_ms = safe_int(
            getattr(self.continuum.config, "job_snapshot_debounce_seconds", 0.5) * 1000,
            default=500,
            minimum=0,
        )
        interval = max(0.0, (raw_interval_ms or 0) / 1000.0)
        if interval <= 0:
            self._save(job)
            return
        async with self._gate_lock:
            gate = self._save_gates.setdefault(job.id, {"last": 0.0, "pending": None})
            loop = asyncio.get_running_loop()
            now = loop.time()
            pending_task: asyncio.Task[None] | None = gate["pending"]
            if force or now - gate["last"] >= interval:
                if pending_task is not None and not pending_task.done():
                    pending_task.cancel()
                gate["pending"] = None
                gate["last"] = now
                with contextlib.suppress(Exception):
                    self._save(job)
                return
            if pending_task is not None and not pending_task.done():
                return

            async def _flush() -> None:
                try:
                    await asyncio.sleep(interval)
                    async with self._gate_lock:
                        gate_state = self._save_gates.get(job.id)
                        if gate_state is not None:
                            gate_state["last"] = loop.time()
                            gate_state["pending"] = None
                        with contextlib.suppress(Exception):
                            self._save(job)

                except asyncio.CancelledError:
                    raise

            gate["pending"] = loop.create_task(_flush())

    async def close_job_persistence(self, job_id: str) -> None:
        """Cancel any scheduled debounced flush and persist final state."""

        async with self._gate_lock:
            gate = self._save_gates.pop(job_id, None)
            if gate is not None and gate.get("pending") is not None:
                task: asyncio.Task[None] | None = gate["pending"]
                if task is not None and not task.done():
                    task.cancel()

    async def _emit(self, job: PersonaCreationJob, event_type: str, **payload: Any) -> None:
        event = {
            "event": event_type,
            "type": event_type,
            "job_id": job.id,
            "timestamp": datetime.now(UTC).isoformat(),
            **payload,
        }
        job.events.append(event)
        job.events = job.events[-300:]
        job.updated_at = datetime.now(UTC).isoformat()
        await self._debounced_save(job, force=event_type in self._FORCE_SAVE_EVENTS)
        for queue in list(self._subscribers.get(job.id, set())):
            with contextlib.suppress(Exception):
                queue.put_nowait(event)
        for callback in list(self._event_callbacks):
            with contextlib.suppress(Exception):
                result = callback(event)
                if inspect.isawaitable(result):
                    await result

    def _sync_control_mirror(self, job: PersonaCreationJob) -> None:
        """Re-apply the authoritative control state onto the job snapshot.

        A worker holds one ``PersonaCreationJob`` object for a long time.  An
        HTTP pause request writes to the control plane, not to that object, so
        every save must refresh the mirror from the control plane -- otherwise
        the worker's next progress save would silently delete the pause
        request.  The mirror exists only for the UI and for legacy readers;
        the control table is the single source of truth.
        """

        control = self.job_control.get(job.id)
        if control.pause_requested:
            job.job_config["pause_requested"] = True
            job.job_config["pause_requested_at"] = control.pause_requested_at
        else:
            job.job_config.pop("pause_requested", None)
            job.job_config.pop("pause_requested_at", None)
        if control.cancel_requested:
            job.job_config["cancel_requested"] = True
        else:
            job.job_config.pop("cancel_requested", None)
        job.job_config["control_version"] = control.control_version

    def _save(self, job: PersonaCreationJob) -> None:
        now = datetime.now(UTC).isoformat()
        job.updated_at = now
        if not job.created_at:
            job.created_at = now
        # Control-plane state always wins over whatever this (possibly stale)
        # in-memory object believes.
        with contextlib.suppress(Exception):
            self._sync_control_mirror(job)
        conn = self.continuum.database.conn
        conn.execute(
            """
            INSERT INTO persona_creation_jobs (
              id, display_name, aliases_json, persona_type, creation_mode, status,
              runtime_source, agent_id, model_id, reasoning_effort, auth_profile_id,
              agent_version, capability_snapshot_json, persona_id, compilation_task_id,
              research_policy_json, source_count, source_ids_json, dimension_progress_json,
              current_stage, coverage_json, error, job_config_json, events_json,
              interview_questions_json, life_stage_progress_json, life_stages_json,
              information_gain_json, research_gaps_json, research_checkpoints_json,
              research_stop_reason, query_history_json, private_coverage_json,
              progress_json, failure_json, agent_call_audits_json,
              checkpoints_json,
              visibility, dismissed_at, superseded_by,
              worker_state, worker_started_at, worker_heartbeat_at,
              worker_finished_at, agent_call_count,
              created_at, updated_at
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(id) DO UPDATE SET
              display_name=excluded.display_name, aliases_json=excluded.aliases_json,
              status=excluded.status, runtime_source=excluded.runtime_source,
              agent_id=excluded.agent_id, model_id=excluded.model_id,
              reasoning_effort=excluded.reasoning_effort, auth_profile_id=excluded.auth_profile_id,
              agent_version=excluded.agent_version,
              capability_snapshot_json=excluded.capability_snapshot_json,
              persona_id=excluded.persona_id, compilation_task_id=excluded.compilation_task_id,
              research_policy_json=excluded.research_policy_json,
              source_count=excluded.source_count,
              source_ids_json=excluded.source_ids_json,
              dimension_progress_json=excluded.dimension_progress_json,
              current_stage=excluded.current_stage, coverage_json=excluded.coverage_json,
              error=excluded.error, job_config_json=excluded.job_config_json,
              events_json=excluded.events_json,
              interview_questions_json=excluded.interview_questions_json,
              life_stage_progress_json=excluded.life_stage_progress_json,
              life_stages_json=excluded.life_stages_json,
              information_gain_json=excluded.information_gain_json,
              research_gaps_json=excluded.research_gaps_json,
              research_checkpoints_json=excluded.research_checkpoints_json,
              research_stop_reason=excluded.research_stop_reason,
              query_history_json=excluded.query_history_json,
              private_coverage_json=excluded.private_coverage_json,
              progress_json=excluded.progress_json,
              failure_json=excluded.failure_json,
              agent_call_audits_json=excluded.agent_call_audits_json,
              checkpoints_json=excluded.checkpoints_json,
              visibility=excluded.visibility,
              dismissed_at=excluded.dismissed_at,
              superseded_by=excluded.superseded_by,
              worker_state=excluded.worker_state,
              worker_started_at=excluded.worker_started_at,
              worker_heartbeat_at=excluded.worker_heartbeat_at,
              worker_finished_at=excluded.worker_finished_at,
              agent_call_count=excluded.agent_call_count,
              updated_at=excluded.updated_at
            """,
            (
                job.id,
                job.display_name,
                dumps(job.aliases),
                job.persona_type.value,
                job.creation_mode,
                job.status,
                job.runtime_source,
                job.agent_id,
                job.model_id,
                job.reasoning_effort,
                job.auth_profile_id,
                job.agent_version,
                dumps(job.capability_snapshot),
                job.persona_id,
                job.compilation_task_id,
                dumps(job.research_policy.model_dump(mode="json")),
                job.source_count,
                dumps(job.source_ids),
                dumps(job.dimension_progress),
                job.current_stage,
                dumps(job.coverage),
                job.error,
                dumps(job.job_config),
                dumps(job.events),
                dumps(job.interview_questions),
                dumps(job.life_stage_progress),
                dumps(job.life_stages),
                dumps(job.information_gain),
                dumps(job.research_gaps),
                dumps(job.research_checkpoints),
                job.research_stop_reason,
                dumps(job.query_history),
                dumps(job.private_coverage),
                dumps(job.progress.model_dump(mode="json")),
                dumps(job.failure_json) if job.failure_json else None,
                dumps(job.agent_call_audits),
                dumps(job.checkpoints),
                job.visibility,
                job.dismissed_at,
                job.superseded_by,
                job.worker_state.value,
                job.worker_started_at,
                job.worker_heartbeat_at,
                job.worker_finished_at,
                job.agent_call_count,
                job.created_at,
                now,
            ),
        )
        conn.commit()

    def _row_to_job(self, row: Any) -> PersonaCreationJob:
        row_keys = set(row.keys())
        return PersonaCreationJob(
            id=str(row["id"]),
            display_name=str(row["display_name"]),
            aliases=list(loads(row["aliases_json"])),
            persona_type=PersonaType(str(row["persona_type"])),
            creation_mode=str(row["creation_mode"]),
            status=str(row["status"]),
            visibility=str(row["visibility"] or JobVisibility.USER.value)
            if "visibility" in row_keys
            else JobVisibility.USER.value,
            dismissed_at=(row["dismissed_at"] if "dismissed_at" in row_keys else None),
            superseded_by=(row["superseded_by"] if "superseded_by" in row_keys else None),
            worker_state=WorkerState(str(row["worker_state"] or WorkerState.STARTING.value))
            if "worker_state" in row_keys
            else WorkerState.STARTING,
            worker_started_at=(
                row["worker_started_at"] if "worker_started_at" in row_keys else None
            ),
            worker_heartbeat_at=(
                row["worker_heartbeat_at"] if "worker_heartbeat_at" in row_keys else None
            ),
            worker_finished_at=(
                row["worker_finished_at"] if "worker_finished_at" in row_keys else None
            ),
            agent_call_count=(
                safe_int(row["agent_call_count"], default=0, minimum=0) or 0
                if "agent_call_count" in row_keys
                else 0
            ),
            runtime_source=str(row["runtime_source"]),
            agent_id=str(row["agent_id"]),
            model_id=str(row["model_id"]),
            reasoning_effort=row["reasoning_effort"],
            auth_profile_id=row["auth_profile_id"],
            agent_version=row["agent_version"],
            capability_snapshot=dict(loads(row["capability_snapshot_json"])),
            persona_id=row["persona_id"],
            compilation_task_id=row["compilation_task_id"],
            research_policy=ResearchPolicy.model_validate(loads(row["research_policy_json"]) or {}),
            source_count=safe_int(row["source_count"], default=0, minimum=0) or 0,
            source_ids=list(loads(row["source_ids_json"])),
            dimension_progress=dict(loads(row["dimension_progress_json"])),
            current_stage=str(row["current_stage"]),
            coverage=dict(loads(row["coverage_json"])),
            error=row["error"],
            job_config=dict(loads(row["job_config_json"])),
            events=list(loads(row["events_json"])),
            interview_questions=list(loads(row["interview_questions_json"])),
            life_stage_progress=dict(loads(row["life_stage_progress_json"] or "{}")),
            life_stages=list(loads(row["life_stages_json"] or "[]")),
            information_gain=list(loads(row["information_gain_json"] or "[]")),
            research_gaps=list(loads(row["research_gaps_json"] or "[]")),
            research_checkpoints=list(loads(row["research_checkpoints_json"] or "[]")),
            research_stop_reason=row["research_stop_reason"],
            query_history=dict(loads(row["query_history_json"] or "{}")),
            private_coverage=dict(loads(row["private_coverage_json"] or "{}")),
            progress=JobProgress.model_validate(loads(row["progress_json"] or "{}")),
            failure_json=(dict(loads(row["failure_json"])) if row["failure_json"] else None),
            agent_call_audits=list(loads(row["agent_call_audits_json"] or "[]")),
            checkpoints=list(loads(row["checkpoints_json"] or "[]")),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _validate_mode(
        self, persona_type: PersonaType, mode: str, enrichment_input_mode: str | None = None
    ) -> None:
        allowed = {
            "public_research",
            "private_materials",
            "guided_interview",
            "fictional",
        }
        if mode not in allowed:
            raise PersonaCreationError(f"unknown_creation_mode:{mode}")
        if (
            persona_type.value.startswith("public_")
            and mode != "public_research"
            and enrichment_input_mode not in {"local_materials", "hybrid"}
        ):
            raise PersonaCreationError("public_person_requires_public_research")
        if persona_type.value.startswith("private_") and mode not in {
            "private_materials",
            "guided_interview",
        }:
            raise PersonaCreationError("private_person_requires_private_materials_or_interview")
        if persona_type == PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON and mode != "fictional":
            raise PersonaCreationError("fictional_person_requires_fictional_mode")

    def _run_mode_for(self, persona_type: PersonaType) -> RunMode:
        if (
            persona_type.value.startswith("private_")
            or persona_type == PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON
        ):
            return RunMode.DIGITAL_CONTINUATION
        return RunMode.COUNTERFACTUAL_CONTINUATION

    def _find_duplicate(self, display_name: str, aliases: list[str]) -> Any | None:
        names = {self._normalise_name(display_name), *(self._normalise_name(a) for a in aliases)}
        names.discard("")
        for persona in self.continuum.personas.list(include_archived=True):
            existing = {
                self._normalise_name(persona.id),
                self._normalise_name(persona.display_name),
                *(self._normalise_name(a) for a in persona.manifest.aliases),
            }
            if names & existing:
                return persona
        return None

    def _persona_by_id(self, personas: list[Any], persona_id: str) -> Any | None:
        return next((persona for persona in personas if persona.id == persona_id), None)

    def _normalise_name(self, value: str) -> str:
        return re.sub(r"[\W_]+", "", str(value).casefold(), flags=re.UNICODE)


__all__ = [
    "AdaptiveResearchStopGate",
    "ContradictionCoverage",
    "DuplicatePersonaError",
    "DeepPersonaResearchPolicy",
    "FictionalPersonaCoverage",
    "InformationGainSnapshot",
    "LifeStageModel",
    "MarginalInformationGainTracker",
    "PersonaCreationError",
    "PersonaAgentOutputError",
    "PersonaAgentStructuredOutputError",
    "PersonaCreationJob",
    "PersonaCreationOrchestrator",
    "PersonaCreationStatus",
    "PersonaMatch",
    "PrivateMaterialConsentRequired",
    "PrivatePersonaCoverage",
    "PrivatePersonaCoveragePolicy",
    "ResearchCapabilityError",
    "ResearchCheckpoint",
    "ResearchGap",
    "ResearchGapAnalyzer",
    "ResearchPolicy",
    "ResearchRichnessEstimator",
    "ResearchToolBroker",
    "SourceIndependenceAnalyzer",
    "RuntimeBindingError",
    "WorldPersonaCompletionResult",
]
