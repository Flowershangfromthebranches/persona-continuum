"""Semantic Facts: extraction, de-duplication, temporal validity, provenance.

Pipeline position::

    committed turn -> Episode -> Episode summary (optional)
                              -> Semantic Fact extraction   <- this module
                                     -> dedup / conflict classification
                                     -> temporal validity
                                     -> fact store + provenance

Design rules that the code enforces rather than hopes for:

* **A fact is never overwritten.**  A value that stops being true gets
  ``valid_until`` and ``superseded_by_fact_id``; the new value is a new row.
* **No silent overwrite on conflict.**  When the extractor is unsure, the new
  claim is stored as ``candidate`` with an explicit conflict marker and the old
  fact stays active.  Storing less beats forgetting history.
* **Replays are free.**  Evidence lives in ``memory_fact_sources`` behind a
  primary key, and an episode that already produced a fact can be re-extracted
  without adding evidence, chaining supersessions, or creating duplicates.
* **Chat never waits.**  Extraction is asynchronous, bounded, and failure is a
  recorded status -- not an exception in a reply path.
* **Nothing here reaches a prompt.**  Phase 4 stores and inspects only; the
  Context Assembly Report says so explicitly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from persona_continuum.application._utils import dt, dumps, loads, new_id, parse_dt
from persona_continuum.application.episode_service import (
    EpisodeService,
    estimate_text_tokens,
)
from persona_continuum.domain.provenance import CHARACTER_VISIBLE
from persona_continuum.domain.semantic_fact import (
    FACT_EXTRACTION_VERSION,
    FactCandidate,
    FactCategory,
    FactDurability,
    FactEvidenceRole,
    FactExtractionPayload,
    FactOrigin,
    FactRelation,
    FactStatus,
    PlanStatus,
    SemanticFact,
    canonical_fact_key,
    canonical_value_key,
)
from persona_continuum.storage.database import Database

#: What the extractor is asked for.  Every field is optional: a partial answer
#: degrades into a weaker candidate instead of an invalid payload.
FACT_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [category.value for category in FactCategory],
                    },
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "value": {"type": "string"},
                    "display_text": {"type": "string"},
                    "origin": {
                        "type": "string",
                        "enum": [origin.value for origin in FactOrigin],
                    },
                    "durability": {
                        "type": "string",
                        "enum": [item.value for item in FactDurability],
                    },
                    "confidence": {"type": "number"},
                    "exclusive": {"type": "boolean"},
                    "relation": {
                        "type": "string",
                        "enum": [relation.value for relation in FactRelation],
                    },
                    "related_fact_id": {"type": ["string", "null"]},
                    "evidence_turn_id": {"type": ["string", "null"]},
                    "evidence_role": {
                        "type": "string",
                        "enum": [role.value for role in FactEvidenceRole],
                    },
                    "temporal_expression": {"type": ["string", "null"]},
                    "temporal_normalized": {"type": ["string", "null"]},
                    "temporal_confidence": {"type": "number"},
                    "plan_status": {
                        "type": "string",
                        "enum": [status.value for status in PlanStatus],
                    },
                    "notes": {"type": ["string", "null"]},
                },
                "required": ["category", "subject", "predicate", "value", "origin"],
            },
        }
    },
    "required": ["facts"],
}

FACT_EXTRACTION_INTRO = (
    "你在为一个长期对话系统抽取「可复用的事实」。输入是一段对话的原始轮次"
    "（以及可选的结构化摘要）。输出是事实列表。这是数据抽取任务，不是角色扮演。\n"
    "硬性要求：\n"
    "1. 只依据输入中真实出现的内容。不新增事实，不推测用户身份、职业或未表达的动机。\n"
    "2. speaker 字段描述这一条轮次里到底是谁在说话：speaker=user+persona 表示同一条轮次"
    "里同时包含用户与人格的发言（文本中的 user: / persona: 前缀标明各自说了什么）；"
    "speaker=user 表示只有用户。请按文本中的说话人分别判断 origin。\n"
    "3. origin 必须如实标注：user_asserted（用户明确说过的）、"
    "persona_asserted（人格说过的）、system_observed（系统直接观察到的）、"
    "inferred（推断）。人格的猜测（例如「你肯定就是舍不得她」）只能标为 inferred，"
    "绝不能标成 user_asserted；反过来，用户自己说的事也不要标成 persona_asserted。\n"
    "4. 不要记录一次性的情绪或状态（「我今天好累」）。只有当某件事具有长期价值时"
    "才抽取；如果它确实值得记录但只是一时状态，标 durability=temporary。"
    "但偏好本身的变化不是一时状态：用户说「喝腻了」「换口味了」「现在更喜欢 X」时，"
    "这是偏好的更新（用 supersedes），不是 temporary。计划的具体要素"
    "（出发日期、地点、预算）同样不是一时状态。\n"
    "5. 假设与条件句不是事实：「如果以后去日本，我可能会想住东京」「要是有机会的话」"
    "表达的是设想，不是已经成立或已经计划的事。除非用户明确把它说成计划，"
    "否则不要抽成 durable 事实。\n"
    "6. 否定要按原意处理：「我不是不喜欢咖啡，只是不喜欢太苦的」不等于「不喜欢咖啡」，"
    "也不要反过来抽成「喜欢咖啡」。语义无法确定时宁可不抽。\n"
    "7. subject 用「用户」或对话中的人物称呼；predicate 用简短中文名词短语"
    "（例如「最喜欢的饮品」「正在做的项目」「下个月的计划」）；value 是具体取值。\n"
    "8. exclusive 表示该 predicate 是否单值（「最喜欢的饮品」是单值；「喜欢的饮品」不是）。\n"
    "9. relation 表示这条与下面列出的『已存在事实』的关系：same / supports / compatible / "
    "supersedes / contradicts / unrelated；related_fact_id 填对应已存在事实的 id。"
    "只有明确是同一件事被更新（例如「喝腻了，现在更喜欢美式」）才用 supersedes。"
    "只是新增一个并列偏好（例如「也喜欢乌龙茶」）用 compatible。无法判断时用 unrelated。\n"
    "10. 计划/目标/承诺请填 plan_status（planned / active / completed / cancelled / expired）。"
    "相对时间（「下个月」）请同时给出 temporal_expression 原文，"
    "尽量给出 temporal_normalized（例如 2026-10）与 temporal_confidence。\n"
    "11. 没有可抽取的事实时返回空列表。宁可少抽，不要编造。\n"
    "只输出结构化结果。"
)


class SemanticFactService:
    """Owns the Semantic Fact store.  Never deletes raw history."""

    def __init__(
        self,
        database: Database,
        episodes: EpisodeService,
        *,
        config: Any = None,
    ) -> None:
        self.database = database
        self.episodes = episodes
        self.config = config
        self.enabled = bool(getattr(config, "memory_fact_extraction_enabled", True))
        self.persist_inferred = bool(getattr(config, "memory_fact_persist_inferred", False))
        self.temporary_hours = max(
            1, int(getattr(config, "memory_fact_temporary_valid_hours", 24) or 24)
        )
        self.confidence_step = max(
            0.0, float(getattr(config, "memory_fact_confidence_step", 0.05) or 0.0)
        )
        self.max_existing_context = max(
            1, int(getattr(config, "memory_fact_max_existing_context", 30) or 30)
        )
        self.input_max_tokens = max(
            512, int(getattr(config, "memory_fact_extraction_input_max_tokens", 12000) or 12000)
        )
        self.batch = max(1, int(getattr(config, "memory_fact_extraction_batch", 3) or 3))

    # -- extraction (async, bounded, retryable) ----------------------------

    async def extract_episode_facts(
        self,
        episode_id: str,
        *,
        extract: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]],
        force: bool = False,
    ) -> dict[str, Any]:
        """Extract facts for one Episode.  Never raises on a provider problem.

        A summary is NOT required: the payload is built from the Episode's raw
        sources, which is also the more faithful input (a summary has already
        compressed once).
        """

        report: dict[str, Any] = {
            "episode_id": episode_id,
            "action": "noop",
            "facts_created": 0,
            "facts_reinforced": 0,
            "facts_superseded": 0,
            "facts_compatible": 0,
            "facts_conflict_pending": 0,
            "facts_replayed": 0,
            "skipped_inferred": 0,
            "skipped_invalid": 0,
            "extraction_version": FACT_EXTRACTION_VERSION,
            "pending": False,
            "error": None,
        }
        if not self.enabled:
            report["error"] = "fact_extraction_disabled"
            return report
        episode = self.episodes.get_episode(episode_id)
        if episode is None:
            report["error"] = "episode_not_found"
            return report
        if episode.fact_extraction_status == "ready" and not force:
            return report

        payload = self.build_extraction_payload(episode)
        if payload is None:
            report["error"] = "no_source_turns"
            self._mark_extraction(episode_id, status="failed", error="no_source_turns")
            report["pending"] = True
            return report

        try:
            raw = await extract(payload, FACT_EXTRACTION_SCHEMA)
        except Exception as exc:  # noqa: BLE001 - any provider failure is retryable
            error = f"extract_failed:{type(exc).__name__}"
            self._mark_extraction(episode_id, status="failed", error=error)
            report["error"] = error
            report["pending"] = True
            return report

        parsed = self.parse_extraction(raw)
        if parsed is None:
            self._mark_extraction(episode_id, status="failed", error="invalid_fact_payload")
            report["error"] = "invalid_fact_payload"
            report["pending"] = True
            return report

        counts = self.apply_candidates(episode, parsed)
        report.update(counts)
        self._mark_extraction(episode_id, status="ready", error=None)
        report["action"] = "extract"
        report["pending"] = False
        return report

    async def extract_pending(
        self,
        *,
        extract: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]],
        limit: int | None = None,
        persona_id: str | None = None,
        room_id: str | None = None,
    ) -> dict[str, Any]:
        """Work through Episodes that still owe fact extraction, in batches."""

        reports: list[dict[str, Any]] = []
        for episode in self.pending_extraction_episodes(
            limit=max(1, int(limit or self.batch)), persona_id=persona_id, room_id=room_id
        ):
            reports.append(
                await self.extract_episode_facts(episode.id, extract=extract)
            )
        return {
            "attempted": len(reports),
            "succeeded": sum(1 for item in reports if item["action"] == "extract"),
            "failed": sum(1 for item in reports if item.get("error")),
            "reports": reports,
        }

    @staticmethod
    def parse_extraction(raw: Any) -> FactExtractionPayload | None:
        """Validate an untrusted extractor payload.  Never trust raw JSON.

        An empty ``facts`` list is a legitimate answer ("nothing worth
        remembering").  A payload that CLAIMED facts and yielded none usable is
        a failure, so a broken model cannot look like a quiet success.
        """

        if isinstance(raw, FactExtractionPayload):
            return raw
        if isinstance(raw, list):
            raw = {"facts": raw}
        if not isinstance(raw, dict) or "facts" not in raw:
            return None
        claimed = raw.get("facts")
        if not isinstance(claimed, list):
            return None
        try:
            payload = FactExtractionPayload.model_validate(raw)
        except ValueError:
            return None
        if claimed and not payload.facts:
            return None
        return payload

    def build_extraction_payload(self, episode: Any) -> dict[str, Any] | None:
        """Raw source turns (+ optional summary) + the existing facts to compare."""

        turns = self.episodes.episode_turns(episode.id)
        if not turns:
            return None
        admitted: list[dict[str, str]] = []
        used = estimate_text_tokens(FACT_EXTRACTION_INTRO)
        budget = self.input_max_tokens
        for turn in turns:
            text = self.episodes.resolve_turn_text(turn)
            if not text:
                continue
            cost = estimate_text_tokens(text)
            if admitted and used + cost > budget:
                break
            admitted.append(
                {
                    "turn_id": turn.turn_id,
                    "speaker": self.episodes.turn_speaker_label(turn),
                    "source_kind": turn.source_kind,
                    "text": text,
                }
            )
            used += cost
        if not admitted:
            return None
        existing = self._existing_fact_context(episode)
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
            "existing_facts": existing,
            "estimated_tokens": used,
        }
        summary = episode.structured_summary()
        if summary and not summary.is_empty:
            payload["episode_summary"] = {
                "title": summary.title,
                "summary": summary.summary,
                "user_stated": summary.user_stated,
                "persona_stated": summary.persona_stated,
                "inferred_context": summary.inferred_context,
            }
        return payload

    def _existing_fact_context(self, episode: Any) -> list[dict[str, Any]]:
        """Active facts in this scope, so the model can classify relations.

        Bounded on purpose: this list is prompt input, and it is the only part
        of the payload that grows with history.
        """

        rows = self.database.conn.execute(
            """
            SELECT id, category, subject, predicate, display_text, value_json, origin
            FROM memory_semantic_facts
            WHERE persona_id = ? AND counterpart_id = ? AND branch_id = ? AND status = 'active'
            ORDER BY COALESCE(last_confirmed_at, updated_at) DESC
            LIMIT ?
            """,
            (
                episode.persona_id,
                episode.counterpart_id,
                episode.branch_id,
                self.max_existing_context,
            ),
        ).fetchall()
        return [
            {
                "fact_id": str(row["id"]),
                "category": str(row["category"]),
                "subject": str(row["subject"]),
                "predicate": str(row["predicate"]),
                "value": str(dict(loads(row["value_json"])).get("text") or ""),
                "display_text": str(row["display_text"] or ""),
                "origin": str(row["origin"]),
            }
            for row in rows
        ]

    # -- consolidation (deterministic application of candidates) -----------

    def apply_candidates(self, episode: Any, payload: FactExtractionPayload) -> dict[str, int]:
        counts = {
            "facts_created": 0,
            "facts_reinforced": 0,
            "facts_superseded": 0,
            "facts_compatible": 0,
            "facts_conflict_pending": 0,
            "facts_replayed": 0,
            "skipped_inferred": 0,
            "skipped_invalid": 0,
        }
        observed_at = episode.started_at or datetime.now(UTC)
        for candidate in payload.usable():
            action = self._apply_candidate(
                episode, candidate, observed_at=observed_at
            )
            if action in counts:
                counts[action] += 1
        self.database.conn.commit()
        return counts

    def _apply_candidate(
        self, episode: Any, candidate: FactCandidate, *, observed_at: datetime
    ) -> str:
        if candidate.origin is FactOrigin.INFERRED and not self.persist_inferred:
            # The persona's reading is not the user's history.  Recorded in the
            # report, never silently dropped.
            return "skipped_inferred"

        scope = (
            episode.persona_id,
            episode.counterpart_id,
            episode.branch_id,
        )
        sources = self._candidate_sources(episode, candidate)
        known = self._find_fact(
            scope, candidate.fact_key, candidate.value_key, statuses=None
        )
        if known is not None and self._has_source(known.id, sources):
            # This episode already produced this fact: a replay, not new
            # evidence.  Also prevents a re-run from flipping a supersession
            # back and forth.
            self._touch_confirmed(known.id, observed_at)
            return "facts_replayed"

        if known is not None and known.status is FactStatus.ACTIVE:
            return self._reinforce(known, candidate, sources, observed_at)

        active_same_slot = self._find_active_by_slot(scope, candidate.fact_key)
        # A temporary state must never deactivate a DURABLE fact.  "Durable"
        # includes an unknown durability: not knowing is not a licence to
        # invalidate.  When the slot holds only another temporary state, that
        # state is exactly what a new observation is allowed to replace --
        # otherwise "不是周五，是周六出发" could never correct itself, because
        # both rows are passing states (found by the Phase 4.1 real-model run).
        protected = [
            item
            for item in active_same_slot
            if item.durability is not FactDurability.TEMPORARY
        ]
        temporary_conflict = (
            candidate.durability is FactDurability.TEMPORARY and bool(protected)
        )
        relation = candidate.relation
        if temporary_conflict:
            relation = FactRelation.UNRELATED
        want_supersede = relation is FactRelation.SUPERSEDES or (
            relation is FactRelation.CONTRADICTS and candidate.exclusive
        )
        if temporary_conflict or (
            relation is FactRelation.COMPATIBLE and candidate.exclusive
        ):
            want_supersede = False
            relation = FactRelation.CONTRADICTS

        if not active_same_slot:
            cited = self._cited_cross_slot(scope, candidate)
            if cited is not None:
                # A slot is an internal identity device; the model's explicit
                # citation is the semantic claim.  "现在基本只喝美式" lands in
                # "当前饮品偏好" while it ends "最喜欢饮品 = 茉莉奶绿" -- without
                # this the persona would keep believing the value the user just
                # said they were tired of (found by the Phase 4.1 real-model
                # run).  The guards below keep it from becoming a licence to
                # invalidate anything the model names.
                new_fact = self._create_fact(
                    episode, candidate, sources, observed_at=observed_at,
                    status=FactStatus.ACTIVE, supersedes_fact_id=cited.id,
                )
                self._apply_supersession(cited, new_fact)
                return "facts_superseded"
            self._create_fact(
                episode, candidate, sources, observed_at=observed_at,
                status=FactStatus.ACTIVE,
            )
            return "facts_created"

        related = self._resolve_related(active_same_slot, candidate)
        # ...and a passing state must not BLOCK a durable fact either: a real
        # preference arriving after a momentary mood in the same exclusive slot
        # replaces it, with the mood kept as history.
        temporary_active = [
            item for item in active_same_slot if item.durability is FactDurability.TEMPORARY
        ]
        if (
            temporary_active
            and candidate.durability is not FactDurability.TEMPORARY
            and candidate.exclusive
            and relation is not FactRelation.COMPATIBLE
        ):
            want_supersede = True
            related = temporary_active[0]
        if want_supersede and related is not None:
            new_fact = self._create_fact(
                episode, candidate, sources, observed_at=observed_at,
                status=FactStatus.ACTIVE, supersedes_fact_id=related.id,
            )
            self._apply_supersession(related, new_fact)
            return "facts_superseded"
        if relation is FactRelation.COMPATIBLE:
            self._create_fact(
                episode, candidate, sources, observed_at=observed_at,
                status=FactStatus.ACTIVE,
            )
            return "facts_compatible"
        # Unclassified disagreement: keep the new claim, do NOT touch the old
        # one, and make the conflict explicit.
        self._create_fact(
            episode, candidate, sources, observed_at=observed_at,
            status=FactStatus.CANDIDATE,
            conflict_with=related.id if related else None,
        )
        return "facts_conflict_pending"

    def _cited_cross_slot(
        self, scope: tuple[str, str, str], candidate: FactCandidate
    ) -> SemanticFact | None:
        """A fact the model explicitly said this one replaces, in another slot.

        Only an ACTIVE fact in the SAME scope qualifies, the claim must be
        ``supersedes`` (or an exclusive ``contradicts``), and a passing state is
        still never allowed to end a durable one.  Everything else keeps the
        existing behaviour: store the new claim and leave the old value alone.
        """

        cited_id = str(candidate.related_fact_id or "").strip()
        if not cited_id:
            return None
        explicit_replacement = candidate.relation is FactRelation.SUPERSEDES or (
            candidate.relation is FactRelation.CONTRADICTS and candidate.exclusive
        )
        if not explicit_replacement:
            return None
        cited = self.get_fact(cited_id)
        if cited is None or not cited.is_active:
            return None
        if (cited.persona_id, cited.counterpart_id, cited.branch_id) != scope:
            return None
        if (
            candidate.durability is FactDurability.TEMPORARY
            and cited.durability is not FactDurability.TEMPORARY
        ):
            return None
        return cited

    def _candidate_sources(
        self, episode: Any, candidate: FactCandidate
    ) -> list[dict[str, Any]]:
        """Evidence rows for one candidate: the cited turn, else the Episode."""

        turn_id = str(candidate.evidence_turn_id or "").strip()
        if turn_id and not self.episodes.episode_has_source(
            turn_id, episode_id=episode.id
        ):
            # A turn the model invented or that belongs to another Episode is
            # not evidence -- fall back to the Episode itself.
            turn_id = ""
        return [
            {
                "source_type": "episode",
                "episode_id": episode.id,
                "turn_id": turn_id,
                "session_id": episode.session_id,
                "room_id": episode.room_id,
                "evidence_role": candidate.evidence_role.value,
            }
        ]

    def _create_fact(
        self,
        episode: Any,
        candidate: FactCandidate,
        sources: list[dict[str, Any]],
        *,
        observed_at: datetime,
        status: FactStatus = FactStatus.ACTIVE,
        supersedes_fact_id: str | None = None,
        conflict_with: str | None = None,
    ) -> SemanticFact:
        now = datetime.now(UTC)
        valid_until = None
        if candidate.durability is FactDurability.TEMPORARY:
            valid_until = observed_at + timedelta(hours=self.temporary_hours)
        metadata: dict[str, Any] = {
            "extraction_version": FACT_EXTRACTION_VERSION,
            "source_episode_id": episode.id,
        }
        if conflict_with:
            metadata["conflict_with_fact_id"] = conflict_with
            metadata["unresolved_conflict"] = True
        if candidate.notes:
            metadata["extractor_note"] = candidate.notes[:400]
        if candidate.durability is FactDurability.TEMPORARY:
            metadata["durability"] = FactDurability.TEMPORARY.value
            metadata["temporary_hours"] = self.temporary_hours
        fact = SemanticFact(
            id=new_id("fact"),
            persona_id=episode.persona_id,
            counterpart_id=episode.counterpart_id,
            branch_id=episode.branch_id,
            category=candidate.category,
            fact_key=candidate.fact_key,
            value_key=candidate.value_key,
            subject=candidate.subject,
            predicate=candidate.predicate,
            value_json=candidate.value_json,
            display_text=candidate.display(),
            status=status,
            origin=candidate.origin,
            durability=candidate.durability,
            plan_status=candidate.plan_status,
            confidence=candidate.confidence,
            evidence_count=0,
            last_confirmed_at=observed_at,
            valid_from=observed_at,
            valid_until=valid_until,
            observed_at=observed_at,
            temporal_expression=candidate.temporal_expression,
            temporal_normalized=candidate.temporal_normalized,
            temporal_confidence=candidate.temporal_confidence,
            supersedes_fact_id=supersedes_fact_id,
            extraction_version=FACT_EXTRACTION_VERSION,
            visibility="room_public" if episode.room_id else "private_session",
            material_scope=CHARACTER_VISIBLE,
            created_at=now,
            updated_at=now,
            metadata=metadata,
        )
        self._insert_fact(fact)
        for source in sources:
            self._insert_source(fact.id, source)
        self._refresh_evidence(fact.id, observed_at=observed_at)
        return fact

    def _reinforce(
        self,
        fact: SemanticFact,
        candidate: FactCandidate,
        sources: list[dict[str, Any]],
        observed_at: datetime,
    ) -> str:
        """The same fact, said again: stronger, not duplicated."""

        inserted = 0
        for source in sources:
            inserted += self._insert_source(fact.id, source)
        confidence = min(
            0.99, max(fact.confidence, candidate.confidence) + self.confidence_step
        )
        metadata = dict(fact.metadata)
        metadata["reconfirmation_count"] = int(metadata.get("reconfirmation_count") or 0) + (
            1 if inserted else 0
        )
        plan_status = fact.plan_status
        if (
            candidate.plan_status is not PlanStatus.NOT_APPLICABLE
            and candidate.plan_status is not fact.plan_status
        ):
            # A plan moving PLANNED -> COMPLETED is a state change, not a new
            # value: the row keeps its identity and its history.
            plan_status = candidate.plan_status
            metadata["plan_status_updated_at"] = observed_at.isoformat()
        origin = _stronger_origin(fact.origin, candidate.origin)
        self.database.conn.execute(
            """
            UPDATE memory_semantic_facts
            SET confidence = ?, origin = ?, plan_status = ?, metadata_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                confidence,
                origin.value,
                plan_status.value,
                dumps(metadata),
                datetime.now(UTC).isoformat(),
                fact.id,
            ),
        )
        self._refresh_evidence(fact.id, observed_at=observed_at)
        return "facts_reinforced" if inserted or fact.evidence_count == 0 else "facts_replayed"

    def _apply_supersession(self, old: SemanticFact, new: SemanticFact) -> None:
        """Close the old fact at the new fact's start; keep both rows forever."""

        metadata = dict(old.metadata)
        metadata["superseded_by_fact_id"] = new.id
        metadata["superseded_at"] = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            """
            UPDATE memory_semantic_facts
            SET status = ?, valid_until = ?, superseded_by_fact_id = ?,
                metadata_json = ?, updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (
                FactStatus.SUPERSEDED.value,
                dt(new.valid_from or new.created_at),
                new.id,
                dumps(metadata),
                datetime.now(UTC).isoformat(),
                old.id,
            ),
        )

    def _touch_confirmed(self, fact_id: str, observed_at: datetime) -> None:
        self.database.conn.execute(
            "UPDATE memory_semantic_facts SET last_confirmed_at = COALESCE(last_confirmed_at, ?) "
            "WHERE id = ?",
            (dt(observed_at), fact_id),
        )

    def _insert_fact(self, fact: SemanticFact) -> None:
        columns = (
            "id", "persona_id", "counterpart_id", "branch_id", "category", "fact_key",
            "value_key", "subject", "predicate", "value_json", "display_text", "status",
            "origin", "durability", "plan_status", "confidence", "evidence_count",
            "last_confirmed_at", "valid_from", "valid_until", "observed_at",
            "temporal_expression", "temporal_normalized", "temporal_confidence",
            "superseded_by_fact_id", "supersedes_fact_id", "extraction_version",
            "visibility", "material_scope", "created_at", "updated_at", "metadata_json",
        )
        values = (
            fact.id, fact.persona_id, fact.counterpart_id, fact.branch_id,
            fact.category.value, fact.fact_key, fact.value_key, fact.subject,
            fact.predicate, dumps(fact.value_json), fact.display_text, fact.status.value,
            fact.origin.value, fact.durability.value, fact.plan_status.value,
            fact.confidence, fact.evidence_count, dt(fact.last_confirmed_at),
            dt(fact.valid_from), dt(fact.valid_until), dt(fact.observed_at),
            fact.temporal_expression, fact.temporal_normalized, fact.temporal_confidence,
            fact.superseded_by_fact_id, fact.supersedes_fact_id, fact.extraction_version,
            fact.visibility, fact.material_scope, dt(fact.created_at), dt(fact.updated_at),
            dumps(fact.metadata),
        )
        self.database.conn.execute(
            f"INSERT INTO memory_semantic_facts ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )

    def _insert_source(self, fact_id: str, source: dict[str, Any]) -> int:
        """Idempotent evidence insert.  Returns 1 when a new row appeared."""

        cursor = self.database.conn.execute(
            """
            INSERT OR IGNORE INTO memory_fact_sources (
              fact_id, source_type, episode_id, turn_id, session_id, room_id,
              evidence_role, excerpt_available, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                fact_id,
                str(source.get("source_type") or "episode"),
                str(source.get("episode_id") or ""),
                str(source.get("turn_id") or ""),
                source.get("session_id"),
                source.get("room_id"),
                str(source.get("evidence_role") or "supporting"),
                1,
                datetime.now(UTC).isoformat(),
            ),
        )
        return int(cursor.rowcount or 0)

    def _refresh_evidence(self, fact_id: str, *, observed_at: datetime) -> None:
        """Recompute evidence from source rows: no counters to drift."""

        row = self.database.conn.execute(
            """
            SELECT COUNT(DISTINCT CASE WHEN episode_id != '' THEN episode_id ELSE turn_id END) AS c
            FROM memory_fact_sources WHERE fact_id = ?
            """,
            (fact_id,),
        ).fetchone()
        stamp = dt(observed_at)
        self.database.conn.execute(
            """
            UPDATE memory_semantic_facts
            SET evidence_count = ?,
                last_confirmed_at = CASE
                  WHEN last_confirmed_at IS NULL OR last_confirmed_at < ? THEN ?
                  ELSE last_confirmed_at END
            WHERE id = ?
            """,
            (int(row["c"]), stamp, stamp, fact_id),
        )

    # -- lookups -----------------------------------------------------------

    def _find_fact(
        self,
        scope: tuple[str, str, str],
        fact_key: str,
        value_key: str,
        *,
        statuses: tuple[FactStatus, ...] | None = None,
    ) -> SemanticFact | None:
        clause = ""
        params: list[Any] = [*scope, fact_key, value_key]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clause = f" AND status IN ({placeholders})"
            params.extend(status.value for status in statuses)
        row = self.database.conn.execute(
            "SELECT * FROM memory_semantic_facts "
            "WHERE persona_id = ? AND counterpart_id = ? AND branch_id = ? "
            f"AND fact_key = ? AND value_key = ?{clause} "
            "ORDER BY (status = 'active') DESC, updated_at DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        return self._row_to_fact(row) if row else None

    def _find_active_by_slot(
        self, scope: tuple[str, str, str], fact_key: str
    ) -> list[SemanticFact]:
        rows = self.database.conn.execute(
            "SELECT * FROM memory_semantic_facts "
            "WHERE persona_id = ? AND counterpart_id = ? AND branch_id = ? "
            "AND fact_key = ? AND status = 'active' ORDER BY updated_at DESC",
            (*scope, fact_key),
        ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def _resolve_related(
        self, active_same_slot: list[SemanticFact], candidate: FactCandidate
    ) -> SemanticFact | None:
        if candidate.related_fact_id:
            for fact in active_same_slot:
                if fact.id == candidate.related_fact_id:
                    return fact
        return active_same_slot[0] if active_same_slot else None

    def _has_source(self, fact_id: str, sources: list[dict[str, Any]]) -> bool:
        for source in sources:
            row = self.database.conn.execute(
                "SELECT 1 AS x FROM memory_fact_sources "
                "WHERE fact_id = ? AND source_type = ? AND episode_id = ? AND turn_id = ?",
                (
                    fact_id,
                    str(source.get("source_type") or "episode"),
                    str(source.get("episode_id") or ""),
                    str(source.get("turn_id") or ""),
                ),
            ).fetchone()
            if row is not None:
                return True
        return False

    # -- extraction bookkeeping -------------------------------------------

    def _mark_extraction(self, episode_id: str, *, status: str, error: str | None) -> None:
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            """
            UPDATE memory_episodes
            SET fact_extraction_status = ?, fact_extraction_error = ?,
                fact_extraction_attempts = COALESCE(fact_extraction_attempts, 0) + 1,
                facts_extracted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, error, now, now, episode_id),
        )
        self.database.conn.commit()

    def recover_pending(self, *, limit: int = 200) -> dict[str, Any]:
        """Startup sweep: Episodes that still owe fact extraction."""

        pending = self.pending_extraction_episodes(limit=limit)
        return {
            "pending": len(pending),
            "pending_episode_ids": [episode.id for episode in pending],
        }

    def pending_extraction_episodes(
        self,
        *,
        limit: int = 50,
        persona_id: str | None = None,
        room_id: str | None = None,
    ) -> list[Any]:
        """Episodes that still owe fact extraction.

        The status column is the durable work list: it survives a restart, and a
        failed attempt stays eligible for retry rather than disappearing.
        """

        clauses = [
            "turn_count > 0",
            "COALESCE(fact_extraction_status, 'pending') != 'ready'",
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

    # -- reading / inspection ---------------------------------------------

    def get_fact(self, fact_id: str) -> SemanticFact | None:
        row = self.database.conn.execute(
            "SELECT * FROM memory_semantic_facts WHERE id = ?", (fact_id,)
        ).fetchone()
        return self._row_to_fact(row) if row else None

    def list_facts(
        self,
        *,
        persona_id: str | None = None,
        counterpart_id: str | None = None,
        branch_id: str | None = None,
        status: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[SemanticFact]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("persona_id", persona_id),
            ("counterpart_id", counterpart_id),
            ("branch_id", branch_id),
            ("status", status),
            ("category", category),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        rows = self.database.conn.execute(
            f"SELECT * FROM memory_semantic_facts {where} "
            "ORDER BY COALESCE(valid_from, created_at) DESC, created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def fact_sources(self, fact_id: str) -> list[dict[str, Any]]:
        rows = self.database.conn.execute(
            "SELECT * FROM memory_fact_sources WHERE fact_id = ? ORDER BY created_at",
            (fact_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def inspect_fact(self, fact_id: str, *, include_sources: bool = True) -> dict[str, Any] | None:
        """Debug view: the fact, its evidence chain, and whether it still resolves."""

        fact = self.get_fact(fact_id)
        if fact is None:
            return None
        payload: dict[str, Any] = {
            "fact": fact.model_dump(mode="json"),
            "superseded_by": fact.superseded_by_fact_id,
            "supersedes": fact.supersedes_fact_id,
            "valid_now": fact.is_valid_now,
        }
        if include_sources:
            sources: list[dict[str, Any]] = []
            for row in self.fact_sources(fact_id):
                episode_id = str(row.get("episode_id") or "")
                turn_id = str(row.get("turn_id") or "")
                episode = self.episodes.get_episode(episode_id) if episode_id else None
                resolvable = False
                if turn_id:
                    for turn in self.episodes.episode_turns(episode_id) if episode else []:
                        if turn.turn_id == turn_id:
                            resolvable = bool(self.episodes.resolve_turn_text(turn))
                            break
                elif episode is not None:
                    resolvable = any(
                        self.episodes.resolve_turn_text(turn)
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
                        "episode_available": episode is not None,
                        "resolvable": resolvable,
                    }
                )
            payload["sources"] = sources
            payload["source_episode_ids"] = sorted(
                {item["episode_id"] for item in sources if item["episode_id"]}
            )
            payload["source_turn_ids"] = sorted(
                {item["turn_id"] for item in sources if item["turn_id"]}
            )
        return payload

    def stats(
        self,
        *,
        persona_id: str | None = None,
        counterpart_id: str | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Fact-store statistics (also what the Context Assembly Report reads)."""

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
            f"SELECT status, category, COUNT(*) AS c FROM memory_semantic_facts {where} "
            "GROUP BY status, category",
            tuple(params),
        ).fetchall()
        by_status: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for row in rows:
            status = str(row["status"])
            category = str(row["category"])
            count = int(row["c"])
            by_status[status] = by_status.get(status, 0) + count
            by_category[category] = by_category.get(category, 0) + count
        return {
            "facts": sum(by_status.values()),
            "active": by_status.get("active", 0),
            "superseded": by_status.get("superseded", 0),
            "candidate": by_status.get("candidate", 0),
            "retracted": by_status.get("retracted", 0),
            "expired": by_status.get("expired", 0),
            "by_category": by_category,
        }

    def count_active(self, *, persona_id: str | None = None) -> int:
        if persona_id:
            row = self.database.conn.execute(
                "SELECT COUNT(*) AS c FROM memory_semantic_facts "
                "WHERE persona_id = ? AND status = 'active'",
                (persona_id,),
            ).fetchone()
        else:
            row = self.database.conn.execute(
                "SELECT COUNT(*) AS c FROM memory_semantic_facts WHERE status = 'active'"
            ).fetchone()
        return int(row["c"])

    @staticmethod
    def _row_to_fact(row: Any) -> SemanticFact:
        def _enum(enum_cls: Any, value: Any, fallback: Any) -> Any:
            try:
                return enum_cls(str(value))
            except ValueError:
                return fallback

        return SemanticFact(
            id=str(row["id"]),
            persona_id=str(row["persona_id"]),
            counterpart_id=str(row["counterpart_id"]),
            branch_id=str(row["branch_id"]),
            category=_enum(FactCategory, row["category"], FactCategory.OTHER),
            fact_key=str(row["fact_key"] or ""),
            value_key=str(row["value_key"] or ""),
            subject=str(row["subject"] or ""),
            predicate=str(row["predicate"] or ""),
            value_json=dict(loads(row["value_json"])),
            display_text=str(row["display_text"] or ""),
            status=_enum(FactStatus, row["status"], FactStatus.CANDIDATE),
            origin=_enum(FactOrigin, row["origin"], FactOrigin.INFERRED),
            durability=_enum(FactDurability, row["durability"], FactDurability.UNKNOWN),
            plan_status=_enum(PlanStatus, row["plan_status"], PlanStatus.NOT_APPLICABLE),
            confidence=float(row["confidence"] or 0.5),
            evidence_count=int(row["evidence_count"] or 0),
            last_confirmed_at=parse_dt(row["last_confirmed_at"]),
            valid_from=parse_dt(row["valid_from"]),
            valid_until=parse_dt(row["valid_until"]),
            observed_at=parse_dt(row["observed_at"]),
            temporal_expression=row["temporal_expression"],
            temporal_normalized=row["temporal_normalized"],
            temporal_confidence=float(row["temporal_confidence"] or 0.0),
            superseded_by_fact_id=row["superseded_by_fact_id"],
            supersedes_fact_id=row["supersedes_fact_id"],
            extraction_version=int(row["extraction_version"] or 1),
            visibility=str(row["visibility"] or "private_session"),
            material_scope=str(row["material_scope"] or CHARACTER_VISIBLE),
            created_at=parse_dt(row["created_at"]) or datetime.now(UTC),
            updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
            metadata=dict(loads(row["metadata_json"])),
        )


#: Stronger origins win when the same fact is asserted again by a better source.
_ORIGIN_STRENGTH = {
    FactOrigin.INFERRED: 0,
    FactOrigin.PERSONA_ASSERTED: 1,
    FactOrigin.SYSTEM_OBSERVED: 2,
    FactOrigin.USER_ASSERTED: 3,
}


def _stronger_origin(current: FactOrigin, incoming: FactOrigin) -> FactOrigin:
    return incoming if _ORIGIN_STRENGTH[incoming] > _ORIGIN_STRENGTH[current] else current


__all__ = [
    "FACT_EXTRACTION_INTRO",
    "FACT_EXTRACTION_SCHEMA",
    "SemanticFactService",
    "canonical_fact_key",
    "canonical_value_key",
]
