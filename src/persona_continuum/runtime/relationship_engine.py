from __future__ import annotations

from datetime import UTC, datetime

from persona_continuum.application._utils import dumps, loads
from persona_continuum.domain.relationship import RelationshipState
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
