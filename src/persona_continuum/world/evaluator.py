from __future__ import annotations

import re
from typing import Any

from persona_continuum.world.models import OutcomeEvaluation, SimulationBranch, WorldState

GENERIC_OUTCOMES = ("Leading Outcome", "Contested Outcome", "Constrained Outcome")


class OutcomeEvaluator:
    """Evaluates simulation questions across multiple counterfactual branches.

    Domain-agnostic rules:
      1. Metrics are caller-defined; a generic default set works for any world.
      2. Analyzes outcomes across N simulation branches from their WorldState.
      3. Outputs a scenario distribution (branch counts) and causal explanations.
      4. NEVER claims 'real probability'—reports proportion in simulated branches.
      5. Never names specific companies, products, or domains—branch outcomes are
         classified only as leading/contested/constrained relative to other branches.
    """

    DEFAULT_METRIC_WEIGHTS: dict[str, float] = {
        "event_momentum": 0.20,
        "project_progress": 0.20,
        "capability_maturity": 0.20,
        "resource_strength": 0.20,
        "relationship_stability": 0.20,
    }

    def evaluate_simulation(
        self,
        world_id: str,
        question: str,
        branches: list[SimulationBranch],
        metrics: dict[str, float] | None = None,
    ) -> OutcomeEvaluation:
        metric_weights = dict(metrics or self.DEFAULT_METRIC_WEIGHTS)
        # Normalize weights
        total_weight = sum(metric_weights.values()) or 1.0
        normalized_weights = {k: v / total_weight for k, v in metric_weights.items()}

        scored: list[tuple[float, dict[str, Any]]] = []
        for branch in branches:
            state = branch.current_state or WorldState(timestamp=branch.created_at.isoformat())
            composite, breakdown = self._score_branch_state(state, normalized_weights)
            scored.append(
                (
                    composite,
                    {
                        "branch_id": branch.id,
                        "branch_name": branch.name,
                        "metric_breakdown": breakdown,
                        "composite_score": round(composite, 3),
                    },
                )
            )

        # Relative classification: a branch "leads" when its composite score
        # clearly exceeds the median, "constrains" when clearly below.
        scores = sorted(item[0] for item in scored)
        median = scores[len(scores) // 2] if scores else 0.0
        distribution: dict[str, int] = {name: 0 for name in GENERIC_OUTCOMES}

        branch_results: list[dict[str, Any]] = []
        for idx, (composite, entry) in enumerate(scored, 1):
            if composite >= median + 0.1:
                scenario = GENERIC_OUTCOMES[0]
            elif composite <= median - 0.1:
                scenario = GENERIC_OUTCOMES[2]
            else:
                scenario = GENERIC_OUTCOMES[1]
            distribution[scenario] = distribution.get(scenario, 0) + 1

            state = (
                branches[idx - 1].current_state
                or WorldState(timestamp=branches[idx - 1].created_at.isoformat())
            )
            result = {
                "branch_id": entry["branch_id"],
                "branch_name": entry["branch_name"],
                "scenario": scenario,
                "composite_score": entry["composite_score"],
                "metric_breakdown": entry["metric_breakdown"],
                "causal_explanation": self._generate_branch_causal_reason(
                    branches[idx - 1], state, scenario
                ),
            }
            branch_results.append(result)

        total_branches = len(branches) or 1
        summary_lines = [
            f"Simulation Question: '{question}'",
            f"Evaluated across {total_branches} independent counterfactual branch(es):",
        ]
        for scen, count in distribution.items():
            pct = (count / total_branches) * 100.0
            summary_lines.append(f"- {scen}: {count} branch(es) ({pct:.1f}% of simulations)")

        summary_lines.append("\nCausal Summary:")
        summary_lines.append(
            "Branch classifications are relative to the simulated branches only. "
            "They describe differences in simulated momentum, capability, resources, "
            "and relationship stability—not predictions about any real-world outcome."
        )

        causal_summary = "\n".join(summary_lines)

        # Safety verification: ensure no fabricated real probability claims
        self._verify_no_real_probability_claims(causal_summary)

        return OutcomeEvaluation(
            world_id=world_id,
            question=question,
            metrics=normalized_weights,
            branch_results=branch_results,
            distribution=distribution,
            causal_summary=causal_summary,
            total_branches=total_branches,
        )

    def _score_branch_state(
        self, state: WorldState, weights: dict[str, float]
    ) -> tuple[float, dict[str, Any]]:
        """Scores a branch state on generic, domain-neutral dimensions in [0, 1]."""
        event_momentum = min(1.0, len(state.events) / 10.0)

        if state.active_projects:
            project_progress = sum(
                float(p.get("progress_percent", 0)) / 100.0 for p in state.active_projects.values()
            ) / len(state.active_projects)
        else:
            project_progress = 0.0

        maturity_values = [
            float(t.get("maturity", 0.0))
            for t in state.technologies.values()
            if isinstance(t, dict) and isinstance(t.get("maturity", None), (int, float))
        ]
        capability_maturity = (
            sum(maturity_values) / len(maturity_values) if maturity_values else 0.0
        )

        numeric_resources = [
            float(v)
            for v in state.resources.values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        cash_values = [
            float(o.get("cash_reserves_billions", 0.0))
            for o in state.organizations.values()
            if isinstance(o, dict) and isinstance(
                o.get("cash_reserves_billions", None), (int, float)
            )
        ]
        resource_pool = numeric_resources + cash_values
        resource_strength = (
            min(1.0, (sum(resource_pool) / len(resource_pool)) / 100.0)
            if resource_pool
            else 0.0
        )

        affinities = [
            float(r.get("affinity", 0.0))
            for r in state.relationships.values()
            if isinstance(r, dict) and isinstance(r.get("affinity", None), (int, float))
        ]
        relationship_stability = (
            (sum(affinities) / len(affinities) + 1.0) / 2.0 if affinities else 0.5
        )

        breakdown = {
            "event_momentum": round(event_momentum, 3),
            "project_progress": round(project_progress, 3),
            "capability_maturity": round(capability_maturity, 3),
            "resource_strength": round(resource_strength, 3),
            "relationship_stability": round(relationship_stability, 3),
        }

        composite = (
            event_momentum * weights.get("event_momentum", 0.0)
            + project_progress * weights.get("project_progress", 0.0)
            + capability_maturity * weights.get("capability_maturity", 0.0)
            + resource_strength * weights.get("resource_strength", 0.0)
            + relationship_stability * weights.get("relationship_stability", 0.0)
        )
        return composite, breakdown

    def _generate_branch_causal_reason(
        self, branch: SimulationBranch, state: WorldState, scenario: str
    ) -> str:
        project_count = len(state.active_projects)
        event_count = len(state.events)
        return (
            f"In branch '{branch.name}', {project_count} active initiative(s) and "
            f"{event_count} causal event(s) led to a terminal outcome classified as {scenario}."
        )

    def _verify_no_real_probability_claims(self, text: str) -> None:
        forbidden = [
            r"real(?:-| )world probability",
            r"true probability is",
            r"actual probability of reality",
        ]
        for pat in forbidden:
            if re.search(pat, text, re.IGNORECASE):
                raise ValueError("Evaluator error: Forbidden claim of real-world probability.")
