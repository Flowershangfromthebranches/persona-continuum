from __future__ import annotations

from persona_continuum.world.director import WorldDirector
from persona_continuum.world.models import (
    SimulationSpeed,
    WorldDirectorPolicy,
    WorldState,
)


def test_director_advances_time() -> None:
    policy = WorldDirectorPolicy(
        event_frequency=1.0,
        simulation_speed=SimulationSpeed.YEAR,
        importance_threshold=0.5,
        intervention_level=0.5,
    )
    director = WorldDirector(policy)

    # 1. Test quiet baseline period -> advances with macro YEAR pace
    quiet_state = WorldState(
        timestamp="2011-10-05",
        active_projects={},
        organizations={},
    )
    decision = director.evaluate_next_step(quiet_state, [])
    assert decision.speed == SimulationSpeed.YEAR

    # 2. Test high competitive tension -> paces dynamically to MONTH resolution
    active_state = WorldState(
        timestamp="2015-06-01",
        active_projects={"apple_neural_engine": {"progress_percent": 92.0, "owner": "apple"}},
        organizations={
            "apple": {"technology": ["custom_ai_chip"]},
            "nvidia": {"technology": ["cuda_gpu_tensor"]},
        },
    )
    tense_decision = director.evaluate_next_step(active_state, [])
    assert tense_decision.speed == SimulationSpeed.MONTH
    assert len(tense_decision.trigger_milestones) > 0
    assert len(tense_decision.generated_events) > 0
    assert any("apple_neural_engine" in e.cause for e in tense_decision.generated_events)
