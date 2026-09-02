from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config


@pytest.fixture()
def app(tmp_path):
    continuum = PersonaContinuum(Config(data_dir=tmp_path / "fc"), include_fake_agent=True)
    continuum.init()
    yield continuum
    continuum.close()


def test_forecast_forks_isolated_branches(app) -> None:
    world, branch, _state = app.worlds.create_world(
        description="故事世界", start_date="2036-01-01", initial_actors=["fang_ning"]
    )
    project = app.narratives.create_project(
        title="推演测试", story_world_id=world.id, canonical_world_branch_id=branch.id
    )
    forecast = app.narratives.forecast_episode(
        project.id,
        20,
        [
            {"label": "believe", "description": "相信陈默"},
            {"label": "suspect", "description": "继续怀疑陈默"},
            {"label": "pretend", "description": "假装相信"},
        ],
        horizon_episodes=5,
    )
    assert len(forecast.directions) == 3
    branch_ids = [d.world_branch_id for d in forecast.directions]
    assert all(branch_ids)
    assert len(set(branch_ids)) == 3  # each direction got its own branch
    assert forecast.context_fingerprint

    for direction_id in branch_ids:
        b = app.worlds.get_branch(direction_id)
        assert b is not None
        assert b.id != branch.id


def test_select_direction_and_stale_detection(app) -> None:
    world, branch, _state = app.worlds.create_world(
        description="故事世界2", start_date="2036-01-01", initial_actors=["fang_ning"]
    )
    project = app.narratives.create_project(
        title="推演选择", story_world_id=world.id, canonical_world_branch_id=branch.id
    )
    forecast = app.narratives.forecast_episode(
        project.id, 20, [{"label": "A"}, {"label": "B"}]
    )
    selected = app.narratives.select_forecast_direction(
        forecast.id, forecast.directions[0].id
    )
    assert selected.selected_direction_id == forecast.directions[0].id

    # Editing the bible invalidates the forecast fingerprint.
    app.narratives.save_bible(project.id, {"premise": "变了"})
    fetched = app.narratives.get_forecast(forecast.id)
    assert fetched.stale is True
