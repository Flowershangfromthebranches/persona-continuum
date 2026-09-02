from __future__ import annotations

import re
from datetime import datetime, timedelta

from persona_continuum.world.models import ActorAction, TimelineEvent, WorldState


class AdaptiveWorldClock:
    """Dynamically steps simulation time based on activity volatility, deadlines, and conflicts.

    - Quiet execution phases: advances by year, quarter, or month.
    - Critical inflection points (releases, negotiations, clashes): advances by day or hour.
    """

    def compute_next_timestep(
        self,
        current_time_str: str,
        state: WorldState,
        pending_actions: list[ActorAction] | None = None,
        recent_events: list[TimelineEvent] | None = None,
    ) -> tuple[str, str]:
        current_dt = self._parse_datetime(current_time_str)
        actions = pending_actions or []
        events = recent_events or []

        # 1. Determine urgency / volatility score (0.0 to 1.0)
        volatility = 0.0

        # Pending high-stakes actions
        for act in actions:
            if act.action_type.value in {"negotiate", "launch_project", "communicate"}:
                volatility += 0.35

        # Critical project milestones
        for proj in state.active_projects.values():
            prog = proj.get("progress_percent", 0)
            if prog >= 80 and prog < 100:
                volatility += 0.4
            elif prog < 80:
                volatility += 0.1

        # Recent intense events
        if events and len(events) >= 3:
            volatility += 0.25

        volatility = min(1.0, volatility)

        # 2. Select step delta based on volatility
        if volatility >= 0.7:
            # Critical inflection point: advance by day
            step_unit = "day"
            next_dt = current_dt + timedelta(days=1)
        elif volatility >= 0.4:
            # Active development / negotiation: advance by month
            step_unit = "month"
            next_dt = self._add_months(current_dt, 1)
        elif volatility >= 0.2:
            # Standard progress: advance by quarter (3 months)
            step_unit = "quarter"
            next_dt = self._add_months(current_dt, 3)
        else:
            # Quiet baseline period: advance by 1 year
            step_unit = "year"
            next_dt = self._add_months(current_dt, 12)

        return next_dt.strftime("%Y-%m-%d"), step_unit

    def _parse_datetime(self, dt_str: str) -> datetime:
        cleaned = dt_str.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", cleaned):
            return datetime.strptime(cleaned, "%Y-%m-%d")
        elif re.match(r"^\d{4}-\d{2}$", cleaned):
            return datetime.strptime(f"{cleaned}-01", "%Y-%m-%d")
        elif re.match(r"^\d{4}$", cleaned):
            return datetime.strptime(f"{cleaned}-01-01", "%Y-%m-%d")
        return datetime(2011, 10, 5)

    def _add_months(self, dt: datetime, months: int) -> datetime:
        new_year = dt.year + (dt.month + months - 1) // 12
        new_month = (dt.month + months - 1) % 12 + 1
        new_day = min(dt.day, 28)
        return datetime(new_year, new_month, new_day)
