"""MemoryBundle: unified candidate memory package for one prompt turn.

Memory semantics decide **what a persona remembers**; context retrieval decides
**what memory is relevant to this turn**; and context policy decides **what the
current model can see**.

A MemoryBundle is the output of the Context Retrieval layer (Memory Retrieval
Planner).  It is a candidate collection of structured memory records, NOT a
prompt string, and it deliberately does NOT decide the final prompt token count:
Context Policy maps the bundle into the actual prompt budget of the active
profile (local_constrained, balanced, or remote_quality).
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from persona_continuum.domain.episode import EpisodeStatus
from persona_continuum.domain.hierarchical_summary import SummaryReadiness, SummaryType
from persona_continuum.domain.raw_recall import HistoricalExcerpt
from persona_continuum.domain.semantic_fact import (
    FactCategory,
    FactOrigin,
    FactStatus,
    PlanStatus,
)
from persona_continuum.domain.thread import ThreadStatus, ThreadType

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
CHARS_PER_TOKEN = 4


def estimate_bundle_tokens(text: Any) -> int:
    """CJK-aware token estimator for bundle candidates."""
    if not text:
        return 0
    raw = str(text)
    cjk = len(_CJK.findall(raw))
    return cjk + -(-max(0, len(raw) - cjk) // CHARS_PER_TOKEN)


class RetrievalMode(StrEnum):
    """How deeply the retrieval planner expanded memory for this turn."""

    #: Routine background retrieval: active relationship, live threads,
    #: current-valid high-confidence facts, current arc summary, recent raw dialogue.
    BASE = "base"

    #: Broad retrieval triggered by entities, past events, plans, commitments,
    #: time expressions, or implicit thread continuation cues.  Adds episodes,
    #: temporal facts, thread milestones, and chapters.
    STANDARD = "standard"

    #: Deep evidence retrieval triggered by explicit past detail inquiries
    #: ("当时", "几点", "原话", specific error code/verbatim quote) or high-relevance
    #: memory hits where summaries lack granular detail.  Adds provenance-backed
    #: HistoricalExcerpts via RawRecall.
    DEEP = "deep"


class FactReliability(StrEnum):
    """Reliability label conveyed in prompt for each retrieved fact."""

    CONFIRMED_USER = "confirmed_user"
    CONFIRMED_PERSONA = "confirmed_persona"
    SYSTEM_OBSERVED = "system_observed"
    INFERRED = "inferred"
    HISTORICAL_SUPERSEDED = "historical_superseded"
    UNCERTAIN_CANDIDATE = "uncertain_candidate"


class BundleFactItem(BaseModel):
    """A semantic fact candidate included in the MemoryBundle."""

    model_config = ConfigDict(extra="ignore")

    id: str
    fact_key: str = ""
    subject: str = ""
    predicate: str = ""
    value: str = ""
    display_text: str = ""
    status: FactStatus = FactStatus.ACTIVE
    origin: FactOrigin = FactOrigin.INFERRED
    reliability: FactReliability = FactReliability.INFERRED
    confidence: float = 0.5
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_valid_now: bool = True
    category: FactCategory = FactCategory.OTHER
    plan_status: PlanStatus = PlanStatus.NOT_APPLICABLE
    relevance_score: float = 0.0
    provenance_strength: float = 0.0
    source_turn_ids: list[str] = Field(default_factory=list)
    source_episode_ids: list[str] = Field(default_factory=list)
    token_estimate: int = 0

    def clean_display(self) -> str:
        """User-safe display text without leaking internal database IDs."""
        if self.display_text:
            return self.display_text
        if self.subject and self.predicate and self.value:
            return f"{self.subject} {self.predicate}: {self.value}"
        return self.value or self.fact_key


class BundleThreadItem(BaseModel):
    """An active or resolved thread candidate in the MemoryBundle."""

    model_config = ConfigDict(extra="ignore")

    id: str
    thread_key: str = ""
    title: str = ""
    summary: str = ""
    thread_type: ThreadType = ThreadType.GENERAL
    status: ThreadStatus = ThreadStatus.ACTIVE
    is_live: bool = True
    current_state: dict[str, Any] = Field(default_factory=dict)
    milestones: list[str] = Field(default_factory=list)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    relevance_score: float = 0.0
    source_episode_ids: list[str] = Field(default_factory=list)
    source_turn_ids: list[str] = Field(default_factory=list)
    token_estimate: int = 0

    def clean_display(self) -> str:
        """User-safe display without internal IDs."""
        parts = [self.title]
        if self.summary and self.summary != self.title:
            parts.append(self.summary)
        if self.milestones:
            parts.append("里程碑: " + " / ".join(self.milestones))
        return " — ".join(parts)


class BundleEpisodeItem(BaseModel):
    """An episode candidate in the MemoryBundle."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str = ""
    summary: str = ""
    status: EpisodeStatus = EpisodeStatus.CLOSED
    started_at: datetime | None = None
    ended_at: datetime | None = None
    time_range_display: str = ""
    topics: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    user_stated: list[str] = Field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.5
    relevance_score: float = 0.0
    source_turn_ids: list[str] = Field(default_factory=list)
    token_estimate: int = 0

    def clean_display(self) -> str:
        """User-safe display without internal IDs."""
        time_str = f"[{self.time_range_display}] " if self.time_range_display else ""
        title_str = f"{self.title}: " if self.title else ""
        return f"{time_str}{title_str}{self.summary}"


class BundleHierarchicalSummaryItem(BaseModel):
    """A chapter or long-term summary candidate in the MemoryBundle."""

    model_config = ConfigDict(extra="ignore")

    id: str
    level: int = 1
    summary_type: SummaryType = SummaryType.CHAPTER
    title: str = ""
    summary: str = ""
    readiness: SummaryReadiness = SummaryReadiness.READY
    is_provisional: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None
    time_range_display: str = ""
    relevance_score: float = 0.0
    source_episode_ids: list[str] = Field(default_factory=list)
    token_estimate: int = 0

    def clean_display(self) -> str:
        """User-safe display without internal IDs."""
        prefix = "（阶段性总结）" if self.is_provisional else ""
        time_str = f"[{self.time_range_display}] " if self.time_range_display else ""
        title_str = f"{self.title}: " if self.title else ""
        return f"{prefix}{time_str}{title_str}{self.summary}"


class BundleRelationshipContext(BaseModel):
    """Relationship context between persona and counterpart."""

    model_config = ConfigDict(extra="ignore")

    current_state: dict[str, Any] = Field(default_factory=dict)
    relationship_kind: str = ""
    stance: str = ""
    recent_events_summary: str = ""
    token_estimate: int = 0


class RetrievalTimings(BaseModel):
    """Microsecond-level latency instrumentation for MemoryBundle construction."""

    model_config = ConfigDict(extra="ignore")

    bundle_build_ms: float = 0.0
    fact_query_ms: float = 0.0
    thread_query_ms: float = 0.0
    episode_query_ms: float = 0.0
    summary_query_ms: float = 0.0
    raw_recall_ms: float = 0.0
    total_retrieval_ms: float = 0.0


class RetrievalMetadata(BaseModel):
    """Metadata detailing why and how memories were gathered for this turn."""

    model_config = ConfigDict(extra="ignore")

    mode: RetrievalMode = RetrievalMode.BASE
    reasons: list[str] = Field(default_factory=list)
    query: str = ""
    timings: RetrievalTimings = Field(default_factory=RetrievalTimings)
    recall_gate_triggered: bool = False
    recall_gate_reasons: list[str] = Field(default_factory=list)
    counts_available: dict[str, int] = Field(default_factory=dict)
    counts_selected: dict[str, int] = Field(default_factory=dict)
    dedup_removed: dict[str, int] = Field(default_factory=dict)


class MemoryBundle(BaseModel):
    """Unified runtime candidate memory bundle assembled for one turn.

    This is an ephemeral in-memory container produced before prompt composition.
    It is NEVER stored back into the `memories` table as a persistent memory.
    """

    model_config = ConfigDict(extra="ignore")

    persona_id: str
    counterpart_id: str
    branch_id: str = "main"

    current_arc: str | None = None
    current_arc_tokens: int = 0

    recent_dialogue: list[dict[str, Any]] = Field(default_factory=list)
    recent_dialogue_tokens: int = 0

    semantic_facts: list[BundleFactItem] = Field(default_factory=list)
    active_threads: list[BundleThreadItem] = Field(default_factory=list)
    relevant_episodes: list[BundleEpisodeItem] = Field(default_factory=list)
    relationship_context: BundleRelationshipContext = Field(
        default_factory=BundleRelationshipContext
    )
    hierarchical_summaries: list[BundleHierarchicalSummaryItem] = Field(
        default_factory=list
    )
    historical_excerpts: list[HistoricalExcerpt] = Field(default_factory=list)

    retrieval_metadata: RetrievalMetadata = Field(default_factory=RetrievalMetadata)

    @property
    def total_candidates(self) -> int:
        return (
            len(self.semantic_facts)
            + len(self.active_threads)
            + len(self.relevant_episodes)
            + len(self.hierarchical_summaries)
            + len(self.historical_excerpts)
        )

    def total_memory_tokens(self) -> int:
        """Estimated token sum of all candidate memory items in this bundle."""
        return (
            self.current_arc_tokens
            + sum(item.token_estimate for item in self.semantic_facts)
            + sum(item.token_estimate for item in self.active_threads)
            + sum(item.token_estimate for item in self.relevant_episodes)
            + sum(item.token_estimate for item in self.hierarchical_summaries)
            + sum(excerpt.token_estimate for excerpt in self.historical_excerpts)
            + self.relationship_context.token_estimate
        )


__all__ = [
    "BundleEpisodeItem",
    "BundleFactItem",
    "BundleHierarchicalSummaryItem",
    "BundleRelationshipContext",
    "BundleThreadItem",
    "FactReliability",
    "MemoryBundle",
    "RetrievalMetadata",
    "RetrievalMode",
    "RetrievalTimings",
    "estimate_bundle_tokens",
]
