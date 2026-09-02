from __future__ import annotations

from typing import Any

from persona_continuum.application._utils import new_id
from persona_continuum.world.models import (
    Actor,
    ActorType,
    DirectorDecision,
    SimulationSpeed,
    TimelineEvent,
    WorldDirectorPolicy,
    WorldState,
)


class WorldDirector:
    """World Director Agent: orchestrates simulation pacing and macro event generation

    without overriding actor autonomy or making decisions for individual personas.
    """

    def __init__(self, policy: WorldDirectorPolicy | None = None) -> None:
        self.policy = policy or WorldDirectorPolicy()

    def evaluate_next_step(
        self,
        world_state: WorldState,
        recent_events: list[TimelineEvent],
        branch_context: dict[str, Any] | None = None,
    ) -> DirectorDecision:
        active_projects = world_state.active_projects or {}
        organizations = world_state.organizations or {}

        # 1. Evaluate urgency and tension in the world
        high_tension = False
        critical_milestones: list[str] = []

        # Check for near-completion or high-stakes projects
        for pid, proj in active_projects.items():
            progress = proj.get("progress_percent", 0.0)
            if 40.0 <= progress <= 60.0:
                critical_milestones.append(f"midpoint_review_{pid}")
            elif progress >= 90.0:
                high_tension = True
                critical_milestones.append(f"impending_launch_{pid}")

        # Check for competitive collisions between actors expanding in the same domain
        comp_count = 0
        for org in organizations.values():
            techs = org.get("technology", [])
            if any(
                "ai" in str(t).lower() or "chip" in str(t).lower() or "gpu" in str(t).lower()
                for t in techs
            ):
                comp_count += 1
        if comp_count >= 2:
            high_tension = True

        # 2. Determine simulation speed based on policy & tension
        speed = self.policy.simulation_speed
        reason = "Standard progression according to director policy."

        if high_tension and self.policy.intervention_level > 0.3:
            speed = SimulationSpeed.MONTH
            reason = "High competitive tension detected: pacing adjusted to monthly resolution."
        elif len(critical_milestones) > 0:
            speed = SimulationSpeed.QUARTER
            reason = "Active project milestones approaching: pacing set to quarterly resolution."
        elif not active_projects and not high_tension:
            speed = SimulationSpeed.YEAR
            reason = "Stable baseline period: pacing advanced to yearly macro simulation."

        # 3. Generate director-level environment & macro events if appropriate
        generated_events: list[TimelineEvent] = []
        if self.policy.event_frequency > 0.0 and critical_milestones:
            for m in critical_milestones[:2]:  # avoid event explosion
                if "midpoint_review" in m:
                    proj_name = m.replace("midpoint_review_", "")
                    evt = TimelineEvent(
                        id=new_id("evt_dir"),
                        event_time=world_state.timestamp,
                        actors=["director", "board"],
                        cause=f"Project {proj_name} reached critical engineering milestone",
                        effect=f"Board reviewed capital allocation and timeline for {proj_name}",
                        causal_chain=[f"Active R&D in {proj_name}"],
                        confidence=0.9,
                        importance=0.6,
                        data={"milestone": m, "source": "world_director"},
                    )
                    generated_events.append(evt)
                elif "impending_launch" in m:
                    proj_name = m.replace("impending_launch_", "")
                    evt = TimelineEvent(
                        id=new_id("evt_dir"),
                        event_time=world_state.timestamp,
                        actors=["director", "market"],
                        cause=f"Project {proj_name} nearing readiness (>90% progress)",
                        effect=f"Market monitors supply chain orders for {proj_name}",
                        causal_chain=[f"High completion rate of {proj_name}"],
                        confidence=0.95,
                        importance=0.8,
                        data={"milestone": m, "source": "world_director"},
                    )
                    generated_events.append(evt)

        return DirectorDecision(
            speed=speed,
            trigger_milestones=critical_milestones,
            reason=reason,
            generated_events=generated_events,
        )

    def select_active_actors(
        self,
        actors: list[Actor],
        world_state: WorldState,
        recent_events: list[TimelineEvent],
        limit: int = 6,
    ) -> list[Actor]:
        """Prioritize actors with goals, resources, or relevance to recent events."""
        event_text = " ".join(
            f"{event.cause} {event.effect} {' '.join(event.actors)}" for event in recent_events[-6:]
        ).lower()
        scored: list[tuple[float, Actor]] = []
        for actor in actors:
            # World conditions (market, policy, technology, resources, etc.)
            # are represented in WorldState and must not be routed through an
            # Agent Adapter.  Only Agent-capable profile types participate in
            # autonomous decision turns.
            if actor.actor_type in {
                ActorType.ENVIRONMENT_ACTOR,
                ActorType.ENVIRONMENT,
            }:
                continue
            score = float(bool(actor.goals)) + min(len(actor.resources), 5) * 0.1
            names = {actor.id.lower(), actor.name.lower()}
            names.update(str(alias).lower() for alias in actor.identity.get("aliases", []))
            if any(name and name in event_text for name in names):
                score += 2.0
            if actor.id in world_state.entities or actor.id in world_state.organizations:
                score += 0.5
            scored.append((score, actor))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return [actor for _, actor in scored[:limit]]
