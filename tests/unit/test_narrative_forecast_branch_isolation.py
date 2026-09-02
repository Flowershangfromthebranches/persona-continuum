from __future__ import annotations

from tests.fixtures.narrative_runtime import seed_project


def test_forecast_branches_are_isolated_and_noncanonical(app) -> None:
    project, _ = seed_project(app, episodes=2)
    app.narratives.generate_outline_sync(project.id, 2, generation_mode="deterministic")
    app.narratives.ensure_story_world(project.id)
    forecast = app.narratives.forecast_episode(
        project.id,
        1,
        [{"label": "A", "description": "Trust"}, {"label": "B", "description": "Doubt"}],
        horizon_episodes=2,
        generation_mode="deterministic",
    )
    branch_ids = [item.world_branch_id for item in forecast.directions]
    assert len(set(branch_ids)) == 2
    assert project.canonical_world_branch_id not in branch_ids
    assert app.narratives.repo.list_canon_entries(project.id) == []
    app.narratives.select_forecast_direction(forecast.id, forecast.directions[0].id)
    assert app.narratives.repo.list_canon_entries(project.id) == []
