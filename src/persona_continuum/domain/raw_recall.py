"""Provenance-backed Raw Recall: from a memory hit back to the real words.

Phase 3-6 answer "what happened" and "what is true".  Phase 7 answers a
different question, which no summary can answer honestly:

> 需要细节时，当时的原话到底是什么？

A HistoricalExcerpt is **evidence**, not narration.  Three properties are
load-bearing, and every one of them is a hard rule rather than a preference:

1. **Raw text only.**  Every message comes from ``session_turns`` or
   ``room_transcripts``.  No LLM rewrite, no LLM summary, no LLM polish -- and
   when the evidence is gone, the service says so instead of reconstructing a
   plausible sentence from a summary.
2. **Bounded.**  A memory hit expands to a few anchor turns plus a little
   context, measured in TOKENS.  It is never "the whole old transcript, back in
   the prompt", and it is never a hardcoded message count.
3. **Explainable.**  Every excerpt carries the memory it came from, the turns it
   covers, a selection reason, and the scores that put it there.  "score=0.84"
   with no reason is not an answer.

This layer is deliberately *not* a Context Policy participant: budgets here are
recall-operation parameters, and Phase 8 decides how much a given profile spends
on them.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from persona_continuum.domain.persona import utc_now

#: CJK-aware estimate.  Kept local so the domain module does not depend on the
#: agent runtime: a raw excerpt budget must never be entangled with prompt
#: planning (that is exactly the Phase 1/2 mistake in a new costume).
CHARS_PER_TOKEN = 4
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def estimate_raw_tokens(text: str) -> int:
    """Same arithmetic as the runtime estimator, without the runtime import."""

    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return cjk + -(-max(0, len(text) - cjk) // CHARS_PER_TOKEN)


# --- vocabulary -------------------------------------------------------------


class RawMemoryRefType(StrEnum):
    """Which memory layer a recall request starts from."""

    EPISODE = "episode"
    FACT = "fact"
    THREAD = "thread"
    SUMMARY = "summary"
    DIGITAL_EXPERIENCE = "digital_experience"


class SelectionReason(StrEnum):
    """Why an anchor turn was picked.  Every excerpt must be able to answer this."""

    #: A `memory_fact_sources` row that names this exact turn.
    FACT_DIRECT_EVIDENCE = "fact_direct_evidence"
    #: A thread event whose `source_turn_id` is this turn.
    THREAD_EVENT_SOURCE = "thread_event_source"
    #: A `memory_thread_sources` row that names this turn.
    THREAD_SOURCE = "thread_source"
    #: The memory names this episode directly (Episode ref, or a source row
    #: without a turn id).
    EPISODE_ANCHOR = "episode_anchor"
    #: The turn matched the query lexically (BM25).
    QUERY_MATCH = "query_match"
    #: Reached by walking down a Chapter's sources.
    CHAPTER_DESCENDANT = "chapter_descendant"
    #: Reached by walking down a Long-term segment's Chapters.
    LONG_TERM_DESCENDANT = "long_term_descendant"
    #: Reached through the generic `lineage` table.
    MEMORY_LINEAGE = "memory_lineage"
    #: Reached through a Digital Experience lineage edge.
    DIGITAL_EXPERIENCE_LINEAGE = "digital_experience_lineage"


class SourceAvailability(StrEnum):
    """How much of a memory's provenance still resolves to real text."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    DELETED = "deleted"
    UNAVAILABLE = "unavailable"


class TruncationReason(StrEnum):
    NONE = "none"
    #: The excerpt would have blown the per-excerpt token cap.
    PER_EXCERPT_BUDGET = "per_excerpt_budget"
    #: The excerpt was trimmed to fit what was left of the global budget.
    TOTAL_BUDGET = "total_budget"
    TOO_MANY_TURNS = "too_many_turns"


# --- request ----------------------------------------------------------------


class RawRecallScope(BaseModel):
    """Identity of the history being asked about.  Raw recall never crosses it."""

    model_config = ConfigDict(extra="ignore")

    persona_id: str
    counterpart_id: str
    branch_id: str = "main"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.persona_id, self.counterpart_id, self.branch_id)


class RawMemoryRef(BaseModel):
    """One memory to walk back from.  Accepts either form downstream."""

    model_config = ConfigDict(extra="ignore")

    ref_type: RawMemoryRefType = RawMemoryRefType.EPISODE
    ref_id: str
    #: Optional strength of this memory on its own (fact confidence, thread
    #: importance, summary importance).  0 means "unknown".
    relevance: float = 0.0

    @field_validator("ref_type", mode="before")
    @classmethod
    def _coerce_type(cls, value: Any) -> Any:
        if isinstance(value, RawMemoryRefType):
            return value
        raw = str(value or "").strip().casefold()
        aliases = {
            "semantic_fact": RawMemoryRefType.FACT,
            "semanticfact": RawMemoryRefType.FACT,
            "active_thread": RawMemoryRefType.THREAD,
            "activethread": RawMemoryRefType.THREAD,
            "chapter": RawMemoryRefType.SUMMARY,
            "long_term": RawMemoryRefType.SUMMARY,
            "long_term_summary": RawMemoryRefType.SUMMARY,
            "summary": RawMemoryRefType.SUMMARY,
        }
        if raw in aliases:
            return aliases[raw]
        for candidate in RawMemoryRefType:
            if candidate.value == raw:
                return candidate
        return value

    @field_validator("relevance", mode="before")
    @classmethod
    def _clamp_relevance(cls, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return min(1.0, max(0.0, number))


class RawRecallBudget(BaseModel):
    """Recall-operation budget.  Deliberately NOT a Context Policy number.

    Nothing here reads `local_constrained` 6000/7000 or the 8-message window:
    those belong to the prompt assembly of a specific model, and Phase 8 is the
    layer that maps a profile onto these fields.  A recall caller with a large
    budget gets a large answer -- the service never quietly clamps it.
    """

    model_config = ConfigDict(extra="ignore")

    max_total_tokens: int = 4000
    max_excerpts: int = 4
    max_tokens_per_excerpt: int = 1200
    #: Safety cap only.  Tokens are the real unit; this exists so a pathological
    #: source cannot turn one excerpt into ten thousand turns.
    max_turns_per_excerpt: int = 12
    #: How far an anchor may expand in either direction, in turns, before the
    #: token budget is the only thing that can stop it.
    max_context_window: int = 4

    @field_validator(
        "max_total_tokens",
        "max_excerpts",
        "max_tokens_per_excerpt",
        "max_turns_per_excerpt",
        "max_context_window",
        mode="before",
    )
    @classmethod
    def _positive(cls, value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, number)


# --- result -----------------------------------------------------------------


class ExcerptMessage(BaseModel):
    """One message of raw history.  The text is verbatim; the label is structure."""

    model_config = ConfigDict(extra="ignore")

    turn_id: str
    #: Canonical speaker label from the episode layer's resolver -- never a
    #: third opinion about who said what.
    speaker: str
    timestamp: str | None = None
    raw_text: str
    source_kind: str = "session_turn"
    token_estimate: int = 0


class HistoricalExcerpt(BaseModel):
    """A bounded, ordered, fully attributed window of real conversation."""

    model_config = ConfigDict(extra="ignore")

    excerpt_id: str
    persona_id: str
    counterpart_id: str
    branch_id: str = "main"

    source_memory_refs: list[RawMemoryRef] = Field(default_factory=list)
    episode_ids: list[str] = Field(default_factory=list)
    turn_ids: list[str] = Field(default_factory=list)

    started_at: str | None = None
    ended_at: str | None = None

    messages: list[ExcerptMessage] = Field(default_factory=list)
    token_estimate: int = 0

    relevance_score: float = 0.0
    provenance_score: float = 0.0
    selection_reason: SelectionReason = SelectionReason.EPISODE_ANCHOR
    selection_detail: dict[str, Any] = Field(default_factory=dict)

    truncated: bool = False
    truncation_reason: TruncationReason = TruncationReason.NONE
    source_availability: SourceAvailability = SourceAvailability.COMPLETE

    @property
    def provenance_strength(self) -> float:
        """The strongest single reason this excerpt exists."""

        return max(
            (ref.relevance for ref in self.source_memory_refs if ref.relevance),
            default=0.0,
        )


class UnavailableRef(BaseModel):
    """A memory that could not be turned into evidence -- and why."""

    model_config = ConfigDict(extra="ignore")

    ref: RawMemoryRef
    availability: SourceAvailability = SourceAvailability.UNAVAILABLE
    detail: str = ""


class RawRecallResult(BaseModel):
    """What one recall operation produced, including what it refused to fake."""

    model_config = ConfigDict(extra="ignore")

    scope: RawRecallScope
    query: str = ""
    budget: RawRecallBudget = Field(default_factory=RawRecallBudget)

    excerpts: list[HistoricalExcerpt] = Field(default_factory=list)
    unavailable: list[UnavailableRef] = Field(default_factory=list)
    #: Refs that produced something, but with gaps.
    partial: list[UnavailableRef] = Field(default_factory=list)

    anchors_considered: int = 0
    windows_merged: int = 0
    turns_deduplicated: int = 0
    resolved_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @property
    def total_tokens(self) -> int:
        return sum(excerpt.token_estimate for excerpt in self.excerpts)

    @property
    def is_empty(self) -> bool:
        return not self.excerpts

    def explain(self) -> list[dict[str, Any]]:
        """One line per excerpt: why it is here.  For inspect/debug output."""

        return [
            {
                "excerpt_id": excerpt.excerpt_id,
                "selection_reason": excerpt.selection_reason.value,
                "relevance_score": round(excerpt.relevance_score, 4),
                "provenance_score": round(excerpt.provenance_score, 4),
                "memory_refs": [
                    f"{ref.ref_type.value}:{ref.ref_id}" for ref in excerpt.source_memory_refs
                ],
                "episode_ids": excerpt.episode_ids,
                "turn_ids": excerpt.turn_ids,
                "messages": len(excerpt.messages),
                "token_estimate": excerpt.token_estimate,
                "truncated": excerpt.truncated,
                "truncation_reason": excerpt.truncation_reason.value,
                "source_availability": excerpt.source_availability.value,
                "started_at": excerpt.started_at,
                "ended_at": excerpt.ended_at,
                "selection_detail": excerpt.selection_detail,
            }
            for excerpt in self.excerpts
        ]


__all__ = [
    "CHARS_PER_TOKEN",
    "ExcerptMessage",
    "HistoricalExcerpt",
    "RawMemoryRef",
    "RawMemoryRefType",
    "RawRecallBudget",
    "RawRecallResult",
    "RawRecallScope",
    "SelectionReason",
    "SourceAvailability",
    "TruncationReason",
    "UnavailableRef",
    "estimate_raw_tokens",
]
