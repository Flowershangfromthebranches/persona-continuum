from __future__ import annotations

from persona_continuum.world.models import (
    TimelineEvent,
    WorldRecord,
    WorldSeed,
)
from persona_continuum.world.report import SimulationReportGenerator


def test_report_is_metric_based() -> None:
    gen = SimulationReportGenerator()

    world = WorldRecord(
        id="world_ai_chips",
        title="Apple AI Chip vs NVIDIA",
        description="Counterfactual continuation with Steve Jobs driving AI silicon.",
        seed=WorldSeed(
            start_date="2011-10-05",
            simulation_end="2030",
        ),
    )

    evts = [
        TimelineEvent(
            event_time="2011-10-05",
            cause="Divergence",
            effect="Jobs survives pancreatic cancer",
            actors=["steve_jobs"],
            importance=1.0,
        ),
        TimelineEvent(
            event_time="2014-06-01",
            cause="Investment",
            effect="Apple establishes Silicon Architecture Lab",
            actors=["apple"],
            importance=0.8,
        ),
    ]

    branches = [
        {"id": "branch_1", "name": "Branch_A", "summary": "Aggressive Datacenter AI TPU"},
        {"id": "branch_2", "name": "Branch_B", "summary": "Edge-only Neural Engine"},
    ]

    dist = {
        "question": "Which architecture dominates enterprise AI compute by 2030?",
        "total_branches": 10,
        "distribution": {
            "NVIDIA CUDA Ecosystem Advantage": 6,
            "Apple Proprietary Silicon Parity": 4,
        },
    }

    report = gen.generate_report(
        world=world,
        timeline_events=evts,
        branch_comparisons=branches,
        outcome_distribution=dist,
    )

    assert report.world_id == "world_ai_chips"
    assert "Simulation Report" in report.title
    assert "## 1. World Summary" in report.markdown_report
    assert "## 2. Initial Divergence" in report.markdown_report
    assert "## 9. Outcome Evaluation" in report.markdown_report
    assert "prophetic prediction" in report.markdown_report.lower()
