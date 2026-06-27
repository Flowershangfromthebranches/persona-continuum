from __future__ import annotations

import math
from datetime import UTC, datetime

from persona_continuum.application._utils import dt, dumps, loads, parse_dt
from persona_continuum.domain.affect import EMOTION_NAMES, AffectState
from persona_continuum.security.validation import clamp
from persona_continuum.storage.database import Database


class AffectEngine:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_emotions(
        self,
        persona_id: str,
        branch_id: str = "main",
        now: datetime | None = None,
        *,
        commit: bool = True,
    ) -> list[AffectState]:
        now = now or datetime.now(UTC)
        states = {state.name: state for state in self._load(persona_id, branch_id)}
        changed = False
        for name in EMOTION_NAMES:
            state = states.get(name, AffectState(name=name, updated_at=now))
            decayed = self._decay(state, now)
            states[name] = decayed
            changed = True
        if changed:
            for state in states.values():
                self._save(persona_id, branch_id, state)
            if commit:
                self.database.conn.commit()
        return list(states.values())

    def update_emotions(
        self,
        persona_id: str,
        observations: dict[str, float],
        reason: str,
        branch_id: str = "main",
        now: datetime | None = None,
        *,
        commit: bool = True,
    ) -> list[AffectState]:
        now = now or datetime.now(UTC)
        states = {
            state.name: state
            for state in self.get_emotions(persona_id, branch_id, now=now, commit=False)
        }
        for name, amount in observations.items():
            if name not in EMOTION_NAMES:
                continue
            state = states[name]
            state.intensity = clamp(max(state.intensity, amount))
            state.updated_at = now
            state.triggers.append(reason)
            state.confidence = clamp(max(state.confidence, 0.65))
            self._save(persona_id, branch_id, state)
        if commit:
            self.database.conn.commit()
        return list(states.values())

    def _decay(self, state: AffectState, now: datetime) -> AffectState:
        elapsed_hours = max(0.0, (now - state.updated_at).total_seconds() / 3600)
        if elapsed_hours == 0:
            return state
        retained = math.exp(-state.decay_rate * elapsed_hours)
        state.intensity = clamp(state.baseline + (state.intensity - state.baseline) * retained)
        state.updated_at = now
        return state

    def _load(self, persona_id: str, branch_id: str) -> list[AffectState]:
        rows = self.database.conn.execute(
            "SELECT * FROM affect_states WHERE persona_id = ? AND branch_id = ?",
            (persona_id, branch_id),
        ).fetchall()
        return [
            AffectState(
                name=str(row["name"]),
                kind=str(row["kind"]),
                intensity=float(row["intensity"]),
                baseline=float(row["baseline"]),
                decay_rate=float(row["decay_rate"]),
                updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
                triggers=list(loads(row["triggers_json"])),
                confidence=float(row["confidence"]),
            )
            for row in rows
        ]

    def _save(self, persona_id: str, branch_id: str, state: AffectState) -> None:
        self.database.conn.execute(
            """
            INSERT OR REPLACE INTO affect_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                persona_id,
                branch_id,
                state.name,
                state.kind,
                state.intensity,
                state.baseline,
                state.decay_rate,
                dt(state.updated_at),
                dumps(state.triggers),
                state.confidence,
            ),
        )
