"""Hierarchical summaries: Chapter / Long-term grouping and consolidation.

Pipeline position::

    committed turn -> Episode -> Episode summary
                            -> Semantic Facts (Phase 4)
                            -> Active Threads (Phase 5)
                            -> Chapter grouping        <- this module (level 1)
                            -> Long-term grouping      <- this module (level 2+)

Design rules the code enforces rather than hopes for:

* **Grouping is deterministic.**  Boundaries are counted (episodes, tokens, time
  span, inactivity) exactly like Episode boundaries; a model is only consulted
  for an optional topic-shift check, and its failure defaults to "continue".
  Chat never waits for any of it.
* **Progressive, never recursive.**  A Chapter is consolidated from its real
  Episodes, a Long-term segment from its real Chapters.  A previous summary is
  never the only input to the next level, which is what stops the classic
  summary-of-a-summary drift.
* **Grounded.**  A summary may only say what its sources already say.  Entities,
  dates, cited ids and event statements are validated against the source corpus
  before a summary can become READY; anything the model concluded goes to
  ``inferences``.
* **Idempotent.**  A source range has one stable hash and one row, so replaying
  a consolidation can never create "September Chapter 2 / 3 / 4".
* **Nothing here reaches a prompt.**  Phase 6 stores and inspects only; the
  Context Assembly Report says so explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from persona_continuum.application._utils import dt, dumps, loads, new_id, parse_dt
from persona_continuum.domain.hierarchical_summary import (
    HIERARCHY_CONSOLIDATION_VERSION,
    HierarchicalSummary,
    SummaryAction,
    SummaryBoundaryReason,
    SummaryContent,
    SummaryReadiness,
    SummarySource,
    SummarySourceType,
    SummaryStatus,
    SummaryType,
    source_type_for_level,
    summary_range_hash,
    summary_source_fingerprint,
    summary_type_for_level,
    text_fingerprint,
)
from persona_continuum.domain.provenance import CHARACTER_VISIBLE
from persona_continuum.storage.database import Database

#: What the summariser is asked for.  Every field is optional: a partial answer
#: degrades into a thinner summary instead of an invalid payload.
SUMMARY_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "major_events": {"type": "array", "items": {"type": "string"}},
        "relationship_changes": {"type": "array", "items": {"type": "string"}},
        "important_decisions": {"type": "array", "items": {"type": "string"}},
        "important_commitments": {"type": "array", "items": {"type": "string"}},
        "important_preferences_or_fact_changes": {
            "type": "array",
            "items": {"type": "string"},
        },
        "resolved_threads": {"type": "array", "items": {"type": "string"}},
        "ongoing_threads": {"type": "array", "items": {"type": "string"}},
        "emotional_arc": {"type": "array", "items": {"type": "string"}},
        "unresolved_topics": {"type": "array", "items": {"type": "string"}},
        "key_entities": {"type": "array", "items": {"type": "string"}},
        "time_range": {"type": "string"},
        "importance": {"type": "number"},
        "inferences": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary"],
}

#: Shared by both levels; the level only changes the framing paragraph.
_SUMMARY_RULES = (
    "硬性要求：\n"
    "1. 只根据输入中出现的内容总结。输入是这一层级的真实来源"
    "（Chapter 的来源是 Episode，Long-term 的来源是 Chapter），"
    "不要新增来源里没有的事件、人物、地点、动机或关系。\n"
    "2. 你自己做出的推断必须写进 inferences，绝不能混进 major_events / "
    "relationship_changes / summary 等事实性字段。事实语义层是 Fact Store，不是这份摘要。\n"
    "3. 尊重事实的时间变化：如果同一件事在来源里前后不同"
    "（例如早期喜欢茉莉奶绿、后期改喝美式），必须把变化写出来"
    "（「这一阶段早期……后期……」），不要只保留旧值或只保留新值。\n"
    "4. 尊重 Thread 的完整过程：如果一个 Thread 在来源里从计划推进到完成，"
    "要描述整个过程（计划 → 买票 → 完成），不要只写它开始时的状态。\n"
    "5. 关系变化要写轨迹，不要只写最终状态：冲突 → 联系 → 和好 → 再冲突 → 和好，"
    "这些都是这一阶段的内容。\n"
    "6. title 要语义化（「考研准备进入执行阶段」），不要用日期当标题；"
    "time_range 用于记录时间范围。\n"
    "7. 输入中的 fact/thread/episode id 只在需要指明来源时引用；不要编造 id。\n"
    "8. 没有内容的字段留空数组。宁可少写，不要编造。\n"
    "只输出结构化结果。"
)

CHAPTER_SUMMARY_INTRO = (
    "你在为一个长期对话系统整理「一个阶段」（Chapter）。"
    "Chapter 由若干个连续的 Episode 组成，回答的是「我们那时处在什么阶段」。"
    "输入是这些 Episode 的结构化摘要、这一阶段的 Semantic Facts 与 Active Threads 事件。"
    "这是历史整理任务，不是角色扮演。\n" + _SUMMARY_RULES
)

LONG_TERM_SUMMARY_INTRO = (
    "你在为一个长期对话系统整理「较长时间尺度的一段历史」（Long-term Summary）。"
    "它由若干个 Chapter 组成，回答的是「我们过去大体一起经历了什么」。"
    "输入是这些 Chapter 的结构化内容，以及它们覆盖的时间范围。"
    "这是跨阶段的历史整理任务，不是角色扮演：重点是阶段的推移与跨阶段的因果/变化，"
    "而不是把每个 Chapter 复述一遍。\n" + _SUMMARY_RULES
)

_MAX_CORPUS_CHARS = 48000
#: How much raw dialogue the grounding corpus may borrow (per chapter).
_MAX_RAW_EXCERPT_CHARS = 12000
_NUMBER_PATTERN = re.compile(r"\d{2,4}(?:[-/年月.]\d{1,2}(?:[-/日.]\d{1,2})?)?")
_ID_PATTERN = re.compile(r"\b(?:fact|thread|episode|summary)_[0-9a-zA-Z]+")
_CJK_RANGE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_WORD = re.compile(r"[a-zA-Z0-9_]{2,}")
#: How much of a statement's vocabulary must appear in the sources.  A fully
#: novel statement shares nothing and is rejected; a paraphrase shares most of
#: it and is only recorded.
_MIN_STATEMENT_OVERLAP = 0.34
_MIN_ENTITY_OVERLAP = 0.5

#: How far past its configured token ceiling a Long-term segment may drift
#: before ``long_term_min_chapters`` stops being able to hold it open.  This is
#: a SAFETY VALVE, not a boundary: the normal ceiling (``..._max_source_tokens``)
#: is 60000, so a segment has to reach 120000 source tokens before the minimum
#: is allowed to lose an argument it would otherwise win.  Without it, a minimum
#: of 3 chapters would be able to pin an unboundedly large segment open forever.
_LONG_TERM_HARD_CEILING_FACTOR = 2

#: Readiness values in which a stored text exists and may be read (as current
#: or as explicitly out-of-date history).
_TEXT_BEARING = frozenset(
    {
        SummaryReadiness.READY,
        SummaryReadiness.PROVISIONAL,
        SummaryReadiness.STALE,
    }
)


def _lexical_tokens(text: str) -> set[str]:
    normalized = " ".join(str(text or "").casefold().split())
    tokens = set(_LATIN_WORD.findall(normalized))
    cjk = [char for char in normalized if _CJK_RANGE.match(char)]
    if len(cjk) == 1:
        tokens.add(cjk[0])
    for index in range(len(cjk) - 1):
        tokens.add(cjk[index] + cjk[index + 1])
    return tokens


def _overlap_ratio(text: str, corpus_tokens: set[str]) -> float:
    tokens = _lexical_tokens(text)
    if not tokens:
        return 1.0
    return len(tokens & corpus_tokens) / float(len(tokens))


@dataclass(slots=True)
class _SourceRecord:
    """One source about to be grouped (an Episode, or a lower-level summary)."""

    source_id: str
    source_type: SummarySourceType
    started_at: datetime
    ended_at: datetime | None
    importance: float
    token_estimate: int
    #: Text used for the optional topic-shift check.
    topic_text: str


class HierarchyService:
    """Owns the Chapter / Long-term store.  Never deletes raw history."""

    def __init__(
        self,
        database: Database,
        episodes: Any,
        facts: Any = None,
        threads: Any = None,
        *,
        config: Any = None,
    ) -> None:
        self.database = database
        self.episodes = episodes
        self.facts = facts
        self.threads = threads
        self.config = config
        self.enabled = bool(getattr(config, "hierarchy_summary_enabled", True))
        self.chapter_max_sources = max(
            2, int(getattr(config, "hierarchy_chapter_max_episodes", 8) or 8)
        )
        self.chapter_max_tokens = max(
            1000, int(getattr(config, "hierarchy_chapter_max_source_tokens", 20000) or 20000)
        )
        self.chapter_max_timespan_days = max(
            1, int(getattr(config, "hierarchy_chapter_max_timespan_days", 14) or 14)
        )
        self.inactivity_gap_days = max(
            1, int(getattr(config, "hierarchy_chapter_inactivity_gap_days", 7) or 7)
        )
        self.topic_boundary_enabled = bool(
            getattr(config, "hierarchy_topic_boundary_enabled", False)
        )
        self.topic_shift_threshold = float(
            getattr(config, "hierarchy_topic_shift_threshold", 0.18) or 0.18
        )
        self.long_term_min_sources = max(
            2, int(getattr(config, "hierarchy_long_term_min_chapters", 3) or 3)
        )
        self.long_term_max_sources = max(
            self.long_term_min_sources,
            int(getattr(config, "hierarchy_long_term_max_chapters", 12) or 12),
        )
        self.long_term_max_tokens = max(
            1000, int(getattr(config, "hierarchy_long_term_max_source_tokens", 60000) or 60000)
        )
        self.long_term_max_timespan_days = max(
            30, int(getattr(config, "hierarchy_long_term_max_timespan_days", 365) or 365)
        )
        self.long_term_inactivity_gap_days = max(
            7, int(getattr(config, "hierarchy_long_term_inactivity_gap_days", 180) or 180)
        )
        #: Safety valve for ``long_term_min_sources``: a segment already this far
        #: over its token ceiling is closed even if it holds fewer than the
        #: configured minimum number of chapters, so a minimum can never pin an
        #: unbounded segment open.
        self.long_term_hard_ceiling_tokens = (
            self.long_term_max_tokens * _LONG_TERM_HARD_CEILING_FACTOR
        )
        self.chapter_target_tokens = max(
            200, int(getattr(config, "hierarchy_chapter_summary_target_tokens", 1600) or 1600)
        )
        self.long_term_target_tokens = max(
            400, int(getattr(config, "hierarchy_long_term_summary_target_tokens", 3000) or 3000)
        )
        self.max_level = max(2, int(getattr(config, "hierarchy_max_level", 2) or 2))
        self.input_max_tokens = max(
            512, int(getattr(config, "hierarchy_summary_input_max_tokens", 24000) or 24000)
        )
        self.consolidation_batch = max(
            1, int(getattr(config, "hierarchy_consolidation_batch", 1) or 1)
        )
        self.backfill_batch = max(1, int(getattr(config, "hierarchy_backfill_batch", 40) or 40))

    # -- grouping (deterministic, no model) --------------------------------

    def assign_episode(
        self, episode: Any, *, boundary_classifier: Any = None
    ) -> dict[str, Any]:
        """Place one Episode into its scope's Chapter (or open a new one).

        Deterministic and cheap: this runs in the same background pass as the
        Episode summary, so a chat turn never waits for it.
        """

        report: dict[str, Any] = {
            "summary_id": None,
            "level": 1,
            "action": SummaryAction.NOOP.value,
            "boundary_reason": SummaryBoundaryReason.NONE.value,
            "source_count": 0,
            "closed_summary_id": None,
            "error": None,
        }
        if not self.enabled:
            report["error"] = "hierarchy_disabled"
            return report
        record = self._episode_record(episode)
        outcome = self._assign_to_level(
            scope=(
                episode.persona_id,
                episode.counterpart_id,
                episode.branch_id,
            ),
            level=1,
            record=record,
            boundary_classifier=boundary_classifier,
        )
        report.update(outcome)
        self.database.conn.commit()
        return report

    def _episode_record(self, episode: Any) -> _SourceRecord:
        summary = episode.structured_summary()
        topic_text = " ".join(
            [
                episode.title,
                summary.title,
                summary.summary,
                *summary.topics,
                *summary.entities,
            ]
        )
        return _SourceRecord(
            source_id=episode.id,
            source_type=SummarySourceType.EPISODE,
            started_at=episode.started_at,
            ended_at=episode.ended_at,
            importance=float(episode.importance or 0.5),
            token_estimate=int(episode.source_token_estimate or 0),
            topic_text=topic_text,
        )

    def _summary_record(self, summary: HierarchicalSummary) -> _SourceRecord:
        content = summary.structured
        return _SourceRecord(
            source_id=summary.id,
            source_type=SummarySourceType.SUMMARY,
            started_at=summary.started_at,
            ended_at=summary.ended_at,
            importance=float(summary.importance or 0.5),
            token_estimate=int(summary.source_token_estimate or 0),
            topic_text=" ".join(
                [summary.title, content.title, content.summary, *content.key_entities]
            ),
        )

    def _assign_to_level(
        self,
        *,
        scope: tuple[str, str, str],
        level: int,
        record: _SourceRecord,
        boundary_classifier: Any = None,
    ) -> dict[str, Any]:
        outcome: dict[str, Any] = {
            "summary_id": None,
            "level": level,
            "action": SummaryAction.NOOP.value,
            "boundary_reason": SummaryBoundaryReason.NONE.value,
            "source_count": 0,
            "closed_summary_id": None,
            "deferred_close_reason": None,
        }
        if self.summary_for_source(record.source_type, record.source_id) is not None:
            # This source is already grouped: a replay, not new history.
            return outcome
        current = self._open_summary(scope, level)
        closed: HierarchicalSummary | None = None
        should_close = False
        reason = SummaryBoundaryReason.NONE
        if current is not None:
            should_close, reason = self._boundary_reached(current, record)
            if not should_close and boundary_classifier is not None:
                should_close, reason = self._topic_boundary(current, record, boundary_classifier)
            # Phase 6.1 / A4: a Long-term segment may not CLOSE ITSELF on fewer
            # than `hierarchy_long_term_min_chapters` chapters.  Two chapters do
            # not make a life stage, and before this guard a single long silence
            # (or a 180-day gap) was enough to finalise one.  Explicit closes,
            # retraction, and the hard token ceiling still close it -- the guard
            # only binds the *automatic* boundary.
            if should_close and level >= 2 and not self._may_auto_close(current):
                outcome["deferred_close_reason"] = reason.value
                metadata = dict(current.metadata)
                metadata["deferred_close_reason"] = reason.value
                metadata["deferred_close_at"] = datetime.now(UTC).isoformat()
                metadata["deferred_close_sources"] = len(self.summary_sources(current.id))
                self.database.conn.execute(
                    "UPDATE memory_hierarchical_summaries SET metadata_json = ?, "
                    "updated_at = ? WHERE id = ? AND status = 'open'",
                    (dumps(metadata), datetime.now(UTC).isoformat(), current.id),
                )
                # ``_append_source`` below writes ``current.metadata`` back, so the
                # in-memory copy has to carry the deferral too or the record is
                # silently wiped by the very next append.
                current.metadata = metadata
                should_close = False
                reason = SummaryBoundaryReason.NONE
        if current is None:
            created = self._create_summary(
                scope, level, [record], SummaryBoundaryReason.FIRST_SOURCE
            )
            outcome.update(
                {
                    "summary_id": created.id,
                    "action": SummaryAction.CREATE.value,
                    "boundary_reason": SummaryBoundaryReason.FIRST_SOURCE.value,
                    "source_count": 1,
                }
            )
            return outcome
        if should_close:
            closed = self.close_summary(current.id, reason=reason)
            created = self._create_summary(scope, level, [record], reason)
            outcome.update(
                {
                    "summary_id": created.id,
                    "action": SummaryAction.CREATE.value,
                    "boundary_reason": reason.value,
                    "source_count": 1,
                    "closed_summary_id": closed.id if closed else None,
                }
            )
        else:
            self._append_source(current, record)
            outcome.update(
                {
                    "summary_id": current.id,
                    "action": SummaryAction.APPEND.value,
                    "boundary_reason": SummaryBoundaryReason.APPEND.value,
                    "source_count": len(self.summary_sources(current.id)),
                }
            )
        if closed is not None:
            self._propagate(closed, boundary_classifier=boundary_classifier)
        return outcome

    def _propagate(self, closed: HierarchicalSummary, *, boundary_classifier: Any = None) -> None:
        """Feed a closed summary into the next level up (bounded by max_level)."""

        if closed.level >= self.max_level:
            return
        record = self._summary_record(closed)
        self._assign_to_level(
            scope=(closed.persona_id, closed.counterpart_id, closed.branch_id),
            level=closed.level + 1,
            record=record,
            boundary_classifier=None,
        )

    def _max_sources_for_level(self, level: int) -> int:
        return self.chapter_max_sources if level <= 1 else self.long_term_max_sources

    def _max_tokens_for_level(self, level: int) -> int:
        return self.chapter_max_tokens if level <= 1 else self.long_term_max_tokens

    def _max_timespan_days_for_level(self, level: int) -> int:
        return (
            self.chapter_max_timespan_days
            if level <= 1
            else self.long_term_max_timespan_days
        )

    def _inactivity_gap_days_for_level(self, level: int) -> int:
        return self.inactivity_gap_days if level <= 1 else self.long_term_inactivity_gap_days

    def _boundary_reached(
        self, summary: HierarchicalSummary, record: _SourceRecord
    ) -> tuple[bool, SummaryBoundaryReason]:
        """Deterministic boundaries.  Order matters only for the reported reason.

        Span and silence are measured from source START times on purpose.  An
        Episode's ``ended_at`` is the moment its own boundary fired -- i.e. the
        start of the NEXT sitting -- so using it would make every chapter look
        as wide as the following gap and would hide real silence.
        """

        sources = self.summary_sources(summary.id)
        if len(sources) >= self._max_sources_for_level(summary.level):
            return True, SummaryBoundaryReason.MAX_SOURCES
        if summary.source_token_estimate + record.token_estimate > self._max_tokens_for_level(
            summary.level
        ):
            return True, SummaryBoundaryReason.MAX_TOKENS
        last_start = self._last_source_start(summary, sources)
        if last_start is not None and (record.started_at - last_start) > timedelta(
            days=self._inactivity_gap_days_for_level(summary.level)
        ):
            return True, SummaryBoundaryReason.INACTIVITY
        if (record.started_at - summary.started_at) > timedelta(
            days=self._max_timespan_days_for_level(summary.level)
        ):
            return True, SummaryBoundaryReason.MAX_TIMESPAN
        return False, SummaryBoundaryReason.NONE

    def _min_sources_satisfied(self, summary: HierarchicalSummary) -> bool:
        """Whether a Long-term segment has enough chapters to be a stage at all.

        Only level 2+ is constrained: a Chapter is *by definition* a handful of
        Episodes, and ``hierarchy_long_term_min_chapters`` says nothing about it.
        """

        if summary.level < 2:
            return True
        return len(self.summary_sources(summary.id)) >= self.long_term_min_sources

    def _may_auto_close(self, summary: HierarchicalSummary) -> bool:
        """Whether an automatic boundary may close this segment right now.

        Two ways to be allowed: the segment already holds at least
        ``long_term_min_sources`` chapters, or it has run so far past its token
        ceiling that holding it open is worse than finalising it early.
        """

        if self._min_sources_satisfied(summary):
            return True
        return int(summary.source_token_estimate or 0) >= self.long_term_hard_ceiling_tokens

    def _last_source_start(
        self, summary: HierarchicalSummary, sources: list[SummarySource]
    ) -> datetime | None:
        if not sources:
            return summary.started_at
        latest = sources[-1].started_at
        return latest or summary.started_at

    def _topic_boundary(
        self, summary: HierarchicalSummary, record: _SourceRecord, classifier: Any
    ) -> tuple[bool, SummaryBoundaryReason]:
        """Optional model-assisted NEW_CHAPTER check.

        Only consulted when no deterministic boundary fired AND the topic shift
        is actually large, and only ever as a *hint*: an UNCERTAIN answer or a
        failed call defaults to CONTINUE, so a broken model cannot stall
        grouping or split a phase by accident.
        """

        if not self.topic_boundary_enabled or classifier is None:
            return False, SummaryBoundaryReason.NONE
        recent = self._recent_source_topic_text(summary)
        if not recent:
            return False, SummaryBoundaryReason.NONE
        overlap = len(_lexical_tokens(record.topic_text) & _lexical_tokens(recent)) / float(
            max(1, len(_lexical_tokens(record.topic_text)))
        )
        if overlap > self.topic_shift_threshold:
            return False, SummaryBoundaryReason.NONE
        try:
            verdict = classifier(summary, record)
        except Exception:  # noqa: BLE001 - a hint must never break the pipeline
            return False, SummaryBoundaryReason.NONE
        if isinstance(verdict, dict):
            verdict = verdict.get("decision")
        decision = str(verdict or "").strip().upper()
        if decision == "NEW_CHAPTER":
            return True, SummaryBoundaryReason.TOPIC_SHIFT
        return False, SummaryBoundaryReason.NONE

    def _recent_source_topic_text(self, summary: HierarchicalSummary) -> str:
        rows = self.summary_sources(summary.id)[-3:]
        parts: list[str] = []
        for row in rows:
            if row.source_type is SummarySourceType.EPISODE:
                episode = self.episodes.get_episode(row.source_id)
                if episode is not None:
                    parts.append(self._episode_record(episode).topic_text)
            else:
                nested = self.get_summary(row.source_id)
                if nested is not None:
                    parts.append(self._summary_record(nested).topic_text)
        return " ".join(parts)

    def _open_summary(
        self, scope: tuple[str, str, str], level: int
    ) -> HierarchicalSummary | None:
        row = self.database.conn.execute(
            "SELECT * FROM memory_hierarchical_summaries WHERE persona_id = ? "
            "AND counterpart_id = ? AND branch_id = ? AND level = ? AND status = 'open' "
            "ORDER BY started_at DESC LIMIT 1",
            (*scope, int(level)),
        ).fetchone()
        return self._row_to_summary(row) if row else None

    def _create_summary(
        self,
        scope: tuple[str, str, str],
        level: int,
        sources: list[_SourceRecord],
        reason: SummaryBoundaryReason,
    ) -> HierarchicalSummary:
        now = datetime.now(UTC)
        ordered = sorted(sources, key=lambda item: item.started_at)
        summary = HierarchicalSummary(
            id=new_id("summary"),
            persona_id=scope[0],
            counterpart_id=scope[1],
            branch_id=scope[2],
            level=int(level),
            summary_type=summary_type_for_level(level),
            sequence=self._next_sequence(scope, level),
            started_at=ordered[0].started_at,
            ended_at=ordered[-1].ended_at or ordered[-1].started_at,
            status=SummaryStatus.OPEN,
            importance=max((item.importance for item in ordered), default=0.5),
            created_at=now,
            updated_at=now,
            consolidation_version=HIERARCHY_CONSOLIDATION_VERSION,
            summary_status=SummaryReadiness.PENDING,
            visibility="private_session",
            material_scope=CHARACTER_VISIBLE,
            metadata={
                "open_reason": reason.value,
                "consolidation_version": HIERARCHY_CONSOLIDATION_VERSION,
            },
        )
        self._insert_summary(summary)
        for record in ordered:
            self._insert_source(summary.id, record, position=self._next_position(summary.id))
        self._refresh_aggregates(summary.id)
        return self.get_summary(summary.id) or summary

    def _append_source(self, summary: HierarchicalSummary, record: _SourceRecord) -> bool:
        inserted = self._insert_source(
            summary.id, record, position=self._next_position(summary.id)
        )
        if inserted:
            # Sources changed, so any stored text is now behind its range: it
            # goes back to "owed" (a provisional text stays readable in
            # ``metadata.previous_provisional`` until it is regenerated).
            metadata = dict(summary.metadata)
            if summary.summary_status is SummaryReadiness.PROVISIONAL and summary.summary:
                metadata["previous_provisional"] = summary.summary[:2000]
            metadata["append_count"] = int(metadata.get("append_count") or 0) + 1
            self.database.conn.execute(
                "UPDATE memory_hierarchical_summaries SET metadata_json = ?, "
                "summary_status = 'pending', updated_at = ? WHERE id = ? AND status = 'open'",
                (dumps(metadata), datetime.now(UTC).isoformat(), summary.id),
            )
            self._refresh_aggregates(summary.id)
            if inserted:
                # The source set of a CHILD moved, so its own text (and with it
                # every parent's inputs) is behind.  Bounded: one level up.
                self.invalidate_parents(summary.id, reason="source_membership_changed")
        return bool(inserted)

    def close_summary(
        self,
        summary_id: str,
        *,
        reason: SummaryBoundaryReason | str = SummaryBoundaryReason.EXPLICIT_CLOSE,
    ) -> HierarchicalSummary | None:
        """Close a summary's source range so it owes a FINAL consolidation."""

        summary = self.get_summary(summary_id)
        if summary is None or summary.status is not SummaryStatus.OPEN:
            return summary
        sources = self.summary_sources(summary_id)
        ended_at = sources[-1].ended_at or sources[-1].started_at if sources else summary.ended_at
        metadata = dict(summary.metadata)
        metadata["close_reason"] = str(getattr(reason, "value", reason))
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            """
            UPDATE memory_hierarchical_summaries
            SET status = 'closed', ended_at = ?, summary_status = 'pending',
                metadata_json = ?, updated_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (dt(ended_at), dumps(metadata), now, summary_id),
        )
        self.database.conn.commit()
        # Closing is a state change a parent can observe: this child is about to
        # be regenerated from its full source set, so anything built on the older
        # text is no longer current.  Still one level up, still not recursive.
        self.invalidate_parents(summary_id, reason="source_closed")
        return self.get_summary(summary_id)

    def summary_for_source(
        self, source_type: SummarySourceType | str, source_id: str
    ) -> HierarchicalSummary | None:
        row = self.database.conn.execute(
            """
            SELECT s.* FROM memory_hierarchical_summaries s
            JOIN memory_summary_sources src ON src.summary_id = s.id
            WHERE src.source_type = ? AND src.source_id = ?
            ORDER BY s.level ASC LIMIT 1
            """,
            (str(getattr(source_type, "value", source_type)), source_id),
        ).fetchone()
        return self._row_to_summary(row) if row else None

    def ungrouped_episodes(
        self, *, limit: int = 40, persona_id: str | None = None
    ) -> list[Any]:
        """Episodes that no Chapter owns yet (the backfill queue)."""

        clauses = [
            "turn_count > 0",
            "NOT EXISTS (SELECT 1 FROM memory_summary_sources s "
            "WHERE s.source_type = 'episode' AND s.source_id = e.id)",
        ]
        params: list[Any] = []
        if persona_id:
            clauses.append("persona_id = ?")
            params.append(persona_id)
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"SELECT e.* FROM memory_episodes e WHERE {' AND '.join(clauses)} "
            "ORDER BY started_at ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self.episodes._row_to_episode(row) for row in rows]

    def count_ungrouped_episodes(self, *, persona_id: str | None = None) -> int:
        clauses = [
            "turn_count > 0",
            "NOT EXISTS (SELECT 1 FROM memory_summary_sources s "
            "WHERE s.source_type = 'episode' AND s.source_id = e.id)",
        ]
        params: list[Any] = []
        if persona_id:
            clauses.append("persona_id = ?")
            params.append(persona_id)
        return int(
            self.database.conn.execute(
                f"SELECT COUNT(*) AS c FROM memory_episodes e WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()["c"]
        )

    def backfill(
        self, *, limit: int | None = None, persona_id: str | None = None
    ) -> dict[str, Any]:
        """Bounded, model-free grouping of a legacy backlog.

        Never consolidates: an operator or a room reopening decides when the
        model runs.  This is what keeps an existing database from paying for a
        whole history of Chapter summaries at startup.
        """

        batch = max(1, int(limit or self.backfill_batch))
        episodes = self.ungrouped_episodes(limit=batch, persona_id=persona_id)
        reports = [self.assign_episode(episode) for episode in episodes]
        return {
            "processed": len(reports),
            "remaining": self.count_ungrouped_episodes(persona_id=persona_id),
            "created": sum(1 for item in reports if item["action"] == "create"),
            "appended": sum(1 for item in reports if item["action"] == "append"),
            "pending_summaries": self.count_pending(persona_id=persona_id),
            "actions": reports,
        }

    # -- consolidation (async, bounded, retryable) -------------------------

    async def consolidate_summary(
        self,
        summary_id: str,
        *,
        summarize: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]],
        allow_open: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        """(Re)generate one summary's text from its REAL sources.

        Never from a previous summary: that is the recursive compression this
        design exists to avoid.  A failure leaves the summary owed and
        retryable, and never touches Episodes, Facts or Threads.

        ``force`` regenerates an already-READY summary **in place** (same row,
        same sources).  That is for the one case where a parent must catch up
        with its children -- e.g. a Chapter that only produced text on the
        second attempt -- and it never rewrites another range.
        """

        summary = self.get_summary(summary_id)
        report: dict[str, Any] = {
            "summary_id": summary_id,
            "level": summary.level if summary else None,
            "action": "noop",
            "status": None,
            "summary_status": None,
            "sources": 0,
            "source_tokens": 0,
            "grounding_failures": 0,
            "version": HIERARCHY_CONSOLIDATION_VERSION,
            "pending": False,
            "stale": bool(summary.is_stale) if summary else False,
            "blocked_by": [],
            "error": None,
        }
        if not self.enabled:
            report["error"] = "hierarchy_disabled"
            return report
        if summary is None:
            report["error"] = "summary_not_found"
            return report
        if summary.status is SummaryStatus.RETRACTED:
            report["error"] = "summary_retracted"
            return report
        if summary.is_ready and summary.is_closed and not force:
            return report
        if summary.is_open and not allow_open:
            return report
        # Phase 6.1 / A1: a parent may not turn an unfinished child into final
        # history.  A Long-term segment whose Chapter has no text yet (still
        # pending, or failed) stays OWED instead of being summarised from the
        # gap -- otherwise the model would be invited to invent what happened.
        children_ready, not_ready = self.child_sources_ready(summary)
        if not children_ready:
            self._mark_blocked(
                summary_id, reason="sources_not_ready", blocked_by=not_ready
            )
            report["error"] = "sources_not_ready"
            report["blocked_by"] = not_ready[:12]
            report["pending"] = True
            return report
        payload, corpus = self.build_consolidation_payload(summary)
        if payload is None:
            self._mark(summary_id, status=SummaryReadiness.FAILED, error="no_sources")
            report["error"] = "no_sources"
            report["pending"] = True
            return report
        report["sources"] = corpus["source_count"]
        report["source_tokens"] = corpus["source_tokens"]
        try:
            raw = await summarize(payload, SUMMARY_CONTENT_SCHEMA)
        except Exception as exc:  # noqa: BLE001 - any provider failure is retryable
            error = f"summarize_failed:{type(exc).__name__}"
            self._mark(summary_id, status=SummaryReadiness.FAILED, error=error)
            report["error"] = error
            report["pending"] = True
            return report
        content = self.parse_content(raw)
        if content is None or content.is_empty:
            self._mark(summary_id, status=SummaryReadiness.FAILED, error="invalid_summary_payload")
            report["error"] = "invalid_summary_payload"
            report["pending"] = True
            return report
        grounding = self.validate_grounding(content, corpus)
        report["grounding_failures"] = len(grounding["failures"])
        if grounding["failures"]:
            self._mark(
                summary_id,
                status=SummaryReadiness.FAILED,
                error="grounding_failed",
                extra={"grounding": grounding},
            )
            report["error"] = "grounding_failed"
            report["pending"] = True
            return report
        readiness = (
            SummaryReadiness.READY if summary.is_closed else SummaryReadiness.PROVISIONAL
        )
        self._store_content(
            summary_id,
            content,
            readiness=readiness,
            grounding=grounding,
            source_tokens=corpus["source_tokens"],
        )
        report["action"] = "consolidate"
        report["status"] = summary.status.value
        report["summary_status"] = readiness.value
        report["pending"] = summary.is_open
        return report

    async def consolidate_pending(
        self,
        *,
        summarize: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]],
        limit: int | None = None,
        persona_id: str | None = None,
        room_id: str | None = None,
        allow_open: bool = True,
    ) -> dict[str, Any]:
        reports: list[dict[str, Any]] = []
        for summary in self.pending_summaries(
            limit=max(1, int(limit or self.consolidation_batch)),
            persona_id=persona_id,
            room_id=room_id,
        ):
            reports.append(
                await self.consolidate_summary(
                    summary.id, summarize=summarize, allow_open=allow_open
                )
            )
        return {
            "attempted": len(reports),
            "succeeded": sum(1 for item in reports if item["action"] == "consolidate"),
            "failed": sum(1 for item in reports if item.get("error")),
            "reports": reports,
        }

    def build_consolidation_payload(
        self, summary: HierarchicalSummary
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Real sources only: Episodes for level 1, Chapters for level 2+."""

        sources = self.summary_sources(summary.id)
        if not sources:
            return None, {}
        if summary.level <= 1:
            payload, corpus = self._chapter_payload(summary, sources)
        else:
            payload, corpus = self._long_term_payload(summary, sources)
        if payload is None:
            return None, {}
        return payload, corpus

    def _chapter_payload(
        self, summary: HierarchicalSummary, sources: list[SummarySource]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        episodes: list[dict[str, Any]] = []
        corpus_parts: list[str] = []
        source_tokens = 0
        excerpt_budget = _MAX_RAW_EXCERPT_CHARS
        for source in sources:
            episode = self.episodes.get_episode(source.source_id)
            if episode is None:
                continue
            structured = episode.structured_summary()
            episodes.append(
                {
                    "episode_id": episode.id,
                    "started_at": episode.started_at.isoformat(),
                    "ended_at": (episode.ended_at or episode.updated_at).isoformat(),
                    "title": episode.title or structured.title,
                    "summary": episode.summary or structured.summary,
                    "major_events": structured.important_events,
                    "commitments": structured.commitments,
                    "unresolved": structured.unresolved,
                    "emotional_arc": structured.emotional_arc,
                    "topics": structured.topics,
                    "entities": structured.entities,
                    "user_stated": structured.user_stated,
                }
            )
            source_tokens += int(episode.source_token_estimate or 0)
            corpus_parts.append(
                " ".join(
                    [
                        episode.title,
                        episode.summary,
                        structured.title,
                        structured.summary,
                        " ".join(structured.important_events),
                        " ".join(structured.commitments),
                        " ".join(structured.unresolved),
                        " ".join(structured.topics),
                        " ".join(structured.entities),
                        " ".join(structured.user_stated),
                        " ".join(structured.persona_stated),
                    ]
                )
            )
            # A bounded peek at the raw turns: an Episode that was never
            # summarised still carries real vocabulary, and grounding must not
            # fail merely because the Episode summary is thin.
            if excerpt_budget > 0:
                excerpt = self._raw_excerpt(episode.id, budget=excerpt_budget)
                excerpt_budget -= len(excerpt)
                corpus_parts.append(excerpt)
        if not episodes:
            return None, {}
        episode_ids = [item["episode_id"] for item in episodes]
        facts = self._facts_for_episodes(episode_ids)
        threads = self._threads_for_episodes(episode_ids)
        corpus_parts.extend(self._facts_corpus(facts))
        corpus_parts.extend(self._threads_corpus(threads))
        return self._finish_payload(
            summary,
            sources=sources,
            sources_payload=episodes,
            facts=facts,
            threads=threads,
            corpus_parts=corpus_parts,
            source_tokens=source_tokens,
            cited_ids=_ID_PATTERN.findall(" ".join(corpus_parts)),
        )

    def _raw_excerpt(self, episode_id: str, *, budget: int) -> str:
        """Bounded raw dialogue for grounding (never for inclusion in a prompt)."""

        parts: list[str] = []
        used = 0
        for turn in self.episodes.episode_turns(episode_id):
            text = self.episodes.resolve_turn_text(turn)
            if not text:
                continue
            room = budget - used
            if room <= 0:
                break
            parts.append(text[:room])
            used += len(text[:room])
        return " ".join(parts)

    def _long_term_payload(
        self, summary: HierarchicalSummary, sources: list[SummarySource]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        chapters: list[dict[str, Any]] = []
        corpus_parts: list[str] = []
        source_tokens = 0
        for source in sources:
            chapter = self.get_summary(source.source_id)
            if chapter is None:
                continue
            content = chapter.structured
            chapters.append(
                {
                    "summary_id": chapter.id,
                    "level": chapter.level,
                    "title": chapter.title or content.title,
                    "summary": chapter.summary,
                    "time_range": f"{chapter.started_at.date()} ~ "
                    f"{(chapter.ended_at or chapter.updated_at).date()}",
                    "major_events": content.major_events,
                    "relationship_changes": content.relationship_changes,
                    "important_decisions": content.important_decisions,
                    "important_commitments": content.important_commitments,
                    "fact_changes": content.important_preferences_or_fact_changes,
                    "resolved_threads": content.resolved_threads,
                    "ongoing_threads": content.ongoing_threads,
                    "unresolved_topics": content.unresolved_topics,
                    "key_entities": content.key_entities,
                }
            )
            source_tokens += int(chapter.source_token_estimate or 0)
            corpus_parts.append(
                " ".join(
                    [
                        chapter.title,
                        chapter.summary,
                        content.title,
                        content.summary,
                        *content.major_events,
                        *content.relationship_changes,
                        *content.important_decisions,
                        *content.important_commitments,
                        *content.important_preferences_or_fact_changes,
                        *content.resolved_threads,
                        *content.ongoing_threads,
                        *content.unresolved_topics,
                        *content.key_entities,
                    ]
                )
            )
            source_tokens += self._append_chapter_episode_context(chapter, corpus_parts)
        if not chapters:
            return None, {}
        return self._finish_payload(
            summary,
            sources=sources,
            sources_payload=chapters,
            facts=[],
            threads=[],
            corpus_parts=corpus_parts,
            source_tokens=source_tokens,
            cited_ids=_ID_PATTERN.findall(" ".join(corpus_parts)),
        )

    def _append_chapter_episode_context(
        self, chapter: HierarchicalSummary, corpus_parts: list[str]
    ) -> int:
        """A bounded peek at the chapters' Episodes, so a Long-term summary can
        still ground an entity that only ever appeared in an Episode title."""

        tokens = 0
        for source in self.summary_sources(chapter.id)[:6]:
            if source.source_type is not SummarySourceType.EPISODE:
                continue
            episode = self.episodes.get_episode(source.source_id)
            if episode is None:
                continue
            corpus_parts.append(f"{episode.title} {episode.summary}")
            tokens += int(episode.source_token_estimate or 0)
        return tokens

    def _finish_payload(
        self,
        summary: HierarchicalSummary,
        *,
        sources: list[SummarySource],
        sources_payload: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        threads: list[dict[str, Any]],
        corpus_parts: list[str],
        source_tokens: int,
        cited_ids: list[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        corpus_text = "\n".join(part for part in corpus_parts if part)[:_MAX_CORPUS_CHARS]
        date_terms = self._date_terms(sources)
        corpus_text = f"{corpus_text}\n{' '.join(date_terms)}"
        target = (
            self.chapter_target_tokens if summary.level <= 1 else self.long_term_target_tokens
        )
        payload: dict[str, Any] = {
            "scope": {
                "persona_id": summary.persona_id,
                "counterpart_id": summary.counterpart_id,
                "branch_id": summary.branch_id,
            },
            "summary": {
                "summary_id": summary.id,
                "level": summary.level,
                "summary_type": summary.summary_type.value,
                "status": summary.status.value,
                "time_range": f"{summary.started_at.date()} ~ "
                f"{(summary.ended_at or summary.updated_at).date()}",
                "source_count": len(sources),
                "target_tokens": target,
            },
            "sources": sources_payload,
        }
        if facts:
            payload["facts"] = facts
        if threads:
            payload["threads"] = threads
        corpus = {
            "text": corpus_text,
            "tokens": _lexical_tokens(corpus_text),
            "source_count": len(sources),
            "source_tokens": source_tokens,
            "episode_ids": [
                source.source_id
                for source in sources
                if source.source_type is SummarySourceType.EPISODE
            ],
            "summary_ids": [
                source.source_id
                for source in sources
                if source.source_type is SummarySourceType.SUMMARY
            ],
            "fact_ids": [str(item["fact_id"]) for item in facts],
            "thread_ids": [str(item["thread_id"]) for item in threads],
            # Ids that merely APPEAR inside the sources (e.g. a thread id quoted
            # in a Chapter's own text).  The model was shown them, so citing one
            # back is not a fabricated citation.
            "cited_ids": sorted(set(cited_ids or [])),
            "date_terms": date_terms,
        }
        return payload, corpus

    @staticmethod
    def _date_terms(sources: list[SummarySource]) -> list[str]:
        """Both date spellings a summary may legitimately use."""

        terms: list[str] = []
        for source in sources:
            for moment in (source.started_at, source.ended_at):
                if moment is None:
                    continue
                terms.append(moment.strftime("%Y-%m-%d"))
                terms.append(f"{moment.year}年{moment.month}月")
                terms.append(f"{moment.month}月{moment.day}日")
        return sorted(set(terms))

    def _facts_for_episodes(self, episode_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not episode_ids or not self._table_exists("memory_semantic_facts"):
            return []
        placeholders = ",".join("?" for _ in episode_ids)
        rows = self.database.conn.execute(
            f"""
            SELECT DISTINCT f.id, f.display_text, f.status, f.origin, f.plan_status,
                   f.valid_from, f.valid_until, f.superseded_by_fact_id, f.category
            FROM memory_semantic_facts f
            JOIN memory_fact_sources s ON s.fact_id = f.id
            WHERE s.episode_id IN ({placeholders})
            ORDER BY COALESCE(f.valid_from, f.created_at) ASC
            LIMIT 64
            """,
            tuple(episode_ids),
        ).fetchall()
        return [
            {
                "fact_id": str(row["id"]),
                "display_text": str(row["display_text"] or ""),
                "status": str(row["status"] or ""),
                "origin": str(row["origin"] or ""),
                "plan_status": str(row["plan_status"] or ""),
                "category": str(row["category"] or ""),
                "valid_from": row["valid_from"],
                "valid_until": row["valid_until"],
                "superseded_by": row["superseded_by_fact_id"],
            }
            for row in rows
        ]

    def _threads_for_episodes(self, episode_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not episode_ids or not self._table_exists("memory_active_threads"):
            return []
        placeholders = ",".join("?" for _ in episode_ids)
        rows = self.database.conn.execute(
            f"""
            SELECT DISTINCT t.id, t.title, t.status, t.thread_type, t.summary,
                   t.current_state_json, t.opened_at, t.last_activity_at, t.resolved_at
            FROM memory_active_threads t
            WHERE EXISTS (
              SELECT 1 FROM memory_thread_sources s
              WHERE s.thread_id = t.id AND s.episode_id IN ({placeholders})
            ) OR EXISTS (
              SELECT 1 FROM memory_thread_events e
              WHERE e.thread_id = t.id AND e.source_episode_id IN ({placeholders})
            )
            ORDER BY t.opened_at ASC
            LIMIT 24
            """,
            (*episode_ids, *episode_ids),
        ).fetchall()
        threads: list[dict[str, Any]] = []
        for row in rows:
            thread_id = str(row["id"])
            events = [
                {
                    "event_type": str(event["event_type"]),
                    "summary": str(event["summary"] or ""),
                    "occurred_at": event["occurred_at"],
                }
                for event in self.database.conn.execute(
                    "SELECT event_type, summary, occurred_at FROM memory_thread_events "
                    f"WHERE thread_id = ? AND source_episode_id IN ({placeholders}) "
                    "ORDER BY occurred_at ASC LIMIT 12",
                    (thread_id, *episode_ids),
                ).fetchall()
            ]
            state = dict(loads(row["current_state_json"]))
            threads.append(
                {
                    "thread_id": thread_id,
                    "title": str(row["title"] or ""),
                    "thread_type": str(row["thread_type"] or ""),
                    "status": str(row["status"] or ""),
                    "summary": str(row["summary"] or ""),
                    "milestones": [str(item) for item in state.get("milestones") or []],
                    "opened_at": row["opened_at"],
                    "resolved_at": row["resolved_at"],
                    "last_activity_at": row["last_activity_at"],
                    "events": events,
                }
            )
        return threads

    @staticmethod
    def _facts_corpus(facts: list[dict[str, Any]]) -> list[str]:
        """Everything the model was shown about a fact, including its dates.

        Grounding must cover the whole payload: a summary may quote a fact's
        validity window, and rejecting that would punish the model for using
        information it was given.
        """

        parts: list[str] = []
        for fact in facts:
            parts.append(
                " ".join(
                    [
                        str(fact.get("display_text") or ""),
                        str(fact.get("status") or ""),
                        str(fact.get("plan_status") or ""),
                        str(fact.get("valid_from") or ""),
                        str(fact.get("valid_until") or ""),
                    ]
                )
            )
        return parts

    @staticmethod
    def _threads_corpus(threads: list[dict[str, Any]]) -> list[str]:
        parts: list[str] = []
        for thread in threads:
            parts.append(
                " ".join(
                    [
                        str(thread.get("title") or ""),
                        str(thread.get("summary") or ""),
                        str(thread.get("status") or ""),
                        str(thread.get("opened_at") or ""),
                        str(thread.get("resolved_at") or ""),
                        str(thread.get("last_activity_at") or ""),
                        " ".join(str(item) for item in thread.get("milestones") or []),
                        " ".join(
                            str(event.get("summary") or "")
                            for event in thread.get("events") or []
                        ),
                    ]
                )
            )
        return parts

    @staticmethod
    def _mask_ids(text: str, allowed_ids: set[str]) -> str:
        """Remove id-like tokens before scanning for dates and numbers.

        A cited id is validated on its own; its hex tail must not be mistaken
        for a fabricated year ("thread_3fee7819…" is not "the year 7819").
        """

        masked = _ID_PATTERN.sub(" ", text)
        for allowed in sorted(allowed_ids, key=len, reverse=True):
            masked = masked.replace(allowed, " ")
        return masked

    @staticmethod
    def parse_content(raw: Any) -> SummaryContent | None:
        """Validate an untrusted summariser payload.  Never trust raw JSON."""

        if isinstance(raw, SummaryContent):
            return raw
        if not isinstance(raw, dict):
            return None
        try:
            return SummaryContent.model_validate(raw)
        except ValueError:
            return None

    def validate_grounding(
        self, content: SummaryContent, corpus: dict[str, Any]
    ) -> dict[str, Any]:
        """Reject summaries that assert things their sources never said.

        Three hard checks (unknown cited ids, novel dates/numbers, entities and
        events with no vocabulary in common with the sources) plus a soft
        warning tier for thin paraphrases.  ``inferences`` are exempt by design:
        the model is allowed to interpret, as long as it labels it.
        """

        corpus_text = str(corpus.get("text") or "")
        tokens: set[str] = corpus.get("tokens") or set()
        failures: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []

        allowed_ids = {
            *[str(item) for item in corpus.get("episode_ids") or []],
            *[str(item) for item in corpus.get("summary_ids") or []],
            *[str(item) for item in corpus.get("fact_ids") or []],
            *[str(item) for item in corpus.get("thread_ids") or []],
            *[str(item) for item in corpus.get("cited_ids") or []],
        }

        statements = content.grounded_fields()
        for field_name, item in statements:
            ratio = _overlap_ratio(item, tokens)
            if ratio <= 0.0:
                failures.append(
                    {"kind": "ungrounded_statement", "field": field_name, "value": item}
                )
            elif ratio < _MIN_STATEMENT_OVERLAP:
                warnings.append(
                    {"kind": "weak_grounding", "field": field_name, "value": item}
                )

        for entity in content.key_entities:
            normalized = str(entity or "").strip()
            if not normalized:
                continue
            if normalized in corpus_text:
                continue
            ratio = _overlap_ratio(normalized, tokens)
            if ratio < _MIN_ENTITY_OVERLAP:
                failures.append(
                    {
                        "kind": "ungrounded_entity",
                        "field": "key_entities",
                        "value": normalized,
                    }
                )

        date_terms = {str(item) for item in corpus.get("date_terms") or []}
        for field_name, item in [("summary", content.summary), *statements]:
            for number in _NUMBER_PATTERN.findall(self._mask_ids(item, allowed_ids)):
                if len(number) < 2:
                    continue
                if number in corpus_text or number in date_terms:
                    continue
                if any(number in term for term in date_terms):
                    continue
                failures.append({"kind": "ungrounded_number", "field": field_name, "value": number})

        content_text = " ".join(
            [content.title, content.summary, *[item for _, item in statements]]
        )
        for cited in set(_ID_PATTERN.findall(content_text)):
            if cited not in allowed_ids:
                failures.append({"kind": "unknown_source_id", "field": "citation", "value": cited})

        return {
            "ok": not failures,
            "failures": failures,
            "warnings": warnings,
            "inferences": list(content.inferences),
            "statements_checked": len(statements),
        }

    # -- long-term planning -------------------------------------------------

    def plan_long_term(
        self, *, persona_id: str, counterpart_id: str, branch_id: str, close_segment: bool = False
    ) -> dict[str, Any]:
        """Give this scope's closed Chapters a Long-term seat.

        One segment stays OPEN and absorbs each newly closed Chapter (that is
        the increment); it closes on its own boundary rules, or explicitly with
        ``close_segment=True`` so it owes a FINAL consolidation.  A Chapter that
        is already inside a segment is never re-used: adding a Chapter later
        produces a segment over the NEW range instead of rewriting history.
        """

        scope = (persona_id, counterpart_id, branch_id)
        chapters = self._chapters_available_for_long_term(scope)
        assigned = 0
        for chapter in chapters[: self.long_term_max_sources]:
            outcome = self._assign_to_level(
                scope=scope, level=2, record=self._summary_record(chapter)
            )
            if outcome["summary_id"]:
                assigned += 1
        current = self._open_summary(scope, 2)
        closed_id: str | None = None
        if close_segment and current is not None:
            closed = self.close_summary(current.id, reason=SummaryBoundaryReason.EXPLICIT_CLOSE)
            closed_id = closed.id if closed else None
            current = None
        self.database.conn.commit()
        return {
            "scope": {
                "persona_id": persona_id,
                "counterpart_id": counterpart_id,
                "branch_id": branch_id,
            },
            "chapters_assigned": assigned,
            "summary_id": current.id if current is not None else closed_id,
            "closed_summary_id": closed_id,
        }

    def _chapters_available_for_long_term(
        self, scope: tuple[str, str, str]
    ) -> list[HierarchicalSummary]:
        rows = self.database.conn.execute(
            """
            SELECT s.* FROM memory_hierarchical_summaries s
            WHERE s.persona_id = ? AND s.counterpart_id = ? AND s.branch_id = ?
              AND s.level = 1 AND s.status = 'closed'
              AND NOT EXISTS (
                SELECT 1 FROM memory_summary_sources src
                WHERE src.source_type = 'summary' AND src.source_id = s.id
              )
            ORDER BY s.started_at ASC
            """,
            scope,
        ).fetchall()
        return [self._row_to_summary(row) for row in rows]

    # -- source fingerprints and dependency invalidation (Phase 6.1) --------

    def source_version(self, source_type: SummarySourceType | str, source_id: str) -> str:
        """The state of ONE source, as the parent's fingerprint sees it.

        Two sources count as "the same version" only when nothing the parent's
        text could depend on has moved: the source's own identity/range, the
        text it currently carries, and how far along its own consolidation is.
        A child stuck at ``failed`` therefore has a DIFFERENT version from the
        same child once it becomes ``ready`` -- which is exactly the Phase 6
        remaining risk this closes.
        """

        kind = str(getattr(source_type, "value", source_type))
        if kind == SummarySourceType.EPISODE.value:
            episode = self.episodes.get_episode(source_id)
            if episode is None:
                return "episode:missing"
            content = dumps(
                {
                    "title": episode.title,
                    "summary": episode.summary,
                    "summary_json": episode.summary_json,
                }
            )
            return (
                f"episode:{episode.source_range_hash}:{episode.summary_status}"
                f":{episode.consolidation_version}:{text_fingerprint(content)}"
            )
        child = self.get_summary(source_id)
        if child is None:
            return "summary:missing"
        if child.status is SummaryStatus.RETRACTED:
            return "summary:retracted"
        content = dumps(
            {
                "title": child.title,
                "summary": child.summary,
                "summary_json": child.content,
                "version": child.consolidation_version,
            }
        )
        return (
            f"summary:{child.source_fingerprint}:{child.summary_status.value}"
            f":{text_fingerprint(content)}"
        )

    def compute_source_fingerprint(self, summary: HierarchicalSummary) -> str:
        """The CURRENT fingerprint of a summary's ordered source set."""

        ordered = [
            (
                source.source_type.value,
                source.source_id,
                self.source_version(source.source_type, source.source_id),
            )
            for source in self.summary_sources(summary.id)
        ]
        return summary_source_fingerprint(
            persona_id=summary.persona_id,
            counterpart_id=summary.counterpart_id,
            branch_id=summary.branch_id,
            level=summary.level,
            sources=ordered,
            consolidation_version=summary.consolidation_version
            or HIERARCHY_CONSOLIDATION_VERSION,
        )

    def refresh_source_fingerprint(self, summary_id: str) -> str:
        """Recompute and store the current fingerprint without touching text."""

        summary = self.get_summary(summary_id)
        if summary is None:
            return ""
        fingerprint = self.compute_source_fingerprint(summary)
        if fingerprint != summary.source_fingerprint:
            self.database.conn.execute(
                "UPDATE memory_hierarchical_summaries SET source_fingerprint = ?, "
                "updated_at = ? WHERE id = ?",
                (fingerprint, datetime.now(UTC).isoformat(), summary_id),
            )
            summary.source_fingerprint = fingerprint
        return fingerprint

    def invalidate_parents(self, summary_id: str, *, reason: str) -> list[str]:
        """Tell the DIRECT parents that this summary changed (Phase 6.1, A1/A2).

        Deliberately non-recursive.  A changed Chapter marks its Long-term
        segment stale; the segment's own parent is only touched once the segment
        has itself been regenerated (which runs this method again).  That is the
        difference between "one Episode changed" and "recompute an entire life"
        -- the propagation is one level per refresh, and each level is durable
        work rather than a synchronous cascade.

        Idempotent by construction: a parent is only marked when its fingerprint
        actually differs from the one its text was built from, so a retry, a
        reopen, or a replay of the same content invalidates nothing.
        """

        stale: list[str] = []
        parents = self.sources_of(SummarySourceType.SUMMARY, summary_id)
        if not parents:
            return stale
        now = datetime.now(UTC).isoformat()
        for parent_id in parents:
            parent = self.get_summary(parent_id)
            if parent is None or parent.status is SummaryStatus.RETRACTED:
                continue
            fingerprint = self.compute_source_fingerprint(parent)
            if fingerprint != parent.source_fingerprint:
                self.database.conn.execute(
                    "UPDATE memory_hierarchical_summaries SET source_fingerprint = ?, "
                    "updated_at = ? WHERE id = ?",
                    (fingerprint, now, parent_id),
                )
            # No recorded text yet: the parent already owes a first consolidation,
            # so there is nothing to invalidate.
            if not parent.consolidated_fingerprint:
                continue
            if fingerprint == parent.consolidated_fingerprint:
                continue
            if parent.summary_status not in _TEXT_BEARING:
                continue
            if parent.summary_status is SummaryReadiness.STALE:
                # Already known to be out of date.  A further change underneath
                # adds no information and must not keep bumping the counter.
                continue
            metadata = dict(parent.metadata)
            metadata["stale_reason"] = reason
            metadata["stale_since"] = now
            metadata["stale_source_fingerprint"] = fingerprint
            metadata["stale_previous_status"] = parent.summary_status.value
            metadata["stale_count"] = int(metadata.get("stale_count") or 0) + 1
            self.database.conn.execute(
                "UPDATE memory_hierarchical_summaries SET summary_status = 'stale', "
                "metadata_json = ?, updated_at = ? WHERE id = ? AND status != 'retracted'",
                (dumps(metadata), now, parent_id),
            )
            stale.append(parent_id)
        self.database.conn.commit()
        return stale

    def stale_summaries(
        self, *, limit: int = 50, persona_id: str | None = None
    ) -> list[HierarchicalSummary]:
        """Direct read of the invalidation backlog (oldest invalidation first)."""

        clauses = ["summary_status = 'stale'", "status != 'retracted'"]
        params: list[Any] = []
        if persona_id:
            clauses.append("persona_id = ?")
            params.append(persona_id)
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"SELECT * FROM memory_hierarchical_summaries WHERE {' AND '.join(clauses)} "
            "ORDER BY level ASC, updated_at ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._row_to_summary(row) for row in rows]

    def child_sources_ready(self, summary: HierarchicalSummary) -> tuple[bool, list[str]]:
        """Whether every source is itself FINAL (READY).

        A FINAL built over a Chapter that never produced text is not a summary,
        it is a summary of nothing -- the text would either be empty or, worse,
        the model would be invited to fill the gap from its own head.  The rule
        is therefore: *final history may only be written over final history*.
        PENDING / FAILED children have no content; PROVISIONAL children have
        content that is about to be regenerated; STALE children have content
        that no longer matches their own sources.  None of them may be baked
        into a parent's FINAL, so the parent stays OWED instead: PENDING/FAILED
        if it never produced text, STALE if it already has text that is now
        demonstrably behind.

        Chapters are always CLOSED by the time a Long-term segment owns them, so
        in practice this reduces to "every Chapter is READY".
        """

        if summary.level < 2:
            return True, []
        pending: list[str] = []
        for source in self.summary_sources(summary.id):
            if source.source_type is not SummarySourceType.SUMMARY:
                continue
            child = self.get_summary(source.source_id)
            if child is None:
                continue
            if child.summary_status is SummaryReadiness.READY:
                continue
            pending.append(source.source_id)
        return not pending, pending

    # -- bookkeeping --------------------------------------------------------

    def _mark(
        self,
        summary_id: str,
        *,
        status: SummaryReadiness,
        error: str | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        summary = self.get_summary(summary_id)
        metadata = dict(summary.metadata) if summary else {}
        if extra:
            metadata.update(extra)
        effective = status
        if (
            status is SummaryReadiness.FAILED
            and summary is not None
            and summary.summary_status is SummaryReadiness.STALE
        ):
            # A FAILED refresh must not throw away text that is still the best
            # available description of this range.  The row stays STALE -- still
            # readable, still owed, still on the work list -- and the failure is
            # recorded instead of being laundered into "no summary at all".
            effective = SummaryReadiness.STALE
            metadata["refresh_failed"] = error
        self.database.conn.execute(
            """
            UPDATE memory_hierarchical_summaries
            SET summary_status = ?, last_error = ?, metadata_json = ?,
                consolidation_attempts = COALESCE(consolidation_attempts, 0) + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (effective.value, error, dumps(metadata), now, summary_id),
        )
        self.database.conn.commit()
        # A readiness change IS a source change: "failed" and "ready" are
        # different versions of the same child, and a parent that cannot tell
        # them apart is the Phase 6 remaining risk this phase closes.
        self.invalidate_parents(summary_id, reason="source_readiness_changed")

    def _mark_blocked(
        self, summary_id: str, *, reason: str, blocked_by: Sequence[str]
    ) -> None:
        """Record "owed, but not able to run yet" without burning an attempt.

        A blocked parent is not a failure: nothing was called, nothing is
        broken, and the row stays on the durable work list.  Existing text is
        demoted to STALE (it can no longer be read as current), text-less rows
        keep their PENDING/FAILED readiness.
        """

        summary = self.get_summary(summary_id)
        if summary is None:
            return
        metadata = dict(summary.metadata)
        metadata["blocked_reason"] = reason
        metadata["blocked_by"] = sorted({str(item) for item in blocked_by})[:12]
        metadata["blocked_at"] = datetime.now(UTC).isoformat()
        status = summary.summary_status
        if status in {SummaryReadiness.READY, SummaryReadiness.PROVISIONAL}:
            status = SummaryReadiness.STALE
        self.database.conn.execute(
            "UPDATE memory_hierarchical_summaries SET summary_status = ?, last_error = ?, "
            "metadata_json = ?, updated_at = ? WHERE id = ?",
            (status.value, reason, dumps(metadata), datetime.now(UTC).isoformat(), summary_id),
        )
        self.database.conn.commit()
        if status is not summary.summary_status:
            self.invalidate_parents(summary_id, reason="source_blocked")

    def _store_content(
        self,
        summary_id: str,
        content: SummaryContent,
        *,
        readiness: SummaryReadiness,
        grounding: dict[str, Any],
        source_tokens: int,
    ) -> None:
        summary = self.get_summary(summary_id)
        metadata = dict(summary.metadata) if summary else {}
        metadata["grounding"] = {
            "warnings": grounding["warnings"][:12],
            "statements_checked": grounding["statements_checked"],
            "inferences": grounding["inferences"][:12],
        }
        metadata["consolidation_version"] = HIERARCHY_CONSOLIDATION_VERSION
        # The text now describes exactly this source state.  Recording the
        # fingerprint here is what makes the NEXT staleness question a hash
        # comparison instead of a model call (Phase 6.1, A3).
        if metadata.get("stale_reason"):
            metadata["last_stale_reason"] = metadata.get("stale_reason")
        metadata.pop("stale_reason", None)
        metadata.pop("stale_since", None)
        metadata.pop("stale_source_fingerprint", None)
        fingerprint = self.compute_source_fingerprint(summary) if summary else ""
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            """
            UPDATE memory_hierarchical_summaries
            SET title = ?, summary = ?, summary_json = ?, importance = ?,
                summary_status = ?, last_error = NULL, consolidated_at = ?,
                consolidation_attempts = COALESCE(consolidation_attempts, 0) + 1,
                source_token_estimate = ?, source_fingerprint = ?,
                consolidated_fingerprint = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                content.title,
                content.summary,
                dumps(content.model_dump(mode="json")),
                content.importance,
                readiness.value,
                now,
                source_tokens,
                fingerprint,
                fingerprint,
                dumps(metadata),
                now,
                summary_id,
            ),
        )
        self.database.conn.commit()
        # A regenerated child is a changed child.  Whether that actually matters
        # to a parent is decided by the fingerprint comparison inside
        # ``invalidate_parents``, not by the fact that a model ran.
        self.invalidate_parents(summary_id, reason="source_content_changed")

    def recover_pending(self, *, limit: int = 200) -> dict[str, Any]:
        """Startup sweep: the durable work list, restored but not run."""

        pending = self.pending_summaries(limit=limit)
        return {
            "pending": len(pending),
            "pending_summary_ids": [summary.id for summary in pending],
        }

    def pending_summaries(
        self,
        *,
        limit: int = 50,
        persona_id: str | None = None,
        room_id: str | None = None,
    ) -> list[HierarchicalSummary]:
        """Summaries that still owe text for their current range, oldest first.

        A READY (final) or PROVISIONAL (current, still open) text is skipped;
        changing the source range, closing the summary, or a source changing
        underneath it puts it back to owed, so nothing is left permanently
        unfinalised.  STALE (Phase 6.1) is owed work for the same reason PENDING
        is -- the text no longer describes the state it was built from.

        ``room_id`` narrows the sweep to Chapters whose Episodes came from that
        room, so opening an old room works its own backlog without touching the
        rest of the database.
        """

        clauses = [
            "s.summary_status IN ('pending', 'failed', 'stale')",
            "s.status != 'retracted'",
        ]
        params: list[Any] = []
        if persona_id:
            clauses.append("s.persona_id = ?")
            params.append(persona_id)
        if room_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM memory_summary_sources src "
                "JOIN memory_episodes e ON e.id = src.source_id "
                "WHERE src.summary_id = s.id AND src.source_type = 'episode' AND e.room_id = ?)"
            )
            params.append(room_id)
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"SELECT s.* FROM memory_hierarchical_summaries s WHERE {' AND '.join(clauses)} "
            "ORDER BY s.level ASC, s.started_at ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._row_to_summary(row) for row in rows]

    # -- reads / inspection ------------------------------------------------

    def get_summary(self, summary_id: str) -> HierarchicalSummary | None:
        row = self.database.conn.execute(
            "SELECT * FROM memory_hierarchical_summaries WHERE id = ?", (summary_id,)
        ).fetchone()
        return self._row_to_summary(row) if row else None

    def list_summaries(
        self,
        *,
        persona_id: str | None = None,
        counterpart_id: str | None = None,
        branch_id: str | None = None,
        level: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[HierarchicalSummary]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("persona_id", persona_id),
            ("counterpart_id", counterpart_id),
            ("branch_id", branch_id),
            ("level", level),
            ("status", status),
        ):
            if value is not None and value != "":
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"SELECT * FROM memory_hierarchical_summaries {where} "
            "ORDER BY level ASC, started_at ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._row_to_summary(row) for row in rows]

    def summary_sources(self, summary_id: str) -> list[SummarySource]:
        rows = self.database.conn.execute(
            "SELECT * FROM memory_summary_sources WHERE summary_id = ? "
            "ORDER BY position ASC, created_at ASC",
            (summary_id,),
        ).fetchall()
        return [self._row_to_source(row) for row in rows]

    def sources_of(self, source_type: SummarySourceType | str, source_id: str) -> list[str]:
        rows = self.database.conn.execute(
            "SELECT summary_id FROM memory_summary_sources WHERE source_type = ? AND source_id = ?",
            (str(getattr(source_type, "value", source_type)), source_id),
        ).fetchall()
        return [str(row["summary_id"]) for row in rows]

    def inspect_summary(
        self, summary_id: str, *, include_chain: bool = True
    ) -> dict[str, Any] | None:
        """Debug view: content, sources, and the walk down to raw text."""

        summary = self.get_summary(summary_id)
        if summary is None:
            return None
        sources: list[dict[str, Any]] = []
        for source in self.summary_sources(summary_id):
            if source.source_type is SummarySourceType.EPISODE:
                episode = self.episodes.get_episode(source.source_id)
                turns = self.episodes.episode_turns(source.source_id) if episode else []
                sources.append(
                    {
                        "source_type": "episode",
                        "source_id": source.source_id,
                        "position": source.position,
                        "available": episode is not None,
                        "title": episode.title if episode else "",
                        "turn_count": len(turns),
                        "resolvable": any(
                            self.episodes.resolve_turn_text(turn) for turn in turns
                        ),
                        "turn_ids": [turn.turn_id for turn in turns] if include_chain else [],
                    }
                )
            else:
                nested = self.get_summary(source.source_id)
                nested_sources = self.summary_sources(source.source_id) if nested else []
                episode_ids = [
                    item.source_id
                    for item in nested_sources
                    if item.source_type is SummarySourceType.EPISODE
                ]
                sources.append(
                    {
                        "source_type": "summary",
                        "source_id": source.source_id,
                        "position": source.position,
                        "available": nested is not None,
                        "title": nested.title if nested else "",
                        "level": nested.level if nested else None,
                        "episode_count": len(episode_ids),
                        "resolvable": bool(episode_ids),
                        "episode_ids": episode_ids if include_chain else [],
                    }
                )
        return {
            "summary": summary.model_dump(mode="json"),
            "content": summary.structured.model_dump(mode="json"),
            "sources": sources,
            "source_ids": [item.source_id for item in self.summary_sources(summary_id)],
            "parent_summary_id": summary.parent_summary_id,
            "time_range": summary.time_range_label(),
            "unavailable_source_count": int(
                summary.metadata.get("unavailable_source_count") or 0
            ),
            "source_availability": summary.metadata.get("source_availability", "complete"),
            # Phase 6.1: why this summary is (or is not) still current.
            "source_fingerprint": summary.source_fingerprint,
            "consolidated_fingerprint": summary.consolidated_fingerprint,
            "fingerprint_mismatch": summary.fingerprint_mismatch,
            "stale_reason": summary.metadata.get("stale_reason"),
            "stale_since": summary.metadata.get("stale_since"),
            "blocked_reason": summary.metadata.get("blocked_reason"),
            "blocked_by": summary.metadata.get("blocked_by") or [],
            "deferred_close_reason": summary.metadata.get("deferred_close_reason"),
            "child_sources_ready": self.child_sources_ready(summary)[0],
            "all_sources_resolvable": all(item["resolvable"] for item in sources)
            if sources
            else False,
        }

    def stats(
        self,
        *,
        persona_id: str | None = None,
        counterpart_id: str | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("persona_id", persona_id),
            ("counterpart_id", counterpart_id),
            ("branch_id", branch_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.database.conn.execute(
            f"SELECT level, status, summary_status, COUNT(*) AS c "
            f"FROM memory_hierarchical_summaries {where} "
            "GROUP BY level, status, summary_status",
            tuple(params),
        ).fetchall()
        by_level: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_readiness: dict[str, int] = {}
        for row in rows:
            count = int(row["c"])
            level = str(int(row["level"]))
            by_level[level] = by_level.get(level, 0) + count
            by_status[str(row["status"])] = by_status.get(str(row["status"]), 0) + count
            readiness = str(row["summary_status"])
            by_readiness[readiness] = by_readiness.get(readiness, 0) + count
        return {
            "summaries": sum(by_level.values()),
            "by_level": by_level,
            "by_status": by_status,
            "by_readiness": by_readiness,
            "available": self.count_available(persona_id=persona_id),
            "pending": self.count_pending(persona_id=persona_id),
            "stale": self.count_stale(persona_id=persona_id),
            "ungrouped_episodes": self.count_ungrouped_episodes(persona_id=persona_id),
        }

    def count_available(self, *, persona_id: str | None = None) -> int:
        """Summaries whose stored text can be read as CURRENT history.

        STALE text is deliberately excluded: it is still on disk, still
        inspectable, and still reported in ``by_readiness`` -- but it no longer
        describes the sources it claims to describe, so it must not be counted
        as an available summary.
        """

        clauses = [
            "status != 'retracted'",
            "summary_status IN ('provisional', 'ready')",
        ]
        params: list[Any] = []
        if persona_id:
            clauses.append("persona_id = ?")
            params.append(persona_id)
        return int(
            self.database.conn.execute(
                f"SELECT COUNT(*) AS c FROM memory_hierarchical_summaries "
                f"WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()["c"]
        )

    def count_stale(self, *, persona_id: str | None = None) -> int:
        clauses = ["summary_status = 'stale'", "status != 'retracted'"]
        params: list[Any] = []
        if persona_id:
            clauses.append("persona_id = ?")
            params.append(persona_id)
        return int(
            self.database.conn.execute(
                f"SELECT COUNT(*) AS c FROM memory_hierarchical_summaries "
                f"WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()["c"]
        )

    def count_pending(
        self, *, persona_id: str | None = None, room_id: str | None = None
    ) -> int:
        return len(
            self.pending_summaries(limit=1000, persona_id=persona_id, room_id=room_id)
        )

    # -- deletion lifecycle (Phase 3.1 semantics) --------------------------

    def stamp_sources_unavailable(
        self, *, episode_ids: Sequence[str], availability: str
    ) -> dict[str, Any]:
        """An explicitly deleted source must be visible, never silently dangling.

        A Chapter that loses every Episode becomes RETRACTED (its source rows
        are removed so the Episodes could be re-grouped if they come back), and
        its tombstone keeps the reason.  A Long-term segment that loses every
        Chapter follows the same rule.
        """

        if not episode_ids or not self._table_exists("memory_hierarchical_summaries"):
            return {"summaries_stamped": 0, "retracted": 0}
        placeholders = ",".join("?" for _ in episode_ids)
        affected = [
            str(row["summary_id"])
            for row in self.database.conn.execute(
                f"SELECT DISTINCT summary_id FROM memory_summary_sources "
                f"WHERE source_type = 'episode' AND source_id IN ({placeholders})",
                tuple(episode_ids),
            ).fetchall()
        ]
        now = datetime.now(UTC).isoformat()
        retracted = 0
        for summary_id in affected:
            unavailable = int(
                self.database.conn.execute(
                    f"SELECT COUNT(*) AS c FROM memory_summary_sources "
                    f"WHERE summary_id = ? AND source_type = 'episode' "
                    f"AND source_id IN ({placeholders})",
                    (summary_id, *episode_ids),
                ).fetchone()["c"]
            )
            total = len(self.summary_sources(summary_id))
            summary = self.get_summary(summary_id)
            if summary is None:
                continue
            metadata = dict(summary.metadata)
            metadata["unavailable_source_count"] = (
                int(metadata.get("unavailable_source_count") or 0) + unavailable
            )
            state = (
                "deleted"
                if total and unavailable >= total
                else ("partial" if unavailable else "complete")
            )
            metadata["source_availability"] = state
            metadata["provenance_note"] = "source episodes removed"
            self.database.conn.execute(
                "UPDATE memory_hierarchical_summaries SET metadata_json = ?, updated_at = ? "
                "WHERE id = ?",
                (dumps(metadata), now, summary_id),
            )
            if state == "deleted":
                metadata["retracted_reason"] = "sources_deleted"
                self.database.conn.execute(
                    "UPDATE memory_hierarchical_summaries SET status = 'retracted', "
                    "metadata_json = ?, updated_at = ? WHERE id = ?",
                    (dumps(metadata), now, summary_id),
                )
                self.database.conn.execute(
                    "DELETE FROM memory_summary_sources WHERE summary_id = ?", (summary_id,)
                )
                retracted += 1
        self.database.conn.commit()
        # A retracted chapter leaves its Long-term parent short a source.
        propagated = self._propagate_availability(
            affected, availability=availability, cutoff_ids=set(episode_ids)
        )
        return {
            "summaries_stamped": len(affected) + propagated,
            "retracted": retracted,
        }

    def _propagate_availability(
        self, summary_ids: Sequence[str], *, availability: str, cutoff_ids: set[str]
    ) -> int:
        parents: set[str] = set()
        for summary_id in summary_ids:
            parents.update(self.sources_of(SummarySourceType.SUMMARY, summary_id))
        stamped = 0
        now = datetime.now(UTC).isoformat()
        for parent_id in sorted(parents):
            parent = self.get_summary(parent_id)
            if parent is None:
                continue
            sources = self.summary_sources(parent_id)
            unavailable = 0
            for source in sources:
                child = self.get_summary(source.source_id)
                if child is None or child.status is SummaryStatus.RETRACTED:
                    unavailable += 1
            metadata = dict(parent.metadata)
            metadata["source_availability"] = (
                "deleted" if sources and unavailable >= len(sources) else "partial"
            )
            metadata["unavailable_source_count"] = unavailable
            # A retracted child is a changed child: the parent's inputs moved.
            self.refresh_source_fingerprint(parent_id)
            if parent.consolidated_fingerprint and unavailable:
                metadata["stale_reason"] = "source_unavailable"
                metadata["stale_since"] = now
            self.database.conn.execute(
                "UPDATE memory_hierarchical_summaries SET metadata_json = ?, updated_at = ? "
                "WHERE id = ?",
                (dumps(metadata), now, parent_id),
            )
            if sources and unavailable >= len(sources):
                metadata["retracted_reason"] = "sources_deleted"
                self.database.conn.execute(
                    "UPDATE memory_hierarchical_summaries SET status = 'retracted', "
                    "metadata_json = ?, updated_at = ? WHERE id = ?",
                    (dumps(metadata), now, parent_id),
                )
                self.database.conn.execute(
                    "DELETE FROM memory_summary_sources WHERE summary_id = ?", (parent_id,)
                )
            stamped += 1
        self.database.conn.commit()
        return stamped

    # -- internals ---------------------------------------------------------

    def _table_exists(self, name: str) -> bool:
        row = self.database.conn.execute(
            "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def _next_sequence(self, scope: tuple[str, str, str], level: int) -> int:
        row = self.database.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS s FROM memory_hierarchical_summaries "
            "WHERE persona_id = ? AND counterpart_id = ? AND branch_id = ? AND level = ?",
            (*scope, int(level)),
        ).fetchone()
        return int(row["s"]) + 1

    def _next_position(self, summary_id: str) -> int:
        row = self.database.conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS p FROM memory_summary_sources "
            "WHERE summary_id = ?",
            (summary_id,),
        ).fetchone()
        return int(row["p"]) + 1

    def _insert_summary(self, summary: HierarchicalSummary) -> None:
        columns = (
            "id", "persona_id", "counterpart_id", "branch_id", "level", "summary_type",
            "sequence", "title", "summary", "summary_json", "started_at", "ended_at",
            "status", "source_count", "source_token_estimate", "importance", "created_at",
            "updated_at", "consolidation_version", "summary_status",
            "consolidation_attempts", "last_error", "consolidated_at", "source_range_hash",
            "source_fingerprint", "consolidated_fingerprint",
            "parent_summary_id", "visibility", "material_scope", "metadata_json",
        )
        values = (
            summary.id, summary.persona_id, summary.counterpart_id, summary.branch_id,
            summary.level, summary.summary_type.value, summary.sequence, summary.title,
            summary.summary, dumps(summary.content), dt(summary.started_at),
            dt(summary.ended_at), summary.status.value, summary.source_count,
            summary.source_token_estimate, summary.importance, dt(summary.created_at),
            dt(summary.updated_at), summary.consolidation_version,
            summary.summary_status.value, summary.consolidation_attempts,
            summary.last_error, dt(summary.consolidated_at), summary.source_range_hash,
            summary.source_fingerprint, summary.consolidated_fingerprint,
            summary.parent_summary_id, summary.visibility, summary.material_scope,
            dumps(summary.metadata),
        )
        self.database.conn.execute(
            f"INSERT INTO memory_hierarchical_summaries ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )

    def _insert_source(
        self, summary_id: str, record: _SourceRecord, *, position: int
    ) -> int:
        cursor = self.database.conn.execute(
            """
            INSERT OR IGNORE INTO memory_summary_sources (
              summary_id, source_type, source_id, position, started_at, ended_at,
              importance, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                summary_id,
                record.source_type.value,
                record.source_id,
                position,
                dt(record.started_at),
                dt(record.ended_at),
                record.importance,
                datetime.now(UTC).isoformat(),
            ),
        )
        return int(cursor.rowcount or 0)

    def _refresh_aggregates(self, summary_id: str) -> None:
        """Recompute from the rows: no counters to drift."""

        row = self.database.conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MIN(started_at), '') AS first_start, "
            "COALESCE(MAX(COALESCE(ended_at, started_at)), '') AS last_end "
            "FROM memory_summary_sources WHERE summary_id = ?",
            (summary_id,),
        ).fetchone()
        source_ids = [item.source_id for item in self.summary_sources(summary_id)]
        summary = self.get_summary(summary_id)
        if summary is None:
            return
        tokens = self._source_tokens(summary, source_ids)
        range_hash = summary_range_hash(
            persona_id=summary.persona_id,
            counterpart_id=summary.counterpart_id,
            branch_id=summary.branch_id,
            level=summary.level,
            source_ids=source_ids,
        )
        # The source set changed, so the CURRENT fingerprint of that set must be
        # recomputed here rather than at read time: staleness is a stored fact.
        fingerprint = self.compute_source_fingerprint(summary)
        started = parse_dt(row["first_start"]) or summary.started_at
        ended = parse_dt(row["last_end"]) or summary.ended_at
        self.database.conn.execute(
            """
            UPDATE memory_hierarchical_summaries
            SET source_count = ?, source_token_estimate = ?, source_range_hash = ?,
                source_fingerprint = ?, started_at = ?, ended_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(row["c"]),
                tokens,
                range_hash,
                fingerprint,
                dt(started),
                dt(ended),
                datetime.now(UTC).isoformat(),
                summary_id,
            ),
        )

    def _source_tokens(self, summary: HierarchicalSummary, source_ids: list[str]) -> int:
        if not source_ids:
            return 0
        if summary.level <= 1:
            placeholders = ",".join("?" for _ in source_ids)
            row = self.database.conn.execute(
                f"SELECT COALESCE(SUM(source_token_estimate), 0) AS t FROM memory_episodes "
                f"WHERE id IN ({placeholders})",
                tuple(source_ids),
            ).fetchone()
            return int(row["t"] or 0)
        total = 0
        for source in self.summary_sources(summary.id):
            total += int(self._summary_source_tokens(source.source_id))
        return total

    def _summary_source_tokens(self, summary_id: str) -> int:
        row = self.database.conn.execute(
            "SELECT source_token_estimate FROM memory_hierarchical_summaries WHERE id = ?",
            (summary_id,),
        ).fetchone()
        return int(row["source_token_estimate"] or 0) if row else 0

    @staticmethod
    def _row_to_summary(row: Any) -> HierarchicalSummary:
        def _enum(enum_cls: Any, value: Any, fallback: Any) -> Any:
            try:
                return enum_cls(str(value))
            except ValueError:
                return fallback

        return HierarchicalSummary(
            id=str(row["id"]),
            persona_id=str(row["persona_id"]),
            counterpart_id=str(row["counterpart_id"]),
            branch_id=str(row["branch_id"]),
            level=int(row["level"] or 1),
            summary_type=_enum(SummaryType, row["summary_type"], SummaryType.CHAPTER),
            sequence=int(row["sequence"] or 1),
            title=str(row["title"] or ""),
            summary=str(row["summary"] or ""),
            content=dict(loads(row["summary_json"])),
            started_at=parse_dt(row["started_at"]) or datetime.now(UTC),
            ended_at=parse_dt(row["ended_at"]),
            status=_enum(SummaryStatus, row["status"], SummaryStatus.OPEN),
            source_count=int(row["source_count"] or 0),
            source_token_estimate=int(row["source_token_estimate"] or 0),
            importance=float(row["importance"] or 0.5),
            created_at=parse_dt(row["created_at"]) or datetime.now(UTC),
            updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
            consolidation_version=int(row["consolidation_version"] or 1),
            summary_status=_enum(
                SummaryReadiness, row["summary_status"], SummaryReadiness.PENDING
            ),
            consolidation_attempts=int(row["consolidation_attempts"] or 0),
            last_error=row["last_error"],
            consolidated_at=parse_dt(row["consolidated_at"]),
            source_range_hash=str(row["source_range_hash"] or ""),
            source_fingerprint=str(row["source_fingerprint"] or ""),
            consolidated_fingerprint=str(row["consolidated_fingerprint"] or ""),
            parent_summary_id=row["parent_summary_id"],
            visibility=str(row["visibility"] or "private_session"),
            material_scope=str(row["material_scope"] or CHARACTER_VISIBLE),
            metadata=dict(loads(row["metadata_json"])),
        )

    @staticmethod
    def _row_to_source(row: Any) -> SummarySource:
        try:
            source_type = SummarySourceType(str(row["source_type"]))
        except ValueError:
            source_type = SummarySourceType.EPISODE
        return SummarySource(
            summary_id=str(row["summary_id"]),
            source_type=source_type,
            source_id=str(row["source_id"]),
            position=int(row["position"] or 0),
            started_at=parse_dt(row["started_at"]),
            ended_at=parse_dt(row["ended_at"]),
            importance=float(row["importance"] or 0.5),
        )


__all__ = [
    "CHAPTER_SUMMARY_INTRO",
    "LONG_TERM_SUMMARY_INTRO",
    "SUMMARY_CONTENT_SCHEMA",
    "HierarchyService",
    "SummaryContent",
    "summary_type_for_level",
    "source_type_for_level",
]
