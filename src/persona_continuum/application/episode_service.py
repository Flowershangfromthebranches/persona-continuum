"""Episode ledger: turn -> Episode assignment, consolidation, and coverage.

Three separate responsibilities live here, and keeping them separate is the
whole point:

**Assignment** (``assign_turn``) is deterministic, cheap and synchronous.  It
runs inside the SAME transaction that writes the committed turn, which is what
makes "no orphaned turn" a structural guarantee rather than a hoped-for
cleanup: a turn that exists in ``session_turns`` cannot exist without an
Episode row, because if the assignment failed the whole commit rolls back.

**Consolidation** (``consolidate_episode``) is the only part that needs a model.
It is asynchronous, idempotent, retryable, and never in the reply path: a
persona's answer is committed and returned before any summary is attempted, and
a failed summary leaves the raw turns untouched.

**Reporting** (``coverage`` / ``inspect_episode``) proves the invariant and
makes a regression visible instead of silent.

Provenance rule: an Episode never stores the transcript.  It stores *which*
turns it covers (``memory_episode_turns``), and the raw text is read back from
``session_turns`` / ``room_transcripts`` when needed.  L0 stays the fact source.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from persona_continuum.agent.context_budget import TokenEstimator
from persona_continuum.application._utils import dt, dumps, loads, new_id, parse_dt
from persona_continuum.domain.episode import (
    EPISODE_CONSOLIDATION_VERSION,
    BoundaryReason,
    ConsolidationAction,
    EpisodeScope,
    EpisodeStatus,
    EpisodeSummary,
    EpisodeTurn,
    MemoryEpisode,
    ResolvedTurnMessage,
    episode_status_from_raw,
)
from persona_continuum.domain.provenance import CHARACTER_VISIBLE
from persona_continuum.storage.database import Database

#: The scope key is (persona, counterpart, branch, session); a room is recorded
#: only because raw provenance for room turns lives in ``room_transcripts``.
#: Kept parenthesised: SQLite row-value comparison needs parentheses on BOTH
#: sides (``(a, b) = (?, ?)``), which a bare column list silently breaks.
SCOPE_COLUMNS = "(persona_id, counterpart_id, branch_id, session_id)"

#: What the summariser is asked for.  Property names are the contract; extra
#: keys are ignored and missing ones default, so an off-schema answer degrades
#: instead of crashing.
EPISODE_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "important_events": {"type": "array", "items": {"type": "string"}},
        "commitments": {"type": "array", "items": {"type": "string"}},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "emotional_arc": {"type": "array", "items": {"type": "string"}},
        "topics": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
        "user_stated": {"type": "array", "items": {"type": "string"}},
        "persona_stated": {"type": "array", "items": {"type": "string"}},
        "inferred_context": {"type": "array", "items": {"type": "string"}},
        "importance": {"type": "number"},
        "confidence": {"type": "number"},
    },
    "required": ["title", "summary"],
}

EPISODE_SUMMARY_INTRO = (
    "你在为一个长期对话系统整理「一段共同经历」。输入是一段连续对话的原始轮次，"
    "输出是一份结构化摘要。这是数据压缩与索引任务，不是角色扮演：不要继续对话，"
    "不要扮演任何角色，不要照抄长段原文。\n"
    "硬性要求：\n"
    "1. 不新增对话中不存在的事实；不推测用户的真实身份、职业或未表达的动机。\n"
    "2. 严格区分三种来源：user_stated（用户明确说过的）、"
    "persona_stated（人格说过的，包含它的猜测与解释）、"
    "inferred_context（推断出来的上下文，不得当作事实）。\n"
    "3. 人格的猜测（例如「你肯定就是舍不得她」）只能放进 persona_stated，"
    "绝不能写成用户事实。\n"
    "4. 不确定的内容降低 confidence；不要为了完整而编造。\n"
    "5. title 用一句话概括这段经历；summary 用 2-5 句客观描述发生了什么。\n"
    "只输出结构化结果。"
)


def estimate_text_tokens(text: str) -> int:
    """CJK-aware estimate shared with the rest of the runtime."""

    return TokenEstimator().estimate(text or "")


def _range_hash(scope: EpisodeScope, turn_ids: list[str]) -> str:
    """Stable identity for one covered range (order-sensitive)."""

    payload = json.dumps(
        [scope.persona_id, scope.counterpart_id, scope.branch_id, scope.session_id, *turn_ids],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class EpisodeService:
    """Owns the Episode ledger.  Never deletes raw history."""

    #: Optional hook bound by the container: the Phase 6 summary layer owns the
    #: retraction rules for Chapters derived from a deleted Episode, and this
    #: service is the one that knows a delete happened.  Declared here so the
    #: wiring is typed rather than an ad-hoc attribute.
    summary_provenance_hook: Callable[..., Any] | None = None

    def __init__(self, database: Database, *, config: Any = None) -> None:
        self.database = database
        self.config = config
        self.max_turns = max(1, int(getattr(config, "memory_episode_max_turns", 24) or 24))
        self.max_tokens = max(1, int(getattr(config, "memory_episode_max_tokens", 6000) or 6000))
        self.idle_gap = timedelta(
            minutes=max(1, int(getattr(config, "memory_episode_idle_gap_minutes", 180) or 180))
        )
        self.enabled = bool(getattr(config, "memory_episode_enabled", True))
        self.summary_input_max_tokens = max(
            512, int(getattr(config, "memory_episode_summary_input_max_tokens", 16384) or 16384)
        )
        self.backfill_batch = max(
            1, int(getattr(config, "memory_episode_backfill_batch", 40) or 40)
        )

    # -- assignment (deterministic, synchronous) ---------------------------

    def assign_turn(
        self,
        *,
        persona_id: str,
        session_id: str,
        turn_id: str,
        counterpart_id: str,
        branch_id: str,
        room_id: str | None,
        occurred_at: datetime,
        user_message: str = "",
        persona_response: str = "",
        visibility: str | None = None,
        shared_user_turn_id: str | None = None,
        shared_user_text: str = "",
        shared_user_occurred_at: datetime | None = None,
        commit: bool = False,
    ) -> dict[str, Any]:
        """Place one committed turn in the Episode ledger.  Idempotent.

        ``commit=False`` by default: the caller is ``SessionService.commit_turn``
        and owns the transaction, so the Episode row and the turn row become
        durable together or not at all.

        ``shared_user_turn_id`` is the room-level user message this turn was
        answering.  It is attached as an ADDITIONAL source with
        ``source_kind="shared_user"``: one raw event, referenced by every
        persona's Episode that actually replied to it.  The raw turn is never
        copied per persona, and the Episode's owner stays the persona -- an
        Episode owns turns, it does not own their speakers.
        """

        report: dict[str, Any] = {
            "turn_id": turn_id,
            "session_id": session_id,
            "room_id": room_id,
            "persona_id": persona_id,
            "counterpart_id": counterpart_id,
            "branch_id": branch_id,
            "action": ConsolidationAction.NOOP.value,
            "boundary_reason": BoundaryReason.NONE.value,
            "current_episode_id": None,
            "closed_episode_id": None,
            "source_turn_count": 0,
            "source_token_estimate": 0,
            "shared_user_turn_id": None,
            "summary_created": False,
            "consolidation_version": EPISODE_CONSOLIDATION_VERSION,
            "pending": False,
            "error": None,
        }
        if not self.enabled:
            report["error"] = "episodes_disabled"
            return report

        scope = EpisodeScope(
            persona_id=persona_id,
            counterpart_id=counterpart_id,
            branch_id=branch_id or "main",
            session_id=session_id,
        )
        existing = self.episode_for_turn(turn_id)
        if existing is not None:
            # Replay of a commit that already ran (retry, resume, duplicate
            # delivery): the ledger already owns this turn.  Never append twice.
            report["action"] = ConsolidationAction.NOOP.value
            report["current_episode_id"] = existing.id
            report["source_turn_count"] = existing.turn_count
            report["pending"] = existing.pending_consolidation
            report["error"] = "turn_already_assigned"
            return report

        turn_tokens = estimate_text_tokens(user_message) + estimate_text_tokens(
            persona_response
        )
        # The boundary is decided from the Episode's OWN turns (see
        # ``_attach_turn``): a shared room message is attached as an extra source
        # afterwards and never lets a max_turns boundary fire on someone else's
        # message.
        current = self.open_episode(scope)
        decision = self.decide_boundary(
            current,
            occurred_at=occurred_at,
            turn_tokens=turn_tokens,
        )
        closed: MemoryEpisode | None = None
        if decision["action"] == "close_then_create" and current is not None:
            closed = self._close_episode(
                current,
                reason=str(decision["reason"]),
                ended_at=occurred_at,
            )
            current = None

        if current is None:
            episode = self._create_episode(
                scope,
                started_at=occurred_at,
                room_id=room_id,
                visibility=visibility,
                boundary_reason=str(decision["reason"]),
            )
            report["action"] = ConsolidationAction.CREATE.value
        else:
            episode = current
            report["action"] = ConsolidationAction.APPEND.value
        report["boundary_reason"] = str(decision["reason"])

        # The shared user turn precedes the reply it triggered, so it is
        # attached first and the Episode reads chronologically.  Dedup is scoped
        # to THIS Episode: the same raw user turn is legitimately a source of
        # every persona's Episode in the room.
        shared_turn_id = str(shared_user_turn_id or "").strip()
        if shared_turn_id and not self.episode_has_source(
            shared_turn_id, episode_id=episode.id
        ):
            from persona_continuum.runtime.turn_normalizer import normalize_turn

            already_in_turn = normalize_turn(shared_user_text).spoken_text == normalize_turn(
                user_message
            ).spoken_text
            self._attach_turn(
                episode,
                EpisodeTurn(
                    episode_id=episode.id,
                    turn_id=shared_turn_id,
                    source_kind="shared_user",
                    position=self._next_position(episode.id),
                    session_id=session_id,
                    room_id=room_id,
                    speaker="user",
                    occurred_at=shared_user_occurred_at or occurred_at,
                    # Already counted on this persona's own turn when the text
                    # is identical; otherwise it is real extra content.
                    token_estimate=(
                        0 if already_in_turn else estimate_text_tokens(shared_user_text)
                    ),
                ),
                occurred_at=shared_user_occurred_at or occurred_at,
            )
            report["shared_user_turn_id"] = shared_turn_id
        self._attach_turn(
            episode,
            EpisodeTurn(
                episode_id=episode.id,
                turn_id=turn_id,
                source_kind="session_turn",
                position=self._next_position(episode.id),
                session_id=session_id,
                room_id=room_id,
                speaker=persona_id,
                occurred_at=occurred_at,
                token_estimate=turn_tokens,
            ),
            occurred_at=occurred_at,
        )
        # The raw narrative lives in the transcript, not here.
        self._insert_lineage(
            persona_id=persona_id,
            child_type="episode",
            child_id=episode.id,
            parent_type="session_turn",
            parent_id=turn_id,
            relation="episode_contains_turn",
            metadata={"position": episode.turn_count - 1, "session_id": session_id},
        )
        refreshed = self.get_episode(episode.id) or episode
        report.update(
            {
                "current_episode_id": refreshed.id,
                "closed_episode_id": closed.id if closed else None,
                "source_turn_count": refreshed.turn_count,
                "source_token_estimate": refreshed.source_token_estimate,
                "pending": refreshed.pending_consolidation,
            }
        )
        if commit:
            self.database.conn.commit()
        return report

    def decide_boundary(
        self,
        current: MemoryEpisode | None,
        *,
        occurred_at: datetime,
        turn_tokens: int,
    ) -> dict[str, Any]:
        """Deterministic, explainable boundary decision.  No model involved.

        First version is intentionally cheap: a "would this turn still belong to
        the same stretch of experience" question is answered from time, size and
        scope, all of which an operator can inspect and predict.
        """

        if current is None:
            return {"action": "create", "reason": BoundaryReason.FIRST_TURN.value}
        if current.status is not EpisodeStatus.OPEN:
            return {"action": "create", "reason": BoundaryReason.SCOPE_CHANGED.value}
        last_at = current.ended_at or current.started_at
        if occurred_at - last_at > self.idle_gap:
            return {"action": "close_then_create", "reason": BoundaryReason.IDLE_GAP.value}
        if current.turn_count + 1 > self.max_turns:
            return {"action": "close_then_create", "reason": BoundaryReason.MAX_TURNS.value}
        if current.source_token_estimate + turn_tokens > self.max_tokens:
            return {"action": "close_then_create", "reason": BoundaryReason.MAX_TOKENS.value}
        return {"action": "append", "reason": BoundaryReason.APPEND.value}

    def close_episode(
        self, episode_id: str, *, reason: str = BoundaryReason.EXPLICIT_CLOSE.value
    ) -> MemoryEpisode | None:
        row = self.get_episode(episode_id)
        if row is None:
            return None
        if row.status is EpisodeStatus.CLOSED:
            return row
        closed = self._close_episode(row, reason=reason, ended_at=row.ended_at)
        self.database.conn.commit()
        return closed

    def _close_episode(
        self, episode: MemoryEpisode, *, reason: str, ended_at: datetime | None
    ) -> MemoryEpisode:
        """Close without a summary: the summary is owed, not fabricated."""

        now = datetime.now(UTC)
        status = (
            EpisodeStatus.CLOSED.value
            if episode.summary_ready
            else EpisodeStatus.PENDING_CONSOLIDATION.value
        )
        metadata = dict(episode.metadata)
        metadata["close_reason"] = reason
        self.database.conn.execute(
            """
            UPDATE memory_episodes
            SET status = ?, boundary_reason = ?, ended_at = ?, updated_at = ?,
                metadata_json = ?
            WHERE id = ?
            """,
            (
                status,
                reason,
                dt(ended_at or episode.ended_at or now),
                now.isoformat(),
                dumps(metadata),
                episode.id,
            ),
        )
        return self.get_episode(episode.id) or episode

    # -- consolidation (asynchronous, idempotent) --------------------------

    async def consolidate_episode(
        self,
        episode_id: str,
        *,
        summarize: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]],
        allow_open: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Produce (or refresh) one Episode's structured summary.

        Never raises on a provider problem: a failure is recorded on the row as
        ``FAILED`` + ``last_error`` and stays retryable, because the alternative
        -- losing the summary requirement into an exception -- is exactly the
        "memory black hole" this layer exists to prevent.
        """

        report: dict[str, Any] = {
            "episode_id": episode_id,
            "action": ConsolidationAction.NOOP.value,
            "boundary_reason": BoundaryReason.NONE.value,
            "source_turn_count": 0,
            "source_token_estimate": 0,
            "summary_created": False,
            "consolidation_version": EPISODE_CONSOLIDATION_VERSION,
            "pending": False,
            "error": None,
        }
        episode = self.get_episode(episode_id)
        if episode is None:
            report["error"] = "episode_not_found"
            return report
        report["boundary_reason"] = episode.boundary_reason
        report["source_turn_count"] = episode.turn_count
        report["source_token_estimate"] = episode.source_token_estimate
        if episode.summary_ready and not force:
            report["pending"] = False
            return report
        if episode.status is EpisodeStatus.OPEN and not allow_open:
            report["pending"] = True
            report["error"] = "episode_open"
            return report

        payload = self.build_consolidation_payload(episode)
        if payload is None:
            report["error"] = "no_source_turns"
            self._record_failure(episode, "no_source_turns")
            report["pending"] = True
            return report
        report["source_turn_count"] = len(payload["turns"])
        report["source_token_estimate"] = int(payload["estimated_tokens"])

        attempt = episode.consolidation_attempts + 1
        try:
            raw = await summarize(payload, EPISODE_SUMMARY_SCHEMA)
        except Exception as exc:  # noqa: BLE001 - any provider failure is retryable
            self._record_failure(episode, f"{type(exc).__name__}")
            report["error"] = f"summarize_failed:{type(exc).__name__}"
            report["pending"] = True
            report["consolidation_version"] = episode.consolidation_version
            return report
        summary = self.parse_summary(raw)
        if summary is None or summary.is_empty:
            self._record_failure(episode, "invalid_summary_payload")
            report["error"] = "invalid_summary_payload"
            report["pending"] = True
            return report

        now = datetime.now(UTC)
        status = (
            EpisodeStatus.OPEN.value
            if episode.status is EpisodeStatus.OPEN
            else EpisodeStatus.CLOSED.value
        )
        metadata = dict(episode.metadata)
        metadata["provisional"] = episode.status is EpisodeStatus.OPEN
        metadata["summary_source"] = "model"
        self.database.conn.execute(
            """
            UPDATE memory_episodes
            SET title = ?, summary = ?, summary_json = ?, status = ?,
                importance = ?, confidence = ?, summary_status = 'ready',
                consolidation_version = ?, consolidation_attempts = ?,
                last_error = NULL, consolidated_at = ?, visibility = ?,
                material_scope = ?, updated_at = ?, metadata_json = ?
            WHERE id = ?
            """,
            (
                summary.title,
                summary.summary,
                dumps(summary.model_dump(mode="json")),
                status,
                summary.importance,
                summary.confidence,
                EPISODE_CONSOLIDATION_VERSION,
                attempt,
                now.isoformat(),
                episode.visibility,
                episode.material_scope,
                now.isoformat(),
                dumps(metadata),
                episode.id,
            ),
        )
        self.database.conn.commit()
        self._insert_lineage(
            persona_id=episode.persona_id,
            child_type="memory_summary",
            child_id=f"episode_summary:{episode.id}:v{EPISODE_CONSOLIDATION_VERSION}",
            parent_type="episode",
            parent_id=episode.id,
            relation="episode_summary_of",
            metadata={"source_turn_count": episode.turn_count},
        )
        # The lineage edge is part of the same logical write: leaving it as an
        # open implicit transaction leaks "cannot start a transaction within a
        # transaction" into the next caller that opens an explicit one.
        self.database.conn.commit()
        report.update(
            {
                "action": ConsolidationAction.CONSOLIDATE.value,
                "summary_created": True,
                "pending": False,
                "consolidation_version": EPISODE_CONSOLIDATION_VERSION,
            }
        )
        return report

    @staticmethod
    def parse_summary(raw: Any) -> EpisodeSummary | None:
        """Validate an untrusted summariser payload into the Episode schema.

        A JSON blob is not a summary: anything that does not fit the schema is
        reported as invalid and retried, never stored as if it were data.
        """

        if isinstance(raw, EpisodeSummary):
            return raw
        if not isinstance(raw, dict):
            return None
        try:
            return EpisodeSummary.model_validate(raw)
        except ValueError:
            return None

    def build_consolidation_payload(self, episode: MemoryEpisode) -> dict[str, Any] | None:
        """Assemble the summariser input from the Episode's source turns.

        Reads the RAW turns (never a previous summary of them) so a second pass
        cannot compound its own drift, and stops at a turn boundary when the
        input budget runs out -- a truncated mid-sentence payload is worse than
        a smaller complete one.
        """

        turns = self.episode_turns(episode.id)
        if not turns:
            return None
        admitted: list[dict[str, str]] = []
        used = 0
        budget = self.summary_input_max_tokens - estimate_text_tokens(EPISODE_SUMMARY_INTRO)
        newest_first = list(reversed(turns))
        for turn in newest_first:
            text = self.resolve_turn_text(turn)
            if not text:
                continue
            cost = estimate_text_tokens(text)
            if admitted and used + cost > budget:
                break
            admitted.append({"turn_id": turn.turn_id, "text": text})
            used += cost
        if not admitted:
            return None
        admitted.reverse()
        return {
            "scope": {
                "persona_id": episode.persona_id,
                "counterpart_id": episode.counterpart_id,
                "branch_id": episode.branch_id,
                "started_at": episode.started_at.isoformat(),
                "ended_at": (episode.ended_at or episode.updated_at).isoformat(),
            },
            "turns": admitted,
            "estimated_tokens": used,
        }

    @staticmethod
    def turn_speaker_label(turn: EpisodeTurn) -> str:
        """Who actually speaks inside this turn's resolved TEXT.

        This is not the same question as "whose row is it".  A ``session_turn``
        resolves to ``"user: ...\\npersona: ..."`` -- both sides inside one row --
        so labelling it with the persona's id tells an extractor that the user's
        own words were said by the persona.  That silently corrupts ``origin``,
        and with it the guarantee that a persona's guess never becomes the
        user's history.  The label must describe the text.
        """

        if turn.source_kind == "session_turn":
            return "user+persona"
        if turn.source_kind == "shared_user":
            return "user"
        return turn.speaker or "user"

    def resolve_turn_text(self, turn: EpisodeTurn) -> str:
        """Walk provenance back to the raw committed text.

        This is the only way an Episode gets content: it never caches the
        transcript, it re-reads it.  ``session_turn`` resolves in
        ``session_turns``; ``shared_user`` / ``room_transcript`` resolve in
        ``room_transcripts`` (the room-level raw event, shared by every persona
        that answered it).
        """

        if turn.source_kind in {"room_transcript", "shared_user"}:
            row = self.database.conn.execute(
                "SELECT speaker_name, content FROM room_transcripts WHERE turn_id = ? "
                "AND (? IS NULL OR room_id = ?) ORDER BY created_at LIMIT 1",
                (turn.turn_id, turn.room_id, turn.room_id),
            ).fetchone()
            if row is None:
                return ""
            return f"{row['speaker_name']}: {row['content']}"
        row = self.database.conn.execute(
            "SELECT user_message, persona_response FROM session_turns WHERE id = ?",
            (turn.turn_id,),
        ).fetchone()
        if row is None:
            return ""
        return f"user: {row['user_message']}\npersona: {row['persona_response']}"

    def resolve_turn_messages(self, turn: EpisodeTurn) -> list[ResolvedTurnMessage]:
        """The same provenance, as individual speakable messages.

        Raw Recall needs per-message granularity, and it needs the speaker to be
        RIGHT -- which is why this lives next to :meth:`turn_speaker_label`
        instead of in a third place that re-derives it.  The rules:

        * ``session_turn`` carries both sides in one row, so it yields two
          messages labelled by ``turn_speaker_label``'s own vocabulary
          (``user`` / ``persona``), in conversation order.
        * ``shared_user`` / ``room_transcript`` are one raw event that several
          personas answered, so it yields ONE message whose speaker is the
          transcript's real ``speaker_name``.  It is never copied per persona.
        * Nothing is rewritten, trimmed or re-punctuated here.  An empty half is
          dropped rather than emitted as an empty message.
        """

        occurred = turn.occurred_at
        if turn.source_kind in {"room_transcript", "shared_user"}:
            row = self.database.conn.execute(
                "SELECT speaker_name, content, created_at FROM room_transcripts "
                "WHERE turn_id = ? AND (? IS NULL OR room_id = ?) ORDER BY created_at LIMIT 1",
                (turn.turn_id, turn.room_id, turn.room_id),
            ).fetchone()
            if row is None:
                return []
            return [
                ResolvedTurnMessage(
                    turn_id=turn.turn_id,
                    source_kind=turn.source_kind,
                    speaker=str(row["speaker_name"] or ""),
                    raw_text=str(row["content"] or ""),
                    occurred_at=occurred,
                    position=turn.position,
                    room_id=turn.room_id,
                    session_id=turn.session_id,
                )
            ]
        row = self.database.conn.execute(
            "SELECT user_message, persona_response, created_at FROM session_turns WHERE id = ?",
            (turn.turn_id,),
        ).fetchone()
        if row is None:
            return []
        label = self.turn_speaker_label(turn)
        # The label describes the text ("user+persona"); the two halves of that
        # text are the user's side and the persona's side, in that order.
        halves = (
            ("user", row["user_message"]),
            ("persona", row["persona_response"]),
        ) if label == "user+persona" else ((label or "user", row["user_message"]),)
        messages: list[ResolvedTurnMessage] = []
        for speaker, text in halves:
            value = str(text or "")
            if not value.strip():
                continue
            messages.append(
                ResolvedTurnMessage(
                    turn_id=turn.turn_id,
                    source_kind=turn.source_kind,
                    speaker=speaker,
                    raw_text=value,
                    occurred_at=occurred,
                    position=turn.position,
                    room_id=turn.room_id,
                    session_id=turn.session_id,
                )
            )
        return messages

    async def consolidate_pending(
        self,
        *,
        summarize: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]],
        limit: int = 5,
        persona_id: str | None = None,
        room_id: str | None = None,
        allow_open: bool = True,
    ) -> dict[str, Any]:
        """Work through the owed-summary list, newest-activity-last.

        The caller owns the pacing: this is the seam a background worker or a
        CLI uses, and it makes no policy decision about how many model calls are
        acceptable.  Each Episode is independent, so one failure never stops
        the rest.
        """

        reports: list[dict[str, Any]] = []
        for episode in self.pending_episodes(
            limit=max(1, int(limit)), persona_id=persona_id, room_id=room_id
        ):
            reports.append(
                await self.consolidate_episode(
                    episode.id, summarize=summarize, allow_open=allow_open
                )
            )
        return {
            "attempted": len(reports),
            "succeeded": sum(1 for item in reports if item["summary_created"]),
            "failed": sum(1 for item in reports if item.get("error")),
            "reports": reports,
        }

    def record_failure(self, episode_id: str, error: str) -> None:
        episode = self.get_episode(episode_id)
        if episode is None:
            return
        self._record_failure(episode, error)

    def _record_failure(self, episode: MemoryEpisode, error: str) -> None:
        now = datetime.now(UTC)
        self.database.conn.execute(
            """
            UPDATE memory_episodes
            SET status = ?, summary_status = 'failed', last_error = ?,
                consolidation_attempts = consolidation_attempts + 1, updated_at = ?
            WHERE id = ?
            """,
            (EpisodeStatus.FAILED.value, str(error)[:240], now.isoformat(), episode.id),
        )
        self.database.conn.commit()

    # -- recovery / backfill ----------------------------------------------

    def recover_pending(self, *, limit: int = 200) -> dict[str, Any]:
        """Restart sweep: make every owed summary durably visible again.

        An Episode that was mid-consolidation when the process died is left in
        ``FAILED``/``PENDING_CONSOLIDATION`` and picked up here.  Nothing is
        re-summarised automatically; this only restores the work list.
        """

        now = datetime.now(UTC)
        cursor = self.database.conn.execute(
            """
            UPDATE memory_episodes
            SET status = ?, updated_at = ?
            WHERE summary_status != 'ready'
              AND status IN (?, ?)
            """,
            (
                EpisodeStatus.PENDING_CONSOLIDATION.value,
                now.isoformat(),
                EpisodeStatus.CLOSED.value,
                EpisodeStatus.FAILED.value,
            ),
        )
        self.database.conn.commit()
        pending = self.pending_episodes(limit=limit)
        return {
            "requeued": int(cursor.rowcount or 0),
            "pending": len(pending),
            "pending_episode_ids": [item.id for item in pending],
        }

    def pending_episodes(
        self,
        *,
        limit: int = 50,
        persona_id: str | None = None,
        room_id: str | None = None,
        session_id: str | None = None,
    ) -> list[MemoryEpisode]:
        clauses = ["summary_status != 'ready'", "turn_count > 0"]
        params: list[Any] = []
        if persona_id:
            clauses.append("persona_id = ?")
            params.append(persona_id)
        if room_id:
            clauses.append("room_id = ?")
            params.append(room_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"SELECT * FROM memory_episodes WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at ASC, sequence ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._row_to_episode(row) for row in rows]

    def unassigned_turns(
        self, *, limit: int = 40, persona_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Committed turns with no Episode yet (legacy history / backfill queue)."""

        clauses = [
            "NOT EXISTS (SELECT 1 FROM memory_episode_turns t WHERE t.turn_id = s.id)",
        ]
        params: list[Any] = []
        if persona_id:
            clauses.append("s.persona_id = ?")
            params.append(persona_id)
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"""
            SELECT s.id, s.session_id, s.persona_id, s.user_message, s.persona_response,
                   s.context_json, s.created_at
            FROM session_turns s
            WHERE {' AND '.join(clauses)}
            ORDER BY s.created_at ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def backfill(
        self, *, limit: int | None = None, persona_id: str | None = None
    ) -> dict[str, Any]:
        """Lazily fold legacy turns into Episodes, oldest first, in batches.

        Deliberately incremental: upgrading the database must not re-run an
        entire history through the model, and a failure here never touches the
        raw turns.
        """

        batch = max(1, int(limit or self.backfill_batch))
        rows = self.unassigned_turns(limit=batch, persona_id=persona_id)
        reports: list[dict[str, Any]] = []
        for row in rows:
            context = dict(loads(row["context_json"]))
            session = self.database.conn.execute(
                "SELECT metadata_json FROM sessions WHERE id = ?", (row["session_id"],)
            ).fetchone()
            session_metadata = dict(loads(session["metadata_json"])) if session else {}
            reports.append(
                self.assign_turn(
                    persona_id=str(row["persona_id"]),
                    session_id=str(row["session_id"]),
                    turn_id=str(row["id"]),
                    counterpart_id=str(
                        context.get("counterpart_id")
                        or session_metadata.get("counterpart_id")
                        or "user"
                    ),
                    branch_id=str(context.get("branch_id") or "main"),
                    room_id=session_metadata.get("room_id"),
                    occurred_at=parse_dt(row["created_at"]) or datetime.now(UTC),
                    user_message=str(row["user_message"] or ""),
                    persona_response=str(row["persona_response"] or ""),
                )
            )
        if reports:
            self.database.conn.commit()
        return {
            "processed": len(reports),
            "remaining": len(self.unassigned_turns(limit=batch, persona_id=persona_id)),
            "created": sum(1 for item in reports if item["action"] == "create"),
            "appended": sum(1 for item in reports if item["action"] == "append"),
            "actions": reports,
        }

    # -- deletion lifecycle (Phase 3.1 / A5) -------------------------------

    def refresh_source_availability(self, episode_ids: Sequence[str]) -> dict[str, Any]:
        """Recompute and stamp each Episode's provenance availability.

        Explicit, never silent: an Episode whose raw sources were removed by an
        intentional deletion says so in ``metadata.source_availability``
        (``complete`` / ``partial`` / ``deleted``) with the number of missing
        sources, instead of looking healthy until someone clicks provenance.
        """

        now = datetime.now(UTC).isoformat()
        summary: dict[str, int] = {"complete": 0, "partial": 0, "deleted": 0}
        for episode_id in episode_ids:
            episode = self.get_episode(episode_id)
            if episode is None:
                continue
            turns = self.episode_turns(episode_id)
            missing = sum(1 for turn in turns if not self.resolve_turn_text(turn))
            if not turns or missing == 0:
                availability = "complete"
            elif missing >= len(turns):
                availability = "deleted"
            else:
                availability = "partial"
            metadata = dict(episode.metadata)
            metadata["source_availability"] = availability
            metadata["unavailable_source_count"] = missing
            metadata["source_availability_checked_at"] = now
            self.database.conn.execute(
                "UPDATE memory_episodes SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (dumps(metadata), now, episode_id),
            )
            summary[availability] += 1
        self.database.conn.commit()
        return summary

    def mark_room_sources_unavailable(self, room_id: str) -> dict[str, Any]:
        """After a room's transcripts are deleted, mark the Episodes that lose sources.

        Room deletion is an existing, explicit operation.  The Episodes are
        persona memory and survive it; their shared-user provenance does not,
        and that fact is recorded rather than left dangling silently.
        """

        episode_ids = [
            str(row["episode_id"])
            for row in self.database.conn.execute(
                "SELECT DISTINCT episode_id FROM memory_episode_turns WHERE room_id = ?",
                (room_id,),
            ).fetchall()
        ]
        if not episode_ids:
            return {"episodes": 0, "unavailable_sources": 0}
        availability = self.refresh_source_availability(episode_ids)
        row = self.database.conn.execute(
            """
            SELECT COUNT(*) AS c FROM memory_episode_turns t
            WHERE t.room_id = ?
              AND NOT EXISTS (SELECT 1 FROM room_transcripts r WHERE r.turn_id = t.turn_id)
            """,
            (room_id,),
        ).fetchone()
        return {
            "episodes": len(episode_ids),
            "availability": availability,
            "unavailable_sources": int(row["c"]),
        }

    def purge_session(
        self, session_id: str, *, turn_ids: Sequence[str], delete_episodes: bool
    ) -> dict[str, Any]:
        """Apply the Episode-layer semantics of ``delete_session``.

        ``delete_episodes=True`` (the default contract, matching
        ``delete_derived_memories``): everything the session uniquely produced is
        removed -- its Episodes, their source relations, their summary lineage,
        and the fact-evidence rows that pointed at them.  A Fact that is left
        with no evidence at all is RETRACTED, never kept as if it were still
        supported.

        ``delete_episodes=False``: the Episodes and Facts survive, because the
        caller explicitly asked to keep derived memory -- but their provenance is
        stamped ``partial``/``deleted`` so the loss of raw evidence is visible.
        """

        episode_ids = [
            str(row["id"])
            for row in self.database.conn.execute(
                "SELECT id FROM memory_episodes WHERE session_id = ?", (session_id,)
            ).fetchall()
        ]
        report: dict[str, Any] = {
            "session_id": session_id,
            "episodes": len(episode_ids),
            "episodes_deleted": 0,
            "episode_source_rows_deleted": 0,
            "facts_retracted": 0,
            "facts_provenance_partial": 0,
            "thread_sources_stamped": 0,
            "summaries_stamped": 0,
            "availability": {},
        }
        # Fact evidence that pointed at the disappearing turns is removed first,
        # in both modes: a pointer to a deleted turn is not evidence.
        if turn_ids:
            retracted, partial = self._remove_fact_evidence(
                turn_ids=list(turn_ids), episode_ids=episode_ids if delete_episodes else []
            )
            report["facts_retracted"] = retracted
            report["facts_provenance_partial"] = partial
            report["thread_sources_stamped"] = self._stamp_thread_provenance(
                episode_ids=list(episode_ids),
                turn_ids=list(turn_ids),
                availability="deleted" if delete_episodes else "partial",
            )
            report["summaries_stamped"] = self._stamp_summary_provenance(
                episode_ids=list(episode_ids),
                availability="deleted" if delete_episodes else "partial",
            )
        if delete_episodes and episode_ids:
            placeholders = ",".join("?" for _ in episode_ids)
            cursor = self.database.conn.execute(
                f"DELETE FROM memory_episode_turns WHERE episode_id IN ({placeholders})",
                tuple(episode_ids),
            )
            report["episode_source_rows_deleted"] = int(cursor.rowcount or 0)
            self.database.conn.execute(
                f"DELETE FROM lineage WHERE child_type = 'episode' "
                f"AND child_id IN ({placeholders})",
                tuple(episode_ids),
            )
            self.database.conn.execute(
                f"DELETE FROM lineage WHERE parent_type = 'episode' "
                f"AND parent_id IN ({placeholders})",
                tuple(episode_ids),
            )
            cursor = self.database.conn.execute(
                f"DELETE FROM memory_episodes WHERE id IN ({placeholders})",
                tuple(episode_ids),
            )
            report["episodes_deleted"] = int(cursor.rowcount or 0)
        elif episode_ids:
            report["availability"] = self.refresh_source_availability(episode_ids)
            # The Episodes survive (the caller asked to keep derived memory), but
            # the Facts they support now rest on unavailable sources.  Stamping
            # them keeps the loss visible from the fact side too.
            report["facts_provenance_partial"] += self._stamp_facts_provenance(episode_ids)
        self.database.conn.commit()
        return report

    def _stamp_summary_provenance(
        self, *, episode_ids: Sequence[str], availability: str
    ) -> int:
        """Hand the deleted Episodes to the summary layer.

        Retraction rules and parent propagation belong to ``HierarchyService``;
        duplicating them here would let two copies diverge.  The container binds
        that service through ``summary_provenance_hook``, and a database without
        it (a pre-Phase-6 build) simply has nothing to stamp.
        """

        hook = getattr(self, "summary_provenance_hook", None)
        if hook is None or not episode_ids:
            return 0
        result = hook(episode_ids=list(episode_ids), availability=availability)
        if isinstance(result, dict):
            return int(result.get("summaries_stamped") or 0)
        return int(result or 0)

    def _stamp_thread_provenance(
        self, *, episode_ids: Sequence[str], turn_ids: Sequence[str], availability: str
    ) -> int:
        """Mark Active-Thread provenance whose raw sources were deleted.

        Same contract as Facts (Phase 3.1): a Thread event that pointed at a
        deleted Episode/turn says so (``source_availability`` plus
        ``metadata.provenance_availability`` on the thread) instead of dangling
        silently.  Thread history itself is kept -- it is what the conversation
        actually did, and deleting the transcript does not rewrite it.
        """

        if not self._table_exists("memory_thread_sources"):
            return 0
        affected: set[str] = set()
        stamped = 0
        now = datetime.now(UTC).isoformat()
        if episode_ids:
            placeholders = ",".join("?" for _ in episode_ids)
            affected.update(
                str(row["thread_id"])
                for row in self.database.conn.execute(
                    f"SELECT DISTINCT thread_id FROM memory_thread_sources "
                    f"WHERE episode_id IN ({placeholders})",
                    tuple(episode_ids),
                ).fetchall()
            )
            stamped += int(
                self.database.conn.execute(
                    f"UPDATE memory_thread_sources SET excerpt_available = 0 "
                    f"WHERE episode_id IN ({placeholders})",
                    tuple(episode_ids),
                ).rowcount
                or 0
            )
            if self._table_exists("memory_thread_events"):
                stamped += int(
                    self.database.conn.execute(
                        f"UPDATE memory_thread_events SET source_availability = ? "
                        f"WHERE source_episode_id IN ({placeholders})",
                        (availability, *episode_ids),
                    ).rowcount
                    or 0
                )
        if turn_ids:
            placeholders = ",".join("?" for _ in turn_ids)
            affected.update(
                str(row["thread_id"])
                for row in self.database.conn.execute(
                    f"SELECT DISTINCT thread_id FROM memory_thread_sources "
                    f"WHERE turn_id IN ({placeholders})",
                    tuple(turn_ids),
                ).fetchall()
            )
            stamped += int(
                self.database.conn.execute(
                    f"UPDATE memory_thread_sources SET excerpt_available = 0 "
                    f"WHERE turn_id IN ({placeholders})",
                    tuple(turn_ids),
                ).rowcount
                or 0
            )
        for thread_id in sorted(affected):
            row = self.database.conn.execute(
                "SELECT metadata_json FROM memory_active_threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                continue
            metadata = dict(loads(row["metadata_json"]))
            metadata["provenance_availability"] = availability
            metadata["provenance_note"] = "source turns removed"
            self.database.conn.execute(
                "UPDATE memory_active_threads SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, dumps(metadata), thread_id),
            )
        return stamped

    def _stamp_facts_provenance(
        self, episode_ids: Sequence[str], *, availability: str = "partial"
    ) -> int:
        if not episode_ids or not self._table_exists("memory_fact_sources"):
            return 0
        placeholders = ",".join("?" for _ in episode_ids)
        rows = self.database.conn.execute(
            f"SELECT DISTINCT fact_id FROM memory_fact_sources "
            f"WHERE episode_id IN ({placeholders})",
            tuple(episode_ids),
        ).fetchall()
        now = datetime.now(UTC).isoformat()
        for row in rows:
            fact_id = str(row["fact_id"])
            metadata = self._fact_metadata(fact_id)
            metadata["provenance_availability"] = availability
            metadata["provenance_note"] = "source turns removed"
            self.database.conn.execute(
                "UPDATE memory_semantic_facts SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, dumps(metadata), fact_id),
            )
        return len(rows)

    def _remove_fact_evidence(
        self, *, turn_ids: Sequence[str], episode_ids: Sequence[str]
    ) -> tuple[int, int]:
        """Remove fact-evidence rows for deleted turns/episodes.

        Returns ``(retracted, partial)``: facts left with no evidence at all are
        retracted; facts that merely lost some evidence are marked partial.
        """

        if not self._table_exists("memory_fact_sources"):
            return (0, 0)
        affected: set[str] = set()
        if turn_ids:
            placeholders = ",".join("?" for _ in turn_ids)
            affected.update(
                str(row["fact_id"])
                for row in self.database.conn.execute(
                    f"SELECT DISTINCT fact_id FROM memory_fact_sources "
                    f"WHERE turn_id IN ({placeholders})",
                    tuple(turn_ids),
                ).fetchall()
            )
        if episode_ids:
            placeholders = ",".join("?" for _ in episode_ids)
            affected.update(
                str(row["fact_id"])
                for row in self.database.conn.execute(
                    f"SELECT DISTINCT fact_id FROM memory_fact_sources "
                    f"WHERE episode_id IN ({placeholders})",
                    tuple(episode_ids),
                ).fetchall()
            )
        if not affected:
            return (0, 0)
        retracted = 0
        partial = 0
        now = datetime.now(UTC).isoformat()
        for fact_id in affected:
            if turn_ids:
                placeholders = ",".join("?" for _ in turn_ids)
                self.database.conn.execute(
                    f"DELETE FROM memory_fact_sources WHERE fact_id = ? "
                    f"AND turn_id IN ({placeholders})",
                    (fact_id, *turn_ids),
                )
            if episode_ids:
                placeholders = ",".join("?" for _ in episode_ids)
                self.database.conn.execute(
                    f"DELETE FROM memory_fact_sources WHERE fact_id = ? "
                    f"AND episode_id IN ({placeholders})",
                    (fact_id, *episode_ids),
                )
            remaining = int(
                self.database.conn.execute(
                    "SELECT COUNT(*) AS c FROM memory_fact_sources WHERE fact_id = ?", (fact_id,)
                ).fetchone()["c"]
            )
            if remaining == 0:
                metadata = self._fact_metadata(fact_id)
                metadata["retracted_reason"] = "sources_deleted"
                self.database.conn.execute(
                    "UPDATE memory_semantic_facts SET status = 'retracted', updated_at = ?, "
                    "metadata_json = ? WHERE id = ? AND status != 'retracted'",
                    (now, dumps(metadata), fact_id),
                )
                retracted += 1
            else:
                metadata = self._fact_metadata(fact_id)
                metadata["provenance_availability"] = "partial"
                metadata["unavailable_source_count"] = 0
                self.database.conn.execute(
                    "UPDATE memory_semantic_facts SET updated_at = ?, "
                    "metadata_json = ? WHERE id = ?",
                    (now, dumps(metadata), fact_id),
                )
                partial += 1
        return retracted, partial

    def _fact_metadata(self, fact_id: str) -> dict[str, Any]:
        """Existing metadata for a fact, so a stamp never drops other keys."""

        if not self._table_exists("memory_semantic_facts"):
            return {}
        row = self.database.conn.execute(
            "SELECT metadata_json FROM memory_semantic_facts WHERE id = ?", (fact_id,)
        ).fetchone()
        return dict(loads(row["metadata_json"])) if row else {}

    def _table_exists(self, name: str) -> bool:
        row = self.database.conn.execute(
            "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    # -- reading -----------------------------------------------------------

    def get_episode(self, episode_id: str) -> MemoryEpisode | None:
        row = self.database.conn.execute(
            "SELECT * FROM memory_episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        return self._row_to_episode(row) if row else None

    def open_episode(self, scope: EpisodeScope) -> MemoryEpisode | None:
        row = self.database.conn.execute(
            f"""
            SELECT * FROM memory_episodes
            WHERE {SCOPE_COLUMNS} = (?, ?, ?, ?) AND status = ?
            ORDER BY sequence DESC LIMIT 1
            """,
            (*scope.key, EpisodeStatus.OPEN.value),
        ).fetchone()
        return self._row_to_episode(row) if row else None

    def episode_for_turn(self, turn_id: str) -> MemoryEpisode | None:
        row = self.database.conn.execute(
            """
            SELECT e.* FROM memory_episodes e
            JOIN memory_episode_turns t ON t.episode_id = e.id
            WHERE t.turn_id = ?
            ORDER BY t.position LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        return self._row_to_episode(row) if row else None

    def episode_turns(self, episode_id: str) -> list[EpisodeTurn]:
        rows = self.database.conn.execute(
            "SELECT * FROM memory_episode_turns WHERE episode_id = ? ORDER BY position ASC",
            (episode_id,),
        ).fetchall()
        return [self._row_to_episode_turn(row) for row in rows]

    def list_episodes(
        self,
        *,
        persona_id: str | None = None,
        counterpart_id: str | None = None,
        branch_id: str | None = None,
        session_id: str | None = None,
        room_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEpisode]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("persona_id", persona_id),
            ("counterpart_id", counterpart_id),
            ("branch_id", branch_id),
            ("session_id", session_id),
            ("room_id", room_id),
            ("status", status),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"SELECT * FROM memory_episodes {where} "
            "ORDER BY started_at DESC, sequence DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._row_to_episode(row) for row in rows]

    def count_episodes(
        self,
        *,
        persona_id: str | None = None,
        room_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Cheap storage statistic (used by the Context Assembly Report)."""

        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("persona_id", persona_id),
            ("room_id", room_id),
            ("session_id", session_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.database.conn.execute(
            f"SELECT COUNT(*) AS c FROM memory_episodes {where}", tuple(params)
        ).fetchone()
        return int(row["c"])

    def inspect_episode(
        self, episode_id: str, *, include_sources: bool = True
    ) -> dict[str, Any] | None:
        """Debug/inspection view: one Episode plus resolvable provenance.

        Returns identifiers and a per-turn resolution flag, never the raw
        dialogue body, so an inspection cannot leak private text into logs.
        """

        episode = self.get_episode(episode_id)
        if episode is None:
            return None
        turns = self.episode_turns(episode_id)
        payload: dict[str, Any] = {
            "episode": episode.model_dump(mode="json"),
            "summary": episode.structured_summary().model_dump(mode="json"),
            "source_turn_ids": [turn.turn_id for turn in turns],
            "turn_count": len(turns),
        }
        if include_sources:
            payload["sources"] = [
                {
                    "turn_id": turn.turn_id,
                    "source_kind": turn.source_kind,
                    "position": turn.position,
                    "session_id": turn.session_id,
                    "room_id": turn.room_id,
                    "occurred_at": turn.occurred_at.isoformat() if turn.occurred_at else None,
                    "token_estimate": turn.token_estimate,
                    "resolvable": bool(self.resolve_turn_text(turn)),
                }
                for turn in turns
            ]
        return payload

    def coverage(
        self, *, persona_id: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        """The invariant, as numbers.

        ``committed == assigned + unassigned`` always, and ``orphaned`` counts
        committed turns that cannot be reached at all -- a turn whose owning
        session vanished, so it can neither be assigned nor backfilled.  It must
        be 0.

        Two neighbouring ideas are tracked separately, because confusing them
        would either hide a real loss or cry wolf:

        * ``unassigned_pending_backfill`` -- a real turn that is merely not
          organised yet (pre-Phase-3 history).
        * ``unavailable_sources`` -- provenance rows whose RAW source was
          removed by an explicit delete (session/room).  The turn is gone on
          purpose, so this is not an orphan; the Episode records an explicit
          ``source_availability`` marker instead of dangling silently.
        """

        where: list[str] = []
        params: list[Any] = []
        if persona_id:
            where.append("s.persona_id = ?")
            params.append(persona_id)
        if session_id:
            where.append("s.session_id = ?")
            params.append(session_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        committed = int(
            self.database.conn.execute(
                f"SELECT COUNT(*) AS c FROM session_turns s {clause}", tuple(params)
            ).fetchone()["c"]
        )
        assigned = int(
            self.database.conn.execute(
                f"""
                SELECT COUNT(DISTINCT t.turn_id) AS c
                FROM memory_episode_turns t
                JOIN session_turns s ON s.id = t.turn_id
                {clause}
                """,
                tuple(params),
            ).fetchone()["c"]
        )
        pending_clause = " ".join([f"AND {item}" for item in where]) if where else ""
        pending = int(
            self.database.conn.execute(
                f"""
                SELECT COUNT(DISTINCT t.turn_id) AS c
                FROM memory_episode_turns t
                JOIN session_turns s ON s.id = t.turn_id
                JOIN memory_episodes e ON e.id = t.episode_id
                WHERE e.summary_status != 'ready'
                {pending_clause}
                """,
                tuple(params),
            ).fetchone()["c"]
        )
        # Structural integrity, in two distinct flavours:
        #  * orphaned  -- a committed turn that has no home at all (dangling
        #    Episode provenance, or a session that no longer exists);
        #  * unavailable_sources -- provenance rows whose RAW source was removed
        #    by an explicit deletion.  The Episode keeps an explicit
        #    ``source_availability`` marker instead of silently dangling.
        dangling = int(
            self.database.conn.execute(
                """
                SELECT COUNT(*) AS c FROM memory_episode_turns t
                WHERE t.source_kind = 'session_turn'
                  AND NOT EXISTS (SELECT 1 FROM session_turns s WHERE s.id = t.turn_id)
                """
            ).fetchone()["c"]
        )
        sessionless = int(
            self.database.conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM session_turns s
                WHERE NOT EXISTS (SELECT 1 FROM sessions x WHERE x.id = s.session_id)
                {'AND ' + ' AND '.join(where) if where else ''}
                """,
                tuple(params),
            ).fetchone()["c"]
        )
        unavailable = int(
            self.database.conn.execute(
                """
                SELECT COUNT(*) AS c FROM memory_episode_turns t
                WHERE (
                        t.source_kind = 'session_turn'
                        AND NOT EXISTS (SELECT 1 FROM session_turns s WHERE s.id = t.turn_id)
                      )
                   OR (
                        t.source_kind IN ('shared_user', 'room_transcript')
                        AND NOT EXISTS (
                          SELECT 1 FROM room_transcripts r WHERE r.turn_id = t.turn_id
                        )
                      )
                """
            ).fetchone()["c"]
        )
        shared_sources = int(
            self.database.conn.execute(
                "SELECT COUNT(*) AS c FROM memory_episode_turns WHERE source_kind = 'shared_user'"
            ).fetchone()["c"]
        )
        unassigned = max(0, committed - assigned)
        return {
            "committed_turns": committed,
            "assigned_to_episode": assigned,
            "pending_consolidation": pending,
            "unassigned_pending_backfill": unassigned,
            "orphaned": sessionless,
            "dangling_provenance": dangling,
            "sessionless_turns": sessionless,
            "shared_user_sources": shared_sources,
            "unavailable_sources": unavailable,
            "episodes": int(
                self.database.conn.execute(
                    "SELECT COUNT(*) AS c FROM memory_episodes"
                    + (" WHERE persona_id = ?" if persona_id else ""),
                    (persona_id,) if persona_id else (),
                ).fetchone()["c"]
            ),
        }

    # -- internals ---------------------------------------------------------

    def _create_episode(
        self,
        scope: EpisodeScope,
        *,
        started_at: datetime,
        room_id: str | None,
        visibility: str | None,
        boundary_reason: str,
    ) -> MemoryEpisode:
        sequence = int(
            self.database.conn.execute(
                f"SELECT COALESCE(MAX(sequence), 0) AS s FROM memory_episodes "
                f"WHERE {SCOPE_COLUMNS} = (?, ?, ?, ?)",
                scope.key,
            ).fetchone()["s"]
        ) + 1
        now = datetime.now(UTC)
        episode = MemoryEpisode(
            id=new_id("episode"),
            persona_id=scope.persona_id,
            counterpart_id=scope.counterpart_id,
            branch_id=scope.branch_id,
            session_id=scope.session_id,
            room_id=room_id,
            sequence=sequence,
            status=EpisodeStatus.OPEN,
            boundary_reason=boundary_reason,
            started_at=started_at,
            created_at=now,
            updated_at=now,
            consolidation_version=EPISODE_CONSOLIDATION_VERSION,
            visibility=visibility
            or ("room_public" if room_id else "private_session"),
            material_scope=CHARACTER_VISIBLE,
            # ``boundary_reason`` is overloaded on purpose: for a CLOSED episode
            # it is why the episode ended, for an OPEN one why it started (it
            # has no end yet).  The two halves are kept separately here so a
            # reader never has to guess which one a row means.
            metadata={"start_reason": boundary_reason},
        )
        columns = (
            "id",
            "persona_id",
            "counterpart_id",
            "branch_id",
            "session_id",
            "room_id",
            "sequence",
            "title",
            "summary",
            "summary_json",
            "status",
            "boundary_reason",
            "started_at",
            "ended_at",
            "turn_count",
            "source_token_estimate",
            "source_first_turn_id",
            "source_last_turn_id",
            "source_range_hash",
            "importance",
            "confidence",
            "consolidation_version",
            "summary_status",
            "consolidation_attempts",
            "last_error",
            "consolidated_at",
            "visibility",
            "provenance",
            "material_scope",
            "created_at",
            "updated_at",
            "metadata_json",
        )
        values = (
            episode.id,
            episode.persona_id,
            episode.counterpart_id,
            episode.branch_id,
            episode.session_id,
            episode.room_id,
            episode.sequence,
            episode.title,
            episode.summary,
            dumps(episode.summary_json),
            episode.status.value,
            episode.boundary_reason,
            dt(episode.started_at),
            None,
            0,
            0,
            None,
            None,
            "",
            episode.importance,
            episode.confidence,
            episode.consolidation_version,
            "pending",
            0,
            None,
            None,
            episode.visibility,
            episode.provenance,
            episode.material_scope,
            dt(episode.created_at),
            dt(episode.updated_at),
            dumps(episode.metadata),
        )
        # Column list and placeholders come from one source of truth, so a
        # future column cannot silently desync them.
        self.database.conn.execute(
            f"INSERT INTO memory_episodes ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        return episode

    def _next_position(self, episode_id: str) -> int:
        """Next slot in one Episode's ordered source list."""

        row = self.database.conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS p FROM memory_episode_turns "
            "WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        return int(row["p"]) + 1

    def episode_has_source(self, turn_id: str, *, episode_id: str | None = None) -> bool:
        """Whether a turn is already a source of an Episode (optionally a given one)."""

        if not turn_id:
            return False
        if episode_id is None:
            row = self.database.conn.execute(
                "SELECT 1 AS x FROM memory_episode_turns WHERE turn_id = ? LIMIT 1", (turn_id,)
            ).fetchone()
        else:
            row = self.database.conn.execute(
                "SELECT 1 AS x FROM memory_episode_turns "
                "WHERE turn_id = ? AND episode_id = ? LIMIT 1",
                (turn_id, episode_id),
            ).fetchone()
        return row is not None

    def _attach_turn(
        self, episode: MemoryEpisode, turn: EpisodeTurn, *, occurred_at: datetime
    ) -> None:
        now = datetime.now(UTC)
        self.database.conn.execute(
            """
            INSERT OR IGNORE INTO memory_episode_turns (
              episode_id, turn_id, source_kind, position, session_id, room_id,
              speaker, occurred_at, token_estimate, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                turn.episode_id,
                turn.turn_id,
                turn.source_kind,
                turn.position,
                turn.session_id,
                turn.room_id,
                turn.speaker,
                dt(turn.occurred_at),
                turn.token_estimate,
                now.isoformat(),
            ),
        )
        turn_ids = [item.turn_id for item in self.episode_turns(episode.id)]
        scope = EpisodeScope(
            persona_id=episode.persona_id,
            counterpart_id=episode.counterpart_id,
            branch_id=episode.branch_id,
            session_id=episode.session_id,
        )
        # Aggregates are recomputed from the rows rather than incremented, so a
        # repeated attach can never inflate them.  ``turn_count`` stays the count
        # of the Episode's OWN committed turns: shared room messages are extra
        # sources and must not be able to trigger a max_turns boundary.
        totals = self.database.conn.execute(
            """
            SELECT COUNT(*) AS sources,
                   COALESCE(SUM(CASE WHEN source_kind = 'session_turn' THEN 1 ELSE 0 END), 0)
                     AS own_turns,
                   COALESCE(SUM(token_estimate), 0) AS tokens
            FROM memory_episode_turns WHERE episode_id = ?
            """,
            (episode.id,),
        ).fetchone()
        own_turn_ids = [
            item.turn_id
            for item in self.episode_turns(episode.id)
            if item.source_kind == "session_turn"
        ]
        self.database.conn.execute(
            """
            UPDATE memory_episodes
            SET turn_count = ?, source_token_estimate = ?,
                source_first_turn_id = COALESCE(source_first_turn_id, ?),
                source_last_turn_id = ?, source_range_hash = ?, ended_at = ?,
                updated_at = ?, room_id = COALESCE(room_id, ?)
            WHERE id = ?
            """,
            (
                int(totals["own_turns"]),
                int(totals["tokens"]),
                turn.turn_id,
                own_turn_ids[-1] if own_turn_ids else turn.turn_id,
                _range_hash(scope, turn_ids),
                dt(occurred_at),
                now.isoformat(),
                turn.room_id,
                episode.id,
            ),
        )

    def _insert_lineage(
        self,
        *,
        persona_id: str,
        child_type: str,
        child_id: str,
        parent_type: str,
        parent_id: str,
        relation: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Reuse the existing ``lineage`` table; no second graph store."""

        self.database.conn.execute(
            "INSERT OR IGNORE INTO lineage "
            "(id, persona_id, child_type, child_id, parent_type, parent_id, relation, "
            " metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                # The parent id is part of the key: an Episode legitimately has
                # one "contains" edge PER source turn.
                f"lin_{child_type}_{child_id}_{relation}_{parent_id}",
                persona_id,
                child_type,
                child_id,
                parent_type,
                parent_id,
                relation,
                dumps(metadata or {}),
                datetime.now(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _row_to_episode(row: Any) -> MemoryEpisode:
        return MemoryEpisode(
            id=str(row["id"]),
            persona_id=str(row["persona_id"]),
            counterpart_id=str(row["counterpart_id"]),
            branch_id=str(row["branch_id"]),
            session_id=str(row["session_id"]),
            room_id=row["room_id"],
            sequence=int(row["sequence"]),
            title=str(row["title"] or ""),
            summary=str(row["summary"] or ""),
            summary_json=dict(loads(row["summary_json"])),
            status=episode_status_from_raw(row["status"]),
            boundary_reason=str(row["boundary_reason"] or "none"),
            started_at=parse_dt(row["started_at"]) or datetime.now(UTC),
            ended_at=parse_dt(row["ended_at"]),
            turn_count=int(row["turn_count"] or 0),
            source_token_estimate=int(row["source_token_estimate"] or 0),
            source_first_turn_id=row["source_first_turn_id"],
            source_last_turn_id=row["source_last_turn_id"],
            source_range_hash=str(row["source_range_hash"] or ""),
            importance=float(row["importance"] or 0.5),
            confidence=float(row["confidence"] or 0.5),
            consolidation_version=int(row["consolidation_version"] or 1),
            summary_status=str(row["summary_status"] or "pending"),
            consolidation_attempts=int(row["consolidation_attempts"] or 0),
            last_error=row["last_error"],
            consolidated_at=parse_dt(row["consolidated_at"]),
            fact_extraction_status=str(row["fact_extraction_status"] or "pending"),
            fact_extraction_attempts=int(row["fact_extraction_attempts"] or 0),
            fact_extraction_error=row["fact_extraction_error"],
            facts_extracted_at=parse_dt(row["facts_extracted_at"]),
            thread_resolution_status=str(row["thread_resolution_status"] or "pending"),
            thread_resolution_attempts=int(row["thread_resolution_attempts"] or 0),
            thread_resolution_error=row["thread_resolution_error"],
            threads_resolved_at=parse_dt(row["threads_resolved_at"]),
            visibility=str(row["visibility"] or "private_session"),
            provenance=str(row["provenance"] or "digital_experience"),
            material_scope=str(row["material_scope"] or CHARACTER_VISIBLE),
            created_at=parse_dt(row["created_at"]) or datetime.now(UTC),
            updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
            metadata=dict(loads(row["metadata_json"])),
        )

    @staticmethod
    def _row_to_episode_turn(row: Any) -> EpisodeTurn:
        return EpisodeTurn(
            episode_id=str(row["episode_id"]),
            turn_id=str(row["turn_id"]),
            source_kind=str(row["source_kind"] or "session_turn"),
            position=int(row["position"] or 0),
            session_id=row["session_id"],
            room_id=row["room_id"],
            speaker=str(row["speaker"] or ""),
            occurred_at=parse_dt(row["occurred_at"]),
            token_estimate=int(row["token_estimate"] or 0),
        )


__all__ = [
    "EPISODE_SUMMARY_INTRO",
    "EPISODE_SUMMARY_SCHEMA",
    "EpisodeService",
    "estimate_text_tokens",
]
