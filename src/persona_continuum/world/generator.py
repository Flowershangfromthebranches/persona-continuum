from __future__ import annotations

from persona_continuum.application._utils import new_id
from persona_continuum.world.models import ActionResolution, ActorAction, TimelineEvent


class WorldEventGenerator:
    """Generates strictly causal TimelineEvent instances from Actor Actions,

    Organization Strategies, and Environmental/Technological state transitions.
    Prohibits ungrounded, uncaused random plot injections.
    """

    def generate_from_action_resolution(
        self,
        action: ActorAction,
        resolution: ActionResolution,
        timestamp: str,
    ) -> TimelineEvent:
        cause = (
            f"Actor '{action.actor_id}' executed '{action.action_type.value}': {action.description}"
        )
        effect = resolution.outcome_description
        actors = [action.actor_id]
        if action.target and action.target not in actors:
            actors.append(action.target)

        importance = 0.5
        if action.action_type.value in ["launch_project", "invest", "acquire"]:
            importance = 0.8
        elif action.action_type.value in ["research", "hire", "negotiate"]:
            importance = 0.65

        return TimelineEvent(
            id=resolution.event_id or new_id("evt_act"),
            event_time=timestamp,
            actors=actors,
            cause=cause,
            effect=effect,
            causal_chain=[
                f"Decision by {action.actor_id}",
                f"Execution of {action.action_type.value}",
            ],
            confidence=resolution.success_probability if resolution.success else 0.4,
            importance=importance,
            data={
                "action_id": action.id,
                "action_type": action.action_type.value,
                "state_mutations": resolution.state_mutations,
                "success": resolution.success,
            },
        )

    def generate_from_tech_breakthrough(
        self,
        tech_id: str,
        tech_name: str,
        lead_org_id: str | None,
        old_maturity: float,
        new_maturity: float,
        timestamp: str,
    ) -> TimelineEvent:
        cause = f"R&D matured '{tech_name}' ({old_maturity:.2f} -> {new_maturity:.2f})"
        effect = f"Production yield threshold reached for '{tech_name}'"
        actors = [lead_org_id] if lead_org_id else ["industry"]

        return TimelineEvent(
            id=new_id("evt_tech"),
            event_time=timestamp,
            actors=actors,
            cause=cause,
            effect=effect,
            causal_chain=[f"R&D advancement in {tech_id}", "Prerequisite satisfaction"],
            confidence=0.95,
            importance=0.85,
            data={
                "tech_id": tech_id,
                "old_maturity": old_maturity,
                "new_maturity": new_maturity,
                "source": "technology_engine",
            },
        )

    def generate_from_macro_economic_shift(
        self,
        indicator_name: str,
        description: str,
        timestamp: str,
    ) -> TimelineEvent:
        return TimelineEvent(
            id=new_id("evt_econ"),
            event_time=timestamp,
            actors=["macro_economy"],
            cause=f"Macro-economic transition: {indicator_name}",
            effect=description,
            causal_chain=["Market forces", f"Capital allocation shift in {indicator_name}"],
            confidence=0.9,
            importance=0.7,
            data={"indicator": indicator_name, "source": "economic_layer"},
        )
