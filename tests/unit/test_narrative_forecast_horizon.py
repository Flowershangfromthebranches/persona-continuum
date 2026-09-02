from __future__ import annotations

from tests.fixtures.narrative_runtime import seed_project


def test_horizon_materializes_exact_episode_steps(app) -> None:
    project, _ = seed_project(app, episodes=5)
    app.narratives.generate_outline_sync(project.id, 5, generation_mode="deterministic")
    app.narratives.ensure_story_world(project.id)
    forecast = app.narratives.forecast_episode(
        project.id,
        1,
        [{"label": "A", "description": "One path"}],
        horizon_episodes=5,
        generation_mode="deterministic",
    )
    assert [step["episode_number"] for step in forecast.directions[0].steps] == [1, 2, 3, 4, 5]
