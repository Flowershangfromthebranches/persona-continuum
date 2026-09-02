from __future__ import annotations

import copy
from typing import Any

from persona_continuum.world.models import TimelineEvent


class EventEngine:
    """Records causal timeline events and maintains explicit causal chain dependency graphs."""

    def __init__(self) -> None:
        self._events: dict[str, TimelineEvent] = {}

    def emit_event(
        self,
        event_time: str,
        cause: str,
        effect: str,
        actors: list[str] | None = None,
        parent_event_ids: list[str] | None = None,
        confidence: float = 1.0,
        data: dict[str, Any] | None = None,
    ) -> TimelineEvent:
        causal_chain = list(parent_event_ids or [])
        # Recursively include ancestor event IDs in causal chain
        all_ancestors: set[str] = set(causal_chain)
        for pid in causal_chain:
            parent_ev = self._events.get(pid)
            if parent_ev:
                all_ancestors.update(parent_ev.causal_chain)

        ev = TimelineEvent(
            event_time=event_time,
            actors=list(actors or []),
            cause=cause,
            effect=effect,
            causal_chain=sorted(all_ancestors),
            confidence=confidence,
            data=data or {},
        )
        self._events[ev.id] = ev
        return copy.deepcopy(ev)

    def get_event(self, event_id: str) -> TimelineEvent | None:
        ev = self._events.get(event_id)
        return copy.deepcopy(ev) if ev else None

    def list_events(self) -> list[TimelineEvent]:
        return [copy.deepcopy(e) for e in sorted(self._events.values(), key=lambda x: x.event_time)]

    def get_causal_chain_events(self, event_id: str) -> list[TimelineEvent]:
        ev = self._events.get(event_id)
        if not ev:
            return []
        chain = [self._events[pid] for pid in ev.causal_chain if pid in self._events]
        chain.append(ev)
        return [copy.deepcopy(e) for e in sorted(chain, key=lambda x: x.event_time)]
