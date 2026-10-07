"""Hierarchical Summaries: the long time scale of a shared history.

Episodes answer "what happened in this stretch of conversation".  A Chapter
answers "what phase were we in", and a Long-term segment answers "what have we
been through together".  The three layers exist at the same time and none
replaces another:

* a Fact records what is true and when it stopped being true;
* a Thread records what is still in flight;
* a Chapter / Long-term Summary records **what a period was like**, at a scale
  no single Episode can express, and quotes the stages a Fact went through
  ("early on they preferred 茉莉奶绿, later they switched to 美式") instead of
  freezing one value.

Four properties are load-bearing:

1. **Grounded.**  A summary only says what its sources already say.  Anything
   the model concluded rather than read belongs in ``inferences``, and the
   service validates the rest against the source corpus before a summary may
   become READY.
2. **Traceable.**  Long-term -> Chapter -> Episode -> Turn -> raw text is
   walkable through ``memory_summary_sources``; a summary that cannot be traced
   is not a summary.
3. **Progressive, never recursive.**  A Chapter is built from its real Episodes,
   a Long-term segment from its real Chapters.  A previous summary is never the
   only input to the next level, which is what stops drift.
4. **Level-agnostic.**  ``level`` is an integer; level 1 groups Episodes, level 2
   groups level-1 summaries, and nothing in the contract caps the depth at 2.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from persona_continuum.domain.persona import utc_now
from persona_continuum.numeric import safe_float

#: Version of the summarisation contract.  Bump when the content schema or the
#: boundary rules change in a way that makes older summaries incomparable.
HIERARCHY_CONSOLIDATION_VERSION = 1

_MAX_ITEM_CHARS = 400
_MAX_TITLE_CHARS = 120
_MAX_SUMMARY_CHARS = 8000
_MAX_ITEMS = 24
_MAX_ENTITIES = 32


class SummaryType(StrEnum):
    """Level 1 is a Chapter; everything above it is a Long-term segment."""

    CHAPTER = "chapter"
    LONG_TERM = "long_term"

    @classmethod
    def from_raw(cls, value: Any) -> SummaryType:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.CHAPTER


class SummaryStatus(StrEnum):
    """Where a summary sits in its own lifecycle (independent of readiness)."""

    OPEN = "open"
    CLOSED = "closed"
    #: Every source was explicitly deleted; the row is kept as a tombstone
    #: rather than pretending the period never existed.
    RETRACTED = "retracted"

    @classmethod
    def from_raw(cls, value: Any) -> SummaryStatus:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.OPEN


class SummaryReadiness(StrEnum):
    """How much the stored text can be trusted.

    ``PROVISIONAL`` exists because an OPEN chapter may be summarised early; a
    provisional text must never be read as final history, and closing the
    chapter regenerates it from the full source set.

    ``STALE`` is the Phase 6.1 addition: the stored text was correct for the
    sources it was built from, but a source has changed underneath it since
    (a Chapter went failed -> ready, an Episode joined the range, a child
    summary was re-consolidated).  It is still readable, but it may no longer
    be read as *current* history, and it owes a refresh -- so it is treated as
    owed work everywhere PENDING is.  A summary is only ever moved to STALE
    from a text-bearing state, which is what stops a stale summary from
    silently passing itself off as final.
    """

    PENDING = "pending"
    PROVISIONAL = "provisional"
    READY = "ready"
    STALE = "stale"
    FAILED = "failed"

    @classmethod
    def from_raw(cls, value: Any) -> SummaryReadiness:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.PENDING


class SummarySourceType(StrEnum):
    EPISODE = "episode"
    SUMMARY = "summary"

    @classmethod
    def from_raw(cls, value: Any) -> SummarySourceType:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.EPISODE


class SummaryBoundaryReason(StrEnum):
    """Why a summary's source range ends where it does."""

    NONE = "none"
    FIRST_SOURCE = "first_source"
    APPEND = "append"
    MAX_SOURCES = "max_sources"
    MAX_TOKENS = "max_tokens"
    MAX_TIMESPAN = "max_timespan"
    INACTIVITY = "inactivity"
    TOPIC_SHIFT = "topic_shift"
    EXPLICIT_CLOSE = "explicit_close"


class SummaryAction(StrEnum):
    """What one grouping pass did."""

    CREATE = "create"
    APPEND = "append"
    CLOSE = "close"
    NOOP = "noop"


def summary_type_for_level(level: int) -> SummaryType:
    return SummaryType.CHAPTER if int(level) <= 1 else SummaryType.LONG_TERM


def source_type_for_level(level: int) -> SummarySourceType:
    return SummarySourceType.EPISODE if int(level) <= 1 else SummarySourceType.SUMMARY


def _clean_items(value: Any, *, limit: int = _MAX_ITEMS) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates: list[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        return []
    cleaned: list[str] = []
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        cleaned.append(text[:_MAX_ITEM_CHARS])
        if len(cleaned) >= limit:
            break
    return list(dict.fromkeys(cleaned))


class SummaryContent(BaseModel):
    """Schema-validated structured content of one summary.

    Never trust raw model JSON.  Every list is bounded, every string truncated,
    and unknown keys are ignored so an off-schema answer degrades instead of
    crashing a background pass.
    """

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    summary: str = ""
    major_events: list[str] = Field(default_factory=list)
    relationship_changes: list[str] = Field(default_factory=list)
    important_decisions: list[str] = Field(default_factory=list)
    important_commitments: list[str] = Field(default_factory=list)
    important_preferences_or_fact_changes: list[str] = Field(default_factory=list)
    resolved_threads: list[str] = Field(default_factory=list)
    ongoing_threads: list[str] = Field(default_factory=list)
    emotional_arc: list[str] = Field(default_factory=list)
    unresolved_topics: list[str] = Field(default_factory=list)
    key_entities: list[str] = Field(default_factory=list)
    time_range: str = ""
    importance: float = 0.5
    #: Statements the summariser CONCLUDED rather than read.  Kept separate on
    #: purpose: the Fact store stays the semantic authority, and an inference
    #: must never be smuggled in as a factual assertion.
    inferences: list[str] = Field(default_factory=list)

    @field_validator(
        "major_events",
        "relationship_changes",
        "important_decisions",
        "important_commitments",
        "important_preferences_or_fact_changes",
        "resolved_threads",
        "ongoing_threads",
        "emotional_arc",
        "unresolved_topics",
        "inferences",
        mode="before",
    )
    @classmethod
    def _clean_lists(cls, value: Any) -> list[str]:
        return _clean_items(value)

    @field_validator("key_entities", mode="before")
    @classmethod
    def _clean_entities(cls, value: Any) -> list[str]:
        return _clean_items(value, limit=_MAX_ENTITIES)

    @field_validator("title", mode="before")
    @classmethod
    def _clean_title(cls, value: Any) -> str:
        return str(value or "").strip()[:_MAX_TITLE_CHARS]

    @field_validator("summary", "time_range", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return str(value or "").strip()[:_MAX_SUMMARY_CHARS]

    @field_validator("importance", mode="before")
    @classmethod
    def _clean_probability(cls, value: Any) -> float:
        normalized = safe_float(value, default=None, minimum=0.0, maximum=1.0)
        return 0.5 if normalized is None else float(normalized)

    @property
    def is_empty(self) -> bool:
        return not (self.title or self.summary or self.major_events)

    def grounded_fields(self) -> list[tuple[str, str]]:
        """Field/value pairs that must be traceable to the sources."""

        pairs: list[tuple[str, str]] = []
        for field_name in (
            "major_events",
            "relationship_changes",
            "important_decisions",
            "important_commitments",
            "important_preferences_or_fact_changes",
            "resolved_threads",
            "ongoing_threads",
            "unresolved_topics",
        ):
            for item in getattr(self, field_name):
                pairs.append((field_name, item))
        if self.summary:
            pairs.append(("summary", self.summary))
        return pairs


class HierarchicalSummary(BaseModel):
    """A persisted Chapter / Long-term segment row."""

    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    counterpart_id: str
    branch_id: str = "main"
    level: int = 1
    summary_type: SummaryType = SummaryType.CHAPTER
    sequence: int = 1
    title: str = ""
    summary: str = ""
    content: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    status: SummaryStatus = SummaryStatus.OPEN
    source_count: int = 0
    source_token_estimate: int = 0
    importance: float = 0.5
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    consolidation_version: int = HIERARCHY_CONSOLIDATION_VERSION
    summary_status: SummaryReadiness = SummaryReadiness.PENDING
    consolidation_attempts: int = 0
    last_error: str | None = None
    consolidated_at: datetime | None = None
    source_range_hash: str = ""
    #: Identity of the source set *including* the state of every source (its own
    #: range/version/content).  Recomputable at any time from the sources, which
    #: is what makes "did anything underneath me change?" an arithmetic question
    #: rather than a guess.
    source_fingerprint: str = ""
    #: The fingerprint that was current when the stored text was generated.
    #: Staleness is exactly ``source_fingerprint != consolidated_fingerprint``,
    #: so a retry or a reopen that reproduces the same source state produces no
    #: work (Phase 6.1, A3).
    consolidated_fingerprint: str = ""
    parent_summary_id: str | None = None
    visibility: str = "private_session"
    material_scope: str = "character_visible"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status is SummaryStatus.OPEN

    @property
    def is_closed(self) -> bool:
        return self.status is SummaryStatus.CLOSED

    @property
    def is_ready(self) -> bool:
        return self.summary_status is SummaryReadiness.READY

    @property
    def needs_consolidation(self) -> bool:
        """Whether the stored text is missing or behind its sources.

        A PROVISIONAL text is not owed work: it describes the range it was built
        from, and ``_append_source`` puts the summary back to PENDING the moment
        that range changes.  Closing the summary does the same, so a FINAL is
        always generated from the complete source set.  STALE (Phase 6.1) is
        owed work for the same reason PENDING is: the text no longer matches
        what it claims to describe.
        """

        return self.summary_status in {
            SummaryReadiness.PENDING,
            SummaryReadiness.FAILED,
            SummaryReadiness.STALE,
        }

    @property
    def is_provisional(self) -> bool:
        return self.summary_status is SummaryReadiness.PROVISIONAL

    @property
    def is_stale(self) -> bool:
        """Text exists, but a source changed after it was generated."""

        return self.summary_status is SummaryReadiness.STALE

    @property
    def has_text(self) -> bool:
        """Whether the stored text may be read at all (current or not)."""

        return self.summary_status in {
            SummaryReadiness.READY,
            SummaryReadiness.PROVISIONAL,
            SummaryReadiness.STALE,
        }

    @property
    def fingerprint_mismatch(self) -> bool:
        """Whether the recorded source state differs from the current one.

        An empty ``consolidated_fingerprint`` means "never recorded" (a row
        written by an older build, or one that never produced text); that is
        NOT a mismatch, or every legacy row would look stale on upgrade.
        """

        if not self.consolidated_fingerprint:
            return False
        return self.source_fingerprint != self.consolidated_fingerprint

    @property
    def structured(self) -> SummaryContent:
        if not self.content:
            return SummaryContent()
        try:
            return SummaryContent.model_validate(self.content)
        except ValueError:
            return SummaryContent()

    def time_range_label(self) -> str:
        if self.ended_at is None:
            return f"{self.started_at:%Y-%m-%d} ~ open"
        if self.started_at.date() == self.ended_at.date():
            return f"{self.started_at:%Y-%m-%d}"
        return f"{self.started_at:%Y-%m-%d} ~ {self.ended_at:%Y-%m-%d}"


class SummarySource(BaseModel):
    """One real source of a summary (an Episode, or a lower-level summary)."""

    model_config = ConfigDict(extra="ignore")

    summary_id: str
    source_type: SummarySourceType = SummarySourceType.EPISODE
    source_id: str
    position: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    importance: float = 0.5


def summary_range_hash(
    *, persona_id: str, counterpart_id: str, branch_id: str, level: int, source_ids: list[str]
) -> str:
    """Stable identity of one source range.

    Replaying the same Episodes through grouping must land on the SAME row;
    this hash is what the unique index is built on, so "September Chapter" can
    never become "September Chapter 2 / 3 / 4".
    """

    payload = json.dumps(
        [
            persona_id,
            counterpart_id,
            branch_id,
            int(level),
            *[str(item) for item in source_ids],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def summary_source_fingerprint(
    *,
    persona_id: str,
    counterpart_id: str,
    branch_id: str,
    level: int,
    sources: Sequence[tuple[str, str, str]],
    consolidation_version: int = HIERARCHY_CONSOLIDATION_VERSION,
) -> str:
    """Identity of "these sources, in this state" (Phase 6.1, A3).

    ``sources`` is an ORDERED sequence of ``(source_type, source_id,
    source_version)``.  ``source_version`` is the source's own identity plus the
    state that the parent's text depends on: an Episode contributes its range
    hash, its readiness and a hash of its summary content; a child summary
    contributes its own ``source_fingerprint``, its readiness and a hash of its
    text.  The ``consolidation_version`` is part of the payload on purpose --
    when the summarisation contract changes, every parent built under the older
    contract is genuinely out of date.

    This is what makes "do I need to rebuild?" cheap and exact: replay the same
    Episodes and the same content and the fingerprint is byte-identical, so a
    retry or a reopen produces no invalidation (and no model call).
    """

    payload = json.dumps(
        [
            persona_id,
            counterpart_id,
            branch_id,
            int(level),
            int(consolidation_version),
            [[str(kind), str(sid), str(version)] for kind, sid, version in sources],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def text_fingerprint(text: str) -> str:
    """Content hash used as a source's "version" contribution."""

    normalized = " ".join(str(text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "HIERARCHY_CONSOLIDATION_VERSION",
    "HierarchicalSummary",
    "SummaryAction",
    "SummaryBoundaryReason",
    "SummaryContent",
    "SummaryReadiness",
    "SummarySource",
    "SummarySourceType",
    "SummaryStatus",
    "SummaryType",
    "source_type_for_level",
    "summary_range_hash",
    "summary_source_fingerprint",
    "summary_type_for_level",
    "text_fingerprint",
]
