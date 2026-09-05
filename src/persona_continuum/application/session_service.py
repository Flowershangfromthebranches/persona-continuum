from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from persona_continuum.application._utils import dumps, loads, new_id, parse_dt
from persona_continuum.application.compiled_context_service import CompiledPersonaContextService
from persona_continuum.application.memory_service import MemoryService
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.application.state_appraisal import (
    AppraisalLimits,
    AppraisalRequest,
    AppraisalResult,
    PersonaStateAppraisalService,
)
from persona_continuum.config import Config
from persona_continuum.domain.affect import EMOTION_NAMES, NEED_NAMES
from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.provenance import (
    NON_CHARACTER_SCOPES,
    PersonaRetrievalPolicy,
    normalise_material_scope,
)
from persona_continuum.domain.session import PreparedTurn, SessionRecord
from persona_continuum.runtime.affect_engine import AffectEngine
from persona_continuum.runtime.motivation_engine import MotivationEngine
from persona_continuum.runtime.relationship_engine import RELATIONSHIP_FIELDS, RelationshipEngine
from persona_continuum.security.validation import CodedError
from persona_continuum.storage.database import Database

EVALUATION_CONTEXT_TAGS = frozenset(
    {
        "evaluation",
        "evaluation_only",
        "expected_answer",
        "test_fixture",
        "验收题",
        "验收问题",
    }
)


def _validate_numeric_map(
    value: dict[str, float],
    *,
    allowed_keys: set[str],
    field_name: str,
    minimum: float,
    maximum: float,
) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key not in allowed_keys:
            raise ValueError(f"{field_name}_unknown_key:{key}")
        if not isinstance(raw_value, int | float):
            raise ValueError(f"{field_name}_non_numeric:{key}")
        numeric = float(raw_value)
        if numeric < minimum or numeric > maximum:
            raise ValueError(f"{field_name}_out_of_range:{key}")
        normalized[key] = numeric
    return normalized


class ReflectionInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    importance: float = Field(default=0.65, ge=0, le=1)


class ReflectionRelationshipDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterpart_id: str = Field(min_length=1)
    changes: dict[str, float] = Field(min_length=1)
    reason: str | None = None

    @field_validator("changes")
    @classmethod
    def _validate_changes(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_numeric_map(
            value,
            allowed_keys=RELATIONSHIP_FIELDS,
            field_name="relationship_delta",
            minimum=0,
            maximum=1,
        )


class ReflectionGoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str = Field(min_length=1)
    status: Literal["active", "completed", "cancelled", "inactive", "paused"]
    content: str | None = None


class ReflectionConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    content: str = Field(min_length=1)
    severity: float = Field(default=0.65, ge=0, le=1)


class ReflectionMemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    importance: float = Field(default=0.65, ge=0, le=1)


class ReflectionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reflection_artifact_id: str = Field(min_length=1)
    new_insights: list[ReflectionInsight]
    relationship_deltas: list[ReflectionRelationshipDelta]
    affect_deltas: dict[str, float]
    need_deltas: dict[str, float]
    goal_updates: list[ReflectionGoalUpdate]
    unresolved_conflicts: list[ReflectionConflict]
    self_narrative_updates: list[str]
    memory_candidates: list[ReflectionMemoryCandidate]
    confidence: float = Field(ge=0, le=1)
    supporting_turn_ids: list[str] = Field(min_length=1)

    @field_validator("affect_deltas")
    @classmethod
    def _validate_affect_deltas(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_numeric_map(
            value,
            allowed_keys=set(EMOTION_NAMES),
            field_name="affect_delta",
            minimum=0,
            maximum=1,
        )

    @field_validator("need_deltas")
    @classmethod
    def _validate_need_deltas(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_numeric_map(
            value,
            allowed_keys=set(NEED_NAMES),
            field_name="need_delta",
            minimum=-1,
            maximum=1,
        )

    @field_validator("supporting_turn_ids")
    @classmethod
    def _validate_unique_turns(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("supporting_turn_ids_not_unique")
        return value


class SessionService:
    def __init__(
        self,
        database: Database,
        personas: PersonaService,
        memories: MemoryService,
        compiled_context: CompiledPersonaContextService,
        affect: AffectEngine,
        motivation: MotivationEngine,
        relationships: RelationshipEngine,
        config: Config | None = None,
    ) -> None:
        self.database = database
        self.personas = personas
        self.memories = memories
        self.compiled_context = compiled_context
        self.affect = affect
        self.motivation = motivation
        self.relationships = relationships
        self.config = config or Config()
        self.state_appraisal = PersonaStateAppraisalService(
            mode=self.config.persona_state_appraisal_mode,
            limits=AppraisalLimits(
                affect=self.config.persona_state_max_affect_delta,
                need=self.config.persona_state_max_need_delta,
                relationship=self.config.persona_state_max_relationship_delta,
            ),
        )

    def start_session(
        self,
        persona_id: str,
        title: str | None = None,
        *,
        branch_id: str | None = None,
        counterpart_id: str = "user",
        session_type: str = "private_session",
        room_id: str | None = None,
    ) -> SessionRecord:
        self.personas.get(persona_id)
        metadata = {
            "session_type": session_type,
            "counterpart_id": counterpart_id,
        }
        if branch_id:
            self._validate_branch(persona_id, branch_id)
            metadata["branch_id"] = branch_id
        if room_id:
            metadata["room_id"] = room_id
        record = SessionRecord(
            id=new_id("sess"), persona_id=persona_id, title=title, metadata=metadata
        )
        self.database.conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                persona_id,
                title,
                record.status,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
                dumps(record.metadata),
            ),
        )
        self.database.conn.commit()
        return record

    def prepare_turn(
        self,
        persona_id: str,
        session_id: str,
        user_message: str,
        current_time: datetime | None = None,
        external_events: list[dict[str, Any]] | None = None,
        max_context_items: int = 8,
        max_context_size: int | None = None,
        counterpart_id: str = "user",
        branch_id: str | None = None,
        preset_memories: list[Any] | None = None,
    ) -> PreparedTurn:
        session = self._require_session(persona_id, session_id, allow_status={"active"})
        persona = self.personas.get(persona_id)
        self._require_counterpart(session, counterpart_id)
        effective_branch_id = self._effective_branch_id(persona_id, session, branch_id)
        query = self._query_from_message(user_message)
        if preset_memories is not None:
            # A room turn retrieves memories exactly once (Recall Gate) and
            # forwards that single result here instead of repeating the search.
            memories = list(preset_memories)[:max_context_items]
        else:
            memories = self.memories.search_memories(
                persona_id,
                query,
                limit=max_context_items,
                branch_id=effective_branch_id,
                include_main_history=True,
                include_shared_pre_divergence=True,
            )
        if max_context_size is not None:
            memories = self._fit_memories(memories, max_context_size)
        compiled_context = self.compiled_context.prepare_context(
            persona_id,
            user_message,
            max_items=max_context_items,
            max_context_size=max_context_size,
            branch_id=effective_branch_id,
        )
        session.metadata["branch_id"] = effective_branch_id
        session.metadata.setdefault("counterpart_id", counterpart_id)
        session.metadata["persona_runtime_version"] = compiled_context.get("runtime_version", {})
        if external_events:
            # Stage for the commit appraisal: see _drain_pending_events.
            staged = list(session.metadata.get("pending_external_events") or [])
            staged.extend(event for event in external_events if isinstance(event, dict))
            session.metadata["pending_external_events"] = staged
        self._save_session_metadata(session)
        self._ensure_runtime_state(persona_id, effective_branch_id)
        compiled_by_key = dict(compiled_context.get("by_key", {}))
        emotional = [
            memory
            for memory in memories
            if memory.type in {MemoryType.EMOTIONAL, MemoryType.EPISODIC}
        ]
        relationship = self.relationships.get_relationship(
            persona_id, counterpart_id, branch_id=effective_branch_id
        )
        current_emotions = self.affect.get_emotions(
            persona_id, effective_branch_id, now=current_time
        )
        current_needs = self.motivation.get_needs(persona_id, effective_branch_id)
        appraisal = self._appraise(user_message, external_events or [])
        persona_type = persona.manifest.persona_type
        policy = PersonaRetrievalPolicy(
            persona_type=persona_type, branch_id=effective_branch_id
        )
        visible = [
            memory
            for memory in memories
            if normalise_material_scope(
                (memory.metadata or {}).get("material_scope")
            )
            not in NON_CHARACTER_SCOPES
            and policy.allows(memory.source_kind)
        ]
        relevant_facts = [memory.content for memory in visible]
        relevant_facts.extend(
            self._relevant_claim_contents(
                persona_id,
                query,
                policy=policy,
                limit=min(4, max(0, max_context_items - len(relevant_facts))),
            )
        )
        relevant_facts = list(dict.fromkeys(relevant_facts))[:max_context_items]
        return PreparedTurn(
            persona_id=persona_id,
            session_id=session_id,
            identity_anchor=persona.manifest,
            current_run_mode=persona.manifest.run_mode.value,
            relevant_persona_facts=relevant_facts,
            relevant_historical_facts=[
                memory.content
                for memory in visible
                if memory.source_kind.startswith("historical")
            ],
            relevant_memories=visible,
            activated_emotional_memories=emotional,
            current_emotions=current_emotions,
            current_mood={
                state.name: round(state.intensity, 3)
                for state in current_emotions
                if state.intensity >= 0.2
            },
            current_needs=current_needs,
            active_goals=list(compiled_context.get("active_goals", [])),
            relationship_state=relationship,
            mental_models=self._context_list(compiled_by_key, "mental_models")
            or self._claim_contents(persona_id, "mental", limit=3),
            decision_patterns=self._context_list(compiled_by_key, "decision_heuristics")
            or self._claim_contents(persona_id, "decision", limit=3),
            contradictions=self._context_list(compiled_by_key, "contradictions")
            or self._claim_contents(persona_id, "contradiction", limit=3),
            event_appraisal=appraisal,
            expression_intent=str(compiled_by_key.get("expression_style") or ""),
            expression_parameters=self._context_dict(compiled_by_key, "expression_style"),
            compiled_persona_context=compiled_context,
            uncertainty=(
                "Respect fact boundaries: do not turn inference, simulation, or "
                "user correction into historical certainty."
            ),
            suggested_memory_candidates=[],
        )

    def commit_turn(
        self,
        persona_id: str,
        session_id: str,
        *,
        user_message: str,
        persona_response: str,
        used_memory_ids: list[str] | None = None,
        user_feedback: str | None = None,
        goal_completed: bool | None = None,
        state_patch: dict[str, Any] | None = None,
        counterpart_id: str = "user",
        used_claim_ids: list[str] | None = None,
        used_memory_ids_extra: list[str] | None = None,
    ) -> dict[str, Any]:
        session = self._require_session(persona_id, session_id, allow_status={"active"})
        self._require_counterpart(session, counterpart_id)
        branch_id = self._effective_branch_id(persona_id, session, None)
        normalized_state_patch = self._validate_state_patch(state_patch or {})
        used_memory_ids = used_memory_ids or []
        if used_memory_ids_extra:
            used_memory_ids.extend(used_memory_ids_extra)
        turn_id = new_id("turn")
        try:
            self.database.conn.execute("BEGIN")
            self.database.conn.execute(
                "INSERT INTO session_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    session_id,
                    persona_id,
                    user_message,
                    persona_response,
                    dumps(used_memory_ids),
                    user_feedback,
                    dumps(
                        {
                            "goal_completed": goal_completed,
                            "state_patch": normalized_state_patch,
                            "counterpart_id": counterpart_id,
                            "used_claim_ids": used_claim_ids or [],
                            "branch_id": branch_id,
                        }
                    ),
                    datetime.now(UTC).isoformat(),
                ),
            )
            memory = self.memories.add_memory(
                persona_id,
                content=f"User asked: {user_message}\nPersona answered: {persona_response}",
                memory_type=MemoryType.DIGITAL_EXPERIENCE,
                importance=0.65 if user_feedback else 0.5,
                source_kind="digital_experience",
                participants=[counterpart_id],
                branch_id=branch_id,
                metadata={
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "counterpart_id": counterpart_id,
                    "branch_id": branch_id,
                    "visibility": "room_public"
                    if counterpart_id.startswith("room:")
                    else "private_session",
                },
                commit=False,
            )
            self._insert_lineage(
                persona_id,
                child_type="memory",
                child_id=memory.id,
                parent_type="session_turn",
                parent_id=turn_id,
                relation="digital_experience_from",
            )
            appraisal = self._appraise_commit(
                persona_id=persona_id,
                branch_id=branch_id,
                counterpart_id=counterpart_id,
                user_message=user_message,
                persona_response=persona_response,
                user_feedback=user_feedback,
                goal_completed=bool(goal_completed),
                external_events=self._drain_pending_events(session),
            )
            if appraisal.affect:
                self._apply_affect_delta(
                    persona_id,
                    branch_id,
                    session_id,
                    turn_id,
                    appraisal.affect,
                    "commit_turn appraisal",
                )
            if appraisal.needs:
                self._apply_need_delta(
                    persona_id,
                    branch_id,
                    session_id,
                    turn_id,
                    appraisal.needs,
                    "commit_turn appraisal",
                )
            for entry in appraisal.relationships:
                self._apply_relationship_delta(
                    persona_id,
                    str(entry["counterpart_id"]),
                    dict(entry["changes"]),
                    branch_id,
                    session_id,
                    turn_id,
                    "commit_turn appraisal",
                )
            if normalized_state_patch:
                self._apply_state_patch(
                    persona_id, session_id, turn_id, branch_id, normalized_state_patch
                )
            session.metadata["branch_id"] = branch_id
            session.metadata.setdefault("counterpart_id", counterpart_id)
            self.database.conn.execute(
                "UPDATE sessions SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), dumps(session.metadata), session_id),
            )
            self.database.conn.commit()
        except Exception:
            self.database.conn.rollback()
            raise
        return {
            "turn_id": turn_id,
            "memory_id": memory.id,
            # Public delta summary for the UI (§20): what moved and by how
            # much, never why.  No chain-of-thought is ever exposed here.
            "state_summary": appraisal.summary,
        }

    def end_session(self, session_id: str) -> bool:
        self.database.conn.execute(
            "UPDATE sessions SET status = 'ended' WHERE id = ?", (session_id,)
        )
        self.database.conn.commit()
        return True

    def delete_session(
        self, persona_id: str, session_id: str, delete_derived_memories: bool = True
    ) -> bool:
        self._require_session(persona_id, session_id, allow_status={"active", "ended"})
        if delete_derived_memories:
            change_rows = self.database.conn.execute(
                """
                SELECT DISTINCT e.id, e.branch_id, e.event_type, e.target_id, e.data_json
                FROM change_events e
                LEFT JOIN change_event_supports s ON s.event_id = e.id
                WHERE e.persona_id = ?
                  AND (e.session_id = ? OR s.session_id = ?)
                """,
                (persona_id, session_id, session_id),
            ).fetchall()
            affected: dict[str, dict[str, set[str]]] = {}
            for row in change_rows:
                branch_id = str(row["branch_id"])
                branch = affected.setdefault(
                    branch_id, {"relationships": set(), "affects": set(), "needs": set()}
                )
                data = dict(loads(row["data_json"]))
                if str(row["event_type"]) == "relationship_delta":
                    branch["relationships"].add(str(row["target_id"]))
                if str(row["event_type"]) == "affect_delta":
                    branch["affects"].update(str(key) for key in self._event_delta(data))
                if str(row["event_type"]) == "need_delta":
                    branch["needs"].update(str(key) for key in self._event_delta(data))
            rows = self.database.conn.execute(
                """
                SELECT id, metadata_json FROM memories
                WHERE persona_id = ?
                  AND source_kind IN (
                    'digital_experience',
                    'reflection_summary',
                    'system_summary',
                    'relationship_update_event',
                    'unresolved_event'
                  )
                """,
                (persona_id,),
            ).fetchall()
            for row in rows:
                metadata = dict(loads(row["metadata_json"]))
                supporting_sessions = list(metadata.get("supporting_session_ids", []))
                should_delete = metadata.get("session_id") == session_id or (
                    session_id in supporting_sessions and len(supporting_sessions) <= 1
                )
                if should_delete:
                    self.database.conn.execute(
                        "DELETE FROM memories_fts WHERE persona_id = ? AND memory_id = ?",
                        (persona_id, row["id"]),
                    )
                    self.database.conn.execute(
                        "DELETE FROM memories WHERE persona_id = ? AND id = ?",
                        (persona_id, row["id"]),
                    )
                elif session_id in supporting_sessions:
                    metadata["supporting_session_ids"] = [
                        value for value in supporting_sessions if value != session_id
                    ]
                    metadata["supporting_turn_ids"] = [
                        value
                        for value in list(metadata.get("supporting_turn_ids", []))
                        if not self._turn_belongs_to_session(str(value), session_id)
                    ]
                    self.database.conn.execute(
                        "UPDATE memories SET metadata_json = ? WHERE persona_id = ? AND id = ?",
                        (dumps(metadata), persona_id, row["id"]),
                    )
            self.database.conn.execute(
                "DELETE FROM change_event_supports WHERE session_id = ?", (session_id,)
            )
            for row in change_rows:
                support_count = self.database.conn.execute(
                    "SELECT COUNT(*) AS count FROM change_event_supports WHERE event_id = ?",
                    (row["id"],),
                ).fetchone()["count"]
                if int(support_count) == 0:
                    self.database.conn.execute(
                        "DELETE FROM change_events WHERE id = ?", (row["id"],)
                    )
            for branch_id, values in affected.items():
                self._replay_runtime_state(
                    persona_id,
                    branch_id=branch_id,
                    affected_relationships=values["relationships"],
                    affected_affects=values["affects"],
                    affected_needs=values["needs"],
                )
        self.database.conn.execute(
            "DELETE FROM sessions WHERE persona_id = ? AND id = ?", (persona_id, session_id)
        )
        self.database.conn.commit()
        return True

    def reset_runtime_state(
        self,
        persona_id: str,
        *,
        branch_id: str = "main",
        include_memories: bool = False,
    ) -> dict[str, Any]:
        """Restore a persona's runtime state to its initial values.

        With ``include_memories=False`` (default) only the volatile runtime
        is reset: affect intensities back to baseline, needs back to
        baseline, relationships removed, plus the change-event log and the
        derived runtime_state.json cache for that branch.  Sessions, session
        turns, memories and compiled persona content are untouched, so the
        conversation history stays visible while the persona "cools down".

        With ``include_memories=True`` the branch's conversational footprint
        goes as well: sessions, session turns, derived memories (experience
        summaries, reflections, relationship events) and -- for the main
        branch -- room transcripts that belong to the persona.  Compiled
        persona content (sources, evidence, dimensions, versions) is never
        touched: this is a state reset, not a persona deletion.
        """

        self.personas.get(persona_id)
        removed: dict[str, int] = {}
        removed["affect_states"] = self._delete_count(
            "DELETE FROM affect_states WHERE persona_id = ? AND branch_id = ?",
            (persona_id, branch_id),
        )
        removed["needs"] = self._delete_count(
            "DELETE FROM needs WHERE persona_id = ? AND branch_id = ?",
            (persona_id, branch_id),
        )
        removed["relationships"] = self._delete_count(
            "DELETE FROM relationships WHERE persona_id = ? AND branch_id = ?",
            (persona_id, branch_id),
        )
        event_ids = [
            str(row["id"])
            for row in self.database.conn.execute(
                "SELECT id FROM change_events WHERE persona_id = ? AND branch_id = ?",
                (persona_id, branch_id),
            ).fetchall()
        ]
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            self.database.conn.execute(
                f"DELETE FROM change_event_supports WHERE event_id IN ({placeholders})",
                event_ids,
            )
            removed["change_event_supports"] = self.database.conn.total_changes
            self.database.conn.execute(
                f"DELETE FROM change_events WHERE id IN ({placeholders})",
                event_ids,
            )
        else:
            removed["change_event_supports"] = 0
        removed["change_events"] = len(event_ids)
        if include_memories:
            removed.update(self._reset_conversational_footprint(persona_id, branch_id))
        self.database.conn.commit()
        self._refresh_runtime_state(persona_id, branch_id)
        return {"persona_id": persona_id, "branch_id": branch_id, "removed": removed}

    def _delete_count(self, sql: str, params: tuple[object, ...]) -> int:
        before = self.database.conn.total_changes
        self.database.conn.execute(sql, params)
        return self.database.conn.total_changes - before

    def _reset_conversational_footprint(
        self, persona_id: str, branch_id: str
    ) -> dict[str, int]:
        """Delete sessions, turns and derived memories for one branch."""

        removed: dict[str, int] = {}
        session_ids = [
            str(row["id"])
            for row in self.database.conn.execute(
                "SELECT id FROM sessions WHERE persona_id = ?", (persona_id,)
            ).fetchall()
        ]
        branch_session_ids = (
            session_ids
            if branch_id == "main"
            else [
                session_id
                for session_id in session_ids
                if self._session_branch(persona_id, session_id) == branch_id
            ]
        )
        if branch_session_ids:
            placeholders = ",".join("?" for _ in branch_session_ids)
            params: tuple[object, ...] = (persona_id, *branch_session_ids)
            removed["session_turns"] = self._delete_count(
                "DELETE FROM session_turns "
                f"WHERE persona_id = ? AND session_id IN ({placeholders})",
                params,
            )
            removed["sessions"] = self._delete_count(
                "DELETE FROM sessions "
                f"WHERE persona_id = ? AND id IN ({placeholders})",
                params,
            )
        else:
            removed["session_turns"] = 0
            removed["sessions"] = 0
        derived_kinds = (
            "digital_experience",
            "reflection_summary",
            "system_summary",
            "relationship_update_event",
            "unresolved_event",
        )
        kind_placeholders = ",".join("?" for _ in derived_kinds)
        if branch_id == "main":
            memory_rows = self.database.conn.execute(
                "SELECT id FROM memories "
                f"WHERE persona_id = ? AND source_kind IN ({kind_placeholders})",
                (persona_id, *derived_kinds),
            ).fetchall()
        else:
            memory_rows = [
                row
                for row in self.database.conn.execute(
                    "SELECT id, metadata_json FROM memories "
                    f"WHERE persona_id = ? AND source_kind IN ({kind_placeholders})",
                    (persona_id, *derived_kinds),
                ).fetchall()
                if (
                    str(dict(loads(str(row["metadata_json"] or "{}"))).get("branch_id") or "main")
                    == branch_id
                )
            ]
        memory_ids = [str(row["id"]) for row in memory_rows]
        if memory_ids:
            placeholders = ",".join("?" for _ in memory_ids)
            params = (persona_id, *memory_ids)
            removed["memories_fts"] = self._delete_count(
                f"DELETE FROM memories_fts WHERE persona_id = ? AND memory_id IN ({placeholders})",
                params,
            )
            removed["memories"] = self._delete_count(
                f"DELETE FROM memories WHERE persona_id = ? AND id IN ({placeholders})",
                params,
            )
        else:
            removed["memories_fts"] = 0
            removed["memories"] = 0
        removed["room_transcripts"] = self._reset_room_transcripts(persona_id, branch_id)
        return removed

    def _session_branch(self, persona_id: str, session_id: str) -> str:
        row = self.database.conn.execute(
            "SELECT metadata_json FROM sessions WHERE persona_id = ? AND id = ?",
            (persona_id, session_id),
        ).fetchone()
        if row is None:
            return "main"
        try:
            return str(dict(loads(str(row["metadata_json"] or "{}"))).get("branch_id") or "main")
        except Exception:
            return "main"

    def _reset_room_transcripts(self, persona_id: str, branch_id: str) -> int:
        """Delete room transcript rows that belong to this persona's branch.

        Room transcripts have no branch column; the main branch owns every
        row where the persona spoke.  Non-main branches leave the shared
        room history alone.
        """

        if branch_id != "main":
            return 0
        return self._delete_count(
            "DELETE FROM room_transcripts WHERE persona_id = ?",
            (persona_id,),
        )

    def list_sessions(self, persona_id: str | None = None) -> list[SessionRecord]:
        if persona_id:
            rows = self.database.conn.execute(
                "SELECT * FROM sessions WHERE persona_id = ? ORDER BY created_at", (persona_id,)
            ).fetchall()
        else:
            rows = self.database.conn.execute(
                "SELECT * FROM sessions ORDER BY created_at"
            ).fetchall()
        return [
            SessionRecord(
                id=str(row["id"]),
                persona_id=str(row["persona_id"]),
                title=row["title"],
                status=str(row["status"]),
                created_at=parse_dt(row["created_at"]) or datetime.now(UTC),
                updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
                metadata=dict(loads(row["metadata_json"])),
            )
            for row in rows
        ]

    def run_reflection(self, persona_id: str, limit: int = 8) -> dict[str, Any]:
        self.personas.get(persona_id)
        rows = self.database.conn.execute(
            """
            SELECT id, session_id, user_message, persona_response, user_feedback, created_at
            FROM session_turns
            WHERE persona_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (persona_id, limit),
        ).fetchall()
        if not rows:
            return {"persona_id": persona_id, "summary": "", "memory_id": None, "turn_count": 0}
        rows = list(reversed(rows))
        summary_parts = [
            f"User: {row['user_message']} | Persona: {row['persona_response']}" for row in rows
        ]
        if any(row["user_feedback"] for row in rows):
            summary_parts.append(
                "Feedback: "
                + "; ".join(str(row["user_feedback"]) for row in rows if row["user_feedback"])
            )
        summary = "\n".join(summary_parts)
        memory = self.memories.add_memory(
            persona_id,
            content=f"Reflection summary:\n{summary}",
            memory_type=MemoryType.SEMANTIC,
            importance=0.7,
            source_kind="reflection_summary",
            participants=["user"],
            metadata={
                "reflection_type": "extractive_fallback",
                "reflection_artifact_id": new_id("refl"),
                "persona_id": persona_id,
                "supporting_turn_ids": [str(row["id"]) for row in rows],
                "supporting_session_ids": sorted({str(row["session_id"]) for row in rows}),
                "visibility": "persona_private",
            },
        )
        return {
            "persona_id": persona_id,
            "summary": summary,
            "memory_id": memory.id,
            "turn_count": len(rows),
            "reflection_type": "extractive_fallback",
        }

    def prepare_reflection(
        self,
        persona_id: str,
        branch_id: str | None = None,
        session_ids: list[str] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        self.personas.get(persona_id)
        effective_branch_id = self._reflection_branch_id(persona_id, branch_id)
        rows = self.database.conn.execute(
            """
            SELECT t.id, t.session_id, t.user_message, t.persona_response,
                   t.user_feedback, t.context_json, t.created_at, s.metadata_json
            FROM session_turns t
            JOIN sessions s ON s.id = t.session_id
            WHERE t.persona_id = ?
            ORDER BY t.created_at DESC
            """,
            (persona_id,),
        ).fetchall()
        selected = []
        allowed_sessions = set(session_ids or [])
        for row in rows:
            if allowed_sessions and str(row["session_id"]) not in allowed_sessions:
                continue
            if self._turn_branch(row) != effective_branch_id:
                continue
            selected.append(row)
            if len(selected) >= limit:
                break
        return {
            "persona_id": persona_id,
            "branch_id": effective_branch_id,
            "session_ids": sorted({str(row["session_id"]) for row in selected}),
            "recent_important_dialogue": [
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "user_message": row["user_message"],
                    "persona_response": row["persona_response"],
                    "user_feedback": row["user_feedback"],
                    "created_at": row["created_at"],
                    "branch_id": self._turn_branch(row),
                }
                for row in selected
            ],
            "current_emotions": [
                state.model_dump(mode="json")
                for state in self.affect.get_emotions(persona_id, effective_branch_id)
            ],
            "current_relationships": [
                state.model_dump(mode="json")
                for state in self.relationships.list_relationships(persona_id, effective_branch_id)
            ],
            "unresolved_events": [],
            "current_needs": [
                state.model_dump(mode="json")
                for state in self.motivation.get_needs(persona_id, effective_branch_id)
            ],
            "current_goals": [
                f"stabilize_{state.name}"
                for state in self.motivation.get_needs(persona_id, effective_branch_id)
                if state.level >= 0.65
            ],
            "activated_memories": [],
            "questions_for_host": [
                "Which changes are semantic insights rather than transcript compression?",
                "Which relationship changes are supported by specific turns?",
            ],
            "output_schema": {
                "required": [
                    "new_insights",
                    "relationship_deltas",
                    "affect_deltas",
                    "need_deltas",
                    "goal_updates",
                    "unresolved_conflicts",
                    "self_narrative_updates",
                    "memory_candidates",
                    "confidence",
                    "supporting_turn_ids",
                ]
            },
        }

    def commit_reflection(
        self, persona_id: str, artifact: dict[str, Any], branch_id: str | None = None
    ) -> dict[str, Any]:
        self.personas.get(persona_id)
        effective_branch_id = self._reflection_branch_id(persona_id, branch_id)
        try:
            validated = ReflectionArtifact.model_validate(artifact)
        except ValidationError as exc:
            raise CodedError("invalid_reflection", str(exc)) from exc
        turn_rows = self._validate_supporting_turns(
            persona_id, validated.supporting_turn_ids, effective_branch_id
        )
        supporting_session_ids = sorted({str(row["session_id"]) for row in turn_rows})
        support_pairs = [
            (str(row["session_id"]), str(row["id"]))
            for row in sorted(turn_rows, key=lambda r: r["id"])
        ]
        memory_ids = []
        try:
            self.database.conn.execute("BEGIN")
            reflection_candidates: list[ReflectionInsight | ReflectionMemoryCandidate] = [
                *validated.new_insights,
                *validated.memory_candidates,
            ]
            for candidate in reflection_candidates:
                memory = self.memories.add_memory(
                    persona_id,
                    content=candidate.content,
                    memory_type=MemoryType.SEMANTIC,
                    importance=candidate.importance,
                    source_kind="reflection_summary",
                    source_confidence=validated.confidence,
                    branch_id=effective_branch_id,
                    metadata={
                        "reflection_type": "semantic_host_artifact",
                        "reflection_artifact_id": validated.reflection_artifact_id,
                        "persona_id": persona_id,
                        "branch_id": effective_branch_id,
                        "supporting_turn_ids": validated.supporting_turn_ids,
                        "supporting_session_ids": supporting_session_ids,
                        "visibility": "persona_private",
                    },
                    commit=False,
                )
                memory_ids.append(memory.id)
                for turn_id in validated.supporting_turn_ids:
                    self._insert_lineage(
                        persona_id,
                        child_type="memory",
                        child_id=memory.id,
                        parent_type="session_turn",
                        parent_id=str(turn_id),
                        relation="reflection_from",
                    )
                if isinstance(candidate, ReflectionInsight):
                    self._insert_change_event(
                        persona_id,
                        effective_branch_id,
                        "reflection_insight",
                        "memory",
                        memory.id,
                        supporting_session_ids[0] if supporting_session_ids else None,
                        validated.supporting_turn_ids[0],
                        {
                            **candidate.model_dump(mode="json"),
                            "reflection_artifact_id": validated.reflection_artifact_id,
                            "supporting_turn_ids": validated.supporting_turn_ids,
                            "supporting_session_ids": supporting_session_ids,
                        },
                        support_pairs=support_pairs,
                    )
            for conflict in validated.unresolved_conflicts:
                conflict_memory = self.memories.add_memory(
                    persona_id,
                    content=conflict.content,
                    memory_type=MemoryType.SEMANTIC,
                    importance=conflict.severity,
                    source_kind="unresolved_event",
                    source_confidence=validated.confidence,
                    unresolved=True,
                    branch_id=effective_branch_id,
                    metadata={
                        "reflection_type": "semantic_host_artifact",
                        "reflection_artifact_id": validated.reflection_artifact_id,
                        "persona_id": persona_id,
                        "branch_id": effective_branch_id,
                        "supporting_turn_ids": validated.supporting_turn_ids,
                        "supporting_session_ids": supporting_session_ids,
                        "visibility": "persona_private",
                    },
                    commit=False,
                )
                memory_ids.append(conflict_memory.id)
            self._apply_reflection_deltas(
                persona_id,
                effective_branch_id,
                validated,
                supporting_session_ids,
                support_pairs,
            )
            self.database.conn.commit()
        except Exception:
            self.database.conn.rollback()
            raise
        return {
            "persona_id": persona_id,
            "branch_id": effective_branch_id,
            "memory_ids": memory_ids,
            "reflection_type": "semantic_host_artifact",
        }

    def _validate_supporting_turns(
        self, persona_id: str, supporting_turn_ids: list[str], branch_id: str
    ) -> list[Any]:
        rows = []
        for turn_id in supporting_turn_ids:
            row = self.database.conn.execute(
                """
                SELECT t.id, t.session_id, t.persona_id, t.context_json, s.metadata_json
                FROM session_turns t
                JOIN sessions s ON s.id = t.session_id
                WHERE t.id = ?
                """,
                (turn_id,),
            ).fetchone()
            if row is None:
                raise CodedError("invalid_reflection", f"supporting_turn_not_found:{turn_id}")
            if str(row["persona_id"]) != persona_id:
                raise CodedError(
                    "invalid_reflection", f"supporting_turn_persona_mismatch:{turn_id}"
                )
            if self._turn_branch(row) != branch_id:
                raise CodedError("invalid_reflection", f"supporting_turn_branch_mismatch:{turn_id}")
            rows.append(row)
        return rows

    def _turn_belongs_to_session(self, turn_id: str, session_id: str) -> bool:
        row = self.database.conn.execute(
            "SELECT 1 FROM session_turns WHERE id = ? AND session_id = ?",
            (turn_id, session_id),
        ).fetchone()
        return row is not None

    def _apply_reflection_deltas(
        self,
        persona_id: str,
        branch_id: str,
        artifact: ReflectionArtifact,
        supporting_session_ids: list[str],
        support_pairs: list[tuple[str, str]],
    ) -> None:
        session_id = supporting_session_ids[0] if supporting_session_ids else None
        turn_id = artifact.supporting_turn_ids[0] if artifact.supporting_turn_ids else None
        for delta in artifact.relationship_deltas:
            self.relationships.update_relationship(
                persona_id,
                delta.counterpart_id,
                delta.changes,
                delta.reason or "reflection_delta",
                branch_id=branch_id,
                commit=False,
            )
            self._insert_change_event(
                persona_id,
                branch_id,
                "relationship_delta",
                "relationship",
                delta.counterpart_id,
                session_id,
                turn_id,
                {
                    **delta.model_dump(mode="json"),
                    "reflection_artifact_id": artifact.reflection_artifact_id,
                    "supporting_turn_ids": artifact.supporting_turn_ids,
                    "supporting_session_ids": supporting_session_ids,
                },
                support_pairs=support_pairs,
            )
        if artifact.affect_deltas:
            affect_updates = {
                key: min(1.0, float(value) + 0.01) for key, value in artifact.affect_deltas.items()
            }
            self.affect.update_emotions(
                persona_id,
                affect_updates,
                "reflection_delta",
                branch_id=branch_id,
                commit=False,
            )
            self._insert_change_event(
                persona_id,
                branch_id,
                "affect_delta",
                "affect",
                "current",
                session_id,
                turn_id,
                {
                    "delta": artifact.affect_deltas,
                    "reflection_artifact_id": artifact.reflection_artifact_id,
                    "supporting_turn_ids": artifact.supporting_turn_ids,
                    "supporting_session_ids": supporting_session_ids,
                },
                support_pairs=support_pairs,
            )
        if artifact.need_deltas:
            self.motivation.update_needs(
                persona_id,
                artifact.need_deltas,
                "reflection_delta",
                branch_id=branch_id,
                commit=False,
            )
            self._insert_change_event(
                persona_id,
                branch_id,
                "need_delta",
                "needs",
                "current",
                session_id,
                turn_id,
                {
                    "delta": artifact.need_deltas,
                    "reflection_artifact_id": artifact.reflection_artifact_id,
                    "supporting_turn_ids": artifact.supporting_turn_ids,
                    "supporting_session_ids": supporting_session_ids,
                },
                support_pairs=support_pairs,
            )
        for goal in artifact.goal_updates:
            self._insert_change_event(
                persona_id,
                branch_id,
                "goal_update",
                "goal",
                goal.goal_id,
                session_id,
                turn_id,
                {
                    **goal.model_dump(mode="json"),
                    "reflection_artifact_id": artifact.reflection_artifact_id,
                    "supporting_turn_ids": artifact.supporting_turn_ids,
                    "supporting_session_ids": supporting_session_ids,
                },
                support_pairs=support_pairs,
            )
        for conflict in artifact.unresolved_conflicts:
            self._insert_change_event(
                persona_id,
                branch_id,
                "unresolved_conflict",
                "memory",
                conflict.id or new_id("conflict"),
                session_id,
                turn_id,
                {
                    **conflict.model_dump(mode="json"),
                    "reflection_artifact_id": artifact.reflection_artifact_id,
                    "supporting_turn_ids": artifact.supporting_turn_ids,
                    "supporting_session_ids": supporting_session_ids,
                },
                support_pairs=support_pairs,
            )
        if artifact.self_narrative_updates:
            self._insert_change_event(
                persona_id,
                branch_id,
                "self_narrative_update",
                "runtime_state",
                "self_narrative",
                session_id,
                turn_id,
                {
                    "updates": artifact.self_narrative_updates,
                    "reflection_artifact_id": artifact.reflection_artifact_id,
                    "supporting_turn_ids": artifact.supporting_turn_ids,
                    "supporting_session_ids": supporting_session_ids,
                },
                support_pairs=support_pairs,
            )
        self._refresh_runtime_state(persona_id, branch_id)

    def _insert_change_event(
        self,
        persona_id: str,
        branch_id: str,
        event_type: str,
        target_type: str,
        target_id: str,
        session_id: str | None,
        turn_id: str | None,
        data: dict[str, Any],
        *,
        support_pairs: list[tuple[str, str]] | None = None,
    ) -> None:
        event_id = new_id("evt")
        self.database.conn.execute(
            "INSERT INTO change_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                persona_id,
                branch_id,
                event_type,
                target_type,
                target_id,
                session_id,
                turn_id,
                dumps(data),
                datetime.now(UTC).isoformat(),
            ),
        )
        pairs = support_pairs or (
            [(session_id, turn_id)] if session_id is not None and turn_id is not None else []
        )
        for support_session_id, support_turn_id in pairs:
            self.database.conn.execute(
                """
                INSERT OR IGNORE INTO change_event_supports
                VALUES (?, ?, ?, ?)
                """,
                (event_id, support_session_id, support_turn_id, 1.0),
            )

    def _apply_affect_delta(
        self,
        persona_id: str,
        branch_id: str,
        session_id: str | None,
        turn_id: str | None,
        delta: dict[str, float],
        reason: str,
    ) -> None:
        before = {
            state.name: state.model_dump(mode="json")
            for state in self.affect.get_emotions(persona_id, branch_id, commit=False)
            if state.name in delta
        }
        states = self.affect.update_emotions(
            persona_id, delta, reason, branch_id=branch_id, commit=False
        )
        after = {
            state.name: state.model_dump(mode="json") for state in states if state.name in delta
        }
        self._insert_change_event(
            persona_id,
            branch_id,
            "affect_delta",
            "affect",
            "current",
            session_id,
            turn_id,
            {
                "branch_id": branch_id,
                "before_state": before,
                "delta": delta,
                "after_state": after,
                "reason": reason,
                "validity": "valid",
            },
        )

    def _apply_need_delta(
        self,
        persona_id: str,
        branch_id: str,
        session_id: str | None,
        turn_id: str | None,
        delta: dict[str, float],
        reason: str,
    ) -> None:
        before = {
            state.name: state.model_dump(mode="json")
            for state in self.motivation.get_needs(persona_id, branch_id)
            if state.name in delta
        }
        states = self.motivation.update_needs(
            persona_id, delta, reason, branch_id=branch_id, commit=False
        )
        after = {
            state.name: state.model_dump(mode="json") for state in states if state.name in delta
        }
        self._insert_change_event(
            persona_id,
            branch_id,
            "need_delta",
            "needs",
            "current",
            session_id,
            turn_id,
            {
                "branch_id": branch_id,
                "before_state": before,
                "delta": delta,
                "after_state": after,
                "reason": reason,
                "validity": "valid",
            },
        )

    def _apply_relationship_delta(
        self,
        persona_id: str,
        counterpart_id: str,
        changes: dict[str, float],
        branch_id: str,
        session_id: str | None,
        turn_id: str | None,
        reason: str,
    ) -> None:
        before = self.relationships.get_relationship(
            persona_id, counterpart_id, branch_id=branch_id
        ).model_dump(mode="json")
        after = self.relationships.update_relationship(
            persona_id, counterpart_id, changes, reason, branch_id=branch_id, commit=False
        )
        self._insert_change_event(
            persona_id,
            branch_id,
            "relationship_delta",
            "relationship",
            counterpart_id,
            session_id,
            turn_id,
            {
                "branch_id": branch_id,
                "before_state": before,
                "delta": {"changes": changes},
                "changes": changes,
                "after_state": after.model_dump(mode="json"),
                "reason": reason,
                "validity": "valid",
            },
        )

    def _append_self_narrative(self, persona_id: str, updates: list[str]) -> None:
        from pathlib import Path

        path = Path(self.personas.get(persona_id).package_path) / "identity" / "self_narrative.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        suffix = "\n".join(update.strip() for update in updates if update.strip())
        path.write_text((existing.rstrip() + "\n" + suffix + "\n").lstrip(), encoding="utf-8")

    def _replay_runtime_state(
        self,
        persona_id: str,
        *,
        branch_id: str,
        affected_relationships: set[str],
        affected_affects: set[str],
        affected_needs: set[str],
    ) -> None:
        for counterpart in affected_relationships:
            self.database.conn.execute(
                """
                DELETE FROM relationships
                WHERE persona_id = ? AND branch_id = ? AND counterpart = ?
                """,
                (persona_id, branch_id, counterpart),
            )
        for name in affected_affects:
            self.database.conn.execute(
                """
                DELETE FROM affect_states
                WHERE persona_id = ? AND branch_id = ? AND name = ?
                """,
                (persona_id, branch_id, name),
            )
        for name in affected_needs:
            self.database.conn.execute(
                "DELETE FROM needs WHERE persona_id = ? AND branch_id = ? AND name = ?",
                (persona_id, branch_id, name),
            )
        rows = self.database.conn.execute(
            """
            SELECT event_type, target_id, data_json
            FROM change_events
            WHERE persona_id = ? AND branch_id = ?
            ORDER BY created_at
            """,
            (persona_id, branch_id),
        ).fetchall()
        for row in rows:
            event_type = str(row["event_type"])
            data = dict(loads(row["data_json"]))
            delta = self._event_delta(data)
            if (
                event_type == "relationship_delta"
                and str(row["target_id"]) in affected_relationships
            ):
                changes = {
                    str(key): float(value)
                    for key, value in dict(delta.get("changes", delta)).items()
                    if isinstance(value, int | float)
                }
                self.relationships.update_relationship(
                    persona_id,
                    str(row["target_id"]),
                    changes,
                    "runtime_replay",
                    branch_id=branch_id,
                    commit=False,
                )
            elif event_type == "affect_delta":
                changes = {
                    str(key): min(1.0, float(value) + 0.01)
                    for key, value in delta.items()
                    if str(key) in affected_affects and isinstance(value, int | float)
                }
                if changes:
                    self.affect.update_emotions(
                        persona_id,
                        changes,
                        "runtime_replay",
                        branch_id=branch_id,
                        commit=False,
                    )
            elif event_type == "need_delta":
                changes = {
                    str(key): float(value)
                    for key, value in delta.items()
                    if str(key) in affected_needs and isinstance(value, int | float)
                }
                if changes:
                    self.motivation.update_needs(
                        persona_id,
                        changes,
                        "runtime_replay",
                        branch_id=branch_id,
                        commit=False,
                    )
        self._refresh_runtime_state(persona_id, branch_id)

    def _ensure_runtime_state(self, persona_id: str, branch_id: str) -> None:
        path = self._runtime_state_path(persona_id, branch_id)
        if not path.exists():
            self._refresh_runtime_state(persona_id, branch_id)

    def _runtime_state_path(self, persona_id: str, branch_id: str) -> Path:
        return (
            Path(self.personas.get(persona_id).package_path)
            / "runtime"
            / "branches"
            / branch_id
            / "runtime_state.json"
        )

    def _refresh_runtime_state(self, persona_id: str, branch_id: str) -> None:
        path = self._runtime_state_path(persona_id, branch_id)

        runtime_state: dict[str, Any] = {
            "schema_version": "1.1",
            "branch_id": branch_id,
            "revision": datetime.now(UTC).isoformat(),
            "active_goals": [],
            "self_narrative_updates": [],
            "unresolved_conflicts": [],
            "reflection_insights": [],
        }
        rows = self.database.conn.execute(
            """
            SELECT event_type, target_id, data_json, created_at
            FROM change_events
            WHERE persona_id = ? AND branch_id = ?
            ORDER BY created_at
            """,
            (persona_id, branch_id),
        ).fetchall()
        active_goals: dict[str, dict[str, Any]] = {}
        for row in rows:
            event_type = str(row["event_type"])
            data = dict(loads(row["data_json"]))
            if event_type == "goal_update":
                goal_id = str(data.get("goal_id", row["target_id"]))
                status = str(data.get("status", "active"))
                if status in {"completed", "cancelled", "inactive"}:
                    active_goals.pop(goal_id, None)
                else:
                    active_goals[goal_id] = {**data, "goal_id": goal_id}
            elif event_type == "self_narrative_update":
                runtime_state["self_narrative_updates"].extend(
                    str(item) for item in list(data.get("updates", [])) if item
                )
            elif event_type == "unresolved_conflict":
                runtime_state["unresolved_conflicts"].append(data)
            elif event_type == "reflection_insight":
                runtime_state["reflection_insights"].append(data)
        runtime_state["active_goals"] = list(active_goals.values())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps(runtime_state), encoding="utf-8")

    def _query_from_message(self, message: str) -> str:
        terms = [word.strip(".,!?;:，。！？；：").lower() for word in message.split()]
        return " OR ".join(term for term in terms if len(term) > 2) or message

    def _fit_memories(self, memories: list[Any], max_context_size: int) -> list[Any]:
        selected = []
        used = 0
        for memory in memories:
            size = len(memory.content)
            if size > max_context_size:
                continue
            if used + size > max_context_size:
                break
            selected.append(memory)
            used += size
            if used >= max_context_size:
                break
        return selected

    def _context_list(self, compiled_by_key: dict[str, Any], key: str) -> list[str]:
        value = compiled_by_key.get(key)
        if isinstance(value, list):
            return [dumps(item) if isinstance(item, dict) else str(item) for item in value]
        if value:
            return [str(value)]
        return []

    def _context_dict(self, compiled_by_key: dict[str, Any], key: str) -> dict[str, Any]:
        value = compiled_by_key.get(key)
        if isinstance(value, dict):
            return value
        return {}

    def _appraise(self, message: str, external_events: list[dict[str, Any]]) -> dict[str, Any]:
        lowered = message.lower()
        return {
            "challenge": any(
                token in lowered for token in ["wrong", "worried", "challenge", "担心"]
            ),
            "support": any(token in lowered for token in ["thanks", "good", "support", "谢谢"]),
            "external_event_count": len(external_events),
        }

    def _claim_contents(self, persona_id: str, keyword: str, limit: int) -> list[str]:
        rows = self.database.conn.execute(
            """
            SELECT content FROM claims
            WHERE persona_id = ? AND (dimension LIKE ? OR content LIKE ?)
            ORDER BY confidence DESC
            LIMIT ?
            """,
            (persona_id, f"%{keyword}%", f"%{keyword}%", limit),
        ).fetchall()
        return [str(row["content"]) for row in rows]

    def _relevant_claim_contents(
        self,
        persona_id: str,
        query: str,
        *,
        policy: PersonaRetrievalPolicy,
        limit: int,
    ) -> list[str]:
        """Backfill memory recall with query-matched, runtime-visible claims."""

        if limit <= 0 or not query.strip():
            return []
        rows = self.database.conn.execute(
            """
            SELECT content, claim_type, confidence, metadata_json
            FROM claims
            WHERE persona_id = ?
            ORDER BY confidence DESC, created_at DESC
            """,
            (persona_id,),
        ).fetchall()
        features = self._retrieval_features(query)
        scored: list[tuple[int, float, str]] = []
        for row in rows:
            metadata = dict(loads(row["metadata_json"] or "{}"))
            if normalise_material_scope(metadata.get("material_scope")) in NON_CHARACTER_SCOPES:
                continue
            if not policy.allows(str(row["claim_type"])):
                continue
            content = str(row["content"])
            normalized = content.lower()
            matched = sum(feature in normalized for feature in features)
            raw_evidence_ids = metadata.get("evidence_ids", [])
            evidence_ids = (
                [str(value) for value in raw_evidence_ids]
                if isinstance(raw_evidence_ids, list)
                else []
            )
            if matched and not self._has_evaluation_evidence(evidence_ids):
                scored.append((matched, float(row["confidence"]), content))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return list(dict.fromkeys(content for _, _, content in scored))[:limit]

    @staticmethod
    def _retrieval_features(query: str) -> set[str]:
        normalized = query.strip().lower()
        features = {
            token.strip(".,!?;:，。！？；：()[]{}\"'")
            for token in normalized.replace(" OR ", " ").split()
        }
        compact = "".join(char for char in normalized if not char.isspace())
        if any("\u4e00" <= char <= "\u9fff" for char in compact):
            for size in (2, 3):
                for index in range(max(0, len(compact) - size + 1)):
                    features.add(compact[index : index + size])
        return {feature for feature in features if feature}

    def _has_evaluation_evidence(self, evidence_ids: list[str]) -> bool:
        unit_ids: set[str] = set()
        for evidence_id in evidence_ids:
            if evidence_id.startswith("evu_"):
                unit_ids.add(evidence_id)
                continue
            if not evidence_id.startswith("evf_"):
                continue
            row = self.database.conn.execute(
                "SELECT supporting_evidence_ids_json FROM persona_fused_evidence WHERE id = ?",
                (evidence_id,),
            ).fetchone()
            if row is not None:
                unit_ids.update(str(value) for value in loads(row[0] or "[]"))
        if not unit_ids:
            return False
        placeholders = ",".join("?" for _ in unit_ids)
        rows = self.database.conn.execute(
            f"SELECT context_tags_json FROM persona_evidence_units "
            f"WHERE id IN ({placeholders})",
            tuple(sorted(unit_ids)),
        ).fetchall()
        for row in rows:
            tags = {str(value).strip().casefold() for value in loads(row[0] or "[]")}
            if tags & EVALUATION_CONTEXT_TAGS:
                return True
        return False

    def _emotion_observations(
        self, user_message: str, persona_response: str, user_feedback: str | None
    ) -> dict[str, float]:
        """Affect-only view of the appraisal, for callers that predate it.

        The previous implementation ended with ``observations or
        {"curiosity": 0.1}``.  ``curiosity`` is a NEED, not an emotion, so
        ``AffectEngine.update_emotions`` skipped it via its
        ``if name not in EMOTION_NAMES: continue`` guard -- the fallback was a
        silent no-op disguised as a state update.  It is gone: when nothing
        fires, nothing is written.
        """

        result = self.state_appraisal.appraise(
            AppraisalRequest(
                user_message=user_message,
                persona_response=persona_response,
                user_feedback=user_feedback,
            )
        )
        return dict(result.affect)

    def _appraise_commit(
        self,
        *,
        persona_id: str,
        branch_id: str,
        counterpart_id: str,
        user_message: str,
        persona_response: str,
        user_feedback: str | None,
        goal_completed: bool,
        external_events: list[dict[str, Any]] | None = None,
    ) -> AppraisalResult:
        """Appraise one committed turn against the persona's *current* state.

        Current values are passed in so the service can cap per-turn movement
        instead of absolute position -- the difference between "trust drifted
        up 4 points" and "trust teleported to 90%".
        """

        emotions = self.affect.get_emotions(persona_id, branch_id, commit=False)
        needs = self.motivation.get_needs(persona_id, branch_id)
        relationship = self.relationships.get_relationship(
            persona_id, counterpart_id, branch_id=branch_id
        )
        return self.state_appraisal.appraise(
            AppraisalRequest(
                user_message=user_message,
                persona_response=persona_response,
                user_feedback=user_feedback,
                external_events=list(external_events or []),
                goal_completed=goal_completed,
                counterpart_id=counterpart_id,
                current_affect={state.name: state.intensity for state in emotions},
                current_needs={state.name: state.level for state in needs},
                current_relationship=relationship.model_dump(mode="python"),
            )
        )

    def _drain_pending_events(self, session: SessionRecord) -> list[dict[str, Any]]:
        """Consume the events staged by ``prepare_turn``.

        Events declared at prepare time describe the world as it was when the
        turn was set up, so they belong to the appraisal of the turn that
        answers them.  Staging them on the session is what lets a caller pass
        events through the normal prepare/commit flow and still have them
        move state, instead of needing a hand-written ``state_patch``.
        """
        pending = session.metadata.pop("pending_external_events", None)
        if not pending:
            return []
        if isinstance(pending, list):
            session.metadata["pending_external_events"] = []
            self._save_session_metadata(session)
            return [event for event in pending if isinstance(event, dict)]
        return []

    def _require_session(
        self, persona_id: str, session_id: str, allow_status: set[str]
    ) -> SessionRecord:
        self.personas.get(persona_id)
        row = self.database.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise CodedError("session_not_found", session_id)
        if str(row["persona_id"]) != persona_id:
            raise CodedError("session_persona_mismatch", session_id)
        status = str(row["status"])
        if status not in allow_status:
            raise CodedError("session_not_active", f"{session_id}:{status}")
        return SessionRecord(
            id=str(row["id"]),
            persona_id=str(row["persona_id"]),
            title=row["title"],
            status=status,
            created_at=parse_dt(row["created_at"]) or datetime.now(UTC),
            updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
            metadata=dict(loads(row["metadata_json"])),
        )

    def _effective_branch_id(
        self, persona_id: str, session: SessionRecord, requested_branch_id: str | None
    ) -> str:
        manifest = self.personas.get(persona_id).manifest
        existing_branch_id = session.metadata.get("branch_id")
        if requested_branch_id:
            self._validate_branch(persona_id, requested_branch_id)
            if existing_branch_id and str(existing_branch_id) != requested_branch_id:
                raise CodedError("session_branch_mismatch", session.id)
            return requested_branch_id
        if existing_branch_id:
            self._validate_branch(persona_id, str(existing_branch_id))
            return str(existing_branch_id)
        if manifest.current_main_branch:
            self._validate_branch(persona_id, manifest.current_main_branch)
            return manifest.current_main_branch
        return "main"

    def _reflection_branch_id(self, persona_id: str, requested_branch_id: str | None) -> str:
        if requested_branch_id:
            self._validate_branch(persona_id, requested_branch_id)
            return requested_branch_id
        manifest = self.personas.get(persona_id).manifest
        if manifest.current_main_branch:
            self._validate_branch(persona_id, manifest.current_main_branch)
            return manifest.current_main_branch
        return "main"

    def _validate_branch(self, persona_id: str, branch_id: str) -> None:
        if branch_id in {"main", "shared_pre_divergence"}:
            return
        row = self.database.conn.execute(
            "SELECT persona_id FROM continuation_branches WHERE id = ?", (branch_id,)
        ).fetchone()
        if row is None:
            raise CodedError("branch_not_found", branch_id)
        if str(row["persona_id"]) != persona_id:
            raise CodedError("branch_persona_mismatch", branch_id)

    def _turn_branch(self, row: Any) -> str:
        context = {}
        metadata = {}
        with contextlib.suppress(Exception):
            context = dict(loads(row["context_json"]))
        with contextlib.suppress(Exception):
            metadata = dict(loads(row["metadata_json"]))
        return str(context.get("branch_id") or metadata.get("branch_id") or "main")

    def _save_session_metadata(self, session: SessionRecord) -> None:
        self.database.conn.execute(
            "UPDATE sessions SET updated_at = ?, metadata_json = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), dumps(session.metadata), session.id),
        )

    def _require_counterpart(self, session: SessionRecord, requested_counterpart: str) -> None:
        bound = session.metadata.get("counterpart_id")
        if bound is None:
            session.metadata["counterpart_id"] = requested_counterpart
            return
        if str(bound) != requested_counterpart:
            raise CodedError("session_counterpart_mismatch", session.id)

    def _event_delta(self, data: dict[str, Any]) -> dict[str, Any]:
        delta = data.get("delta", data)
        return dict(delta) if isinstance(delta, dict) else {}

    def _validate_state_patch(self, state_patch: dict[str, Any]) -> dict[str, Any]:
        """Normalise a caller-supplied state patch into explicit semantics.

        Affect used to accept a single ``affect`` key whose meaning was
        ambiguous: the engine treats it as an absolute floor
        (``max(current, amount)``) while the name reads like a delta.  The two
        are now separate, explicitly named keys and ``affect`` is kept only as
        a deprecated alias for ``affect_set``.
        """
        allowed = {
            "affect_set",
            "affect_delta",
            "affect",  # deprecated alias for affect_set
            "needs",
            "relationships",
            "unresolved_events",
        }
        unknown = set(state_patch) - allowed
        if unknown:
            raise CodedError("invalid_state_patch", ",".join(sorted(unknown)))
        has_explicit = "affect_set" in state_patch or "affect_delta" in state_patch
        if "affect" in state_patch and has_explicit:
            raise CodedError("invalid_state_patch", "affect_conflicts_with_affect_set_or_delta")
        normalized: dict[str, Any] = {}
        if "affect_set" in state_patch or "affect" in state_patch:
            raw = state_patch.get("affect_set", state_patch.get("affect"))
            if not isinstance(raw, dict):
                raise CodedError("invalid_state_patch", "affect_set")
            try:
                normalized["affect_set"] = _validate_numeric_map(
                    raw,
                    allowed_keys=set(EMOTION_NAMES),
                    field_name="affect_set",
                    minimum=0,
                    maximum=1,
                )
            except ValueError as exc:
                raise CodedError("invalid_state_patch", str(exc)) from exc
        if "affect_delta" in state_patch:
            raw = state_patch.get("affect_delta")
            if not isinstance(raw, dict):
                raise CodedError("invalid_state_patch", "affect_delta")
            try:
                normalized["affect_delta"] = _validate_numeric_map(
                    raw,
                    allowed_keys=set(EMOTION_NAMES),
                    field_name="affect_delta",
                    minimum=-1,
                    maximum=1,
                )
            except ValueError as exc:
                raise CodedError("invalid_state_patch", str(exc)) from exc
        if "needs" in state_patch:
            needs = state_patch.get("needs")
            if not isinstance(needs, dict):
                raise CodedError("invalid_state_patch", "needs")
            try:
                normalized["needs"] = _validate_numeric_map(
                    needs,
                    allowed_keys=set(NEED_NAMES),
                    field_name="needs",
                    minimum=-1,
                    maximum=1,
                )
            except ValueError as exc:
                raise CodedError("invalid_state_patch", str(exc)) from exc
        relationships = state_patch.get("relationships", []) or []
        if not isinstance(relationships, list):
            raise CodedError("invalid_state_patch", "relationships")
        normalized_relationships = []
        for delta in relationships:
            if not isinstance(delta, dict) or not str(delta.get("counterpart_id", "")):
                raise CodedError("invalid_state_patch", "relationships")
            # ``changes`` is the historical name for what is actually an
            # absolute set.  ``set`` says so; ``changes`` is kept as an alias
            # so existing callers keep working.
            if "set" in delta and "changes" in delta:
                raise CodedError("invalid_state_patch", "relationship_set_conflicts_with_changes")
            changes = delta.get("set", delta.get("changes"))
            if not isinstance(changes, dict) or not changes:
                raise CodedError("invalid_state_patch", "relationships")
            try:
                normalized_relationships.append(
                    {
                        "counterpart_id": str(delta["counterpart_id"]),
                        "set": _validate_numeric_map(
                            changes,
                            allowed_keys=RELATIONSHIP_FIELDS,
                            field_name="relationships",
                            minimum=0,
                            maximum=1,
                        ),
                    }
                )
            except ValueError as exc:
                raise CodedError("invalid_state_patch", str(exc)) from exc
        if normalized_relationships:
            normalized["relationships"] = normalized_relationships
        if "unresolved_events" in state_patch:
            unresolved = state_patch.get("unresolved_events")
            if not isinstance(unresolved, list):
                raise CodedError("invalid_state_patch", "unresolved_events")
            normalized["unresolved_events"] = unresolved
        return normalized

    def _apply_state_patch(
        self,
        persona_id: str,
        session_id: str,
        turn_id: str,
        branch_id: str,
        state_patch: dict[str, Any],
    ) -> None:
        if affect_set := state_patch.get("affect_set"):
            # Floor semantics: AffectEngine raises to the target and never
            # lowers.  Cooling is the engine's exponential decay.
            self._apply_affect_delta(
                persona_id,
                branch_id,
                session_id,
                turn_id,
                dict(affect_set),
                "state_patch.affect_set",
            )
        if affect_delta := state_patch.get("affect_delta"):
            # Additive semantics: can raise or lower within one turn.
            self.affect.apply_deltas(
                persona_id,
                dict(affect_delta),
                "state_patch.affect_delta",
                branch_id=branch_id,
            )
            self._insert_change_event(
                persona_id,
                branch_id,
                "affect_delta",
                "affect",
                "current",
                session_id,
                turn_id,
                dict(affect_delta),
            )
        if needs := state_patch.get("needs"):
            self._apply_need_delta(
                persona_id,
                branch_id,
                session_id,
                turn_id,
                dict(needs),
                "state_patch",
            )
        for entry in state_patch.get("relationships", []) or []:
            self._apply_relationship_delta(
                persona_id,
                str(entry["counterpart_id"]),
                dict(entry["set"]),
                branch_id,
                session_id,
                turn_id,
                "state_patch",
            )

    def _insert_lineage(
        self,
        persona_id: str,
        *,
        child_type: str,
        child_id: str,
        parent_type: str,
        parent_id: str,
        relation: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.database.conn.execute(
            """
            INSERT OR IGNORE INTO lineage
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("lin"),
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
