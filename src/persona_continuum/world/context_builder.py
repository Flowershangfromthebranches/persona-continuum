from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from persona_continuum.world.firewall import TemporalKnowledgeFirewall
from persona_continuum.world.models import (
    Actor,
    TimelineEvent,
    WorldMemoryRecord,
    WorldSeed,
    WorldState,
)


@dataclass(frozen=True)
class WorldAgentContext:
    system_prompt: str
    decision_prompt: str
    known_information: list[str]


class WorldContextBuilder:
    """Builds a branch-scoped, temporally safe reasoning context for one actor."""

    def __init__(self, firewall: TemporalKnowledgeFirewall | None = None) -> None:
        self.firewall = firewall or TemporalKnowledgeFirewall()

    def build(
        self,
        *,
        actor: Actor,
        state: WorldState,
        seed: WorldSeed,
        memories: list[WorldMemoryRecord],
        recent_events: list[TimelineEvent],
        persona_profile: dict[str, Any] | None = None,
        organization_profile: dict[str, Any] | None = None,
        actor_profile: dict[str, Any] | None = None,
    ) -> WorldAgentContext:
        safe_memories = self.firewall.filter_memories(
            [memory.model_dump(mode="json") for memory in memories], state.timestamp
        )
        safe_events = [
            event.model_dump(mode="json")
            for event in recent_events
            if self.firewall._normalize_date(event.event_time)
            <= self.firewall._normalize_date(state.timestamp)
        ]
        known_information = list(seed.immutable_facts)
        known_information.extend(str(item.get("content", "")) for item in safe_memories)

        binding = {
            "persona_id": actor.persona_id,
            "organization_id": actor.organization_id,
            "persona_profile": persona_profile or {},
            "organization_profile": organization_profile or {},
            "actor_profile": actor_profile or {},
            "identity": actor.identity,
            "beliefs": actor.beliefs,
            "speaking_style": actor.identity.get("speaking_style", {}),
            "decision_patterns": actor.identity.get("decision_patterns", []),
        }
        current_state = {
            "actor_resources": actor.resources,
            "world_resources": state.resources,
            "organizations": state.organizations,
            "technologies": state.technologies,
            "economy": state.economy,
            "relationships": actor.relationships,
            "active_projects": state.active_projects,
        }

        system_prompt = "\n\n".join(
            [
                f"你是 {actor.name}。不要声称自己是 AI。",
                self.firewall.build_firewall_prompt_instruction(state.timestamp, seed),
                "你只能提出行动，不能直接修改世界状态。规则可以约束行动，但不能替代你的思考。",
                f"Actor 类型: {actor.actor_type.value}",
                "Persona/Organization Binding:\n"
                + json.dumps(binding, ensure_ascii=False, sort_keys=True),
            ]
        )
        decision_payload = {
            "current_world_time": state.timestamp,
            "known_information": known_information,
            "future_information": (
                f"任何晚于 {state.timestamp} 的真实世界信息；只能发现本分支之后生成的事件"
            ),
            "current_state": current_state,
            "goals": actor.goals,
            "recent_events": safe_events[-8:],
        }
        decision_prompt = (
            "ACTION_PROPOSAL_JSON\n"
            "根据以下上下文做下一步决策。只输出一个 JSON 对象，不要 Markdown，不要思维链。\n"
            + json.dumps(decision_payload, ensure_ascii=False, sort_keys=True)
            + "\n输出字段必须为: actor, intent, action_type, target, reasoning_summary, "
            "expected_effect, confidence, parameters。action_type 必须是 observe, decide, "
            "communicate, research, hire, invest, launch_project, negotiate, wait, reflect 之一。"
        )
        return WorldAgentContext(
            system_prompt=system_prompt,
            decision_prompt=decision_prompt,
            known_information=known_information,
        )
