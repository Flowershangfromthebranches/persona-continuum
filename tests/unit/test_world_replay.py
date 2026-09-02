from __future__ import annotations

from persona_continuum.world.models import TimelineEvent, WorldState
from persona_continuum.world.replay import WorldReplayEngine


def test_world_can_replay() -> None:
    engine = WorldReplayEngine()

    state_0 = WorldState(timestamp="2011-10-05")
    evt_0 = TimelineEvent(
        event_time="2011-10-05",
        cause="T0 Divergence",
        effect="Jobs survives",
        actors=["steve_jobs"],
    )
    engine.record_step("2011-10-05", state_0, [evt_0], ["Divergence @ 2011-10-05"])

    state_1 = WorldState(timestamp="2012-10-05")
    evt_1 = TimelineEvent(
        event_time="2012-10-05",
        cause="R&D Push",
        effect="Apple builds dedicated TPU lab",
        actors=["steve_jobs", "apple"],
        importance=0.8,
    )
    engine.record_step("2012-10-05", state_1, [evt_1], ["Lab established"])

    state_2 = WorldState(timestamp="2015-10-05")
    evt_2 = TimelineEvent(
        event_time="2015-10-05",
        cause="Tapeout Success",
        effect="Apple Neural Engine V1 yields at TSMC",
        actors=["apple", "tsmc"],
        importance=0.9,
    )
    engine.record_step("2015-10-05", state_2, [evt_2], ["Tapeout 16nm"])

    # 1. Trajectory check
    traj = engine.get_trajectory("w_test", "main", outcome_summary="Apple Neural Engine Lead")
    assert len(traj.steps) == 3

    # 2. Historical scrub to 2012
    step_1 = engine.get_step_at_time("2012-10-05")
    assert step_1 is not None
    assert step_1.timestamp == "2012-10-05"
    assert step_1.active_events[0].effect == "Apple builds dedicated TPU lab"

    # 3. Rewind to step 0
    state_rewound, events_rewound = engine.rewind_to_step(0)
    assert state_rewound is not None
    assert state_rewound.timestamp == "2011-10-05"
    assert len(events_rewound) == 1

    # 4. Milestones
    milestones = engine.trace_historical_milestones()
    assert len(milestones) == 2
    assert any("Neural Engine V1" in m["effect"] for m in milestones)
