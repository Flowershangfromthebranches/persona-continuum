from __future__ import annotations

from typing import Any

from persona_continuum.world.models import (
    ReplayStep,
    ReplayTrajectory,
    TimelineEvent,
    WorldState,
)


class WorldReplayEngine:
    """Provides historical time-scrubbing, reverse causal tracing, and comparative

    counterfactual trajectory analysis.
    """

    def __init__(self, steps: list[ReplayStep] | None = None) -> None:
        self.steps: list[ReplayStep] = steps or []

    def record_step(
        self,
        timestamp: str,
        state: WorldState,
        active_events: list[TimelineEvent],
        causal_milestones: list[str] | None = None,
        diff_summary: str = "",
    ) -> ReplayStep:
        step = ReplayStep(
            step_index=len(self.steps),
            timestamp=timestamp,
            state_snapshot=state,
            active_events=active_events,
            causal_milestones=causal_milestones or [],
            diff_summary=diff_summary,
        )
        self.steps.append(step)
        return step

    def get_trajectory(
        self, world_id: str, branch_id: str, outcome_summary: str = ""
    ) -> ReplayTrajectory:
        return ReplayTrajectory(
            world_id=world_id,
            branch_id=branch_id,
            steps=self.steps,
            outcome_summary=outcome_summary,
        )

    def get_step_at_index(self, index: int) -> ReplayStep | None:
        if 0 <= index < len(self.steps):
            return self.steps[index]
        return None

    def get_step_at_time(self, timestamp: str) -> ReplayStep | None:
        for s in reversed(self.steps):
            if s.timestamp == timestamp:
                return s
        return None

    def rewind_to_step(self, step_index: int) -> tuple[WorldState | None, list[TimelineEvent]]:
        step = self.get_step_at_index(step_index)
        if not step:
            return None, []
        return step.state_snapshot, step.active_events

    def trace_historical_milestones(self) -> list[dict[str, Any]]:
        """Extracts key turning points and milestones across the historical trajectory."""
        milestones: list[dict[str, Any]] = []
        for step in self.steps:
            for evt in step.active_events:
                if evt.importance >= 0.7:
                    milestones.append(
                        {
                            "step_index": step.step_index,
                            "time": evt.event_time,
                            "cause": evt.cause,
                            "effect": evt.effect,
                            "actors": evt.actors,
                            "importance": evt.importance,
                        }
                    )
        return milestones

    def export_list(self) -> list[dict[str, Any]]:
        return [s.model_dump() for s in self.steps]

    def import_list(self, data: list[dict[str, Any]]) -> None:
        self.steps = [ReplayStep.model_validate(s) for s in data]
