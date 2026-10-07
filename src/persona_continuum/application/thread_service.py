"""Active Threads: resolution, lifecycle, provenance, ambiguity safety.

Pipeline position::

    committed turn -> Episode -> Episode summary (optional)
                              -> Semantic Facts (Phase 4)
                              -> Active Thread resolution   <- this module
                                     -> CREATE / UPDATE / MILESTONE / WAITING
                                     -> RESOLVE / CANCEL / REOPEN / NOOP
                                     -> candidate link (when unsure)

Design rules the code enforces rather than hopes for:

* **A thread survives the window.**  Nothing here reads a message count, a
  prompt budget, or a rolling summary.  A thread ends when the conversation
  says it ended, never because it scrolled out of view.
* **Weak continuation is the point.**  "票我买好了。" carries no keyword that
  matches "重庆旅行", so candidate selection always keeps the most recently
  active threads in front of the model instead of relying on lexical overlap
  alone.  RecallGate's regexes are NOT extended for this: that is Phase 8's
  problem, and this layer exists precisely because regex cannot solve it.
* **Unsure means candidate, not wrong binding.**  Below the configured
  confidence threshold (or when the resolver reports several plausible targets)
  nothing is written to a thread; an explicit ``memory_thread_link_candidates``
  row is recorded for inspection instead.
* **History is append-only.**  Every accepted action appends a
  ``memory_thread_events`` row behind a deterministic ``event_key``, so a
  replayed Episode cannot duplicate a milestone or a thread.
* **Nothing here reaches a prompt.**  Phase 5 stores, resolves and inspects;
  the Context Assembly Report says so explicitly.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from persona_continuum.application._utils import dt, dumps, loads, new_id, parse_dt
from persona_continuum.domain.provenance import CHARACTER_VISIBLE
from persona_continuum.domain.thread import (
    LIVE_STATUSES,
    REOPENABLE_STATUSES,
    THREAD_RESOLUTION_VERSION,
    MemoryActiveThread,
    ThreadAction,
    ThreadEvent,
    ThreadEventType,
    ThreadEvidenceRole,
    ThreadFactRelation,
    ThreadOperation,
    ThreadResolutionPayload,
    ThreadStatus,
    ThreadType,
    canonical_thread_key,
    normalize_thread_text,
)
from persona_continuum.storage.database import Database

#: What the resolver is asked for.  Every field is optional so a partial answer
#: degrades into a weaker action instead of an invalid payload.
THREAD_RESOLUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [operation.value for operation in ThreadOperation],
                    },
                    "thread_id": {"type": ["string", "null"]},
                    "thread_type": {
                        "type": "string",
                        "enum": [item.value for item in ThreadType],
                    },
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "thread_key_hint": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "state_update": {"type": ["string", "null"]},
                    "milestone": {"type": ["string", "null"]},
                    "source_turn_ids": {"type": "array", "items": {"type": "string"}},
                    "related_fact_ids": {"type": "array", "items": {"type": "string"}},
                    "ambiguous_thread_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["operation"],
            },
        }
    },
    "required": ["threads"],
}

THREAD_RESOLUTION_INTRO = (
    "你在为一个长期对话系统维护「进行中的事情」（Active Threads）。"
    "Thread 表示一件还没有真正结束、未来聊天仍可能继续发展的事情"
    "（计划、目标、承诺、未解决的矛盾、项目、等待结果、长期推进的事项）。"
    "输入是这段对话的原始轮次、它的结构化摘要、这段对话产出的事实，"
    "以及当前作用域下已经在进行中的 Thread 候选列表。这是状态维护任务，不是角色扮演。\n"
    "speaker 字段描述这一条轮次里谁在说话：speaker=user+persona 表示同一条轮次里同时包含"
    "用户与人格的发言（文本中的 user: / persona: 前缀标明各自说了什么）。\n"
    "硬性要求：\n"
    "1. Thread 与事实不同：一次性的事实、情绪、闲聊不要建 Thread。"
    "「今天午饭挺好吃」→ 通常 noop。只有未来仍可能继续发展的事情才建 Thread。\n"
    "2. 优先延续已有的 Thread，而不是新建。用户说的往往只是进展"
    "（例如 Thread「重庆旅行」，用户只说「票我买好了。」）——"
    "这种情况下必须 update 或 milestone 那个已有 Thread，不要新建「买票」Thread。"
    "候选列表里可能没有关键词重合，请按语义判断：只要它在讲同一件事的后续，就用那个 thread_id。\n"
    "3. 指代（「她又联系我了」「还是之前那个」）要结合候选列表判断；"
    "如果同时有多个同样合理的候选、证据不足，不要猜：把这些 thread_id 放进 ambiguous_thread_ids，"
    "operation 用 noop，让系统记为待确认。\n"
    "3.1 候选列表里 status=resolved/cancelled 的 Thread 是已经结束的事："
    "如果用户明确在讲这件事又重新开始了（「结果今天我们又吵起来了」），用 reopen 指向它；"
    "如果只是顺带提起过去，用 noop。\n"
    "4. operation 取值：create（新建）/ update（延续或状态更新）/ milestone（推进到一个节点，"
    "例如买票、报名、第一步做完）/ waiting（在等别人或等结果）/ resolve（这件事已经结束、"
    "有明确结束证据）/ cancel（明确取消）/ reopen（已经结束的事情又开始了）/ noop（什么都不做）。\n"
    "5. 只有出现了明确的结束证据（「已经和好了」「问题修好了」「我从重庆回来了」）才能 resolve；"
    "仅仅是很久没聊到不能算结束。\n"
    "6. 阶段推进不等于完成：买了票不等于旅行结束。\n"
    "7. thread_id 只能从候选列表里取；新建 Thread 时不要编造 id。"
    "thread_key_hint 用一个简短的主题词（例如「重庆旅行」），不要带「计划」「问题」这类泛词。\n"
    "8. source_turn_ids 只能引用本段轮次里真实出现的 turn_id；"
    "related_fact_ids 只能引用本次输入列出的事实 id。\n"
    "9. confidence 要如实：证据不足就降低，并考虑用 ambiguous_thread_ids 而不是硬绑。\n"
    "只输出结构化结果。"
)

_MAX_INPUT_TURNS_TEXT_CHARS = 24000
_MAX_STATE_MILESTONES = 24
_MAX_REASON_CHARS = 400

_CJK_RANGE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_WORD = re.compile(r"[a-zA-Z0-9_]{2,}")


def _lexical_tokens(text: str) -> set[str]:
    """CJK bigrams + latin words.  Cheap, deterministic, no segmentation model."""

    normalized = normalize_thread_text(text)
    tokens = set(_LATIN_WORD.findall(normalized))
    cjk = [char for char in normalized if _CJK_RANGE.match(char)]
    if len(cjk) == 1:
        tokens.add(cjk[0])
    for index in range(len(cjk) - 1):
        tokens.add(cjk[index] + cjk[index + 1])
    return tokens


def _overlap_ratio(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    smaller = min(len(left), len(right))
    return len(left & right) / float(smaller or 1)


def _event_identity(*parts: str) -> str:
    payload = json.dumps(
        [str(part or "") for part in parts], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class ThreadService:
    """Owns the Active Thread store.  Never deletes raw history."""

    def __init__(
        self,
        database: Database,
        episodes: Any,
        facts: Any = None,
        *,
        config: Any = None,
    ) -> None:
        self.database = database
        self.episodes = episodes
        self.facts = facts
        self.config = config
        self.enabled = bool(getattr(config, "memory_thread_resolution_enabled", True))
        self.batch = max(1, int(getattr(config, "memory_thread_resolution_batch", 2) or 2))
        self.max_candidates = max(
            4, int(getattr(config, "memory_thread_max_candidates", 24) or 24)
        )
        self.top_recent = max(1, int(getattr(config, "memory_thread_top_recent", 8) or 8))
        self.link_threshold = float(
            getattr(config, "memory_thread_link_confidence_threshold", 0.55) or 0.55
        )
        self.stale_after_days = max(
            0, int(getattr(config, "memory_thread_stale_after_days", 60) or 0)
        )
        self.reopen_window_days = max(
            0, int(getattr(config, "memory_thread_reopen_window_days", 30) or 0)
        )
        self.max_creates = max(
            1, int(getattr(config, "memory_thread_max_creates_per_episode", 3) or 3)
        )
        self.input_max_tokens = max(
            512,
            int(getattr(config, "memory_thread_resolution_input_max_tokens", 12000) or 12000),
        )
        self.confidence_step = max(
            0.0, float(getattr(config, "memory_fact_confidence_step", 0.05) or 0.0)
        )

    # -- candidate selection (deterministic, no model) ----------------------

    def live_threads(
        self,
        *,
        persona_id: str,
        counterpart_id: str,
        branch_id: str,
        limit: int | None = None,
    ) -> list[MemoryActiveThread]:
        statuses = tuple(status.value for status in sorted(LIVE_STATUSES))
        placeholders = ",".join("?" for _ in statuses)
        params: list[Any] = [persona_id, counterpart_id, branch_id, *statuses]
        clause = ""
        if limit is not None:
            clause = " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            "SELECT * FROM memory_active_threads "
            "WHERE persona_id = ? AND counterpart_id = ? AND branch_id = ? "
            f"AND status IN ({placeholders}) "
            f"ORDER BY last_activity_at DESC{clause}",
            tuple(params),
        ).fetchall()
        return [self._row_to_thread(row) for row in rows]

    def recently_closed_threads(
        self,
        *,
        persona_id: str,
        counterpart_id: str,
        branch_id: str,
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[MemoryActiveThread]:
        """Threads that ended inside the reopen window.

        Without these the resolver could never REOPEN anything: a closed thread
        would be invisible, and "结果今天我们又吵起来了。" would look like a
        brand-new subject every time.  Only the recent ones are shown, so
        ancient history cannot crowd out what is actually in flight.
        """

        if self.reopen_window_days <= 0:
            return []
        # The window is measured on the conversation's timeline, not the wall
        # clock, so imported history can still reopen its own threads.
        moment = now or datetime.now(UTC)
        cutoff = (moment - timedelta(days=self.reopen_window_days)).isoformat()
        rows = self.database.conn.execute(
            """
            SELECT * FROM memory_active_threads
            WHERE persona_id = ? AND counterpart_id = ? AND branch_id = ?
              AND status IN ('resolved', 'cancelled')
              AND COALESCE(resolved_at, cancelled_at, last_activity_at) >= ?
            ORDER BY COALESCE(resolved_at, cancelled_at, last_activity_at) DESC
            LIMIT ?
            """,
            (persona_id, counterpart_id, branch_id, cutoff, max(1, int(limit))),
        ).fetchall()
        return [self._row_to_thread(row) for row in rows]

    def candidate_threads(
        self, episode: Any, *, limit: int | None = None
    ) -> list[MemoryActiveThread]:
        """Shortlist what the resolver may continue.

        Three sources are merged, because any one of them alone loses a case
        that matters:

        * **recent activity** -- always keeps ``top_recent`` live threads, even
          with zero lexical overlap.  This is what lets "票我买好了。" see
          "重庆旅行" at all.
        * **lexical/topic overlap** -- a strong signal when it exists.
        * **fact lineage** -- a thread already linked to one of this episode's
          facts is almost certainly the continuation target.

        Recently closed threads (inside the reopen window) are appended after
        the live ones, because a REOPEN decision needs to see what it would
        revive.

        The result is capped by ``memory_thread_max_candidates``; this is a
        Memory-Consolidation budget and deliberately NOT a prompt budget.
        """

        scope = (
            episode.persona_id,
            episode.counterpart_id,
            episode.branch_id,
        )
        live = self.live_threads(
            persona_id=scope[0], counterpart_id=scope[1], branch_id=scope[2]
        )
        cap = max(1, int(limit or self.max_candidates))
        episode_tokens = _lexical_tokens(self._episode_text(episode))
        fact_ids = self._episode_fact_ids(episode.id)
        linked_thread_ids: set[str] = set()
        if fact_ids:
            placeholders = ",".join("?" for _ in fact_ids)
            linked_thread_ids = {
                str(row["thread_id"])
                for row in self.database.conn.execute(
                    f"SELECT DISTINCT thread_id FROM memory_thread_facts "
                    f"WHERE fact_id IN ({placeholders})",
                    tuple(fact_ids),
                ).fetchall()
            }
        now: datetime = (
            getattr(episode, "ended_at", None)
            or getattr(episode, "started_at", None)
            or datetime.now(UTC)
        )
        scored: list[tuple[float, MemoryActiveThread]] = []
        for thread in live:
            score = 1.5 * float(thread.importance)
            if thread.id in linked_thread_ids:
                score += 2.5
            if episode_tokens:
                thread_tokens = _lexical_tokens(
                    " ".join(
                        [
                            thread.title,
                            thread.summary,
                            *[
                                str(value)
                                for value in thread.current_state.values()
                                if isinstance(value, str)
                            ],
                        ]
                    )
                )
                score += 3.0 * _overlap_ratio(episode_tokens, thread_tokens)
            age_days = max(0.0, (now - (thread.last_activity_at or thread.updated_at)).days)
            score += max(0.0, 1.0 - (age_days / 30.0))
            scored.append((score, thread))
        scored.sort(key=lambda item: (item[0], item[1].last_activity_at), reverse=True)
        selected: list[MemoryActiveThread] = [thread for _, thread in scored[:cap]]
        selected_ids = {thread.id for thread in selected}
        # Recent threads are guaranteed a seat: semantic continuation must not
        # depend on the episode happening to share vocabulary with the thread.
        for thread in sorted(live, key=lambda item: item.last_activity_at, reverse=True):
            if len(selected) >= cap:
                break
            if thread.id in selected_ids:
                continue
            selected.append(thread)
            selected_ids.add(thread.id)
        # Recently CLOSED threads come last: enough room for a REOPEN, without
        # letting ended matters crowd out what is still in flight.
        for thread in self.recently_closed_threads(
            persona_id=scope[0],
            counterpart_id=scope[1],
            branch_id=scope[2],
            limit=max(1, cap // 4),
            now=now,
        ):
            if len(selected) >= cap:
                break
            if thread.id in selected_ids:
                continue
            selected.append(thread)
            selected_ids.add(thread.id)
        return selected

    def _episode_text(self, episode: Any) -> str:
        parts: list[str] = []
        used = 0
        for turn in self.episodes.episode_turns(episode.id):
            text = self.episodes.resolve_turn_text(turn)
            if not text:
                continue
            parts.append(text)
            used += len(text)
            if used >= _MAX_INPUT_TURNS_TEXT_CHARS:
                break
        summary = episode.structured_summary()
        if summary and not summary.is_empty:
            parts.extend([summary.title, summary.summary, *summary.topics, *summary.entities])
        return "\n".join(part for part in parts if part)

    def _episode_fact_ids(self, episode_id: str) -> list[str]:
        if not self._table_exists("memory_fact_sources"):
            return []
        return [
            str(row["fact_id"])
            for row in self.database.conn.execute(
                "SELECT DISTINCT fact_id FROM memory_fact_sources WHERE episode_id = ?",
                (episode_id,),
            ).fetchall()
        ]

    def build_resolution_payload(self, episode: Any) -> dict[str, Any] | None:
        """Turns + summary + this episode's facts + the live thread shortlist."""

        turns = self.episodes.episode_turns(episode.id)
        if not turns:
            return None
        admitted: list[dict[str, str]] = []
        used = 0
        for turn in turns:
            text = self.episodes.resolve_turn_text(turn)
            if not text:
                continue
            if admitted and used + len(text) > _MAX_INPUT_TURNS_TEXT_CHARS:
                break
            admitted.append(
                {
                    "turn_id": turn.turn_id,
                    "speaker": self.episodes.turn_speaker_label(turn),
                    "text": text,
                }
            )
            used += len(text)
        if not admitted:
            return None
        payload: dict[str, Any] = {
            "scope": {
                "persona_id": episode.persona_id,
                "counterpart_id": episode.counterpart_id,
                "branch_id": episode.branch_id,
            },
            "episode": {
                "episode_id": episode.id,
                "started_at": episode.started_at.isoformat(),
                "ended_at": (episode.ended_at or episode.updated_at).isoformat(),
                "title": episode.title,
            },
            "turns": admitted,
            "existing_threads": [
                {
                    "thread_id": thread.id,
                    "thread_type": thread.thread_type.value,
                    "title": thread.title,
                    "summary": thread.summary,
                    "status": thread.status.value,
                    "opened_at": thread.opened_at.isoformat(),
                    "last_activity_at": thread.last_activity_at.isoformat(),
                    "milestones": thread.milestones,
                    "current_state": {
                        key: value
                        for key, value in thread.current_state.items()
                        if isinstance(value, str)
                    },
                }
                for thread in self.candidate_threads(episode)
            ],
            "episode_facts": self._episode_fact_context(episode.id),
        }
        summary = episode.structured_summary()
        if summary and not summary.is_empty:
            payload["episode_summary"] = {
                "title": summary.title,
                "summary": summary.summary,
                "commitments": summary.commitments,
                "unresolved": summary.unresolved,
                "topics": summary.topics,
                "entities": summary.entities,
                "user_stated": summary.user_stated,
            }
        return payload

    def _episode_fact_context(self, episode_id: str) -> list[dict[str, Any]]:
        if not self._table_exists("memory_fact_sources"):
            return []
        rows = self.database.conn.execute(
            """
            SELECT f.id, f.category, f.display_text, f.status, f.plan_status, f.subject,
                   f.predicate
            FROM memory_semantic_facts f
            WHERE EXISTS (
              SELECT 1 FROM memory_fact_sources s
              WHERE s.fact_id = f.id AND s.episode_id = ?
            )
            ORDER BY f.created_at ASC
            LIMIT 32
            """,
            (episode_id,),
        ).fetchall()
        return [
            {
                "fact_id": str(row["id"]),
                "category": str(row["category"]),
                "display_text": str(row["display_text"] or ""),
                "status": str(row["status"] or ""),
                "plan_status": str(row["plan_status"] or ""),
            }
            for row in rows
        ]

    # -- resolution (async, bounded, retryable) ----------------------------

    async def resolve_episode_threads(
        self,
        episode_id: str,
        *,
        resolve: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]],
        force: bool = False,
    ) -> dict[str, Any]:
        """Resolve one Episode's threads.  Never raises on a provider problem."""

        report: dict[str, Any] = {
            "episode_id": episode_id,
            "action": "noop",
            "threads_created": 0,
            "threads_updated": 0,
            "threads_milestoned": 0,
            "threads_waiting": 0,
            "threads_resolved": 0,
            "threads_cancelled": 0,
            "threads_reopened": 0,
            "threads_merged": 0,
            "candidates_recorded": 0,
            "candidate_links": 0,
            "skipped_low_confidence": 0,
            "skipped_noop": 0,
            "skipped_invalid": 0,
            "replayed": 0,
            "resolution_version": THREAD_RESOLUTION_VERSION,
            "pending": False,
            "error": None,
        }
        if not self.enabled:
            report["error"] = "thread_resolution_disabled"
            return report
        episode = self.episodes.get_episode(episode_id)
        if episode is None:
            report["error"] = "episode_not_found"
            return report
        if episode.thread_resolution_status == "ready" and not force:
            return report
        payload = self.build_resolution_payload(episode)
        if payload is None:
            report["error"] = "no_source_turns"
            self._mark_resolution(episode_id, status="failed", error="no_source_turns")
            report["pending"] = True
            return report
        try:
            raw = await resolve(payload, THREAD_RESOLUTION_SCHEMA)
        except Exception as exc:  # noqa: BLE001 - any provider failure is retryable
            error = f"resolve_failed:{type(exc).__name__}"
            self._mark_resolution(episode_id, status="failed", error=error)
            report["error"] = error
            report["pending"] = True
            return report
        parsed = self.parse_resolution(raw)
        if parsed is None:
            self._mark_resolution(episode_id, status="failed", error="invalid_thread_payload")
            report["error"] = "invalid_thread_payload"
            report["pending"] = True
            return report
        counts = self.apply_actions(episode, parsed)
        report.update(counts)
        self._mark_resolution(episode_id, status="ready", error=None)
        report["action"] = "resolve"
        return report

    async def resolve_pending(
        self,
        *,
        resolve: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]],
        limit: int | None = None,
        persona_id: str | None = None,
        room_id: str | None = None,
    ) -> dict[str, Any]:
        """Bounded pass over Episodes that still owe thread resolution."""

        reports: list[dict[str, Any]] = []
        for episode in self.pending_resolution_episodes(
            limit=max(1, int(limit or self.batch)), persona_id=persona_id, room_id=room_id
        ):
            reports.append(await self.resolve_episode_threads(episode.id, resolve=resolve))
        return {
            "attempted": len(reports),
            "succeeded": sum(1 for item in reports if item["action"] == "resolve"),
            "failed": sum(1 for item in reports if item.get("error")),
            "reports": reports,
        }

    @staticmethod
    def parse_resolution(raw: Any) -> ThreadResolutionPayload | None:
        """Validate an untrusted resolver payload.

        An empty list is a legitimate answer ("nothing in flight").  A payload
        that CLAIMED actions and yielded none usable is a failure, so a broken
        model cannot look like a quiet success.
        """

        if isinstance(raw, ThreadResolutionPayload):
            return raw
        if isinstance(raw, list):
            raw = {"threads": raw}
        if not isinstance(raw, dict) or "threads" not in raw:
            return None
        claimed = raw.get("threads")
        if not isinstance(claimed, list):
            return None
        try:
            payload = ThreadResolutionPayload.model_validate(raw)
        except ValueError:
            return None
        if claimed and not payload.threads:
            return None
        return payload

    # -- application (deterministic) ---------------------------------------

    def apply_actions(self, episode: Any, payload: ThreadResolutionPayload) -> dict[str, int]:
        counts = {
            "threads_created": 0,
            "threads_updated": 0,
            "threads_milestoned": 0,
            "threads_waiting": 0,
            "threads_resolved": 0,
            "threads_cancelled": 0,
            "threads_reopened": 0,
            "threads_merged": 0,
            "candidates_recorded": 0,
            "skipped_low_confidence": 0,
            "skipped_noop": 0,
            "skipped_invalid": 0,
            "replayed": 0,
        }
        created = 0
        for action in payload.usable():
            if action.operation is ThreadOperation.CREATE:
                created += 1
                if created > self.max_creates:
                    # A single episode may not spawn an unbounded thread farm.
                    counts["skipped_invalid"] += 1
                    continue
            outcome = self._apply_action(episode, action)
            if outcome in counts:
                counts[outcome] += 1
        # ``candidates_recorded`` counts the DECISIONS that were left unapplied;
        # the number of links they produced is a different, equally useful fact
        # (one ambiguous decision can point at several threads).
        counts["candidate_links"] = self._pending_candidate_count(episode.id)
        self.database.conn.commit()
        return counts

    def _pending_candidate_count(self, episode_id: str) -> int:
        return int(
            self.database.conn.execute(
                "SELECT COUNT(*) AS c FROM memory_thread_link_candidates "
                "WHERE episode_id = ? AND status = 'pending'",
                (episode_id,),
            ).fetchone()["c"]
        )

    def _apply_action(self, episode: Any, action: ThreadAction) -> str:
        scope = (episode.persona_id, episode.counterpart_id, episode.branch_id)
        outside_scope = self._outside_scope_ids(action, scope)
        if outside_scope:
            # A thread or fact from another persona/branch/counterpart is not a
            # candidate: refuse rather than silently rebind it.
            return "skipped_invalid"
        if action.ambiguous_thread_ids:
            # The resolver itself says it cannot tell these apart.  Recording
            # the alternatives is the honest outcome; guessing is not.  This
            # comes BEFORE the noop shortcut, because "I could not decide" is
            # not the same statement as "nothing happened".
            self._record_candidates(
                episode,
                action.ambiguous_thread_ids,
                confidence=action.confidence,
                reason=action.reason or "resolver reported ambiguity",
            )
            return "candidates_recorded"
        if action.operation is ThreadOperation.NOOP:
            return "skipped_noop"
        low_confidence = float(action.confidence) < self.link_threshold
        if action.operation is ThreadOperation.CREATE:
            return self._apply_create(episode, action, scope, low_confidence)
        thread = self.get_thread(str(action.thread_id or ""))
        if thread is None:
            return "skipped_invalid"
        if low_confidence:
            self._record_candidates(
                episode,
                [thread.id],
                confidence=action.confidence,
                reason=action.reason or "below link threshold",
            )
            return "candidates_recorded"
        return self._apply_existing(episode, thread, action)

    def _outside_scope_ids(self, action: ThreadAction, scope: tuple[str, str, str]) -> list[str]:
        """Ids the resolver cited that do not belong to this episode's scope."""

        outside: list[str] = []
        referenced = [*action.ambiguous_thread_ids]
        if action.thread_id:
            referenced.append(action.thread_id)
        for thread_id in referenced:
            thread = self.get_thread(thread_id)
            if thread is None or (
                thread.persona_id,
                thread.counterpart_id,
                thread.branch_id,
            ) != scope:
                outside.append(thread_id)
        return outside

    def _apply_create(
        self,
        episode: Any,
        action: ThreadAction,
        scope: tuple[str, str, str],
        low_confidence: bool,
    ) -> str:
        if low_confidence:
            # Creating a thread on a guess is exactly the pollution this layer
            # must not cause; the reason is kept in the report, not the store.
            return "skipped_low_confidence"
        event_type = ThreadEventType.CREATE
        key = action.key
        existing = self._find_live_by_key(scope, key)
        if existing is None:
            existing = self._find_containing_live(scope, action)
        if existing is not None:
            if self._has_source(existing.id, episode.id):
                # This Episode already produced this thread: a replay, not new
                # evidence.  Without this check a re-resolution would append a
                # second event and drift confidence on every retry.
                return "replayed"
            self._remember_alias(existing, action)
            # Deterministic merge: the same subject under a new spelling is the
            # same thread, and this is what makes a replayed Episode idempotent
            # even when the model re-proposes a CREATE.
            outcome = self._apply_existing(
                episode,
                existing,
                action.model_copy(update={"operation": ThreadOperation.UPDATE}),
                merged=True,
            )
            return outcome
        previous = self._find_latest_by_key(scope, key)
        occurred = self._action_time(episode, action)
        state: dict[str, Any] = {"created_by_episode_id": episode.id}
        if action.state_update:
            state["state"] = action.state_update
        if action.milestone:
            state["milestones"] = [action.milestone]
        thread = MemoryActiveThread(
            id=new_id("thread"),
            persona_id=scope[0],
            counterpart_id=scope[1],
            branch_id=scope[2],
            thread_key=key,
            thread_type=action.thread_type,
            title=action.display_title() or action.key.split("|", 1)[-1],
            summary=action.summary,
            status=ThreadStatus.ACTIVE,
            importance=max(0.0, min(1.0, float(action.confidence))),
            confidence=float(action.confidence),
            opened_at=occurred,
            last_activity_at=occurred,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            current_state=state,
            consolidation_version=THREAD_RESOLUTION_VERSION,
            visibility="room_public" if episode.room_id else "private_session",
            material_scope=CHARACTER_VISIBLE,
            related_previous_thread_id=previous.id if previous is not None else None,
            metadata={
                "resolution_version": THREAD_RESOLUTION_VERSION,
                "source_episode_id": episode.id,
            },
        )
        self._insert_thread(thread)
        self._append_event(
            thread.id,
            event_type,
            episode=episode,
            action=action,
            summary=action.summary or action.display_title(),
            state={"status": ThreadStatus.ACTIVE.value},
            occurred_at=occurred,
        )
        self._link_sources(
            thread.id,
            episode,
            action,
            role=ThreadEvidenceRole.OPENED,
        )
        self._link_facts(thread.id, episode, action, relation=ThreadFactRelation.PRIMARY)
        return "threads_created"

    def _find_containing_live(
        self, scope: tuple[str, str, str], action: ThreadAction
    ) -> MemoryActiveThread | None:
        """Deterministic second identity net, independent of the model.

        ``thread_key`` catches the same subject spelled with different noise
        ("重庆旅行计划" -> "重庆旅行").  This catches the other common shape: a
        longer wording of the SAME subject ("重庆旅行安排"), where one normalized
        title fully contains the other.  The type guard keeps it conservative --
        a plan and a project that share a word are not merged.
        """

        subject = normalize_thread_text(action.subject)
        if len(subject) < 2:
            return None
        for thread in self.live_threads(
            persona_id=scope[0], counterpart_id=scope[1], branch_id=scope[2]
        ):
            if (
                thread.thread_type is not ThreadType.GENERAL
                and action.thread_type is not ThreadType.GENERAL
                and thread.thread_type is not action.thread_type
            ):
                continue
            title = normalize_thread_text(thread.title)
            if len(title) < 2:
                continue
            if title in subject or subject in title:
                return thread
        return None

    def _remember_alias(self, thread: MemoryActiveThread, action: ThreadAction) -> None:
        """Keep the wording that was merged in, so identity stays explainable."""

        alias = action.display_title()
        if not alias or alias == thread.title:
            return
        metadata = dict(thread.metadata)
        aliases = [str(item) for item in metadata.get("merged_subjects") or []]
        if alias in aliases:
            return
        aliases.append(alias)
        metadata["merged_subjects"] = aliases[-8:]
        self.database.conn.execute(
            "UPDATE memory_active_threads SET metadata_json = ? WHERE id = ?",
            (dumps(metadata), thread.id),
        )

    def _apply_existing(
        self,
        episode: Any,
        thread: MemoryActiveThread,
        action: ThreadAction,
        *,
        merged: bool = False,
    ) -> str:
        occurred = self._action_time(episode, action)
        operation = action.operation
        if thread.status is ThreadStatus.CANCELLED and operation in {
            ThreadOperation.UPDATE,
            ThreadOperation.MILESTONE,
            ThreadOperation.WAITING,
            ThreadOperation.REOPEN,
        }:
            # A cancelled thread was deliberately dropped.  Reviving it silently
            # would forge a decision the conversation never made, so a later
            # mention becomes a NEW thread that still points back at this one.
            if operation is ThreadOperation.REOPEN:
                return self._revive_as_new_thread(episode, thread, action, occurred)
            return "skipped_invalid"
        if operation is ThreadOperation.RESOLVE and thread.status is ThreadStatus.RESOLVED:
            return "replayed"
        if operation is ThreadOperation.REOPEN:
            if thread.status in {ThreadStatus.OPEN, ThreadStatus.ACTIVE}:
                # Already in flight: the honest reading is a continuation, not
                # a resurrection.
                operation = ThreadOperation.UPDATE
            elif thread.status is ThreadStatus.RESOLVED and (
                self._days_between(thread.resolved_at or thread.last_activity_at, occurred)
                > self.reopen_window_days
            ):
                # Long dead: this is a new occurrence of the subject, and it
                # gets its own thread that still points back at the old one.
                return self._revive_as_new_thread(episode, thread, action, occurred)
            # Otherwise the thread revives in place, through the normal
            # event path below -- which is what keeps it from forking a clone.

        event_type = {
            ThreadOperation.UPDATE: ThreadEventType.UPDATE,
            ThreadOperation.MILESTONE: ThreadEventType.MILESTONE,
            ThreadOperation.WAITING: ThreadEventType.WAITING,
            ThreadOperation.RESOLVE: ThreadEventType.RESOLVE,
            ThreadOperation.CANCEL: ThreadEventType.CANCEL,
            ThreadOperation.REOPEN: ThreadEventType.REOPEN,
        }.get(operation, ThreadEventType.UPDATE)
        summary = (
            action.milestone
            or action.state_update
            or action.summary
            or action.display_title()
        )
        inserted = self._append_event(
            thread.id,
            event_type,
            episode=episode,
            action=action,
            summary=summary,
            state={
                "status": self._status_after(thread, operation).value,
                "state_update": action.state_update,
                "milestone": action.milestone,
                "reason": action.reason[:200],
            },
            occurred_at=occurred,
        )
        self._link_sources(
            thread.id,
            episode,
            action,
            role=(
                ThreadEvidenceRole.MILESTONE
                if operation is ThreadOperation.MILESTONE
                else ThreadEvidenceRole.SUPPORTING
            ),
        )
        self._link_facts(thread.id, episode, action, relation=ThreadFactRelation.SUPPORTING)
        if not inserted:
            # The event was already there: this Episode was resolved before.
            # Touch nothing, so a replay cannot drift confidence or activity.
            return "replayed"
        self._update_thread_projection(thread, action, operation, occurred, episode=episode)
        if merged:
            return "threads_merged"
        return {
            ThreadOperation.CREATE: "threads_created",
            ThreadOperation.UPDATE: "threads_updated",
            ThreadOperation.MILESTONE: "threads_milestoned",
            ThreadOperation.WAITING: "threads_waiting",
            ThreadOperation.RESOLVE: "threads_resolved",
            ThreadOperation.CANCEL: "threads_cancelled",
            ThreadOperation.REOPEN: "threads_reopened",
        }.get(operation, "threads_updated")

    def _revive_as_new_thread(
        self,
        episode: Any,
        thread: MemoryActiveThread,
        action: ThreadAction,
        occurred: datetime,
    ) -> str:
        """A long-dead thread coming back as a new event, with the link kept.

        "小陈冲突 #2" three months later is a different episode of the same
        subject.  Creating a second thread is allowed; losing the pointer to the
        first one is not.
        """

        gap_days = self._days_between(
            thread.resolved_at or thread.cancelled_at or thread.last_activity_at, occurred
        )
        state: dict[str, Any] = {
            "created_by_episode_id": episode.id,
            "revived_previous_thread_id": thread.id,
        }
        if action.state_update:
            state["state"] = action.state_update
        new_thread = MemoryActiveThread(
            id=new_id("thread"),
            persona_id=thread.persona_id,
            counterpart_id=thread.counterpart_id,
            branch_id=thread.branch_id,
            thread_key=thread.thread_key,
            thread_type=thread.thread_type,
            title=action.display_title() or thread.title,
            summary=action.summary or thread.summary,
            status=ThreadStatus.ACTIVE,
            importance=max(thread.importance, float(action.confidence)),
            confidence=float(action.confidence),
            opened_at=occurred,
            last_activity_at=occurred,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            current_state=state,
            consolidation_version=THREAD_RESOLUTION_VERSION,
            visibility=thread.visibility,
            material_scope=thread.material_scope,
            related_previous_thread_id=thread.id,
            metadata={
                "resolution_version": THREAD_RESOLUTION_VERSION,
                "source_episode_id": episode.id,
                "revived_after_days": gap_days,
            },
        )
        self._insert_thread(new_thread)
        self._append_event(
            new_thread.id,
            ThreadEventType.CREATE,
            episode=episode,
            action=action,
            summary=action.summary or action.display_title(),
            state={
                "status": ThreadStatus.ACTIVE.value,
                "revived_previous_thread_id": thread.id,
                "revived_after_days": gap_days,
            },
            occurred_at=occurred,
        )
        self._link_sources(new_thread.id, episode, action, role=ThreadEvidenceRole.OPENED)
        self._link_facts(new_thread.id, episode, action, relation=ThreadFactRelation.PRIMARY)
        return "threads_created"

    def _status_after(self, thread: MemoryActiveThread, operation: ThreadOperation) -> ThreadStatus:
        if operation is ThreadOperation.RESOLVE:
            return ThreadStatus.RESOLVED
        if operation is ThreadOperation.CANCEL:
            return ThreadStatus.CANCELLED
        if operation is ThreadOperation.WAITING:
            return ThreadStatus.WAITING
        if operation is ThreadOperation.REOPEN:
            return ThreadStatus.ACTIVE
        if thread.status in {ThreadStatus.RESOLVED, ThreadStatus.CANCELLED}:
            return ThreadStatus.ACTIVE
        if thread.status is ThreadStatus.STALE:
            return ThreadStatus.ACTIVE
        return thread.status

    def _update_thread_projection(
        self,
        thread: MemoryActiveThread,
        action: ThreadAction,
        operation: ThreadOperation,
        occurred: datetime,
        *,
        episode: Any,
    ) -> None:
        """Apply the action to the current row (history already appended).

        Only called when a NEW event was inserted, so a replay cannot inflate
        confidence, activity, or milestone lists.  The projection is a
        read-modify-write against the CURRENT stored row, not against the
        snapshot the caller resolved with: an alias or milestone written a
        moment ago must not be clobbered by a stale copy.
        """

        current = self.get_thread(thread.id) or thread
        status = self._status_after(current, operation)
        state = dict(current.current_state)
        if action.state_update:
            state["state"] = action.state_update
        if action.milestone:
            milestones = [str(item) for item in state.get("milestones") or []]
            if action.milestone not in milestones:
                milestones.append(action.milestone)
            state["milestones"] = milestones[-_MAX_STATE_MILESTONES:]
        metadata = dict(current.metadata)
        metadata["last_episode_id"] = getattr(episode, "id", None)
        confidence = min(
            0.99,
            max(current.confidence, float(action.confidence)) + self.confidence_step,
        )
        summary = action.summary or current.summary
        title = current.title or action.display_title()
        thread_type = current.thread_type
        if thread_type is ThreadType.GENERAL and action.thread_type is not ThreadType.GENERAL:
            thread_type = action.thread_type
        importance = max(current.importance, min(1.0, float(action.confidence)))
        updates: dict[str, Any] = {
            "status": status.value,
            "summary": summary,
            "title": title,
            "thread_type": thread_type.value,
            "confidence": confidence,
            "importance": importance,
            "current_state_json": dumps(state),
            "last_activity_at": dt(max(occurred, current.last_activity_at)),
            "updated_at": datetime.now(UTC).isoformat(),
            "metadata_json": dumps(metadata),
        }
        if status is ThreadStatus.RESOLVED:
            updates["resolved_at"] = dt(occurred)
            updates["stale_at"] = None
        elif status is ThreadStatus.CANCELLED:
            updates["cancelled_at"] = dt(occurred)
        elif status is ThreadStatus.ACTIVE:
            updates["resolved_at"] = None
            updates["stale_at"] = None
        assignments = ", ".join(f"{column} = ?" for column in updates)
        self.database.conn.execute(
            f"UPDATE memory_active_threads SET {assignments} WHERE id = ?",
            (*updates.values(), thread.id),
        )

    # -- writable helpers --------------------------------------------------

    def _insert_thread(self, thread: MemoryActiveThread) -> None:
        columns = (
            "id", "persona_id", "counterpart_id", "branch_id", "thread_key", "thread_type",
            "title", "summary", "status", "importance", "confidence", "opened_at",
            "last_activity_at", "resolved_at", "cancelled_at", "stale_at", "created_at",
            "updated_at", "current_state_json", "consolidation_version", "visibility",
            "material_scope", "related_previous_thread_id", "metadata_json",
        )
        values = (
            thread.id, thread.persona_id, thread.counterpart_id, thread.branch_id,
            thread.thread_key, thread.thread_type.value, thread.title, thread.summary,
            thread.status.value, thread.importance, thread.confidence, dt(thread.opened_at),
            dt(thread.last_activity_at), dt(thread.resolved_at), dt(thread.cancelled_at),
            dt(thread.stale_at), dt(thread.created_at), dt(thread.updated_at),
            dumps(thread.current_state), thread.consolidation_version, thread.visibility,
            thread.material_scope, thread.related_previous_thread_id, dumps(thread.metadata),
        )
        self.database.conn.execute(
            f"INSERT INTO memory_active_threads ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )

    def _append_event(
        self,
        thread_id: str,
        event_type: ThreadEventType,
        *,
        episode: Any,
        action: ThreadAction,
        summary: str,
        state: dict[str, Any],
        occurred_at: datetime,
    ) -> bool:
        """Append one history row behind a deterministic key.

        Returns True when a NEW row appeared.  A replayed Episode computes the
        same key, so the insert is ignored and the caller leaves the projection
        untouched -- which is what makes milestone/thread counts stable across
        replays.
        """

        turn_id = self._evidence_turn_id(episode, action)
        event_key = _event_identity(
            event_type.value,
            episode.id,
            turn_id,
            normalize_thread_text(summary),
        )
        event = ThreadEvent(
            event_id=new_id("tevent"),
            thread_id=thread_id,
            event_type=event_type,
            event_key=event_key,
            summary=summary[:_MAX_REASON_CHARS],
            state=state,
            occurred_at=occurred_at,
            source_episode_id=episode.id,
            source_turn_id=turn_id or None,
            source_availability="complete",
            confidence=float(action.confidence),
            created_at=datetime.now(UTC),
            metadata={"resolution_version": THREAD_RESOLUTION_VERSION},
        )
        cursor = self.database.conn.execute(
            """
            INSERT OR IGNORE INTO memory_thread_events (
              event_id, thread_id, event_type, event_key, summary, state_json, occurred_at,
              source_episode_id, source_turn_id, source_availability, confidence,
              created_at, metadata_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event.event_id,
                event.thread_id,
                event.event_type.value,
                event.event_key,
                event.summary,
                dumps(event.state),
                dt(event.occurred_at),
                event.source_episode_id,
                event.source_turn_id,
                event.source_availability,
                event.confidence,
                dt(event.created_at),
                dumps(event.metadata),
            ),
        )
        return bool(int(cursor.rowcount or 0))

    def _link_sources(
        self,
        thread_id: str,
        episode: Any,
        action: ThreadAction,
        *,
        role: ThreadEvidenceRole,
    ) -> int:
        """Thread -> Episode (+ the cited turn) provenance rows."""

        inserted = 0
        candidates = [
            {"turn_id": self._evidence_turn_id(episode, action), "role": role.value},
            {"turn_id": "", "role": ThreadEvidenceRole.SUPPORTING.value},
        ]
        for item in candidates:
            cursor = self.database.conn.execute(
                """
                INSERT OR IGNORE INTO memory_thread_sources (
                  thread_id, source_type, episode_id, turn_id, session_id, room_id,
                  evidence_role, excerpt_available, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    thread_id,
                    "episode",
                    episode.id,
                    str(item["turn_id"] or ""),
                    episode.session_id,
                    episode.room_id,
                    str(item["role"]),
                    1,
                    datetime.now(UTC).isoformat(),
                ),
            )
            inserted += int(cursor.rowcount or 0)
        return inserted

    def _link_facts(
        self,
        thread_id: str,
        episode: Any,
        action: ThreadAction,
        *,
        relation: ThreadFactRelation,
        title_hint: str = "",
    ) -> int:
        """Link only facts that exist, belong to this episode, and are on topic.

        The cited ``related_fact_ids`` are the model's own answer.  The fallback
        (this episode's facts) is filtered by lexical overlap with the thread so
        an unrelated fact that merely happened in the same stretch of dialogue
        does not get adopted by the thread.
        """

        if not self._table_exists("memory_semantic_facts"):
            return 0
        allowed = set(self._episode_fact_ids(episode.id))
        linked = 0
        wanted = {fact_id for fact_id in action.related_fact_ids if fact_id in allowed}
        topic = _lexical_tokens(title_hint or action.display_title() or action.summary)
        if topic and allowed:
            placeholders = ",".join("?" for _ in allowed)
            rows = self.database.conn.execute(
                f"SELECT id, display_text FROM memory_semantic_facts "
                f"WHERE id IN ({placeholders})",
                tuple(sorted(allowed)),
            ).fetchall()
            for row in rows:
                if _lexical_tokens(str(row["display_text"] or "")) & topic:
                    wanted.add(str(row["id"]))
        for fact_id in sorted(wanted):
            cursor = self.database.conn.execute(
                "INSERT OR IGNORE INTO memory_thread_facts "
                "(thread_id, fact_id, relation, created_at) VALUES (?,?,?,?)",
                (thread_id, fact_id, relation.value, datetime.now(UTC).isoformat()),
            )
            linked += int(cursor.rowcount or 0)
        return linked

    def _record_candidates(
        self,
        episode: Any,
        thread_ids: Sequence[str],
        *,
        confidence: float,
        reason: str,
    ) -> None:
        """Record an unresolved link instead of guessing at one."""

        now = datetime.now(UTC).isoformat()
        for thread_id in thread_ids:
            if not thread_id or self.get_thread(thread_id) is None:
                continue
            self.database.conn.execute(
                """
                INSERT INTO memory_thread_link_candidates (
                  id, persona_id, counterpart_id, branch_id, episode_id, thread_id,
                  confidence, reason, status, created_at, updated_at, resolved_at,
                  metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(episode_id, thread_id) DO UPDATE SET
                  confidence = excluded.confidence,
                  reason = excluded.reason,
                  updated_at = excluded.updated_at
                """,
                (
                    new_id("tcand"),
                    episode.persona_id,
                    episode.counterpart_id,
                    episode.branch_id,
                    episode.id,
                    thread_id,
                    float(confidence),
                    reason[:_MAX_REASON_CHARS],
                    "pending",
                    now,
                    now,
                    None,
                    "{}",
                ),
            )

    def decide_link_candidate(self, candidate_id: str, *, accept: bool) -> dict[str, Any]:
        """Human/debug decision on a pending continuation.

        Accepting appends a real UPDATE event for the candidate thread, through
        the same deterministic key path, so accepting twice is still one event.
        """

        row = self.database.conn.execute(
            "SELECT * FROM memory_thread_link_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return {"candidate_id": candidate_id, "status": "not_found"}
        episode = self.episodes.get_episode(str(row["episode_id"]))
        now = datetime.now(UTC).isoformat()
        status = "accepted" if accept else "rejected"
        if accept and episode is not None:
            thread = self.get_thread(str(row["thread_id"]))
            if thread is not None:
                action = ThreadAction(
                    operation=ThreadOperation.UPDATE,
                    thread_id=thread.id,
                    confidence=float(row["confidence"] or 0.0),
                    reason=str(row["reason"] or "link candidate accepted"),
                )
                self._apply_existing(episode, thread, action)
        self.database.conn.execute(
            "UPDATE memory_thread_link_candidates SET status = ?, resolved_at = ?, "
            "updated_at = ? WHERE id = ?",
            (status, now, now, candidate_id),
        )
        self.database.conn.commit()
        return {"candidate_id": candidate_id, "status": status}

    def pending_link_candidates(
        self, *, episode_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        clauses = ["status = 'pending'"]
        params: list[Any] = []
        if episode_id:
            clauses.append("episode_id = ?")
            params.append(episode_id)
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"SELECT * FROM memory_thread_link_candidates WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- lifecycle ---------------------------------------------------------

    def sweep_stale(self, *, limit: int = 200, now: datetime | None = None) -> dict[str, Any]:
        """Long inactivity makes a thread STALE -- never RESOLVED.

        "We have not talked about it" is not evidence that it is over, so the
        only automatic transition silence may cause is a visible downgrade.
        """

        if self.stale_after_days <= 0:
            return {"stale": 0, "swept": 0}
        moment = now or datetime.now(UTC)
        cutoff = (moment - timedelta(days=self.stale_after_days)).isoformat()
        rows = self.database.conn.execute(
            """
            SELECT * FROM memory_active_threads
            WHERE status IN ('open', 'active', 'waiting')
              AND resolved_at IS NULL AND cancelled_at IS NULL
              AND last_activity_at <= ?
            ORDER BY last_activity_at ASC LIMIT ?
            """,
            (cutoff, max(1, int(limit))),
        ).fetchall()
        stale = 0
        for row in rows:
            thread = self._row_to_thread(row)
            metadata = dict(thread.metadata)
            metadata["stale_reason"] = "inactivity"
            metadata["stale_since"] = moment.isoformat()
            self.database.conn.execute(
                "UPDATE memory_active_threads SET status = ?, stale_at = ?, updated_at = ?, "
                "metadata_json = ? WHERE id = ?",
                (
                    ThreadStatus.STALE.value,
                    moment.isoformat(),
                    moment.isoformat(),
                    dumps(metadata),
                    thread.id,
                ),
            )
            self._append_event(
                thread.id,
                ThreadEventType.STALE,
                episode=_SyntheticEpisode(scope=thread),
                action=ThreadAction(
                    operation=ThreadOperation.UPDATE,
                    thread_id=thread.id,
                    confidence=thread.confidence,
                    reason="inactivity",
                ),
                summary="长期未推进，标记为 STALE",
                state={"status": ThreadStatus.STALE.value},
                occurred_at=moment,
            )
            stale += 1
        self.database.conn.commit()
        return {"stale": stale, "swept": len(rows)}

    def reopen_thread(
        self, thread_id: str, *, reason: str = "manual reopen"
    ) -> dict[str, Any]:
        thread = self.get_thread(thread_id)
        if thread is None:
            return {"thread_id": thread_id, "status": "not_found"}
        if thread.status not in REOPENABLE_STATUSES:
            return {"thread_id": thread_id, "status": "not_reopenable"}
        now = datetime.now(UTC)
        self._append_event(
            thread.id,
            ThreadEventType.REOPEN,
            episode=_SyntheticEpisode(scope=thread),
            action=ThreadAction(
                operation=ThreadOperation.REOPEN,
                thread_id=thread.id,
                confidence=max(thread.confidence, 0.6),
                reason=reason,
            ),
            summary=reason,
            state={"status": ThreadStatus.ACTIVE.value},
            occurred_at=now,
        )
        self.database.conn.execute(
            "UPDATE memory_active_threads SET status = ?, resolved_at = NULL, stale_at = NULL, "
            "last_activity_at = ?, updated_at = ? WHERE id = ?",
            (ThreadStatus.ACTIVE.value, now.isoformat(), now.isoformat(), thread.id),
        )
        self.database.conn.commit()
        return {"thread_id": thread_id, "status": ThreadStatus.ACTIVE.value}

    # -- resolution bookkeeping -------------------------------------------

    def _mark_resolution(self, episode_id: str, *, status: str, error: str | None) -> None:
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            """
            UPDATE memory_episodes
            SET thread_resolution_status = ?, thread_resolution_error = ?,
                thread_resolution_attempts = COALESCE(thread_resolution_attempts, 0) + 1,
                threads_resolved_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, error, now, now, episode_id),
        )
        self.database.conn.commit()

    def recover_pending(self, *, limit: int = 200) -> dict[str, Any]:
        """Startup sweep: Episodes that still owe thread resolution."""

        pending = self.pending_resolution_episodes(limit=limit)
        return {
            "pending": len(pending),
            "pending_episode_ids": [episode.id for episode in pending],
        }

    def pending_resolution_episodes(
        self,
        *,
        limit: int = 50,
        persona_id: str | None = None,
        room_id: str | None = None,
    ) -> list[Any]:
        """The durable work list, shared with Episodes/Facts.

        Bounded and lazy on purpose: the 109 Episodes that predate Phase 5 are
        NOT replayed at startup.  A room reopening, an explicit CLI call, or a
        future background worker drains this list in small steps.
        """

        clauses = [
            "turn_count > 0",
            "COALESCE(thread_resolution_status, 'pending') != 'ready'",
        ]
        params: list[Any] = []
        if persona_id:
            clauses.append("persona_id = ?")
            params.append(persona_id)
        if room_id:
            clauses.append("room_id = ?")
            params.append(room_id)
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"SELECT * FROM memory_episodes WHERE {' AND '.join(clauses)} "
            "ORDER BY started_at ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self.episodes._row_to_episode(row) for row in rows]

    def backfill(
        self,
        *,
        limit: int | None = None,
        persona_id: str | None = None,
    ) -> dict[str, Any]:
        """Bounded view of the backlog.  Never processes everything at once."""

        batch = max(1, int(limit or self.batch))
        pending = self.pending_resolution_episodes(limit=batch, persona_id=persona_id)
        total = int(
            self.database.conn.execute(
                "SELECT COUNT(*) AS c FROM memory_episodes "
                "WHERE turn_count > 0 AND COALESCE(thread_resolution_status, 'pending') != 'ready'"
            ).fetchone()["c"]
        )
        return {
            "batch": len(pending),
            "remaining": max(0, total - len(pending)),
            "episode_ids": [episode.id for episode in pending],
        }

    # -- reading / inspection ---------------------------------------------

    def get_thread(self, thread_id: str) -> MemoryActiveThread | None:
        row = self.database.conn.execute(
            "SELECT * FROM memory_active_threads WHERE id = ?", (thread_id,)
        ).fetchone()
        return self._row_to_thread(row) if row else None

    def find_thread_by_key(
        self, *, persona_id: str, counterpart_id: str, branch_id: str, thread_key: str
    ) -> MemoryActiveThread | None:
        row = self.database.conn.execute(
            "SELECT * FROM memory_active_threads WHERE persona_id = ? AND counterpart_id = ? "
            "AND branch_id = ? AND thread_key = ? ORDER BY updated_at DESC LIMIT 1",
            (persona_id, counterpart_id, branch_id, thread_key),
        ).fetchone()
        return self._row_to_thread(row) if row else None

    def list_threads(
        self,
        *,
        persona_id: str | None = None,
        counterpart_id: str | None = None,
        branch_id: str | None = None,
        status: str | None = None,
        thread_type: str | None = None,
        live_only: bool = False,
        limit: int = 50,
    ) -> list[MemoryActiveThread]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("persona_id", persona_id),
            ("counterpart_id", counterpart_id),
            ("branch_id", branch_id),
            ("status", status),
            ("thread_type", thread_type),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if live_only:
            statuses = tuple(sorted(item.value for item in LIVE_STATUSES))
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"SELECT * FROM memory_active_threads {where} "
            "ORDER BY last_activity_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._row_to_thread(row) for row in rows]

    def thread_events(self, thread_id: str, *, limit: int = 100) -> list[ThreadEvent]:
        rows = self.database.conn.execute(
            "SELECT * FROM memory_thread_events WHERE thread_id = ? "
            "ORDER BY occurred_at ASC, created_at ASC LIMIT ?",
            (thread_id, max(1, int(limit))),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def thread_sources(self, thread_id: str) -> list[dict[str, Any]]:
        rows = self.database.conn.execute(
            "SELECT * FROM memory_thread_sources WHERE thread_id = ? ORDER BY created_at",
            (thread_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def thread_facts(self, thread_id: str) -> list[dict[str, Any]]:
        if not self._table_exists("memory_semantic_facts"):
            return []
        rows = self.database.conn.execute(
            """
            SELECT l.fact_id, l.relation, f.display_text, f.status, f.origin, f.category
            FROM memory_thread_facts l
            LEFT JOIN memory_semantic_facts f ON f.id = l.fact_id
            WHERE l.thread_id = ?
            ORDER BY l.created_at
            """,
            (thread_id,),
        ).fetchall()
        return [
            {
                "fact_id": str(row["fact_id"]),
                "relation": str(row["relation"] or ""),
                "display_text": str(row["display_text"] or ""),
                "status": str(row["status"] or ""),
                "origin": str(row["origin"] or ""),
                "category": str(row["category"] or ""),
                "available": row["display_text"] is not None,
            }
            for row in rows
        ]

    def threads_for_episode(self, episode_id: str) -> list[dict[str, Any]]:
        rows = self.database.conn.execute(
            """
            SELECT DISTINCT thread_id, evidence_role FROM memory_thread_sources
            WHERE episode_id = ?
            UNION
            SELECT DISTINCT thread_id, '' AS evidence_role FROM memory_thread_events
            WHERE source_episode_id = ?
            """,
            (episode_id, episode_id),
        ).fetchall()
        return [
            {"thread_id": str(row["thread_id"]), "evidence_role": str(row["evidence_role"] or "")}
            for row in rows
        ]

    def inspect_thread(self, thread_id: str) -> dict[str, Any] | None:
        """Debug view along the whole provenance chain.

        Thread -> Event -> Episode -> Turn -> raw text, with a per-row
        ``resolvable`` flag, so an inspection can prove the chain instead of
        asserting it.  Never prints raw dialogue.
        """

        thread = self.get_thread(thread_id)
        if thread is None:
            return None
        events = self.thread_events(thread_id)
        sources: list[dict[str, Any]] = []
        for row in self.thread_sources(thread_id):
            episode_id = str(row.get("episode_id") or "")
            turn_id = str(row.get("turn_id") or "")
            episode = self.episodes.get_episode(episode_id) if episode_id else None
            resolvable = False
            if turn_id and episode is not None:
                resolvable = any(
                    turn.turn_id == turn_id and bool(self.episodes.resolve_turn_text(turn))
                    for turn in self.episodes.episode_turns(episode_id)
                )
            elif episode is not None:
                resolvable = any(
                    bool(self.episodes.resolve_turn_text(turn))
                    for turn in self.episodes.episode_turns(episode.id)
                )
            sources.append(
                {
                    "source_type": str(row.get("source_type") or ""),
                    "episode_id": episode_id,
                    "turn_id": turn_id,
                    "session_id": row.get("session_id"),
                    "room_id": row.get("room_id"),
                    "evidence_role": str(row.get("evidence_role") or ""),
                    "excerpt_available": bool(row.get("excerpt_available", 1)),
                    "episode_available": episode is not None,
                    "resolvable": resolvable,
                }
            )
        previous = (
            self.get_thread(thread.related_previous_thread_id)
            if thread.related_previous_thread_id
            else None
        )
        return {
            "thread": thread.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
            "sources": sources,
            "facts": self.thread_facts(thread_id),
            "candidates": [
                item
                for item in self.pending_link_candidates(limit=200)
                if str(item.get("thread_id")) == thread_id
            ],
            "related_previous_thread_id": thread.related_previous_thread_id,
            "preceded_by": previous.model_dump(mode="json") if previous is not None else None,
            "source_episode_ids": sorted(
                {item["episode_id"] for item in sources if item["episode_id"]}
            ),
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
            f"SELECT status, thread_type, COUNT(*) AS c FROM memory_active_threads {where} "
            "GROUP BY status, thread_type",
            tuple(params),
        ).fetchall()
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for row in rows:
            by_status[str(row["status"])] = by_status.get(str(row["status"]), 0) + int(row["c"])
            by_type[str(row["thread_type"])] = by_type.get(str(row["thread_type"]), 0) + int(
                row["c"]
            )
        live = sum(count for status, count in by_status.items() if status in {
            item.value for item in LIVE_STATUSES
        })
        pending_candidates = int(
            self.database.conn.execute(
                "SELECT COUNT(*) AS c FROM memory_thread_link_candidates WHERE status = 'pending'"
            ).fetchone()["c"]
        )
        events = int(
            self.database.conn.execute(
                "SELECT COUNT(*) AS c FROM memory_thread_events"
            ).fetchone()["c"]
        )
        return {
            "threads": sum(by_status.values()),
            "live": live,
            "events": events,
            "pending_candidates": pending_candidates,
            "by_status": by_status,
            "by_type": by_type,
        }

    def count_live(self, *, persona_id: str | None = None) -> int:
        statuses = tuple(sorted(item.value for item in LIVE_STATUSES))
        placeholders = ",".join("?" for _ in statuses)
        if persona_id:
            row = self.database.conn.execute(
                f"SELECT COUNT(*) AS c FROM memory_active_threads "
                f"WHERE persona_id = ? AND status IN ({placeholders})",
                (persona_id, *statuses),
            ).fetchone()
        else:
            row = self.database.conn.execute(
                f"SELECT COUNT(*) AS c FROM memory_active_threads "
                f"WHERE status IN ({placeholders})",
                statuses,
            ).fetchone()
        return int(row["c"])

    def count_pending_resolution(
        self, *, persona_id: str | None = None, room_id: str | None = None
    ) -> int:
        clauses = ["turn_count > 0", "COALESCE(thread_resolution_status, 'pending') != 'ready'"]
        params: list[Any] = []
        if persona_id:
            clauses.append("persona_id = ?")
            params.append(persona_id)
        if room_id:
            clauses.append("room_id = ?")
            params.append(room_id)
        return int(
            self.database.conn.execute(
                f"SELECT COUNT(*) AS c FROM memory_episodes WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()["c"]
        )

    # -- deletion lifecycle (Phase 3.1 semantics) --------------------------

    def stamp_sources_unavailable(
        self, *, episode_ids: Sequence[str], turn_ids: Sequence[str], availability: str
    ) -> dict[str, int]:
        """An explicitly deleted source must be visible, never silently dangling."""

        now = datetime.now(UTC).isoformat()
        affected_threads: set[str] = set()
        sources = 0
        if episode_ids:
            placeholders = ",".join("?" for _ in episode_ids)
            affected_threads.update(
                str(row["thread_id"])
                for row in self.database.conn.execute(
                    f"SELECT DISTINCT thread_id FROM memory_thread_sources "
                    f"WHERE episode_id IN ({placeholders})",
                    tuple(episode_ids),
                ).fetchall()
            )
            cursor = self.database.conn.execute(
                f"UPDATE memory_thread_sources SET excerpt_available = 0 "
                f"WHERE episode_id IN ({placeholders})",
                tuple(episode_ids),
            )
            sources += int(cursor.rowcount or 0)
            cursor = self.database.conn.execute(
                f"UPDATE memory_thread_events SET source_availability = ? "
                f"WHERE source_episode_id IN ({placeholders})",
                (availability, *episode_ids),
            )
            sources += int(cursor.rowcount or 0)
        if turn_ids:
            placeholders = ",".join("?" for _ in turn_ids)
            affected_threads.update(
                str(row["thread_id"])
                for row in self.database.conn.execute(
                    f"SELECT DISTINCT thread_id FROM memory_thread_sources "
                    f"WHERE turn_id IN ({placeholders})",
                    tuple(turn_ids),
                ).fetchall()
            )
            cursor = self.database.conn.execute(
                f"UPDATE memory_thread_sources SET excerpt_available = 0 "
                f"WHERE turn_id IN ({placeholders})",
                tuple(turn_ids),
            )
            sources += int(cursor.rowcount or 0)
        for thread_id in sorted(affected_threads):
            thread = self.get_thread(thread_id)
            if thread is None:
                continue
            metadata = dict(thread.metadata)
            metadata["provenance_availability"] = availability
            metadata["provenance_note"] = "source turns removed"
            self.database.conn.execute(
                "UPDATE memory_active_threads SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, dumps(metadata), thread_id),
            )
        self.database.conn.commit()
        return {"sources_stamped": sources, "threads_stamped": len(affected_threads)}

    # -- internals ---------------------------------------------------------

    def _find_live_by_key(
        self, scope: tuple[str, str, str], thread_key: str
    ) -> MemoryActiveThread | None:
        statuses = tuple(sorted(item.value for item in LIVE_STATUSES))
        placeholders = ",".join("?" for _ in statuses)
        row = self.database.conn.execute(
            "SELECT * FROM memory_active_threads WHERE persona_id = ? AND counterpart_id = ? "
            f"AND branch_id = ? AND thread_key = ? AND status IN ({placeholders}) "
            "ORDER BY last_activity_at DESC LIMIT 1",
            (*scope, thread_key, *statuses),
        ).fetchone()
        return self._row_to_thread(row) if row else None

    def _find_latest_by_key(
        self, scope: tuple[str, str, str], thread_key: str
    ) -> MemoryActiveThread | None:
        row = self.database.conn.execute(
            "SELECT * FROM memory_active_threads WHERE persona_id = ? AND counterpart_id = ? "
            "AND branch_id = ? AND thread_key = ? ORDER BY updated_at DESC LIMIT 1",
            (*scope, thread_key),
        ).fetchone()
        return self._row_to_thread(row) if row else None

    @staticmethod
    def _days_between(reference: datetime | None, moment: datetime) -> int:
        if reference is None:
            return 0
        return max(0, (moment - reference).days)

    @staticmethod
    def _action_time(episode: Any, action: ThreadAction) -> datetime:
        return (
            getattr(episode, "ended_at", None)
            or getattr(episode, "started_at", None)
            or datetime.now(UTC)
        )

    def _evidence_turn_id(self, episode: Any, action: ThreadAction) -> str:
        for turn_id in action.source_turn_ids:
            if self.episodes.episode_has_source(turn_id, episode_id=getattr(episode, "id", None)):
                return turn_id
        return ""

    def _has_source(self, thread_id: str, episode_id: str) -> bool:
        row = self.database.conn.execute(
            "SELECT 1 AS x FROM memory_thread_sources WHERE thread_id = ? AND episode_id = ? "
            "LIMIT 1",
            (thread_id, episode_id),
        ).fetchone()
        return row is not None

    def _table_exists(self, name: str) -> bool:
        row = self.database.conn.execute(
            "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_thread(row: Any) -> MemoryActiveThread:
        def _enum(enum_cls: Any, value: Any, fallback: Any) -> Any:
            try:
                return enum_cls(str(value))
            except ValueError:
                return fallback

        return MemoryActiveThread(
            id=str(row["id"]),
            persona_id=str(row["persona_id"]),
            counterpart_id=str(row["counterpart_id"]),
            branch_id=str(row["branch_id"]),
            thread_key=str(row["thread_key"] or ""),
            thread_type=_enum(ThreadType, row["thread_type"], ThreadType.GENERAL),
            title=str(row["title"] or ""),
            summary=str(row["summary"] or ""),
            status=_enum(ThreadStatus, row["status"], ThreadStatus.ACTIVE),
            importance=float(row["importance"] or 0.0),
            confidence=float(row["confidence"] or 0.0),
            opened_at=parse_dt(row["opened_at"]) or datetime.now(UTC),
            last_activity_at=parse_dt(row["last_activity_at"]) or datetime.now(UTC),
            resolved_at=parse_dt(row["resolved_at"]),
            cancelled_at=parse_dt(row["cancelled_at"]),
            stale_at=parse_dt(row["stale_at"]),
            created_at=parse_dt(row["created_at"]) or datetime.now(UTC),
            updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
            current_state=dict(loads(row["current_state_json"])),
            consolidation_version=int(row["consolidation_version"] or 1),
            visibility=str(row["visibility"] or "private_session"),
            material_scope=str(row["material_scope"] or CHARACTER_VISIBLE),
            related_previous_thread_id=row["related_previous_thread_id"],
            metadata=dict(loads(row["metadata_json"])),
        )

    @staticmethod
    def _row_to_event(row: Any) -> ThreadEvent:
        try:
            event_type = ThreadEventType(str(row["event_type"]))
        except ValueError:
            event_type = ThreadEventType.UPDATE
        return ThreadEvent(
            event_id=str(row["event_id"]),
            thread_id=str(row["thread_id"]),
            event_type=event_type,
            event_key=str(row["event_key"] or ""),
            summary=str(row["summary"] or ""),
            state=dict(loads(row["state_json"])),
            occurred_at=parse_dt(row["occurred_at"]) or datetime.now(UTC),
            source_episode_id=row["source_episode_id"],
            source_turn_id=row["source_turn_id"],
            source_availability=str(row["source_availability"] or "complete"),
            confidence=float(row["confidence"] or 0.5),
            created_at=parse_dt(row["created_at"]) or datetime.now(UTC),
            metadata=dict(loads(row["metadata_json"])),
        )


class _SyntheticEpisode:
    """A minimal episode stand-in for lifecycle events that no Episode caused.

    Staleness and manual reopens are real history, but they are not the result
    of a conversation, so they must not pretend to have a source turn -- the
    event key below keeps them distinct and replay-safe.
    """

    __slots__ = ("id", "persona_id", "counterpart_id", "branch_id", "session_id", "room_id")

    def __init__(self, *, scope: MemoryActiveThread) -> None:
        self.id = f"lifecycle:{scope.id}"
        self.persona_id = scope.persona_id
        self.counterpart_id = scope.counterpart_id
        self.branch_id = scope.branch_id
        self.session_id = None
        self.room_id = None


__all__ = [
    "THREAD_RESOLUTION_INTRO",
    "THREAD_RESOLUTION_SCHEMA",
    "ThreadService",
    "canonical_thread_key",
]
