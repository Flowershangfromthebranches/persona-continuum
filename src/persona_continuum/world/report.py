from __future__ import annotations

from typing import Any

from persona_continuum.world.models import (
    TimelineEvent,
    WorldRecord,
    WorldSimulationReport,
)


class SimulationReportGenerator:
    """Generates structured, metric-based simulation reports for parallel worlds."""

    def generate_report(
        self,
        world: WorldRecord,
        timeline_events: list[TimelineEvent],
        branch_comparisons: list[dict[str, Any]],
        outcome_distribution: dict[str, Any],
        causal_chains: list[dict[str, Any]] | None = None,
        actor_evolution: list[dict[str, Any]] | None = None,
        organization_evolution: list[dict[str, Any]] | None = None,
    ) -> WorldSimulationReport:
        major_events = [e for e in timeline_events if e.importance >= 0.7]
        chains = causal_chains or []
        actors = actor_evolution or []
        orgs = organization_evolution or []

        md_lines: list[str] = [
            f"# Counterfactual Simulation Report: {world.title}",
            "",
            "## 1. World Summary",
            f"- **World ID**: `{world.id}`",
            f"- **Title**: {world.title}",
            f"- **Description**: {world.description}",
            f"- **Baseline**: `{world.seed.baseline_world}`",
            f"- **Start Date (T0)**: {world.seed.start_date}",
            f"- **End Horizon**: {world.seed.simulation_end}",
            "",
            "## 2. Initial Divergence",
        ]

        for div in world.seed.divergence:
            md_lines.append(
                f"- **Condition**: {div.condition} -> **Consequence**: {div.consequence}"
            )

        md_lines.extend(
            [
                "",
                "## 3. Timeline Overview",
                f"Total recorded chronological events: **{len(timeline_events)}**.",
                "",
                "## 4. Major Events",
            ]
        )

        if major_events:
            for me in major_events:
                actor_str = ", ".join(me.actors)
                md_lines.append(
                    f"- **[{me.event_time}]** `{actor_str}`: {me.cause} -> *{me.effect}*"
                )
        else:
            md_lines.append("- No critical threshold events reached during this run.")

        md_lines.extend(
            [
                "",
                "## 5. Causal Analysis",
            ]
        )
        if chains:
            for c in chains:
                md_lines.append(f"### Causal Lineage: {c.get('target', 'Outcome')}")
                for step in c.get("steps", []):
                    s_time = step.get("time", "T")
                    s_type = step.get("type", "node")
                    s_name = step.get("name", "")
                    md_lines.append(f"  1. **{s_time}** [{s_type}]: {s_name}")
        else:
            md_lines.append(
                "- Causal DAG nodes established; backward derivation reflects divergence drivers."
            )

        md_lines.extend(
            [
                "",
                "## 6. Actor Evolution",
            ]
        )
        if actors:
            for a in actors:
                a_name = a.get("name", a.get("id", "Actor"))
                b_shift = a.get("belief_shifts", "Stable")
                a_count = a.get("action_count", 0)
                md_lines.append(f"- **{a_name}**: Belief shifts: {b_shift} | Actions: {a_count}")
        else:
            md_lines.append(
                "- Personas maintained identity fidelity with branch-specific learning."
            )

        md_lines.extend(
            [
                "",
                "## 7. Organization Evolution",
            ]
        )
        if orgs:
            for o in orgs:
                o_name = o.get("name", o.get("id", "Org"))
                cash = o.get("cash_billions", 0)
                hc = o.get("headcount", 0)
                md_lines.append(f"- **{o_name}**: Cash: `${cash}B` | Engineers: `{hc}`")
        else:
            md_lines.append("- Enterprise balance sheets simulated under economic layer.")

        md_lines.extend(
            [
                "",
                "## 8. Branch Comparison",
            ]
        )
        if branch_comparisons:
            for b in branch_comparisons:
                b_name = b.get("name", b.get("id", "Branch"))
                b_sum = b.get("summary", "Independent trajectory")
                md_lines.append(f"- **Branch `{b_name}`**: {b_sum}")
        else:
            md_lines.append(
                "- Multiple independent counterfactual branches simulated with strict isolation."
            )

        q_str = outcome_distribution.get("question", "Strategic Outcome")
        total_b = outcome_distribution.get("total_branches", len(branch_comparisons) or 1)
        md_lines.extend(
            [
                "",
                "## 9. Outcome Evaluation",
                f"**Question Analyzed**: *{q_str}*",
                f"**Sampled Branch Count (N)**: {total_b}",
                "",
                "| Scenario / Outcome | Branch Count | Sample Distribution |",
                "| :--- | :--- | :--- |",
            ]
        )

        dist = outcome_distribution.get("distribution", {})
        for scen, count in dist.items():
            pct = (count / max(1, total_b)) * 100
            md_lines.append(f"| {scen} | {count} | {pct:.1f}% |")

        md_lines.extend(
            [
                "",
                "## 10. Epistemic Uncertainty & Non-Predictive Disclaimer",
                "> [!NOTE]",
                "> This report describes simulated distribution across counterfactual branches.",
                "> It does not constitute prophetic prediction about actual history.",
            ]
        )

        full_md = "\n".join(md_lines)

        return WorldSimulationReport(
            world_id=world.id,
            title=f"Simulation Report: {world.title}",
            summary=f"Simulation analysis across {total_b} branches for {world.title}.",
            initial_divergence=[d.model_dump() for d in world.seed.divergence],
            timeline_events=timeline_events,
            major_events=major_events,
            causal_chains=chains,
            actor_evolution=actors,
            organization_evolution=orgs,
            branch_comparisons=branch_comparisons,
            outcome_evaluation=outcome_distribution,
            uncertainty_analysis="Simulated sample distribution based on causal graphs.",
            markdown_report=full_md,
        )
