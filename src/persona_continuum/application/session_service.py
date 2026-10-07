from __future__ import annotations

import contextlib
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from persona_continuum.application._utils import dumps, loads, new_id, parse_dt
from persona_continuum.application.compiled_context_service import CompiledPersonaContextService
from persona_continuum.application.episode_service import EpisodeService
from persona_continuum.application.memory_service import MemoryService
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.application.state_appraisal import (
    AppraisalLimits,
    AppraisalRequest,
    AppraisalResult,
    PersonaStateAppraisalService,
)
from persona_continuum.auth.profiles import redact_secrets
from persona_continuum.config import Config
from persona_continuum.domain.affect import (
    EMOTION_NAMES,
    NEED_DEFAULT_BASELINES,
    NEED_NAMES,
    AffectState,
    NeedState,
)
from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.provenance import (
    NON_CHARACTER_SCOPES,
    PersonaRetrievalPolicy,
    normalise_material_scope,
)
from persona_continuum.domain.relationship import RelationshipState
from persona_continuum.domain.session import PreparedTurn, SessionRecord
from persona_continuum.runtime.affect_engine import AffectEngine
from persona_continuum.runtime.bond_dynamics import (
    appraise_bond,
    initial_state,
    relationship_stance,
)
from persona_continuum.runtime.motivation_engine import MotivationEngine
from persona_continuum.runtime.persona_seed import build_seed
from persona_continuum.runtime.relationship_engine import RELATIONSHIP_FIELDS, RelationshipEngine
from persona_continuum.runtime.turn_normalizer import normalize_turn, semantic_experience
from persona_continuum.security.validation import CodedError
from persona_continuum.storage.database import Database

_metadata_trace_logger = logging.getLogger("persona_continuum.room.metadata")

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
        if not math.isfinite(numeric) or numeric < minimum or numeric > maximum:
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
            minimum=-1,
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
    counterpart_id: str | None = None


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
            minimum=-1,
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
        episodes: EpisodeService | None = None,
    ) -> None:
        self.database = database
        self.personas = personas
        self.memories = memories
        self.compiled_context = compiled_context
        self.affect = affect
        self.motivation = motivation
        self.relationships = relationships
        self.config = config or Config()
        # Owns the Episode ledger (Memory Architecture v2, Phase 3).  Optional
        # so every existing construction path keeps working; absent an Episode
        # service a committed turn simply has no organisation layer.
        self.episodes = episodes
        self.semantic_reflector: Callable[[dict[str, Any]], dict[str, Any]] | None = None
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
        initial_relationship: dict[str, Any] | None = None,
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
        effective_branch = (
            branch_id or self.personas.get(persona_id).manifest.current_main_branch or "main"
        )
        seed = build_seed(self.compiled_context.runtime_seed_components(persona_id))
        prior = initial_relationship or seed.relationship_priors.get(counterpart_id, {})
        initial_state(persona_id, counterpart_id, prior)  # Validate before any persistent writes.
        self._ensure_runtime_state(persona_id, effective_branch)
        existing = self.database.conn.execute(
            "SELECT 1 FROM relationships WHERE persona_id=? AND branch_id=? AND counterpart=?",
            (persona_id, effective_branch, counterpart_id),
        ).fetchone()
        if not existing:
            state = self.relationships.initialize(
                persona_id, counterpart_id, prior, effective_branch, commit=False
            )
            self._insert_change_event(
                persona_id,
                effective_branch,
                "relationship_prior",
                "relationship",
                counterpart_id,
                None,
                None,
                {"state": state.model_dump(mode="json")},
            )
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

    def initialize_relationship(
        self, persona_id: str, counterpart_id: str, prior: dict[str, Any], branch_id: str = "main"
    ) -> RelationshipState:
        self.personas.get(persona_id)
        if not prior:
            prior = build_seed(
                self.compiled_context.runtime_seed_components(persona_id)
            ).relationship_priors.get(counterpart_id, {})
        initial_state(persona_id, counterpart_id, prior)
        exists = self.database.conn.execute(
            "SELECT 1 FROM relationships WHERE persona_id=? AND branch_id=? AND counterpart=?",
            (persona_id, branch_id, counterpart_id),
        ).fetchone()
        state = self.relationships.initialize(
            persona_id, counterpart_id, prior, branch_id, commit=False
        )
        if not exists:
            self._insert_change_event(
                persona_id,
                branch_id,
                "relationship_prior",
                "relationship",
                counterpart_id,
                None,
                None,
                {"state": state.model_dump(mode="json")},
            )
        self.database.conn.commit()
        return state

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
        interaction_counterpart_id: str | None = None,
        interaction_message: str | None = None,
    ) -> PreparedTurn:
        session = self._require_session(persona_id, session_id, allow_status={"active"})
        persona = self.personas.get(persona_id)
        self._require_counterpart(session, counterpart_id)
        if interaction_counterpart_id:
            session.metadata["pending_interaction"] = {
                "counterpart_id": interaction_counterpart_id,
                "message": str(redact_secrets(interaction_message or user_message)),
            }
            counterpart_id = interaction_counterpart_id
            user_message = interaction_message or user_message
        effective_branch_id = self._effective_branch_id(persona_id, session, branch_id)
        if current_time is not None:
            session.metadata["pending_scene_time"] = current_time.isoformat()
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
        memories = [
            memory
            for memory in memories
            if not (memory.metadata or {}).get("relationship_memory")
            or (memory.metadata or {}).get("counterpart_id") == counterpart_id
        ]
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
            staged.extend(
                redact_secrets(event) for event in external_events if isinstance(event, dict)
            )
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
        current_needs = self.motivation.get_needs(persona_id, effective_branch_id, now=current_time)
        preview = self.state_appraisal.appraise_interaction(
            AppraisalRequest(
                user_message=user_message,
                external_events=external_events or [],
                counterpart_id=counterpart_id,
                current_relationship=relationship.model_dump(mode="python"),
                current_needs={
                    **{n.name: n.level for n in current_needs},
                    **{f"_satiation_{n.name}": n.satiation_response for n in current_needs},
                },
                current_affect={e.name: e.intensity for e in current_emotions},
            )
        )
        for emotion in current_emotions:
            emotion.intensity = max(
                0.0, min(1.0, emotion.intensity + preview.affect.get(emotion.name, 0.0))
            )
        for need in current_needs:
            need.level = max(0.0, min(1.0, need.level + preview.needs.get(need.name, 0.0)))
        appraisal = self._appraise(user_message, external_events or [])
        appraisal["interaction_act"] = preview.act
        stance_state = relationship.model_copy(
            update={
                "boundary_explicitness": preview.state.boundary_explicitness,
                "recent_acts": preview.state.recent_acts,
            }
        )
        persona_type = persona.manifest.persona_type
        policy = PersonaRetrievalPolicy(persona_type=persona_type, branch_id=effective_branch_id)
        visible = [
            memory
            for memory in memories
            if normalise_material_scope((memory.metadata or {}).get("material_scope"))
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
            current_time=current_time,
            persona_id=persona_id,
            session_id=session_id,
            identity_anchor=persona.manifest,
            current_run_mode=persona.manifest.run_mode.value,
            relevant_persona_facts=relevant_facts,
            relevant_historical_facts=[
                memory.content for memory in visible if memory.source_kind.startswith("historical")
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
            relationship_state=stance_state,
            relationship_stance=relationship_stance(
                stance_state, {n.name: n.level for n in current_needs}
            ),
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
            reflection_due=bool(session.metadata.get("reflection_due")),
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
        occurred_at: datetime | None = None,
        scene_events: list[dict[str, Any]] | None = None,
        source_turn_id: str | None = None,
        shared_user_turn: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_message = str(redact_secrets(user_message))
        persona_response = str(redact_secrets(persona_response))
        user_feedback = str(redact_secrets(user_feedback)) if user_feedback else None
        session = self._require_session(persona_id, session_id, allow_status={"active"})
        self._require_counterpart(session, counterpart_id)
        pending_interaction = session.metadata.pop("pending_interaction", {})
        counterpart_id = str(pending_interaction.get("counterpart_id") or counterpart_id)
        user_message = str(pending_interaction.get("message") or user_message)
        pending_time = parse_dt(session.metadata.pop("pending_scene_time", None))
        if pending_time is not None and occurred_at is not None and pending_time != occurred_at:
            raise ValueError("scene_time_changed_between_prepare_and_commit")
        occurred_at = occurred_at or pending_time or datetime.now(UTC)
        raw_user_message, raw_persona_response = user_message, persona_response
        normalized_user = normalize_turn(user_message)
        normalized_response = normalize_turn(persona_response, actor=persona_id)
        user_message = normalized_user.spoken_text
        persona_response = normalized_response.spoken_text
        branch_id = self._effective_branch_id(persona_id, session, None)
        normalized_state_patch = self._validate_state_patch(state_patch or {})
        used_memory_ids = used_memory_ids or []
        if used_memory_ids_extra:
            used_memory_ids.extend(used_memory_ids_extra)
        turn_id = source_turn_id or new_id("turn")
        try:
            self.database.conn.execute("BEGIN")
            self.database.conn.execute(
                "INSERT INTO session_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    session_id,
                    persona_id,
                    raw_user_message,
                    raw_persona_response,
                    dumps(used_memory_ids),
                    user_feedback,
                    dumps(
                        {
                            "occurred_at": occurred_at.isoformat(),
                            "user_channels": normalized_user.model_dump(mode="json"),
                            "persona_channels": normalized_response.model_dump(mode="json"),
                            "scene_events": scene_events or [],
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
                content=semantic_experience(
                    raw_user_message,
                    raw_persona_response,
                    counterpart=counterpart_id,
                    events=scene_events,
                ),
                occurred_at=occurred_at,
                memory_type=MemoryType.DIGITAL_EXPERIENCE,
                importance=0.65 if user_feedback else 0.5,
                source_kind="digital_experience",
                participants=[counterpart_id],
                branch_id=branch_id,
                metadata={
                    "retrieval_role": "event",
                    "semantic_experience_version": 1,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "counterpart_id": counterpart_id,
                    "branch_id": branch_id,
                    "visibility": "room_public"
                    if session.metadata.get("room_id") or counterpart_id.startswith("room:")
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
            events = list(redact_secrets(self._drain_pending_events(session)))
            before_relationship = self.relationships.get_relationship(
                persona_id, counterpart_id, branch_id
            )
            bond_needs = self.motivation.get_needs(persona_id, branch_id, now=occurred_at)
            bond_need_levels = {n.name: n.level for n in bond_needs}
            bond_need_levels.update(
                {f"_satiation_{n.name}": n.satiation_response for n in bond_needs}
            )
            bond_need_levels["_attachment_baseline"] = next(
                n.baseline for n in bond_needs if n.name == "attachment"
            )
            bond = appraise_bond(
                before_relationship,
                user_message,
                events,
                bond_need_levels,
                {
                    e.name: e.intensity
                    for e in self.affect.get_emotions(
                        persona_id, branch_id, now=occurred_at, commit=False
                    )
                },
                response=persona_response,
            )
            appraisal = self._appraise_commit(
                persona_id=persona_id,
                branch_id=branch_id,
                counterpart_id=counterpart_id,
                user_message=user_message,
                persona_response=persona_response,
                user_feedback=user_feedback,
                goal_completed=bool(goal_completed),
                external_events=events,
                now=occurred_at,
            )
            if bond.act != "neutral":
                appraisal.relationships = (
                    [] if not events or bond.act != "conversation" else appraisal.relationships
                )
                # Semantic acts take precedence over ambiguous emotion words in quoted replies.
                if bond.act != "conversation":
                    appraisal.affect = {}
                    appraisal.needs = {}
                self._apply_affect_delta(
                    persona_id,
                    branch_id,
                    session_id,
                    turn_id,
                    bond.affect,
                    "interaction_semantics",
                    now=occurred_at,
                    additive=True,
                )
                if bond.needs:
                    self._apply_need_delta(
                        persona_id,
                        branch_id,
                        session_id,
                        turn_id,
                        bond.needs,
                        "interaction_semantics",
                        now=occurred_at,
                    )
                appraisal.summary = (
                    f"{appraisal.summary}; "
                    f"{bond.state.relationship_kind.value}: {bond.state.trajectory}; "
                    f"trust {before_relationship.trust:.3f} -> {bond.state.trust:.3f}; "
                    f"affection {before_relationship.affection:.3f} -> {bond.state.affection:.3f}"
                )
                self.relationships._save(bond.state, branch_id, commit=False)
                self._insert_change_event(
                    persona_id,
                    branch_id,
                    "relationship_delta",
                    "relationship",
                    counterpart_id,
                    session_id,
                    turn_id,
                    {
                        "semantics": "interaction",
                        "message": "",
                        "events": [{"type": bond.act}],
                        "needs": bond_need_levels,
                        "before_state": before_relationship.model_dump(mode="json"),
                        "after_state": bond.state.model_dump(mode="json"),
                        "act": bond.act,
                    },
                )
                if bond.salient:
                    self._store_relationship_memory(
                        persona_id,
                        branch_id,
                        session_id,
                        turn_id,
                        counterpart_id,
                        bond.state,
                        bond.act,
                    )
            if appraisal.affect:
                self._apply_affect_delta(
                    persona_id,
                    branch_id,
                    session_id,
                    turn_id,
                    appraisal.affect,
                    "commit_turn appraisal",
                    now=occurred_at,
                )
            if appraisal.needs:
                self._apply_need_delta(
                    persona_id,
                    branch_id,
                    session_id,
                    turn_id,
                    appraisal.needs,
                    "commit_turn appraisal",
                    now=occurred_at,
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
                    persona_id,
                    session_id,
                    turn_id,
                    branch_id,
                    normalized_state_patch,
                    now=occurred_at,
                )
            self.motivation.update_needs(
                persona_id, {}, "scene_time_elapsed", branch_id, now=occurred_at, commit=False
            )
            session.metadata["last_scene_time"] = occurred_at.isoformat()
            session.metadata["reflection_due"] = (
                bond.salient
                or (
                    bond.state.meaningful_interactions > 0
                    and bond.state.meaningful_interactions % 12 == 0
                )
                or bool(session.metadata.get("reflection_due"))
            )
            session.metadata["branch_id"] = branch_id
            session.metadata.setdefault("counterpart_id", counterpart_id)
            self.database.conn.execute(
                "UPDATE sessions SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), dumps(session.metadata), session_id),
            )
            # Memory Architecture v2 / Phase 3: place this committed turn in the
            # Episode ledger, inside the same transaction as the turn itself.
            # Summarisation is a separate, asynchronous step.
            #
            # If the assignment itself fails the turn still commits: a persona
            # reply must never be lost to a bookkeeping bug.  The turn is then
            # UNASSIGNED, which is not the same as orphaned -- it shows up in
            # ``EpisodeService.coverage()['unassigned_pending_backfill']`` and
            # ``backfill()`` reclaims it.  The failure is logged, never silent.
            episode_report: dict[str, Any] | None = None
            if self.episodes is not None:
                try:
                    episode_report = self.episodes.assign_turn(
                        persona_id=persona_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        counterpart_id=counterpart_id,
                        branch_id=branch_id,
                        room_id=session.metadata.get("room_id"),
                        occurred_at=occurred_at,
                        user_message=user_message,
                        persona_response=persona_response,
                        shared_user_turn_id=(
                            str((shared_user_turn or {}).get("turn_id") or "") or None
                        ),
                        shared_user_text=str((shared_user_turn or {}).get("text") or ""),
                        shared_user_occurred_at=(
                            shared_user_turn or {}
                        ).get("occurred_at"),
                    )
                except Exception as exc:  # noqa: BLE001 - chat must not fail
                    _metadata_trace_logger.warning(
                        "MEMORY_EPISODE_ASSIGN_FAILED turn=%s session=%s error=%s:%s",
                        turn_id,
                        session_id,
                        type(exc).__name__,
                        str(exc)[:200],
                    )
                    episode_report = {
                        "turn_id": turn_id,
                        "session_id": session_id,
                        "action": "noop",
                        "error": f"assign_failed:{type(exc).__name__}",
                        "current_episode_id": None,
                        "pending": True,
                    }
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
            "relationship_state": self.relationships.get_relationship(
                persona_id, counterpart_id, branch_id
            ).model_dump(mode="json"),
            "reflection_due": bond.salient
            or (
                bond.state.meaningful_interactions > 0
                and bond.state.meaningful_interactions % 12 == 0
            ),
            "episode": episode_report,
            "episode_id": (episode_report or {}).get("current_episode_id"),
        }

    def end_session(self, session_id: str) -> bool:
        row = self.database.conn.execute(
            "SELECT metadata_json FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row:
            metadata = dict(loads(row["metadata_json"]))
            metadata["reflection_due"] = True
            self.database.conn.execute(
                "UPDATE sessions SET metadata_json=? WHERE id=?", (dumps(metadata), session_id)
            )
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
        # The raw turns a session owns are about to disappear (the FK cascade
        # removes session_turns).  Collect them first so the Episode layer can
        # apply its deletion semantics while we still know what to look for.
        turn_ids = [
            str(row["id"])
            for row in self.database.conn.execute(
                "SELECT id FROM session_turns WHERE session_id = ?", (session_id,)
            ).fetchall()
        ]
        self.database.conn.execute(
            "DELETE FROM sessions WHERE persona_id = ? AND id = ?", (persona_id, session_id)
        )
        if self.episodes is not None:
            # A5: never leave provenance silently dangling.  ``keep derived``
            # keeps the Episodes and stamps them unavailable; the default
            # contract deletes everything the session uniquely produced.
            self.episodes.purge_session(
                session_id,
                turn_ids=turn_ids,
                delete_episodes=bool(delete_derived_memories),
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
        goes as well: sessions, session turns, conversation-derived memories,
        and rooms that include this persona (including ``rooms.state_json``
        chat history).  Compiled persona content (sources, evidence,
        dimensions, versions) and research/seed memories are never touched:
        this is a state reset, not a persona deletion.
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

    def _reset_conversational_footprint(self, persona_id: str, branch_id: str) -> dict[str, Any]:
        """Delete sessions, turns, chat memories and rooms for one branch."""

        removed: dict[str, Any] = {}
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
                f"DELETE FROM sessions WHERE persona_id = ? AND id IN ({placeholders})",
                params,
            )
        else:
            removed["session_turns"] = 0
            removed["sessions"] = 0
        memory_ids = self._conversation_memory_ids(persona_id, branch_id)
        if memory_ids:
            placeholders = ",".join("?" for _ in memory_ids)
            id_params: tuple[object, ...] = tuple(memory_ids)
            removed["memories_fts"] = self._delete_count(
                f"DELETE FROM memories_fts WHERE memory_id IN ({placeholders})",
                id_params,
            )
            removed["memories"] = self._delete_count(
                f"DELETE FROM memories WHERE persona_id = ? AND id IN ({placeholders})",
                (persona_id, *memory_ids),
            )
        else:
            removed["memories_fts"] = 0
            removed["memories"] = 0
        room_ids = self._persona_room_ids(persona_id) if branch_id == "main" else []
        removed["rooms"] = self._delete_persona_rooms(room_ids) if room_ids else 0
        removed["room_ids"] = room_ids
        removed["room_transcripts"] = self._reset_room_transcripts(persona_id, branch_id)
        return removed

    def _conversation_memory_ids(self, persona_id: str, branch_id: str) -> list[str]:
        """Memories written by chat, not compile/research/seed identity."""

        keep_kinds = {
            "fictional_author_defined",
            "fictional_canon",
            "historical_self_report",
            "historical_third_party_report",
            "historical_inference",
            "seed",
        }
        ids: list[str] = []
        for row in self.database.conn.execute(
            "SELECT id, type, source_kind, metadata_json FROM memories WHERE persona_id = ?",
            (persona_id,),
        ):
            metadata = dict(loads(str(row["metadata_json"] or "{}")))
            row_branch = str(metadata.get("branch_id") or "main")
            if branch_id != "main" and row_branch != branch_id:
                continue
            source_kind = str(row["source_kind"] or "")
            if source_kind in keep_kinds:
                continue
            if metadata.get("artifact_id") and metadata.get("compile_task_id"):
                continue
            ids.append(str(row["id"]))
        return ids

    def _persona_room_ids(self, persona_id: str) -> list[str]:
        ids: list[str] = []
        for row in self.database.conn.execute("SELECT id, persona_ids_json FROM rooms"):
            raw = loads(str(row["persona_ids_json"] or "[]"))
            if isinstance(raw, list) and persona_id in raw:
                ids.append(str(row["id"]))
        return ids

    def _delete_persona_rooms(self, room_ids: list[str]) -> int:
        if not room_ids:
            return 0
        placeholders = ",".join("?" for _ in room_ids)
        params = tuple(room_ids)
        with contextlib.suppress(Exception):
            self.database.conn.execute(
                f"DELETE FROM room_scene_events WHERE room_id IN ({placeholders})",
                params,
            )
        with contextlib.suppress(Exception):
            self.database.conn.execute(
                f"DELETE FROM room_runs WHERE room_id IN ({placeholders})",
                params,
            )
        self.database.conn.execute(
            f"DELETE FROM room_transcripts WHERE room_id IN ({placeholders})",
            params,
        )
        return self._delete_count(
            f"DELETE FROM rooms WHERE id IN ({placeholders})",
            params,
        )

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

    def run_reflection(
        self,
        persona_id: str,
        limit: int = 12,
        *,
        branch_id: str | None = None,
        artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if artifact is not None:
            return self.commit_reflection(persona_id, artifact, branch_id=branch_id)
        prepared = self.prepare_reflection(persona_id, branch_id=branch_id, limit=limit)
        if self.semantic_reflector is not None:
            return self.commit_reflection(
                persona_id, self.semantic_reflector(prepared), branch_id=prepared["branch_id"]
            )
        rows = list(reversed(prepared["recent_important_dialogue"]))
        if not rows:
            return {"persona_id": persona_id, "summary": "", "memory_id": None, "turn_count": 0}
        summary = "\n".join(
            f"User: {row['user_message']} | Persona: {row['persona_response']}" for row in rows
        )
        memory = self.memories.add_memory(
            persona_id,
            content=f"Reflection summary:\n{summary}",
            memory_type=MemoryType.SEMANTIC,
            importance=0.45,
            source_kind="reflection_summary",
            branch_id=prepared["branch_id"],
            metadata={
                "reflection_type": "extractive_fallback",
                "reflection_artifact_id": new_id("refl"),
                "persona_id": persona_id,
                "branch_id": prepared["branch_id"],
                "supporting_turn_ids": [row["id"] for row in rows],
                "supporting_session_ids": prepared["session_ids"],
                "visibility": "persona_private",
            },
        )
        for row in rows:
            self._insert_lineage(
                persona_id,
                child_type="memory",
                child_id=memory.id,
                parent_type="session_turn",
                parent_id=row["id"],
                relation="reflection_from",
            )
        self.database.conn.commit()
        return {
            "persona_id": persona_id,
            "summary": summary,
            "memory_id": memory.id,
            "turn_count": len(rows),
            "reflection_type": "extractive_fallback",
            "semantic_reflection_required": True,
            "prepared_reflection": prepared,
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
                    "user_message": normalize_turn(row["user_message"]).spoken_text,
                    "persona_response": normalize_turn(
                        row["persona_response"], actor="persona"
                    ).spoken_text,
                    "scene_events": loads(row["context_json"]).get("scene_events", []),
                    "occurred_at": loads(row["context_json"]).get("occurred_at"),
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
                "All *_deltas are signed additive changes in [-1,1], never absolute targets.",
                "Identify habits, repair, unresolved conflicts and self narrative changes "
                "with supporting turns.",
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
        prior_artifacts = self.database.conn.execute(
            "SELECT data_json FROM change_events WHERE persona_id=? AND branch_id=?",
            (persona_id, effective_branch_id),
        ).fetchall()
        if any(
            loads(row["data_json"]).get("reflection_artifact_id")
            == validated.reflection_artifact_id
            for row in prior_artifacts
        ):
            raise CodedError("invalid_reflection", "reflection_artifact_already_committed")
        counterparts = {
            str(loads(row["context_json"]).get("counterpart_id", "user")) for row in turn_rows
        }
        for memory_candidate in validated.memory_candidates:
            if (
                memory_candidate.counterpart_id
                and memory_candidate.counterpart_id not in counterparts
            ):
                raise CodedError("invalid_reflection", "memory_counterpart_not_supported")
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
                    participants=[candidate.counterpart_id]
                    if isinstance(candidate, ReflectionMemoryCandidate) and candidate.counterpart_id
                    else [],
                    branch_id=effective_branch_id,
                    metadata={
                        "counterpart_id": getattr(candidate, "counterpart_id", None),
                        "relationship_memory": bool(getattr(candidate, "counterpart_id", None)),
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
            self._insert_change_event(
                persona_id,
                effective_branch_id,
                "reflection_committed",
                "runtime",
                validated.reflection_artifact_id,
                supporting_session_ids[0],
                validated.supporting_turn_ids[0],
                {
                    "reflection_artifact_id": validated.reflection_artifact_id,
                    "supporting_turn_ids": validated.supporting_turn_ids,
                    "supporting_session_ids": supporting_session_ids,
                },
                support_pairs=support_pairs,
            )
            for supporting_session in supporting_session_ids:
                row = self.database.conn.execute(
                    "SELECT metadata_json FROM sessions WHERE id=?", (supporting_session,)
                ).fetchone()
                metadata = dict(loads(row["metadata_json"]))
                metadata["reflection_due"] = False
                self.database.conn.execute(
                    "UPDATE sessions SET metadata_json=? WHERE id=?",
                    (dumps(metadata), supporting_session),
                )
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
            self.relationships.apply_deltas(
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
                    "semantics": "additive",
                    "reflection_artifact_id": artifact.reflection_artifact_id,
                    "supporting_turn_ids": artifact.supporting_turn_ids,
                    "supporting_session_ids": supporting_session_ids,
                },
                support_pairs=support_pairs,
            )
        if artifact.affect_deltas:
            self.affect.apply_deltas(
                persona_id,
                artifact.affect_deltas,
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
                    "semantics": "additive",
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
                dumps(redact_secrets(data)),
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
        *,
        additive: bool = False,
        now: datetime | None = None,
    ) -> None:
        before = {
            state.name: state.model_dump(mode="json")
            for state in self.affect.get_emotions(persona_id, branch_id, now=now, commit=False)
            if state.name in delta
        }
        apply = self.affect.apply_deltas if additive else self.affect.update_emotions
        states = apply(persona_id, delta, reason, branch_id=branch_id, now=now, commit=False)
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
                "semantics": "additive" if additive else "floor",
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
        *,
        now: datetime | None = None,
    ) -> None:
        before = {
            state.name: state.model_dump(mode="json")
            for state in self.motivation.get_needs(persona_id, branch_id, now=now)
            if state.name in delta
        }
        states = self.motivation.update_needs(
            persona_id, delta, reason, branch_id=branch_id, now=now, commit=False
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
        *,
        additive: bool = False,
    ) -> None:
        before = self.relationships.get_relationship(
            persona_id, counterpart_id, branch_id=branch_id
        ).model_dump(mode="json")
        apply = (
            self.relationships.apply_deltas if additive else self.relationships.update_relationship
        )
        after = apply(
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
                "semantics": "additive" if additive else "absolute",
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
            if event_type == "runtime_profile":
                for name, value in data.get("need_baselines", {}).items():
                    if name in affected_needs:
                        self.database.conn.execute(
                            "UPDATE needs SET baseline=? WHERE persona_id=? "
                            "AND branch_id=? AND name=?",
                            (value, persona_id, branch_id, name),
                        )
            if event_type == "runtime_seed":
                for name in affected_affects:
                    value = data.get("emotion_baselines", {}).get(name, 0.0)
                    self.affect._save(
                        persona_id,
                        branch_id,
                        AffectState.model_validate(data["initial_affect_states"][name])
                        if name in data.get("initial_affect_states", {})
                        else AffectState(
                            name=name,
                            baseline=value,
                            intensity=value,
                            decay_rate=data.get("decay_rate", 0.08),
                        ),
                    )
                for name in affected_needs:
                    value = data.get("need_baselines", {}).get(
                        name, NEED_DEFAULT_BASELINES.get(name, 0.5)
                    )
                    self.motivation._save(
                        persona_id,
                        branch_id,
                        NeedState.model_validate(data["initial_need_states"][name])
                        if name in data.get("initial_need_states", {})
                        else NeedState(name=name, baseline=value, level=value),
                    )
            if (
                event_type == "relationship_prior"
                and str(row["target_id"]) in affected_relationships
            ):
                self.relationships._save(
                    RelationshipState.model_validate(data["state"]), branch_id, commit=False
                )
            if event_type == "relationship_delta" and data.get("semantics") == "interaction":
                counterpart = str(row["target_id"])
                if counterpart in affected_relationships:
                    prior = self.relationships.get_relationship(persona_id, counterpart, branch_id)
                    result = appraise_bond(
                        prior, data["message"], data.get("events", []), data.get("needs", {}), {}
                    )
                    self.relationships._save(result.state, branch_id, commit=False)
                continue
            if (
                event_type == "relationship_delta"
                and str(row["target_id"]) in affected_relationships
            ):
                changes = {
                    str(key): float(value)
                    for key, value in dict(delta.get("changes", delta)).items()
                    if isinstance(value, int | float)
                }
                apply_relationship = (
                    self.relationships.apply_deltas
                    if data.get("semantics") == "additive"
                    else self.relationships.update_relationship
                )
                apply_relationship(
                    persona_id,
                    str(row["target_id"]),
                    changes,
                    "runtime_replay",
                    branch_id=branch_id,
                    commit=False,
                )
            elif event_type == "affect_delta":
                changes = {
                    str(key): float(value)
                    if data.get("semantics") == "additive"
                    else min(
                        1.0, float(value) + (0.0 if data.get("semantics") == "floor" else 0.01)
                    )
                    for key, value in delta.items()
                    if str(key) in affected_affects and isinstance(value, int | float)
                }
                if changes:
                    apply_affect = (
                        self.affect.apply_deltas
                        if data.get("semantics") == "additive"
                        else self.affect.update_emotions
                    )
                    apply_affect(
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
        seed = build_seed(self.compiled_context.runtime_seed_components(persona_id))
        seeded = self.database.conn.execute(
            "SELECT 1 FROM change_events WHERE persona_id=? AND branch_id=? "
            "AND event_type='runtime_seed'",
            (persona_id, branch_id),
        ).fetchone()
        if not seeded:
            for name in EMOTION_NAMES:
                exists = self.database.conn.execute(
                    "SELECT 1 FROM affect_states WHERE persona_id=? AND branch_id=? AND name=?",
                    (persona_id, branch_id, name),
                ).fetchone()
                if not exists:
                    value = seed.emotion_baselines.get(name, 0.0)
                    self.affect._save(
                        persona_id,
                        branch_id,
                        AffectState(
                            name=name, baseline=value, intensity=value, decay_rate=seed.decay_rate
                        ),
                    )
            for name in NEED_NAMES:
                exists = self.database.conn.execute(
                    "SELECT 1 FROM needs WHERE persona_id=? AND branch_id=? AND name=?",
                    (persona_id, branch_id, name),
                ).fetchone()
                if not exists:
                    value = seed.need_baselines.get(name, NEED_DEFAULT_BASELINES[name])
                    self.motivation._save(
                        persona_id, branch_id, NeedState(name=name, baseline=value, level=value)
                    )
            # Adopt missing priors on legacy runtimes without replacing lived intensities/levels.
            for name, baseline in seed.emotion_baselines.items():
                self.database.conn.execute(
                    "UPDATE affect_states SET baseline=?, decay_rate=? "
                    "WHERE persona_id=? AND branch_id=? AND name=? AND baseline=0",
                    (baseline, seed.decay_rate, persona_id, branch_id, name),
                )
            for name, baseline in seed.need_baselines.items():
                self.database.conn.execute(
                    "UPDATE needs SET baseline=? WHERE persona_id=? AND branch_id=? "
                    "AND name=? AND baseline=0.5",
                    (baseline, persona_id, branch_id, name),
                )
            self._insert_change_event(
                persona_id,
                branch_id,
                "runtime_seed",
                "runtime",
                branch_id,
                None,
                None,
                {
                    **seed.model_dump(mode="json"),
                    "initial_affect_states": {
                        e.name: e.model_dump(mode="json")
                        for e in self.affect._load(persona_id, branch_id)
                    },
                    "initial_need_states": {
                        n.name: n.model_dump(mode="json")
                        for n in self.motivation.get_needs(persona_id, branch_id)
                    },
                },
            )
            self.database.conn.commit()
        else:
            profile_row = self.database.conn.execute(
                "SELECT data_json FROM change_events WHERE persona_id=? AND branch_id=? "
                "AND event_type IN ('runtime_seed', 'runtime_profile') "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (persona_id, branch_id),
            ).fetchone()
            previous = loads(profile_row["data_json"])
            profile = seed.model_dump(mode="json")
            fields = ("version", "need_baselines", "need_rebound_rates", "need_satiation_responses")
            if any(previous.get(key) != profile.get(key) for key in fields):
                # Materialise elapsed homeostasis before changing its target; preserve lived levels.
                for state in self.motivation.get_needs(persona_id, branch_id):
                    if state.name in seed.need_baselines:
                        state.baseline = seed.need_baselines[state.name]
                        self.motivation._save(persona_id, branch_id, state)
                self._insert_change_event(
                    persona_id,
                    branch_id,
                    "runtime_profile",
                    "runtime",
                    branch_id,
                    None,
                    None,
                    profile,
                )
                self.database.conn.commit()
        path = self._runtime_state_path(persona_id, branch_id)
        if not path.exists():
            self._refresh_runtime_state(persona_id, branch_id)

    def _store_relationship_memory(
        self,
        persona_id: str,
        branch_id: str,
        session_id: str,
        turn_id: str,
        counterpart: str,
        state: RelationshipState,
        act: str,
    ) -> None:
        rows = self.database.conn.execute(
            "SELECT id, session_id, context_json FROM session_turns "
            "WHERE persona_id=? ORDER BY created_at DESC",
            (persona_id,),
        ).fetchall()
        supports = []
        for row in rows:
            context = loads(row["context_json"])
            if (
                context.get("branch_id", "main") == branch_id
                and context.get("counterpart_id", "user") == counterpart
            ):
                supports.append((str(row["session_id"]), str(row["id"])))
            if len(supports) >= 12:
                break
        session_metadata = loads(
            self.database.conn.execute(
                "SELECT metadata_json FROM sessions WHERE id=?", (session_id,)
            ).fetchone()["metadata_json"]
        )
        memory = self.memories.add_memory(
            persona_id,
            content=(
                f"With {counterpart}, an interaction of {act} contributed to "
                f"our {state.relationship_kind.value} relationship: {state.trajectory}. "
                f"Recent recurring experiences: {', '.join(dict.fromkeys(state.recent_acts))}."
            ),
            memory_type=MemoryType.EMOTIONAL,
            importance=0.7,
            source_kind="digital_experience",
            participants=[counterpart],
            branch_id=branch_id,
            metadata={
                "session_id": session_id,
                "turn_id": turn_id,
                "counterpart_id": counterpart,
                "branch_id": branch_id,
                "supporting_turn_ids": [item[1] for item in supports],
                "supporting_session_ids": sorted({item[0] for item in supports}),
                "visibility": "room_public"
                if session_metadata.get("room_id") or counterpart.startswith("room:")
                else "private_session",
                "relationship_memory": True,
            },
            commit=False,
        )
        for _, supporting_turn in supports:
            self._insert_lineage(
                persona_id,
                child_type="memory",
                child_id=memory.id,
                parent_type="session_turn",
                parent_id=supporting_turn,
                relation="relationship_from",
            )

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
            f"SELECT context_tags_json FROM persona_evidence_units WHERE id IN ({placeholders})",
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
        now: datetime | None = None,
    ) -> AppraisalResult:
        """Appraise one committed turn against the persona's *current* state.

        Current values are passed in so the service can cap per-turn movement
        instead of absolute position -- the difference between "trust drifted
        up 4 points" and "trust teleported to 90%".
        """

        emotions = self.affect.get_emotions(persona_id, branch_id, now=now, commit=False)
        needs = self.motivation.get_needs(persona_id, branch_id, now=now)
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
            if sum(key in delta for key in ("set", "changes", "delta")) > 1:
                raise CodedError("invalid_state_patch", "relationship_set_conflicts_with_changes")
            changes = delta.get("delta", delta.get("set", delta.get("changes")))
            if not isinstance(changes, dict) or not changes:
                raise CodedError("invalid_state_patch", "relationships")
            try:
                normalized_relationships.append(
                    {
                        "counterpart_id": str(delta["counterpart_id"]),
                        ("delta" if "delta" in delta else "set"): _validate_numeric_map(
                            changes,
                            allowed_keys=RELATIONSHIP_FIELDS,
                            field_name="relationships",
                            minimum=-1 if "delta" in delta else 0,
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
        *,
        now: datetime | None = None,
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
                now=now,
            )
        if affect_delta := state_patch.get("affect_delta"):
            self._apply_affect_delta(
                persona_id,
                branch_id,
                session_id,
                turn_id,
                dict(affect_delta),
                "state_patch.affect_delta",
                now=now,
                additive=True,
            )
        if needs := state_patch.get("needs"):
            self._apply_need_delta(
                persona_id,
                branch_id,
                session_id,
                turn_id,
                dict(needs),
                "state_patch",
                now=now,
            )
        for entry in state_patch.get("relationships", []) or []:
            self._apply_relationship_delta(
                persona_id,
                str(entry["counterpart_id"]),
                dict(entry.get("delta", entry.get("set", {}))),
                branch_id,
                session_id,
                turn_id,
                "state_patch",
                additive="delta" in entry,
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
