"""RawRecallService: one service, every memory type, one kind of evidence.

Every memory layer built so far (Episode, Fact, Thread, Chapter, Long-term)
records WHERE its content came from.  This service is the single place that
walks those pointers back to the real words in ``session_turns`` and
``room_transcripts``.  Nothing here summarises, rewrites or guesses; nothing
here reaches a production prompt (that is Phase 8).

Design rules the code enforces:

* **One service, five entry points.**  Episode / Fact / Thread / Chapter /
  Long-term (plus a best-effort Digital Experience lineage walk) all resolve
  through the same anchor model, so no memory type grows its own private
  excerpt builder.
* **Tokens are the unit.**  Message counts are a safety cap, never the budget.
  Nothing here reads a Context Policy profile.
* **Anchor first, context second.**  An anchor is a turn a memory actually
  points at (or that the query hits); surrounding turns are included only while
  a budget allows, and always in a way that keeps a question with its answer.
* **Honest availability.**  Provenance that no longer resolves is reported as
  partial / deleted / unavailable.  A missing source never becomes an invented
  message.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from persona_continuum.application._utils import new_id
from persona_continuum.domain.episode import EpisodeTurn
from persona_continuum.domain.raw_recall import (
    ExcerptMessage,
    HistoricalExcerpt,
    RawMemoryRef,
    RawMemoryRefType,
    RawRecallBudget,
    RawRecallResult,
    RawRecallScope,
    SelectionReason,
    SourceAvailability,
    TruncationReason,
    UnavailableRef,
    estimate_raw_tokens,
)
from persona_continuum.storage.database import Database

#: Provenance strength of each entry point.  Direct evidence outranks a
#: descendant: a Fact that names a turn is a stronger reason to show it than a
#: Long-term summary that mentions the phase it happened in.
STRENGTH_FACT_TURN = 1.0
STRENGTH_THREAD_EVENT_TURN = 0.9
STRENGTH_THREAD_SOURCE_TURN = 0.85
STRENGTH_EPISODE_TURN = 0.5
STRENGTH_FACT_EPISODE = 0.55
STRENGTH_THREAD_EPISODE = 0.55
STRENGTH_CHAPTER_EPISODE = 0.35
STRENGTH_LONG_TERM_EPISODE = 0.3
STRENGTH_LINEAGE_TURN = 0.6

#: How far down a summary chain raw recall walks before it stops.
_MAX_SUMMARY_DEPTH = 4
_MAX_EPISODES_PER_SUMMARY = 32
#: Upper bound on turns loaded for query scoring.  Scoring is local BM25 over
#: the candidate pool, never over the whole database.
_MAX_SCORING_TURNS = 600

_CJK_RANGE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_WORD = re.compile(r"[a-zA-Z0-9_]{2,}")

_BM25_K1 = 1.2
_BM25_B = 0.75


def _lexical_tokens(text: str) -> list[str]:
    """CJK bigrams + latin words.  Same vocabulary family as the summarisers."""

    normalized = " ".join(str(text or "").casefold().split())
    tokens = [match.group(0) for match in _LATIN_WORD.finditer(normalized)]
    cjk = [char for char in normalized if _CJK_RANGE.match(char)]
    if len(cjk) == 1:
        tokens.append(cjk[0])
    for index in range(len(cjk) - 1):
        tokens.append(cjk[index] + cjk[index + 1])
    return tokens


@dataclass(slots=True)
class _Anchor:
    """One turn (or one whole episode) that a memory points at."""

    episode_id: str
    turn_id: str
    position: int
    strength: float
    reason: SelectionReason
    ref: RawMemoryRef
    memory_relevance: float

    @property
    def key(self) -> tuple[str, str]:
        return (self.episode_id, self.turn_id)

    @property
    def base_score(self) -> float:
        """Provenance x memory relevance, before the query is considered."""

        return self.strength * (0.4 + 0.3 * self.memory_relevance)


@dataclass(slots=True)
class _LoadedTurn:
    """An Episode turn with its raw messages resolved (or proven missing)."""

    episode_id: str
    turn_id: str
    position: int
    occurred_at: str | None
    messages: list[ExcerptMessage] = field(default_factory=list)
    available: bool = False
    query_score: float = 0.0

    @property
    def token_estimate(self) -> int:
        return sum(message.token_estimate for message in self.messages)


@dataclass(slots=True)
class _Window:
    """A candidate contiguous stretch of one Episode."""

    episode_id: str
    positions: list[int]
    anchor_turn_id: str
    anchor_position: int
    strength: float
    reason: SelectionReason
    ref: RawMemoryRef
    memory_relevance: float
    query_score: float = 0.0

    @property
    def span(self) -> tuple[int, int]:
        return (min(self.positions), max(self.positions))

    @property
    def base_score(self) -> float:
        return self.strength * (0.4 + 0.3 * self.memory_relevance)

    def overlaps(self, other: _Window) -> bool:
        low, high = self.span
        other_low, other_high = other.span
        return low <= other_high + 1 and other_low <= high + 1


class RawRecallService:
    """Resolve memory hits back to bounded windows of real conversation."""

    def __init__(
        self,
        database: Database,
        episodes: Any,
        facts: Any = None,
        threads: Any = None,
        hierarchies: Any = None,
        *,
        config: Any = None,
    ) -> None:
        self.database = database
        self.episodes = episodes
        self.facts = facts
        self.threads = threads
        self.hierarchies = hierarchies
        self.config = config

    # -- public entry point -------------------------------------------------

    def recall(
        self,
        *,
        scope: RawRecallScope | dict[str, Any],
        query: str = "",
        memory_refs: list[RawMemoryRef | dict[str, Any]] | None = None,
        budget: RawRecallBudget | dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> RawRecallResult:
        """Turn memory hits into HistoricalExcerpts of REAL conversation.

        Stateless by design: nothing is cached between calls, nothing is queued,
        and a restart cannot change an answer.  The only durable thing this
        service touches is the raw transcript it reads.
        """

        del options  # reserved for recall-operation flags; no policy lives here
        recall_scope = _coerce_scope(scope)
        recall_budget = _coerce_budget(budget)
        refs = _coerce_refs(memory_refs)
        result = RawRecallResult(
            scope=recall_scope, query=str(query or "").strip(), budget=recall_budget
        )
        if not refs:
            return result

        anchors: list[_Anchor] = []
        turns_cache: dict[str, list[_LoadedTurn]] = {}
        for ref in refs:
            found, unavailable = self._resolve_ref(ref, recall_scope, turns_cache)
            if unavailable is not None:
                result.unavailable.append(unavailable)
            anchors.extend(found)
        result.anchors_considered = len(anchors)
        if not anchors:
            return result

        anchors, deduplicated = _deduplicate_anchors(anchors)
        result.turns_deduplicated = deduplicated
        _score_anchors(anchors, result.query, turns_cache)

        windows = self._build_windows(anchors, recall_budget, turns_cache)
        windows, merged = merge_windows(windows)
        result.windows_merged = merged

        excerpts = self._build_excerpts(windows, turns_cache, recall_budget, recall_scope)
        result.excerpts = self._enforce_total_budget(excerpts, recall_budget)
        return result

    # -- reference resolution ------------------------------------------------

    def _in_scope(self, row: Any, scope: RawRecallScope) -> bool:
        return (
            str(getattr(row, "persona_id", "") or "") == scope.persona_id
            and str(getattr(row, "counterpart_id", "") or "") == scope.counterpart_id
            and str(getattr(row, "branch_id", "") or "main") == scope.branch_id
        )

    def _resolve_ref(
        self,
        ref: RawMemoryRef,
        scope: RawRecallScope,
        turns_cache: dict[str, list[_LoadedTurn]],
    ) -> tuple[list[_Anchor], UnavailableRef | None]:
        try:
            if ref.ref_type is RawMemoryRefType.EPISODE:
                return self._resolve_episode_ref(ref, scope, turns_cache)
            if ref.ref_type is RawMemoryRefType.FACT:
                return self._resolve_fact_ref(ref, scope, turns_cache)
            if ref.ref_type is RawMemoryRefType.THREAD:
                return self._resolve_thread_ref(ref, scope, turns_cache)
            if ref.ref_type is RawMemoryRefType.SUMMARY:
                return self._resolve_summary_ref(ref, scope, turns_cache)
            if ref.ref_type is RawMemoryRefType.DIGITAL_EXPERIENCE:
                return self._resolve_lineage_ref(ref, scope, turns_cache)
        except Exception as exc:  # noqa: BLE001 - recall must never raise upward
            return [], UnavailableRef(
                ref=ref,
                availability=SourceAvailability.UNAVAILABLE,
                detail=f"resolution_failed:{type(exc).__name__}",
            )
        return [], UnavailableRef(
            ref=ref, availability=SourceAvailability.UNAVAILABLE, detail="unknown_ref_type"
        )

    def _resolve_episode_ref(
        self, ref: RawMemoryRef, scope: RawRecallScope, turns_cache: dict[str, list[_LoadedTurn]]
    ) -> tuple[list[_Anchor], UnavailableRef | None]:
        episode = self.episodes.get_episode(ref.ref_id)
        if episode is None:
            return [], UnavailableRef(
                ref=ref, availability=SourceAvailability.DELETED, detail="episode_not_found"
            )
        if not self._in_scope(episode, scope):
            return [], UnavailableRef(
                ref=ref, availability=SourceAvailability.UNAVAILABLE, detail="scope_mismatch"
            )
        turns = self._load_episode_turns(episode.id, turns_cache)
        if not any(turn.available for turn in turns):
            return [], UnavailableRef(
                ref=ref,
                availability=_availability_of(episode.metadata),
                detail="episode_has_no_resolvable_turns",
            )
        relevance = ref.relevance or float(episode.importance or 0.5)
        anchors = [
            _Anchor(
                episode_id=episode.id,
                turn_id=turn.turn_id,
                position=turn.position,
                strength=STRENGTH_EPISODE_TURN,
                reason=SelectionReason.EPISODE_ANCHOR,
                ref=ref,
                memory_relevance=relevance,
            )
            for turn in turns
            if turn.available
        ]
        return anchors, None

    def _resolve_fact_ref(
        self, ref: RawMemoryRef, scope: RawRecallScope, turns_cache: dict[str, list[_LoadedTurn]]
    ) -> tuple[list[_Anchor], UnavailableRef | None]:
        fact = self.facts.get_fact(ref.ref_id) if self.facts is not None else None
        if fact is None:
            return [], UnavailableRef(
                ref=ref, availability=SourceAvailability.DELETED, detail="fact_not_found"
            )
        if not self._in_scope(fact, scope):
            return [], UnavailableRef(
                ref=ref, availability=SourceAvailability.UNAVAILABLE, detail="scope_mismatch"
            )
        relevance = ref.relevance or float(fact.confidence or 0.5)
        anchors: list[_Anchor] = []
        for row in self._fact_sources(ref.ref_id):
            episode_id = str(row.get("episode_id") or "")
            turn_id = str(row.get("turn_id") or "")
            if turn_id:
                anchors.append(
                    _Anchor(
                        episode_id=episode_id,
                        turn_id=turn_id,
                        position=self._turn_position(episode_id, turn_id, turns_cache),
                        strength=STRENGTH_FACT_TURN,
                        reason=SelectionReason.FACT_DIRECT_EVIDENCE,
                        ref=ref,
                        memory_relevance=relevance,
                    )
                )
            elif episode_id:
                anchors.append(
                    _Anchor(
                        episode_id=episode_id,
                        turn_id="",
                        position=-1,
                        strength=STRENGTH_FACT_EPISODE,
                        reason=SelectionReason.EPISODE_ANCHOR,
                        ref=ref,
                        memory_relevance=relevance,
                    )
                )
        if not anchors:
            return [], UnavailableRef(
                ref=ref, availability=SourceAvailability.UNAVAILABLE, detail="fact_has_no_sources"
            )
        return anchors, None

    def _resolve_thread_ref(
        self, ref: RawMemoryRef, scope: RawRecallScope, turns_cache: dict[str, list[_LoadedTurn]]
    ) -> tuple[list[_Anchor], UnavailableRef | None]:
        thread = self.threads.get_thread(ref.ref_id) if self.threads is not None else None
        if thread is None:
            return [], UnavailableRef(
                ref=ref, availability=SourceAvailability.DELETED, detail="thread_not_found"
            )
        if not self._in_scope(thread, scope):
            return [], UnavailableRef(
                ref=ref, availability=SourceAvailability.UNAVAILABLE, detail="scope_mismatch"
            )
        relevance = ref.relevance or float(thread.importance or 0.5)
        anchors: list[_Anchor] = []
        # Events first: they carry the milestone a query is usually about.
        for event in self.threads.thread_events(ref.ref_id):
            turn_id = str(getattr(event, "source_turn_id", "") or "")
            episode_id = str(getattr(event, "source_episode_id", "") or "")
            if turn_id:
                anchors.append(
                    _Anchor(
                        episode_id=episode_id,
                        turn_id=turn_id,
                        position=self._turn_position(episode_id, turn_id, turns_cache),
                        strength=STRENGTH_THREAD_EVENT_TURN,
                        reason=SelectionReason.THREAD_EVENT_SOURCE,
                        ref=ref,
                        memory_relevance=relevance,
                    )
                )
            elif episode_id:
                anchors.append(
                    _Anchor(
                        episode_id=episode_id,
                        turn_id="",
                        position=-1,
                        strength=STRENGTH_THREAD_EPISODE,
                        reason=SelectionReason.EPISODE_ANCHOR,
                        ref=ref,
                        memory_relevance=relevance,
                    )
                )
        for row in self.threads.thread_sources(ref.ref_id):
            episode_id = str(row.get("episode_id") or "")
            turn_id = str(row.get("turn_id") or "")
            if turn_id:
                anchors.append(
                    _Anchor(
                        episode_id=episode_id,
                        turn_id=turn_id,
                        position=self._turn_position(episode_id, turn_id, turns_cache),
                        strength=STRENGTH_THREAD_SOURCE_TURN,
                        reason=SelectionReason.THREAD_SOURCE,
                        ref=ref,
                        memory_relevance=relevance,
                    )
                )
            elif episode_id:
                anchors.append(
                    _Anchor(
                        episode_id=episode_id,
                        turn_id="",
                        position=-1,
                        strength=STRENGTH_THREAD_EPISODE,
                        reason=SelectionReason.EPISODE_ANCHOR,
                        ref=ref,
                        memory_relevance=relevance,
                    )
                )
        if not anchors:
            return [], UnavailableRef(
                ref=ref,
                availability=SourceAvailability.UNAVAILABLE,
                detail="thread_has_no_sources",
            )
        return anchors, None

    def _resolve_summary_ref(
        self, ref: RawMemoryRef, scope: RawRecallScope, turns_cache: dict[str, list[_LoadedTurn]]
    ) -> tuple[list[_Anchor], UnavailableRef | None]:
        summary = self.hierarchies.get_summary(ref.ref_id) if self.hierarchies else None
        if summary is None:
            return [], UnavailableRef(
                ref=ref, availability=SourceAvailability.DELETED, detail="summary_not_found"
            )
        if not self._in_scope(summary, scope):
            return [], UnavailableRef(
                ref=ref, availability=SourceAvailability.UNAVAILABLE, detail="scope_mismatch"
            )
        relevance = ref.relevance or float(summary.importance or 0.5)
        episode_ids = self._summary_episode_ids(ref.ref_id, depth=0)
        if not episode_ids:
            return [], UnavailableRef(
                ref=ref,
                availability=_availability_of(summary.metadata),
                detail="summary_has_no_resolvable_sources",
            )
        is_chapter = int(summary.level) <= 1
        reason = (
            SelectionReason.CHAPTER_DESCENDANT
            if is_chapter
            else SelectionReason.LONG_TERM_DESCENDANT
        )
        strength = STRENGTH_CHAPTER_EPISODE if is_chapter else STRENGTH_LONG_TERM_EPISODE
        anchors: list[_Anchor] = []
        for episode_id in episode_ids:
            for turn in self._load_episode_turns(episode_id, turns_cache):
                if not turn.available:
                    continue
                anchors.append(
                    _Anchor(
                        episode_id=episode_id,
                        turn_id=turn.turn_id,
                        position=turn.position,
                        strength=strength,
                        reason=reason,
                        ref=ref,
                        memory_relevance=relevance,
                    )
                )
        return anchors, None

    def _resolve_lineage_ref(
        self, ref: RawMemoryRef, scope: RawRecallScope, turns_cache: dict[str, list[_LoadedTurn]]
    ) -> tuple[list[_Anchor], UnavailableRef | None]:
        """Best-effort walk through the generic ``lineage`` table.

        Digital Experience provenance is NOT part of the Phase 3-6 contract, so
        this is deliberately conservative: only edges whose other side is a real
        Episode or a real turn id are used, and an unusable graph is reported
        rather than guessed around.  It must never distort the stores that ARE
        contractually provenance-backed.
        """

        if not self._table_exists("lineage"):
            return [], UnavailableRef(
                ref=ref, availability=SourceAvailability.UNAVAILABLE, detail="lineage_missing"
            )
        rows = self.database.conn.execute(
            "SELECT child_type, child_id, parent_type, parent_id FROM lineage "
            "WHERE child_id = ? OR parent_id = ? LIMIT 64",
            (ref.ref_id, ref.ref_id),
        ).fetchall()
        episode_ids: list[str] = []
        anchors: list[_Anchor] = []
        for row in rows:
            if str(row["child_id"] or "") != ref.ref_id:
                continue
            other_type = str(row["parent_type"] or "")
            other_id = str(row["parent_id"] or "")
            if not other_id:
                continue
            if other_type == "episode":
                episode_ids.append(other_id)
            elif "turn" in other_type:
                episode_id = self._episode_for_turn(other_id)
                if episode_id:
                    anchors.append(
                        _Anchor(
                            episode_id=episode_id,
                            turn_id=other_id,
                            position=self._turn_position(episode_id, other_id, turns_cache),
                            strength=STRENGTH_LINEAGE_TURN,
                            reason=SelectionReason.DIGITAL_EXPERIENCE_LINEAGE,
                            ref=ref,
                            memory_relevance=ref.relevance or 0.5,
                        )
                    )
        for episode_id in dict.fromkeys(episode_ids):
            episode = self.episodes.get_episode(episode_id)
            if episode is None or not self._in_scope(episode, scope):
                continue
            for turn in self._load_episode_turns(episode_id, turns_cache):
                if not turn.available:
                    continue
                anchors.append(
                    _Anchor(
                        episode_id=episode_id,
                        turn_id=turn.turn_id,
                        position=turn.position,
                        strength=STRENGTH_LINEAGE_TURN,
                        reason=SelectionReason.MEMORY_LINEAGE,
                        ref=ref,
                        memory_relevance=ref.relevance or 0.5,
                    )
                )
        if not anchors:
            return [], UnavailableRef(
                ref=ref,
                availability=SourceAvailability.UNAVAILABLE,
                detail="digital_experience_no_resolvable_lineage",
            )
        return anchors, None

    def _fact_sources(self, fact_id: str) -> list[dict[str, Any]]:
        if not self._table_exists("memory_fact_sources"):
            return []
        rows = self.database.conn.execute(
            "SELECT episode_id, turn_id, evidence_role, excerpt_available "
            "FROM memory_fact_sources WHERE fact_id = ?",
            (fact_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _summary_episode_ids(self, summary_id: str, *, depth: int) -> list[str]:
        """Walk Long-term -> Chapter -> Episode without ever re-entering."""

        if depth > _MAX_SUMMARY_DEPTH or self.hierarchies is None:
            return []
        found: list[str] = []
        for source in self.hierarchies.summary_sources(summary_id):
            if source.source_type.value == "episode":
                found.append(source.source_id)
            else:
                found.extend(self._summary_episode_ids(source.source_id, depth=depth + 1))
            if len(found) >= _MAX_EPISODES_PER_SUMMARY:
                break
        return list(dict.fromkeys(found))[:_MAX_EPISODES_PER_SUMMARY]

    # -- ranking ---------------------------------------------------------------

    # -- windows ---------------------------------------------------------------

    def _build_windows(
        self,
        anchors: list[_Anchor],
        budget: RawRecallBudget,
        turns_cache: dict[str, list[_LoadedTurn]],
    ) -> list[_Window]:
        ranked = sorted(
            anchors,
            key=lambda item: (-item.base_score, item.episode_id, item.position, item.turn_id),
        )
        windows: list[_Window] = []
        covered: set[tuple[str, int]] = set()
        for anchor in ranked:
            if len(windows) >= budget.max_excerpts:
                break
            if anchor.turn_id and (anchor.episode_id, anchor.position) in covered:
                continue
            window = self._expand(anchor, budget, turns_cache, covered)
            if window is None:
                continue
            windows.append(window)
            for position in window.positions:
                covered.add((window.episode_id, position))
        return windows

    def _expansion_order(self, anchor_position: int, max_window: int) -> list[int]:
        """Where context may come from, in priority order.

        The turn BEFORE the anchor is the question the anchor is answering, so it
        always comes first.  After that the window grows outwards alternately --
        there is no "always two on each side" rule to outlive its welcome.
        """

        order = [anchor_position - 1]
        for step in range(1, max(1, max_window) + 1):
            order.append(anchor_position + step)
            order.append(anchor_position - (step + 1))
        return order

    def _expand(
        self,
        anchor: _Anchor,
        budget: RawRecallBudget,
        turns_cache: dict[str, list[_LoadedTurn]],
        covered: set[tuple[str, int]],
    ) -> _Window | None:
        turns = turns_cache.get(anchor.episode_id)
        if turns is None:
            turns = self._load_episode_turns(anchor.episode_id, turns_cache)
        by_position = {turn.position: turn for turn in turns}
        if not any(turn.available for turn in turns):
            return None
        anchor_position = anchor.position
        anchor_turn = by_position.get(anchor_position)
        if anchor_turn is None or not anchor_turn.available:
            # An episode-level anchor, or a turn that no longer resolves: start
            # from the nearest resolvable turn so the excerpt is never empty.
            resolvable = [turn for turn in turns if turn.available]
            anchor_turn = min(
                resolvable, key=lambda item: (abs(item.position - anchor_position), item.position)
            )
            anchor_position = anchor_turn.position

        total_cap = (
            budget.max_total_tokens if budget.max_total_tokens > 0 else per_excerpt_default()
        )
        per_excerpt = max(1, min(budget.max_tokens_per_excerpt, total_cap))
        window = _Window(
            episode_id=anchor.episode_id,
            positions=[anchor_position],
            anchor_turn_id=anchor_turn.turn_id,
            anchor_position=anchor_position,
            strength=anchor.strength,
            reason=anchor.reason,
            ref=anchor.ref,
            memory_relevance=anchor.memory_relevance,
            query_score=anchor_turn.query_score,
        )
        used = anchor_turn.token_estimate
        turn_cap = max(1, budget.max_turns_per_excerpt)
        blocked_down = False
        blocked_up = False
        for position in self._expansion_order(anchor_position, budget.max_context_window):
            if len(window.positions) >= turn_cap:
                break
            if position < anchor_position:
                if blocked_down:
                    continue
            elif blocked_up:
                continue
            if (window.episode_id, position) in covered:
                continue
            turn = by_position.get(position)
            if turn is None or not turn.available:
                # A hole in the raw record: stop this side rather than stitch
                # across a gap and present it as one conversation.
                if position < anchor_position:
                    blocked_down = True
                else:
                    blocked_up = True
                continue
            if used + turn.token_estimate > per_excerpt:
                if position < anchor_position:
                    blocked_down = True
                else:
                    blocked_up = True
                continue
            window.positions.append(position)
            used += turn.token_estimate
        window.positions.sort()
        return window

    def _merge_windows(self, windows: list[_Window]) -> tuple[list[_Window], int]:
        """Overlapping or adjacent stretches of one Episode become one excerpt."""

        merged: list[_Window] = []
        merges = 0
        by_episode: dict[str, list[_Window]] = {}
        for window in windows:
            by_episode.setdefault(window.episode_id, []).append(window)
        for episode_id in sorted(by_episode):
            group = sorted(by_episode[episode_id], key=lambda item: item.anchor_position)
            current: _Window | None = None
            for window in group:
                if current is not None and current.overlaps(window):
                    current.positions = sorted({*current.positions, *window.positions})
                    current.query_score = max(current.query_score, window.query_score)
                    merges += 1
                    # The strongest claim survives the merge.
                    if (window.strength, window.memory_relevance) > (
                        current.strength,
                        current.memory_relevance,
                    ):
                        current.strength = window.strength
                        current.reason = window.reason
                        current.ref = window.ref
                        current.memory_relevance = window.memory_relevance
                        current.anchor_turn_id = window.anchor_turn_id
                        current.anchor_position = window.anchor_position
                    continue
                current = window
                merged.append(current)
        return merged, merges

    # -- excerpt construction ---------------------------------------------------

    def _build_excerpts(
        self,
        windows: list[_Window],
        turns_cache: dict[str, list[_LoadedTurn]],
        budget: RawRecallBudget,
        scope: RawRecallScope,
    ) -> list[HistoricalExcerpt]:
        ranked = sorted(
            windows,
            key=lambda item: (
                -(item.base_score * (0.55 + 0.45 * item.query_score)),
                item.episode_id,
                item.anchor_position,
            ),
        )
        excerpts: list[HistoricalExcerpt] = []
        for window in ranked:
            turns = turns_cache.get(window.episode_id) or []
            by_position = {turn.position: turn for turn in turns}
            selected = [
                by_position[position]
                for position in window.positions
                if position in by_position and by_position[position].available
            ]
            if not selected:
                continue
            excerpts.append(self._excerpt_from_window(window, selected, budget, scope))
        return excerpts

    def _excerpt_from_window(
        self,
        window: _Window,
        selected: list[_LoadedTurn],
        budget: RawRecallBudget,
        scope: RawRecallScope,
    ) -> HistoricalExcerpt:
        messages = _dedupe_shared_user(
            [message for turn in selected for message in turn.messages]
        )
        messages = _dedupe_shared_user(
            [message for turn in selected for message in turn.messages]
        )
        truncated = False
        reason = TruncationReason.NONE
        if len(selected) > budget.max_turns_per_excerpt:
            selected = _trim_turns(selected, window, budget.max_turns_per_excerpt)
            truncated = True
            reason = TruncationReason.TOO_MANY_TURNS
        total = sum(message.token_estimate for message in messages)
        if total > budget.max_tokens_per_excerpt:
            trimmed = _trim_turns(selected, window, max(1, len(selected) - 1))
            while len(trimmed) > 1 and sum(
                turn.token_estimate for turn in trimmed
            ) > budget.max_tokens_per_excerpt:
                trimmed = _trim_turns(trimmed, window, len(trimmed) - 1)
            if len(trimmed) != len(selected):
                truncated = True
                reason = TruncationReason.PER_EXCERPT_BUDGET
            selected = trimmed
            messages = _dedupe_shared_user(
                [message for turn in selected for message in turn.messages]
            )
            total = sum(message.token_estimate for message in messages)

        provenance = window.base_score
        relevance = provenance * (0.55 + 0.45 * window.query_score)
        return HistoricalExcerpt(
            excerpt_id=new_id("excerpt"),
            persona_id=scope.persona_id,
            counterpart_id=scope.counterpart_id,
            branch_id=scope.branch_id,
            source_memory_refs=[window.ref],
            episode_ids=[window.episode_id],
            turn_ids=[turn.turn_id for turn in selected],
            started_at=selected[0].occurred_at if selected else None,
            ended_at=selected[-1].occurred_at if selected else None,
            messages=messages,
            token_estimate=total,
            relevance_score=round(min(1.0, max(0.0, relevance)), 4),
            provenance_score=round(min(1.0, max(0.0, provenance)), 4),
            selection_reason=window.reason,
            selection_detail={
                "anchor_turn_id": window.anchor_turn_id,
                "anchor_position": window.anchor_position,
                "memory_relevance": round(window.memory_relevance, 4),
                "query_score": round(window.query_score, 4),
                "provenance_strength": window.strength,
                "window_positions": [turn.position for turn in selected],
            },
            truncated=truncated,
            truncation_reason=reason,
            source_availability=self._window_availability(window),
        )

    def _window_availability(self, window: _Window) -> SourceAvailability:
        turns = self._load_episode_turns(window.episode_id, {})
        if not turns:
            return SourceAvailability.UNAVAILABLE
        episode = self.episodes.get_episode(window.episode_id)
        metadata = getattr(episode, "metadata", {}) or {}
        missing = sum(1 for turn in turns if not turn.available)
        recorded = str(metadata.get("source_availability") or "")
        if recorded == "deleted" or missing >= len(turns):
            return SourceAvailability.DELETED
        if recorded == "partial" or missing:
            return SourceAvailability.PARTIAL
        return SourceAvailability.COMPLETE

    def _enforce_total_budget(
        self, excerpts: list[HistoricalExcerpt], budget: RawRecallBudget
    ) -> list[HistoricalExcerpt]:
        """Global cap.  Excerpts arrive ranked; the budget only cuts."""

        kept: list[HistoricalExcerpt] = []
        used = 0
        for excerpt in excerpts:
            if len(kept) >= budget.max_excerpts:
                break
            room = budget.max_total_tokens - used
            if excerpt.token_estimate <= room:
                kept.append(excerpt)
                used += excerpt.token_estimate
                continue
            if room <= 0:
                break
            trimmed = _trim_excerpt_tokens(excerpt, room)
            if trimmed is not None:
                trimmed.truncated = True
                trimmed.truncation_reason = TruncationReason.TOTAL_BUDGET
                kept.append(trimmed)
            break
        return kept

    # -- loading ---------------------------------------------------------------

    def _load_episode_turns(
        self, episode_id: str, turns_cache: dict[str, list[_LoadedTurn]]
    ) -> list[_LoadedTurn]:
        cached = turns_cache.get(episode_id)
        if cached is not None:
            return cached
        episode = self.episodes.get_episode(episode_id)
        metadata = (getattr(episode, "metadata", {}) or {}) if episode is not None else {}
        turns = [
            self._load_turn(episode_id, row)
            for row in self.episodes.episode_turns(episode_id)
        ]
        # A source explicitly stamped "deleted" means every one of its turns is
        # gone; the per-turn resolution below already reports that as unavailable.
        del metadata
        turns_cache[episode_id] = turns
        return turns

    def _load_turn(self, episode_id: str, row: EpisodeTurn) -> _LoadedTurn:
        messages = [
            ExcerptMessage(
                turn_id=row.turn_id,
                speaker=message.speaker,
                timestamp=message.occurred_at.isoformat() if message.occurred_at else None,
                raw_text=message.raw_text,
                source_kind=message.source_kind,
                token_estimate=estimate_raw_tokens(message.raw_text),
            )
            for message in self.episodes.resolve_turn_messages(row)
        ]
        messages = [message for message in messages if message.raw_text.strip()]
        return _LoadedTurn(
            episode_id=episode_id,
            turn_id=row.turn_id,
            position=row.position,
            occurred_at=row.occurred_at.isoformat() if row.occurred_at else None,
            messages=messages,
            available=bool(messages),
        )

    def _turn_position(
        self, episode_id: str, turn_id: str, turns_cache: dict[str, list[_LoadedTurn]]
    ) -> int:
        for turn in self._load_episode_turns(episode_id, turns_cache):
            if turn.turn_id == turn_id:
                return turn.position
        return -1

    def _episode_for_turn(self, turn_id: str) -> str | None:
        episode = self.episodes.episode_for_turn(turn_id)
        return episode.id if episode is not None else None

    def _table_exists(self, name: str) -> bool:
        row = self.database.conn.execute(
            "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None


# --- pure helpers ---------------------------------------------------------------


def merge_windows(windows: list[_Window]) -> tuple[list[_Window], int]:
    """Overlapping or adjacent stretches of one Episode become one excerpt."""

    merged: list[_Window] = []
    merges = 0
    by_episode: dict[str, list[_Window]] = {}
    for window in windows:
        by_episode.setdefault(window.episode_id, []).append(window)
    for episode_id in sorted(by_episode):
        group = sorted(by_episode[episode_id], key=lambda item: item.anchor_position)
        current: _Window | None = None
        for window in group:
            if current is not None and current.overlaps(window):
                current.positions = sorted({*current.positions, *window.positions})
                current.query_score = max(current.query_score, window.query_score)
                merges += 1
                # The strongest claim survives the merge.
                if (window.strength, window.memory_relevance) > (
                    current.strength,
                    current.memory_relevance,
                ):
                    current.strength = window.strength
                    current.reason = window.reason
                    current.ref = window.ref
                    current.memory_relevance = window.memory_relevance
                    current.anchor_turn_id = window.anchor_turn_id
                    current.anchor_position = window.anchor_position
                continue
            current = window
            merged.append(current)
    return merged, merges


def _deduplicate_anchors(anchors: list[_Anchor]) -> tuple[list[_Anchor], int]:
    """The same turn claimed twice keeps the STRONGEST reason, not both."""

    best: dict[tuple[str, str], _Anchor] = {}
    dropped = 0
    for anchor in anchors:
        current = best.get(anchor.key)
        if current is None:
            best[anchor.key] = anchor
            continue
        dropped += 1
        if (anchor.strength, anchor.memory_relevance) > (
            current.strength,
            current.memory_relevance,
        ):
            best[anchor.key] = anchor
    return list(best.values()), dropped


def _score_anchors(
    anchors: list[_Anchor],
    query: str,
    turns_cache: dict[str, list[_LoadedTurn]],
) -> None:
    """Rank support: provenance x memory relevance x query relevance.

    The query never promotes a turn ABOVE its provenance -- it decides which of
    a memory's OWN turns is the one being asked about.  "我那次为什么没去成重庆？"
    therefore surfaces the cancellation rather than the earliest plan, without a
    lexical accident ever outranking direct evidence.
    """

    pool: list[tuple[_Anchor, _LoadedTurn]] = []
    for anchor in anchors:
        if not anchor.turn_id:
            continue
        for turn in turns_cache.get(anchor.episode_id) or []:
            if turn.turn_id == anchor.turn_id:
                pool.append((anchor, turn))
                break
    scores = _bm25_scores(
        query, [turn.messages for _, turn in pool[:_MAX_SCORING_TURNS]]
    )
    for (_anchor, turn), score in zip(pool, scores, strict=False):
        turn.query_score = score


def _bm25_scores(query: str, message_groups: list[list[ExcerptMessage]]) -> list[float]:
    query_tokens = set(_lexical_tokens(query))
    if not query_tokens or not message_groups:
        return [0.0] * len(message_groups)
    documents = [
        [token for message in group for token in _lexical_tokens(message.raw_text)]
        for group in message_groups
    ]
    counts: dict[str, int] = {}
    for tokens in documents:
        for token in set(tokens):
            counts[token] = counts.get(token, 0) + 1
    total = len(documents)
    avg_len = sum(len(tokens) for tokens in documents) / max(1, total)
    raw: list[float] = []
    for tokens in documents:
        score = 0.0
        length = len(tokens) or 1
        for token in query_tokens:
            frequency = tokens.count(token)
            if not frequency:
                continue
            df = counts.get(token, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            score += idf * (
                frequency
                * (_BM25_K1 + 1)
                / (frequency + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / max(1.0, avg_len)))
            )
        raw.append(score)
    peak = max(raw, default=0.0)
    if peak <= 0:
        return [0.0] * len(raw)
    return [min(1.0, value / peak) for value in raw]


def _dedupe_shared_user(messages: list[ExcerptMessage]) -> list[ExcerptMessage]:
    """One raw user event is one message, even though it is stored twice.

    Phase 3.1 attaches a room's user message to every persona Episode that
    answered it, AND the persona's own ``session_turn`` row carries the same
    text.  Both are authoritative stores, so the same words legitimately exist
    twice -- but an excerpt that shows them twice is lying about the
    conversation.  When a ``shared_user`` message and a ``session_turn`` user
    half carry identical text, the room-level event (the original) wins.

    Scoped on purpose: identical text from two DIFFERENT turns of the same kind
    is a real repetition and is never touched.
    """

    shared_texts = {
        message.raw_text.strip()
        for message in messages
        if message.source_kind == "shared_user"
    }
    if not shared_texts:
        return messages
    kept: list[ExcerptMessage] = []
    for message in messages:
        if (
            message.source_kind == "session_turn"
            and message.speaker == "user"
            and message.raw_text.strip() in shared_texts
        ):
            continue
        kept.append(message)
    return kept


def _trim_turns(
    selected: list[_LoadedTurn], window: _Window, limit: int
) -> list[_LoadedTurn]:
    """Drop from the ENDS, never the anchor, so the excerpt stays coherent."""

    turns = list(selected)
    while len(turns) > max(1, limit):
        anchor_index = next(
            (
                index
                for index, turn in enumerate(turns)
                if turn.turn_id == window.anchor_turn_id
            ),
            None,
        )
        if anchor_index is None:
            turns = turns[:limit]
            break
        if anchor_index == 0:
            turns.pop()
        elif anchor_index == len(turns) - 1:
            turns.pop(0)
        # Drop whichever end is further from the anchor.
        elif anchor_index < len(turns) - 1 - anchor_index:
            turns.pop()
        else:
            turns.pop(0)
    return turns


def _trim_excerpt_tokens(excerpt: HistoricalExcerpt, limit: int) -> HistoricalExcerpt | None:
    """Cut whole messages from the end; never split a message in half."""

    messages = list(excerpt.messages)
    while messages and sum(message.token_estimate for message in messages) > limit:
        messages.pop()
    if not messages:
        return None
    excerpt.messages = messages
    excerpt.turn_ids = list(dict.fromkeys(message.turn_id for message in messages))
    excerpt.token_estimate = sum(message.token_estimate for message in messages)
    excerpt.ended_at = messages[-1].timestamp
    return excerpt


def _availability_of(metadata: dict[str, Any]) -> SourceAvailability:
    state = str(metadata.get("source_availability") or "")
    if state == "deleted":
        return SourceAvailability.DELETED
    if state == "partial":
        return SourceAvailability.PARTIAL
    return SourceAvailability.UNAVAILABLE


def per_excerpt_default() -> int:
    """Fallback used only when a caller passes a zeroed budget."""

    return 1200


# --- coercion helpers ---------------------------------------------------------


def _coerce_scope(value: Any) -> RawRecallScope:
    if isinstance(value, RawRecallScope):
        return value
    if isinstance(value, dict):
        return RawRecallScope.model_validate(value)
    raise TypeError("scope must be RawRecallScope or dict")


def _coerce_budget(value: Any) -> RawRecallBudget:
    if value is None:
        return RawRecallBudget()
    if isinstance(value, RawRecallBudget):
        return value
    if isinstance(value, dict):
        return RawRecallBudget.model_validate(value)
    raise TypeError("budget must be RawRecallBudget or dict")


def _coerce_refs(value: Any) -> list[RawMemoryRef]:
    refs: list[RawMemoryRef] = []
    for item in value or []:
        if isinstance(item, RawMemoryRef):
            refs.append(item)
        elif isinstance(item, dict):
            refs.append(RawMemoryRef.model_validate(item))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            refs.append(
                RawMemoryRef(
                    ref_type=RawMemoryRefType(str(item[0])), ref_id=str(item[1])
                )
            )
        elif isinstance(item, str) and ":" in item:
            kind, _, identifier = item.partition(":")
            refs.append(
                RawMemoryRef(ref_type=RawMemoryRefType(kind), ref_id=identifier)
            )
    return refs


__all__ = ["RawRecallService", "per_excerpt_default"]
