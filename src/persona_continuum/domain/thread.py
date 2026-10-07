"""Active Threads: what is still going on between a persona and a counterpart.

A Semantic Fact answers "what is true" ("用户计划下个月去重庆").  A Thread answers
"what is still in flight" -- the trip being planned, the argument that has not
settled, the project still being pushed forward.  The two are related but never
interchangeable, and the distinction is load-bearing:

* A Fact is a **value** with a validity window.  A Thread is a **process** with
  a state and a history.
* A Fact can be true forever.  A Thread ends when the thing ends.
* A Thread's lifetime is owned by this layer -- NOT by the recent-dialogue
  window, a message count, or a prompt budget.  "重庆计划" survives a month of
  unrelated conversation; that is the entire reason the layer exists.

Three further properties are enforced rather than hoped for:

1. **Identity is canonical.**  ``thread_key`` is derived from type + subject, so
   "重庆旅行" / "去重庆" / "重庆计划" cannot fork into four live threads, and one
   live thread per (scope, key) is a database constraint.
2. **History is append-only.**  Every change is a ``memory_thread_events`` row;
   the current row is a projection of that ledger, never a replacement for it.
3. **Provenance is mandatory.**  A Thread points at the Episodes and turns that
   justify it.  A thread that cannot be walked back to raw dialogue is not
   allowed to exist.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from persona_continuum.domain.persona import utc_now
from persona_continuum.numeric import safe_float

#: Version of the resolution contract.  Bump when the candidate schema or the
#: lifecycle rules change in a way that makes older threads incomparable.
THREAD_RESOLUTION_VERSION = 1

_MAX_TEXT_CHARS = 400
_MAX_TITLE_CHARS = 120
_MAX_REASON_CHARS = 400
#: Ceiling on how many thread actions one Episode may produce.  A model that
#: ignores the schema must not be able to create an unbounded thread farm.
_MAX_ACTIONS_PER_EPISODE = 8
_MAX_REFERENCES = 16


class ThreadType(StrEnum):
    """Deliberately small.  This is for candidate selection and display, not an
    ontology."""

    PLAN = "plan"
    GOAL = "goal"
    COMMITMENT = "commitment"
    RELATIONSHIP_ISSUE = "relationship_issue"
    PROJECT = "project"
    TASK = "task"
    ONGOING_EVENT = "ongoing_event"
    WAITING = "waiting"
    GENERAL = "general"

    @classmethod
    def from_raw(cls, value: Any) -> ThreadType:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.GENERAL


class ThreadStatus(StrEnum):
    """Lifecycle.  ``RESOLVED`` is never inferred from silence: a thread that
    goes quiet becomes ``STALE`` at worst, because "we have not talked about it"
    is not the same as "it is over".""" 

    OPEN = "open"
    ACTIVE = "active"
    WAITING = "waiting"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    STALE = "stale"

    @classmethod
    def from_raw(cls, value: Any) -> ThreadStatus:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.ACTIVE


#: Statuses that still count as "this thing is in flight".  They are the ones
#: the live-uniqueness index covers and the ones candidate selection considers.
LIVE_STATUSES: frozenset[ThreadStatus] = frozenset(
    {ThreadStatus.OPEN, ThreadStatus.ACTIVE, ThreadStatus.WAITING, ThreadStatus.STALE}
)

#: Statuses a REOPEN may revive: a cancelled thread was deliberately dropped
#: and must not be silently resurrected by a later mention.
REOPENABLE_STATUSES: frozenset[ThreadStatus] = frozenset(
    {ThreadStatus.RESOLVED, ThreadStatus.STALE, ThreadStatus.WAITING}
)


class ThreadEventType(StrEnum):
    """One step of a thread's history.  Every one of these is an appended row."""

    CREATE = "create"
    UPDATE = "update"
    MILESTONE = "milestone"
    WAITING = "waiting"
    RESOLVE = "resolve"
    CANCEL = "cancel"
    REOPEN = "reopen"
    STALE = "stale"


class ThreadOperation(StrEnum):
    """What the resolver asks the store to do."""

    CREATE = "create"
    UPDATE = "update"
    MILESTONE = "milestone"
    WAITING = "waiting"
    RESOLVE = "resolve"
    CANCEL = "cancel"
    REOPEN = "reopen"
    NOOP = "noop"

    @classmethod
    def from_raw(cls, value: Any) -> ThreadOperation:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.NOOP


class ThreadFactRelation(StrEnum):
    """How a fact participates in a thread.  The fact keeps its own text."""

    PRIMARY = "primary"
    SUPPORTING = "supporting"
    MILESTONE = "milestone"
    RESOLVED_BY = "resolved_by"

    @classmethod
    def from_raw(cls, value: Any) -> ThreadFactRelation:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.SUPPORTING


class ThreadEvidenceRole(StrEnum):
    OPENED = "opened"
    SUPPORTING = "supporting"
    MILESTONE = "milestone"
    RESOLVED = "resolved"

    @classmethod
    def from_raw(cls, value: Any) -> ThreadEvidenceRole:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.SUPPORTING


def _clean_text(value: Any, *, limit: int = _MAX_TEXT_CHARS) -> str:
    return str(value or "").strip()[:limit]


def _clean_ids(value: Any) -> list[str]:
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
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= _MAX_REFERENCES:
            break
    return cleaned


def normalize_thread_text(value: Any) -> str:
    """Canonical form used for identity and for lexical overlap.

    Whitespace inside CJK is incidental ("重庆 旅行" is "重庆旅行"), so it is
    removed rather than preserved -- two spellings of one subject must converge
    on one identity.
    """

    text = " ".join(str(value or "").strip().casefold().split())
    return _CJK_SPACE.sub("", text)


#: Noise that carries no identity.  Stripped before a key is derived so
#: "重庆旅行计划" and "重庆旅行" are the same subject.
_KEY_NOISE = (
    "这件事",
    "这个事情",
    "的问题",
    "的事情",
    "计划",
    "事情",
    "问题",
    "安排",
    "相关",
)

#: Whitespace that only separates two CJK characters carries no meaning.
_CJK_SPACE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")


def canonical_thread_key(thread_type: ThreadType | str, subject: Any) -> str:
    """Stable identity of a thread inside its scope.

    The key is derived on the SERVER from the type and the subject phrase; a
    model never supplies a database key.  Keyword noise is removed so that two
    spellings of one subject converge, which is what keeps a single ongoing
    matter from becoming four threads.
    """

    type_value = (
        thread_type.value if isinstance(thread_type, ThreadType) else str(thread_type or "")
    )
    text = normalize_thread_text(subject)
    for noise in _KEY_NOISE:
        text = text.replace(noise, "")
    text = " ".join(text.split())
    # A trailing nominaliser is left over once "X的事情" loses its noun.
    while text.endswith("的"):
        text = text[:-1]
    return "|".join([type_value, text])


class ThreadAction(BaseModel):
    """One model-proposed action, exactly as the resolver must present it.

    Nothing here is trusted: ids are validated against the store, text is
    bounded, enums degrade, and a confidence below the configured threshold
    never mutates a thread -- it becomes an explicit candidate link instead.
    """

    model_config = ConfigDict(extra="ignore")

    operation: ThreadOperation = ThreadOperation.NOOP
    thread_id: str | None = None
    thread_type: ThreadType = ThreadType.GENERAL
    title: str = ""
    summary: str = ""
    thread_key_hint: str | None = None
    confidence: float = 0.5
    reason: str = ""
    state_update: str | None = None
    milestone: str | None = None
    source_turn_ids: list[str] = Field(default_factory=list)
    related_fact_ids: list[str] = Field(default_factory=list)
    #: When the model cannot decide between several live threads it says so
    #: instead of guessing; the server records candidates and mutates nothing.
    ambiguous_thread_ids: list[str] = Field(default_factory=list)

    @field_validator("operation", mode="before")
    @classmethod
    def _clean_operation(cls, value: Any) -> ThreadOperation:
        return ThreadOperation.from_raw(value)

    @field_validator("thread_type", mode="before")
    @classmethod
    def _clean_type(cls, value: Any) -> ThreadType:
        return ThreadType.from_raw(value)

    @field_validator("title", mode="before")
    @classmethod
    def _clean_title(cls, value: Any) -> str:
        return _clean_text(value, limit=_MAX_TITLE_CHARS)

    @field_validator("summary", mode="before")
    @classmethod
    def _clean_summary(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("state_update", "milestone", "thread_key_hint", mode="before")
    @classmethod
    def _clean_optional_text(cls, value: Any) -> str | None:
        text = _clean_text(value)
        return text or None

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: Any) -> str:
        return _clean_text(value, limit=_MAX_REASON_CHARS)

    @field_validator("thread_id", mode="before")
    @classmethod
    def _clean_thread_id(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("source_turn_ids", "related_fact_ids", "ambiguous_thread_ids", mode="before")
    @classmethod
    def _clean_reference_lists(cls, value: Any) -> list[str]:
        return _clean_ids(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clean_confidence(cls, value: Any) -> float:
        normalized = safe_float(value, default=None, minimum=0.0, maximum=1.0)
        return 0.5 if normalized is None else float(normalized)

    @property
    def subject(self) -> str:
        return self.thread_key_hint or self.title

    @property
    def key(self) -> str:
        return canonical_thread_key(self.thread_type, self.subject)

    @property
    def is_usable(self) -> bool:
        """A CREATE needs a subject; every other operation needs a target."""

        if self.operation is ThreadOperation.CREATE:
            return bool(self.title.strip() or (self.thread_key_hint or "").strip())
        if self.operation is ThreadOperation.NOOP:
            return True
        return bool(self.thread_id)

    def display_title(self) -> str:
        title = self.title.strip() or (self.thread_key_hint or "").strip()
        return title[:_MAX_TITLE_CHARS]


class ThreadResolutionPayload(BaseModel):
    """Schema-validated resolver output."""

    model_config = ConfigDict(extra="ignore")

    threads: list[ThreadAction] = Field(default_factory=list)

    @field_validator("threads", mode="before")
    @classmethod
    def _bound_actions(cls, value: Any) -> list[Any]:
        """Drop junk entries instead of losing the whole resolution."""

        if not isinstance(value, list):
            return []
        usable = [item for item in value if isinstance(item, (dict, ThreadAction))]
        return usable[:_MAX_ACTIONS_PER_EPISODE]

    def usable(self) -> list[ThreadAction]:
        return [action for action in self.threads if action.is_usable]


class ThreadEvent(BaseModel):
    """One appended step of a thread's history."""

    model_config = ConfigDict(extra="ignore")

    event_id: str
    thread_id: str
    event_type: ThreadEventType = ThreadEventType.UPDATE
    event_key: str = ""
    summary: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)
    source_episode_id: str | None = None
    source_turn_id: str | None = None
    #: ``complete`` / ``partial`` / ``deleted`` -- mirrors Phase 3.1 so an
    #: explicitly deleted source is visible rather than silently dangling.
    source_availability: str = "complete"
    confidence: float = 0.5
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryActiveThread(BaseModel):
    """A persisted Active Thread (the current projection of its event ledger)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    counterpart_id: str
    branch_id: str = "main"
    thread_key: str = ""
    thread_type: ThreadType = ThreadType.GENERAL
    title: str = ""
    summary: str = ""
    status: ThreadStatus = ThreadStatus.ACTIVE
    importance: float = 0.5
    confidence: float = 0.5
    opened_at: datetime = Field(default_factory=utc_now)
    last_activity_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
    cancelled_at: datetime | None = None
    stale_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    current_state: dict[str, Any] = Field(default_factory=dict)
    consolidation_version: int = THREAD_RESOLUTION_VERSION
    visibility: str = "private_session"
    material_scope: str = "character_visible"
    related_previous_thread_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def is_resolved(self) -> bool:
        return self.status is ThreadStatus.RESOLVED

    @property
    def milestones(self) -> list[str]:
        value = self.current_state.get("milestones")
        if isinstance(value, list):
            return [str(item) for item in value if str(item or "").strip()]
        return []


__all__ = [
    "LIVE_STATUSES",
    "REOPENABLE_STATUSES",
    "THREAD_RESOLUTION_VERSION",
    "MemoryActiveThread",
    "ThreadAction",
    "ThreadEvent",
    "ThreadEventType",
    "ThreadEvidenceRole",
    "ThreadFactRelation",
    "ThreadOperation",
    "ThreadResolutionPayload",
    "ThreadStatus",
    "ThreadType",
    "canonical_thread_key",
    "normalize_thread_text",
]
