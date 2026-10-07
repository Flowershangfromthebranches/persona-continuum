"""Episode domain model: a bounded stretch of shared experience.

An Episode is NOT a message and NOT one user/persona pair.  It is the answer to
"what happened between these two people around this time", and it is the layer
that turns a flat stream of committed turns into something a persona can
remember as *an event*:

    turn 1..n  ->  Episode("讨论考研与重庆理工", 14:02-14:41)

Three properties are load-bearing:

1. **Provenance-backed.**  An Episode owns no facts of its own.  It points at
   the committed turns that produced it (``source_turn_ids``), and the raw
   transcript stays the single source of truth in L0.  A summary that cannot be
   traced back to turns is not an Episode.
2. **Scoped.**  Episodes are isolated by persona, counterpart and branch.  A
   conversation with counterpart A can never be appended to counterpart B's
   episode, and a diverged branch can never leak into ``main``.
3. **Faithful.**  The structured summary separates what the user stated, what
   the persona said, and what was only inferred.  The persona's own guess
   ("你肯定就是舍不得她") must never be promoted to a user fact.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from persona_continuum.domain.persona import utc_now
from persona_continuum.numeric import safe_float, safe_int

#: Version of the consolidation contract.  Bump it when the summary schema or
#: the boundary rules change in a way that makes older summaries incomparable.
EPISODE_CONSOLIDATION_VERSION = 1


class EpisodeStatus(StrEnum):
    """Lifecycle of one Episode.

    ``OPEN``                  accepting appends; no final summary yet.
    ``CLOSED``                boundary reached and the summary is READY.
    ``PENDING_CONSOLIDATION`` boundary reached, summary still owed (durable:
                              the row survives a crash, so a restart resumes it).
    ``FAILED``                the last consolidation attempt failed; retryable,
                              and the raw turns are untouched.
    """

    OPEN = "open"
    CLOSED = "closed"
    PENDING_CONSOLIDATION = "pending_consolidation"
    FAILED = "failed"


class BoundaryReason(StrEnum):
    """Why an Episode ended.  Deterministic and explainable by construction."""

    NONE = "none"
    FIRST_TURN = "first_turn"
    APPEND = "append"
    IDLE_GAP = "idle_gap"
    MAX_TURNS = "max_turns"
    MAX_TOKENS = "max_tokens"
    SCOPE_CHANGED = "scope_changed"
    EXPLICIT_CLOSE = "explicit_close"
    SESSION_ENDED = "session_ended"


class ConsolidationAction(StrEnum):
    """What one commit did to the Episode ledger."""

    CREATE = "create"
    APPEND = "append"
    CLOSE = "close"
    CONSOLIDATE = "consolidate"
    RETRY = "retry"
    NOOP = "noop"


#: Cap on every generated list.  A model that ignores the schema must not be
#: able to write an unbounded blob into the database.
_MAX_SUMMARY_ITEMS = 24
_MAX_ITEM_CHARS = 400
_MAX_TITLE_CHARS = 120
_MAX_SUMMARY_CHARS = 4000


def _clean_items(value: Any) -> list[str]:
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
        if len(cleaned) >= _MAX_SUMMARY_ITEMS:
            break
    return list(dict.fromkeys(cleaned))


class EpisodeSummary(BaseModel):
    """Schema-validated episode summary.  Never trust raw model JSON.

    ``user_stated`` / ``persona_stated`` / ``inferred_context`` exist so a later
    Semantic-Fact layer can tell a confirmed fact from a persona's guess without
    re-reading the transcript.
    """

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    summary: str = ""
    important_events: list[str] = Field(default_factory=list)
    commitments: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    emotional_arc: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    #: Statements the USER actually made in this episode.
    user_stated: list[str] = Field(default_factory=list)
    #: Things the PERSONA said.  May include the persona's interpretation; it is
    #: never evidence about the user.
    persona_stated: list[str] = Field(default_factory=list)
    #: Context inferred rather than stated.  Must never be read as fact.
    inferred_context: list[str] = Field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.5

    @field_validator(
        "important_events",
        "commitments",
        "unresolved",
        "emotional_arc",
        "topics",
        "entities",
        "user_stated",
        "persona_stated",
        "inferred_context",
        mode="before",
    )
    @classmethod
    def _clean_lists(cls, value: Any) -> list[str]:
        return _clean_items(value)

    @field_validator("title", mode="before")
    @classmethod
    def _clean_title(cls, value: Any) -> str:
        return str(value or "").strip()[:_MAX_TITLE_CHARS]

    @field_validator("summary", mode="before")
    @classmethod
    def _clean_summary(cls, value: Any) -> str:
        return str(value or "").strip()[:_MAX_SUMMARY_CHARS]

    @field_validator("importance", "confidence", mode="before")
    @classmethod
    def _clean_probability(cls, value: Any) -> float:
        normalized = safe_float(value, default=None, minimum=0.0, maximum=1.0)
        return 0.5 if normalized is None else float(normalized)

    @property
    def is_empty(self) -> bool:
        return not (self.title or self.summary or self.important_events)


class EpisodeTurn(BaseModel):
    """One committed turn inside an Episode, with resolvable provenance."""

    model_config = ConfigDict(extra="ignore")

    episode_id: str
    turn_id: str
    #: ``session_turn`` -> ``session_turns.id``; ``room_transcript`` ->
    #: ``room_transcripts.turn_id``.  Which store to resolve the raw text from.
    source_kind: str = "session_turn"
    position: int = 0
    session_id: str | None = None
    room_id: str | None = None
    speaker: str = ""
    occurred_at: datetime | None = None
    token_estimate: int = 0


class ResolvedTurnMessage(BaseModel):
    """One speakable half of a resolved turn.

    A ``session_turn`` holds BOTH sides of the exchange in one row, so the raw
    layer must be able to present them as two messages without inventing a
    third speaker opinion.  This is structure over an authoritative row, not
    inference: ``speaker`` comes from :meth:`EpisodeService.turn_speaker_label`
    and the text is the column verbatim.
    """

    model_config = ConfigDict(extra="ignore")

    turn_id: str
    source_kind: str = "session_turn"
    speaker: str = ""
    raw_text: str = ""
    occurred_at: datetime | None = None
    position: int = 0
    room_id: str | None = None
    session_id: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.raw_text.strip()


class MemoryEpisode(BaseModel):
    """A persisted Episode row."""

    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    counterpart_id: str
    branch_id: str = "main"
    #: The conversation container that owns the turns.  Authoritative scope key:
    #: ``session_turns`` references it, and it is stable across process restarts.
    session_id: str
    #: Present only when the conversation happened inside a room.  A room is a
    #: runtime/UI container, not an identity scope -- but it is what
    #: ``room_transcripts`` is keyed by, so provenance needs it.
    room_id: str | None = None
    sequence: int = 1
    title: str = ""
    summary: str = ""
    summary_json: dict[str, Any] = Field(default_factory=dict)
    status: EpisodeStatus = EpisodeStatus.OPEN
    #: Why this Episode is bounded.  For a CLOSED Episode it is the trigger
    #: that ended it; for an OPEN one it is the trigger that started it.
    #: ``metadata["start_reason"]`` / ``metadata["close_reason"]`` carry the two
    #: halves explicitly so the overload is never ambiguous.
    boundary_reason: str = BoundaryReason.NONE.value
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    turn_count: int = 0
    source_token_estimate: int = 0
    source_first_turn_id: str | None = None
    source_last_turn_id: str | None = None
    source_range_hash: str = ""
    importance: float = 0.5
    confidence: float = 0.5
    consolidation_version: int = EPISODE_CONSOLIDATION_VERSION
    summary_status: str = "pending"
    consolidation_attempts: int = 0
    last_error: str | None = None
    consolidated_at: datetime | None = None
    #: The Episode row is also the durable work list for the layers derived from
    #: it.  These mirror real columns (never metadata): a failed attempt must
    #: stay visible and retryable across a restart, and a ready one must be
    #: skippable without re-reading JSON.
    fact_extraction_status: str = "pending"
    fact_extraction_attempts: int = 0
    fact_extraction_error: str | None = None
    facts_extracted_at: datetime | None = None
    thread_resolution_status: str = "pending"
    thread_resolution_attempts: int = 0
    thread_resolution_error: str | None = None
    threads_resolved_at: datetime | None = None
    visibility: str = "private_session"
    provenance: str = "digital_experience"
    material_scope: str = "character_visible"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def summary_ready(self) -> bool:
        return self.summary_status == "ready"

    @property
    def pending_consolidation(self) -> bool:
        """Whether this episode still owes a summary.

        ``FAILED`` counts as pending: the requirement is that a failed
        consolidation stays retryable, never that it becomes a terminal state.
        """

        return not self.summary_ready

    @property
    def is_open(self) -> bool:
        return self.status is EpisodeStatus.OPEN

    @property
    def facts_ready(self) -> bool:
        return self.fact_extraction_status == "ready"

    @property
    def threads_resolved(self) -> bool:
        return self.thread_resolution_status == "ready"

    def structured_summary(self) -> EpisodeSummary:
        if not self.summary_json:
            return EpisodeSummary()
        try:
            return EpisodeSummary.model_validate(self.summary_json)
        except ValueError:
            # A corrupt payload must degrade to "no structured summary", never
            # crash an inspection or a coverage report.
            return EpisodeSummary()


class EpisodeScope(BaseModel):
    """Identity of the conversation an Episode belongs to.

    ``session_id`` is the scope key (see ``MemoryEpisode.session_id``);
    ``room_id`` is recorded for provenance but never used for isolation because
    a persona can revisit the same counterpart in a different room.
    """

    model_config = ConfigDict(extra="ignore")

    persona_id: str
    counterpart_id: str
    branch_id: str = "main"
    session_id: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.persona_id, self.counterpart_id, self.branch_id, self.session_id)


def episode_status_from_raw(value: Any) -> EpisodeStatus:
    raw = str(value or "").strip().casefold()
    for candidate in EpisodeStatus:
        if candidate.value == raw:
            return candidate
    return EpisodeStatus.OPEN


def safe_episode_int(value: Any, *, default: int = 0, minimum: int = 0) -> int:
    normalized = safe_int(value, default=None, minimum=minimum)
    return default if normalized is None else int(normalized)


__all__ = [
    "BoundaryReason",
    "ConsolidationAction",
    "EPISODE_CONSOLIDATION_VERSION",
    "EpisodeScope",
    "EpisodeStatus",
    "EpisodeSummary",
    "EpisodeTurn",
    "MemoryEpisode",
    "ResolvedTurnMessage",
    "episode_status_from_raw",
    "safe_episode_int",
]
