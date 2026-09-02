from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from persona_continuum.world.models import DivergenceItem, WorldSeed


class WorldBuilderService:
    """Constructs structured WorldSeed from natural language or structured inputs.

    Strict rule: World Builder ONLY builds the starting world conditions.
    It NEVER predicts outcomes or pre-generates simulation endings.
    """

    def build_seed(
        self,
        description: str,
        baseline: str = "real_world",
        start_date: str | None = None,
        simulation_end: str | int = "2030",
        rules: list[str] | None = None,
        divergence_items: list[dict[str, str]] | None = None,
        immutable_facts: list[str] | None = None,
        initial_actors: list[str] | None = None,
        location: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorldSeed:
        # 1. Parse or infer start_date
        resolved_start_date = start_date
        if not resolved_start_date:
            date_match = re.search(r"\b(19\d{2}|20\d{2})(-\d{2}-\d{2})?\b", description)
            if date_match:
                matched = date_match.group(0)
                resolved_start_date = f"{matched}-10-05" if len(matched) == 4 else matched
            else:
                resolved_start_date = f"{datetime.now().year}-01-01"

        # 2. Parse or construct divergence list
        divergences: list[DivergenceItem] = []
        if divergence_items:
            for item in divergence_items:
                divergences.append(
                    DivergenceItem(
                        type=item.get("type", "historical_change"),
                        condition=item.get("condition", "Counterfactual condition"),
                        consequence=item.get("consequence", "Initial historical deviation"),
                    )
                )
        else:
            # Infer divergence from description
            cond = description.split("，")[0].split(",")[0].strip()
            conseq = description[len(cond) :].strip("，, 。.\n")
            if not conseq:
                conseq = "Initial timeline divergence from baseline historical trajectory"
            divergences.append(
                DivergenceItem(
                    type="historical_change",
                    condition=cond,
                    consequence=conseq,
                )
            )

        # 3. Facts prior to divergence (immutable historical ground truth)
        facts = list(immutable_facts or [])
        if not facts:
            facts = [
                (
                    f"Historical facts up to {resolved_start_date} remain unchanged "
                    f"from baseline {baseline}."
                ),
                "Laws of physics, thermodynamics, and manufacturing scaling limits apply.",
            ]

        # 4. Rules
        world_rules = list(rules or [])
        if not world_rules:
            world_rules = [
                (
                    "Economic resources, capital, talent, and technological maturity "
                    "govern action feasibility."
                ),
                (
                    "Actors make decisions based only on knowledge available up to "
                    "the current world timestamp."
                ),
            ]

        # 5. Actors: only what the caller supplies. Actor discovery for natural
        # language descriptions is the LLM World Builder / entity
        # classification pipeline's job, not a hardcoded default roster.
        actors = list(initial_actors or [])

        # 6. Validate no outcome prediction
        self._validate_no_outcome_prediction(description, divergences)

        return WorldSeed(
            baseline_world=baseline,
            start_date=resolved_start_date,
            divergence=divergences,
            immutable_facts=facts,
            simulation_end=str(simulation_end),
            rules=world_rules,
            location=location,
            initial_actors=actors,
            metadata=metadata or {},
        )

    def _validate_no_outcome_prediction(
        self, description: str, divergences: list[DivergenceItem]
    ) -> None:
        forbidden_patterns = [
            r"最终(?:战胜|失败|赢得|统治|破产|主导)",
            r"will (?:definitely|certainly) (?:win|fail|dominate|defeat|go bankrupt)",
            r"outcome is guaranteed",
        ]
        text = f"{description} " + " ".join(f"{d.condition} {d.consequence}" for d in divergences)
        for pat in forbidden_patterns:
            if re.search(pat, text, re.IGNORECASE):
                raise ValueError(
                    f"WorldBuilder violation: seed contains forbidden outcome prediction: '{pat}'. "
                    "WorldBuilder must only establish starting conditions."
                )
