from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from persona_continuum.agent.models import (
    AgentSessionConfig,
    PermissionProfile,
)
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
from persona_continuum.agent.structured_output import (
    StructuredOutputEngine,
    StructuredOutputParseError,
    StructuredResult,
)
from persona_continuum.application._utils import new_id
from persona_continuum.world.builder import WorldBuilderService
from persona_continuum.world.models import DivergenceItem, WorldSeed

if TYPE_CHECKING:
    from persona_continuum.agent.adapter import AgentAdapter

logger = logging.getLogger(__name__)


WORLD_BUILDER_SYSTEM_PROMPT = """You are the World Builder Agent for Persona Continuum.

Your sole duty is to transform a user's natural language historical divergence into a
structured initial WorldSeed (T0 starting state).

CRITICAL CONSTRAINTS:
1. ONLY generate the starting conditions at start_date (T0).
2. NEVER predict future outcomes or pre-generate the simulation ending.
3. Output MUST be a valid JSON object matching the requested schema.
4. Extract relevant initial actors (individuals and organizations) and baseline technologies.
5. entity_candidates.subtype_hint must be one of: person, organization, institution,
   collective, event, policy, technology, resource, market_state, product, project,
   location, metric, or process.

Output JSON Schema:
{
  "baseline_world": "real_world",
  "start_date": "YYYY-MM-DD",
  "simulation_end": "YYYY",
  "divergence": [
    {
      "type": "historical_change",
      "condition": "<Counterfactual trigger condition>",
      "consequence": "<Immediate initial divergence effect at T0>"
    }
  ],
  "immutable_facts": [
    "<Fact 1 true up to start_date>",
    "<Fact 2>"
  ],
  "rules": [
    "<World physical / economic rule 1>",
    "<World rule 2>"
  ],
  "entity_candidates": [
    {
      "id": "stable_entity_id",
      "name": "Entity Name",
      "extracted_context": "Why this entity matters at T0",
      "category_hint": "agent|non_agent",
      "subtype_hint": "<allowed subtype>"
    }
  ],
  "initial_actors": [
    {
      "id": "unique_actor_id",
      "name": "Display Name",
      "actor_type": "persona_actor",
      "persona_id": "optional_matching_persona_id",
      "organization_id": "optional_org_id",
      "goals": ["Goal 1", "Goal 2"],
      "beliefs": {"belief_key": 0.9}
    }
  ],
  "organizations": [
    {
      "id": "org_id",
      "name": "Org Name",
      "cash_reserves_billions": 10.0,
      "headcount_engineers": 500
    }
  ],
  "known_technologies": [
    {
      "id": "tech_id",
      "name": "Technology Name",
      "maturity_level": "concept"
    }
  ],
  "simulation_goal": "<Objective of the counterfactual exploration>"
}
"""


class LLMWorldBuilder:
    """Invokes an LLM Agent to parse natural language descriptions into a structured WorldSeed."""

    def __init__(
        self,
        fallback_builder: WorldBuilderService | None = None,
        runtime_executor: AgentRuntimeExecutor | None = None,
    ) -> None:
        self.fallback_builder = fallback_builder or WorldBuilderService()
        self.runtime_executor = runtime_executor or AgentRuntimeExecutor()

    async def build(
        self,
        *,
        description: str,
        adapter: AgentAdapter | None = None,
        model_id: str | None = None,
        reasoning_effort: str | None = None,
        auth_profile_id: str | None = None,
        baseline: str = "real_world",
        start_date: str | None = None,
        simulation_end: str | int = "2030",
        allow_fallback: bool = False,
    ) -> tuple[WorldSeed, list[dict[str, Any]], dict[str, Any]]:
        """Builds a WorldSeed using an LLM Agent.

        Returns (seed, raw_actors, raw_metadata).
        """
        if adapter is None:
            if allow_fallback:
                seed = self.fallback_builder.build_seed(
                    description=description,
                    baseline=baseline,
                    start_date=start_date,
                    simulation_end=simulation_end,
                )
                return seed, [], {"builder_source": "deterministic_rule"}
            raise RuntimeError("World Builder Agent is required but none was provided or ready.")

        user_prompt = (
            f"User Divergence Description: {description}\n"
            f"Baseline World: {baseline}\n"
            f"Start Date Hint: {start_date or 'Infer from description'}\n"
            f"Simulation End: {simulation_end}\n\n"
            f"Please generate the complete structured WorldSeed JSON object."
        )

        session_cfg = AgentSessionConfig(
            session_id=new_id("wb_sess"),
            room_id="world_builder",
            participant_id="world_builder",
            persona_id="world_builder",
            model_id=model_id,
            reasoning_effort=reasoning_effort,
            auth_profile_id=auth_profile_id,
            permission_profile=PermissionProfile.CHAT_SAFE,
            allow_mcp=False,
            tools=[],
        )

        session_binding = None
        try:
            session_binding = await self.runtime_executor.open_session(adapter, session_cfg)
            result = await self.runtime_executor.execute_structured(
                session_binding,
                system_prompt=WORLD_BUILDER_SYSTEM_PROMPT,
                user_message=user_prompt,
                schema={"type": "object"},
                phase="world_builder",
                metadata={"world_builder": True},
            )
            if not isinstance(result, StructuredResult) or not isinstance(result.value, dict):
                raise ValueError("LLM World Builder did not return a JSON object.")
            parsed_data = result.value

            seed_start_date = (
                parsed_data.get("start_date")
                or start_date
                or f"{datetime.now().year}-01-01"
            )
            div_list = [
                DivergenceItem(
                    type=item.get("type", "historical_change"),
                    condition=item.get("condition", "Counterfactual condition"),
                    consequence=item.get("consequence", "Initial historical deviation"),
                )
                for item in parsed_data.get("divergence", [])
            ]
            if not div_list:
                div_list = [
                    DivergenceItem(
                        type="historical_change",
                        condition=description.split("，")[0].split(",")[0].strip(),
                        consequence="Initial timeline divergence",
                    )
                ]

            raw_actors = parsed_data.get("initial_actors", [])
            actor_ids: list[str] = [
                str(a.get("id")) if isinstance(a, dict) and a.get("id") else str(a)
                for a in raw_actors
            ] or [description.split()[0] if description else "primary_actor"]

            seed = WorldSeed(
                baseline_world=parsed_data.get("baseline_world", baseline),
                start_date=str(seed_start_date),
                simulation_end=str(parsed_data.get("simulation_end", simulation_end)),
                divergence=div_list,
                immutable_facts=parsed_data.get(
                    "immutable_facts",
                    [f"Historical facts up to {seed_start_date} remain unchanged from {baseline}."],
                ),
                rules=parsed_data.get(
                    "rules",
                    ["Economic resources and technological maturity govern action feasibility."],
                ),
                initial_actors=actor_ids,
                location=parsed_data.get("location"),
                metadata={
                    "builder_source": "llm",
                    "builder_agent": adapter.adapter_id,
                    "builder_model": model_id,
                    "simulation_goal": parsed_data.get("simulation_goal", ""),
                    "organizations": parsed_data.get("organizations", []),
                    "known_technologies": parsed_data.get("known_technologies", []),
                    # Preserve the structured actor roster for the shared
                    # Persona Match/Completion gate before world persistence.
                    "initial_actors": raw_actors,
                    "entity_candidates": parsed_data.get("entity_candidates", []),
                    "non_agent_entities": parsed_data.get("non_agent_entities", []),
                    "classification": parsed_data.get("entity_classification", []),
                },
            )
            return seed, raw_actors, parsed_data
        except Exception as exc:
            logger.warning("LLM World Builder failed: %s", exc)
            if allow_fallback:
                logger.info("Falling back to deterministic WorldBuilderService")
                seed = self.fallback_builder.build_seed(
                    description=description,
                    baseline=baseline,
                    start_date=start_date,
                    simulation_end=simulation_end,
                )
                return (
                    seed,
                    [],
                    {"builder_source": "deterministic_rule_fallback", "error": str(exc)},
                )
            raise RuntimeError(f"World Builder AI Generation failed: {exc}") from exc
        finally:
            if session_binding is not None:
                try:
                    await self.runtime_executor.close(session_binding)
                except Exception:
                    logger.debug("World Builder session close failed", exc_info=True)

    def _extract_json(self, text: str) -> dict[str, Any] | None:
        try:
            val = StructuredOutputEngine.parse_json(text)
        except StructuredOutputParseError:
            return None
        if isinstance(val, dict):
            return val
        return None
