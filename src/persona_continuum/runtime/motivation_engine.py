from __future__ import annotations

import math
from datetime import UTC, datetime

from persona_continuum.application._utils import dt, dumps, loads, parse_dt
from persona_continuum.domain.affect import NEED_DEFAULT_BASELINES, NEED_NAMES, NeedState
from persona_continuum.security.validation import clamp
from persona_continuum.storage.database import Database


class MotivationEngine:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_needs(
        self, persona_id: str, branch_id: str = "main", now: datetime | None = None
    ) -> list[NeedState]:
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
        # Kinetics are immutable seed configuration, separate from lived need rows.
        seed_row = self.database.conn.execute(
            "SELECT data_json FROM change_events WHERE persona_id=? AND branch_id=? "
            "AND event_type IN ('runtime_seed', 'runtime_profile') "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (persona_id, branch_id),
        ).fetchone()
        profile = loads(seed_row["data_json"]) if seed_row else {}
        now = now or datetime.now(UTC)
        for state in states.values():
            state.rebound_rate = profile.get("need_rebound_rates", {}).get(state.name, 0.12)
            state.satiation_response = profile.get("need_satiation_responses", {}).get(
                state.name, 0.25
            )
            self._hydrate_provenance(state)
            hours = max(0.0, (now - state.updated_at).total_seconds() / 3600)
            # Unmet drives rebound toward their persona baseline after satisfaction.
            state.level = clamp(
                state.baseline
                + (state.level - state.baseline) * math.exp(-state.rebound_rate * hours)
            )
            state.updated_at = max(state.updated_at, now)
        for name in NEED_NAMES:
            states.setdefault(
                name,
                NeedState(
                    name=name,
                    updated_at=now,
                    baseline=NEED_DEFAULT_BASELINES[name],
                    level=NEED_DEFAULT_BASELINES[name],
                ),
            )
        return list(states.values())

    def update_needs(
        self,
        persona_id: str,
        observations: dict[str, float],
        reason: str,
        branch_id: str = "main",
        *,
        commit: bool = True,
        now: datetime | None = None,
    ) -> list[NeedState]:
        now = now or datetime.now(UTC)
        states = {state.name: state for state in self.get_needs(persona_id, branch_id, now=now)}
        for name, delta in observations.items():
            if name not in NEED_NAMES:
                continue
            state = states[name]
            state.level = clamp(state.level + delta)
            state.updated_at = max(state.updated_at, now)
            state.reasons.append(reason)
            state.last_change_kind = "delta"
            state.last_change_reason = reason
            if (
                name in {"intimacy", "touch_closeness"}
                and delta < 0
                and "interaction_semantics" in reason
            ):
                stamp = dt(state.updated_at) or ""
                state.last_satiated_at = state.updated_at
                state.reasons.append(f"satiated_at={stamp}")
        for state in states.values():
            self._save(persona_id, branch_id, state)
        if commit:
            self.database.conn.commit()
        return list(states.values())

    def replace_need_level(
        self,
        persona_id: str,
        name: str,
        level: float,
        reason: str,
        branch_id: str = "main",
        *,
        kind: str = "runtime_repair",
        commit: bool = True,
        now: datetime | None = None,
    ) -> NeedState:
        """Write a reconstructed current level without applying homeostasis first."""
        if name not in NEED_NAMES:
            raise ValueError(name)
        row = self.database.conn.execute(
            "SELECT * FROM needs WHERE persona_id=? AND branch_id=? AND name=?",
            (persona_id, branch_id, name),
        ).fetchone()
        if row is None:
            state = NeedState(name=name, level=clamp(level), baseline=NEED_DEFAULT_BASELINES[name])
        else:
            state = NeedState(
                name=str(row["name"]),
                level=float(row["level"]),
                baseline=float(row["baseline"]),
                updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
                confidence=float(row["confidence"]),
                reasons=list(loads(row["reasons_json"])),
            )
        state.level = clamp(level)
        state.updated_at = max(state.updated_at, now or datetime.now(UTC))
        state.last_change_kind = kind
        state.last_change_reason = reason
        state.reasons.append(f"{kind}:{reason}")
        self._save(persona_id, branch_id, state)
        if commit:
            self.database.conn.commit()
        return state

    @staticmethod
    def _hydrate_provenance(state: NeedState) -> None:
        if state.reasons:
            state.last_change_reason = state.reasons[-1]
        for token in reversed(state.reasons):
            if token.startswith("satiated_at=") and state.last_satiated_at is None:
                state.last_satiated_at = parse_dt(token.split("=", 1)[1])
            elif token.startswith("satiation_event=") and state.last_satiation_event_id is None:
                state.last_satiation_event_id = token.split("=", 1)[1]
            elif token.startswith("runtime_repair:") and state.last_change_kind is None:
                state.last_change_kind = "runtime_repair"

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
