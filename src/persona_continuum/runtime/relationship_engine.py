from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from persona_continuum.application._utils import dumps, loads
from persona_continuum.domain.relationship import RelationshipKind, RelationshipState
from persona_continuum.runtime.bond_dynamics import appraise_bond, initial_state
from persona_continuum.security.validation import clamp
from persona_continuum.storage.database import Database

RELATIONSHIP_FIELDS = {
    "familiarity",
    "trust",
    "affection",
    "respect",
    "dependence",
    "resentment",
    "jealousy",
    "perceived_threat",
    "unresolved_conflict",
}
CLASSIFICATION_FIELDS = {
    "relationship_kind",
    "bond_stage",
    "trajectory",
    "relationship_prior",
}


class RelationshipEngine:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_relationship(
        self, persona_id: str, counterpart: str, branch_id: str = "main"
    ) -> RelationshipState:
        row = self.database.conn.execute(
            """
            SELECT state_json FROM relationships
            WHERE persona_id = ? AND branch_id = ? AND counterpart = ?
            """,
            (persona_id, branch_id, counterpart),
        ).fetchone()
        if row is None:
            return RelationshipState(persona_id=persona_id, counterpart=counterpart)
        return RelationshipState.model_validate(loads(row["state_json"]))

    def update_relationship(
        self,
        persona_id: str,
        counterpart: str,
        changes: dict[str, float],
        reason: str,
        branch_id: str = "main",
        *,
        commit: bool = True,
    ) -> RelationshipState:
        state = self.get_relationship(persona_id, counterpart, branch_id)
        for field, value in changes.items():
            if field in RELATIONSHIP_FIELDS:
                setattr(state, field, clamp(value))
        state.updated_at = datetime.now(UTC)
        state.reasons.append(reason)
        self._save(state, branch_id, commit=commit)
        return state

    def initialize(
        self,
        persona_id: str,
        counterpart: str,
        prior: dict[str, Any],
        branch_id: str = "main",
        *,
        commit: bool = True,
    ) -> RelationshipState:
        from persona_continuum.runtime.bond_dynamics import initial_state

        row = self.database.conn.execute(
            "SELECT 1 FROM relationships WHERE persona_id=? AND branch_id=? AND counterpart=?",
            (persona_id, branch_id, counterpart),
        ).fetchone()
        if row:
            return self.get_relationship(persona_id, counterpart, branch_id)
        state = initial_state(persona_id, counterpart, prior)
        self._save(state, branch_id, commit=commit)
        return state

    def apply_deltas(
        self,
        persona_id: str,
        counterpart: str,
        deltas: dict[str, float],
        reason: str,
        branch_id: str = "main",
        *,
        commit: bool = True,
    ) -> RelationshipState:
        state = self.get_relationship(persona_id, counterpart, branch_id)
        return self.update_relationship(
            persona_id,
            counterpart,
            {
                key: getattr(state, key) + value
                for key, value in deltas.items()
                if key in RELATIONSHIP_FIELDS
            },
            reason,
            branch_id,
            commit=commit,
        )

    def patch_fields(
        self,
        persona_id: str,
        counterpart: str,
        fields: dict[str, Any],
        reason: str,
        branch_id: str = "main",
        *,
        commit: bool = True,
    ) -> RelationshipState:
        """Patch classification (and optional numeric) fields without replacing history."""
        state = self.get_relationship(persona_id, counterpart, branch_id)
        for field, value in fields.items():
            if field == "relationship_kind":
                state.relationship_kind = RelationshipKind(value)
            elif field in CLASSIFICATION_FIELDS:
                setattr(state, field, value)
            elif field in RELATIONSHIP_FIELDS:
                setattr(state, field, clamp(value))
        state.updated_at = datetime.now(UTC)
        state.reasons.append(reason)
        self._save(state, branch_id, commit=commit)
        return state

    def replay_interactions(
        self,
        persona_id: str,
        counterpart: str,
        prior: dict[str, Any],
        turns: list[tuple[str, str, str]],
        needs: dict[str, float],
        emotions: dict[str, float],
        reason: str,
        branch_id: str = "main",
        *,
        commit: bool = True,
    ) -> tuple[RelationshipState, RelationshipState, list[str]]:
        """Replay messages from a corrected relationship prior.

        ``turns`` is ``(turn_id, user_message, persona_response)``.
        """
        before = self.get_relationship(persona_id, counterpart, branch_id)
        state = initial_state(persona_id, counterpart, prior)
        supporting: list[str] = []
        for turn_id, user_message, persona_response in turns:
            bond = appraise_bond(
                state, user_message, [], needs, emotions, response=persona_response
            )
            state = bond.state
            supporting.append(turn_id)
        state.reasons.append(reason)
        self._save(state, branch_id, commit=commit)
        return before, state, supporting

    def list_relationships(
        self, persona_id: str, branch_id: str = "main"
    ) -> list[RelationshipState]:
        rows = self.database.conn.execute(
            "SELECT state_json FROM relationships WHERE persona_id = ? AND branch_id = ?",
            (persona_id, branch_id),
        ).fetchall()
        return [RelationshipState.model_validate(loads(row["state_json"])) for row in rows]

    def _save(self, state: RelationshipState, branch_id: str, *, commit: bool) -> None:
        self.database.conn.execute(
            "INSERT OR REPLACE INTO relationships VALUES (?, ?, ?, ?, ?)",
            (
                state.persona_id,
                branch_id,
                state.counterpart,
                dumps(state.model_dump()),
                state.updated_at.isoformat(),
            ),
        )
        if commit:
            self.database.conn.commit()
