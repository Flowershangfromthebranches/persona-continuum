from __future__ import annotations

from datetime import UTC, datetime

from persona_continuum.application._utils import dt, dumps, loads, parse_dt
from persona_continuum.domain.affect import NEED_NAMES, NeedState
from persona_continuum.security.validation import clamp
from persona_continuum.storage.database import Database


class MotivationEngine:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_needs(self, persona_id: str, branch_id: str = "main") -> list[NeedState]:
        rows = self.database.conn.execute(
            "SELECT * FROM needs WHERE persona_id = ? AND branch_id = ?",
            (persona_id, branch_id),
        ).fetchall()
        states = {
            str(row["name"]): NeedState(
                name=str(row["name"]),
                level=float(row["level"]),
                baseline=float(row["baseline"]),
                updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
                confidence=float(row["confidence"]),
                reasons=list(loads(row["reasons_json"])),
            )
            for row in rows
        }
        for name in NEED_NAMES:
            states.setdefault(name, NeedState(name=name))
        return list(states.values())

    def update_needs(
        self,
        persona_id: str,
        observations: dict[str, float],
        reason: str,
        branch_id: str = "main",
        *,
        commit: bool = True,
    ) -> list[NeedState]:
        states = {state.name: state for state in self.get_needs(persona_id, branch_id)}
        for name, delta in observations.items():
            if name not in NEED_NAMES:
                continue
            state = states[name]
            state.level = clamp(state.level + delta)
            state.updated_at = datetime.now(UTC)
            state.reasons.append(reason)
            self._save(persona_id, branch_id, state)
        if commit:
            self.database.conn.commit()
        return list(states.values())

    def _save(self, persona_id: str, branch_id: str, state: NeedState) -> None:
        self.database.conn.execute(
            "INSERT OR REPLACE INTO needs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                persona_id,
                branch_id,
                state.name,
                state.level,
                state.baseline,
                dt(state.updated_at),
                state.confidence,
                dumps(state.reasons),
            ),
        )
