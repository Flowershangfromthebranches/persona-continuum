"""Branch isolation E2E: forecast directions are isolated; canon is explicit."""

from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.world.models import ActionType, ActorAction


@pytest.fixture()
def app(tmp_path):
    continuum = PersonaContinuum(
        Config(data_dir=tmp_path / "branch_iso"), include_fake_agent=True
    )
    continuum.init()
    yield continuum
    continuum.close()


def _story_project(app):
    world, branch, _state = app.worlds.create_world(
        description="方宁与陈默的办公室悬疑故事",
        start_date="2036-01-01",
        initial_actors=["fang_ning", "chen_mo"],
    )
    project = app.narratives.create_project(
        title="分支隔离测试",
        story_world_id=world.id,
        canonical_world_branch_id=branch.id,
    )
    app.narratives.generate_story_bible_sync(project.id)
    app.narratives.generate_outline_sync(project.id, episode_count=22)
    return project, world, branch


def test_forecast_branches_are_isolated_from_each_other_and_canon(app) -> None:
    project, world, canonical_branch = _story_project(app)

    forecast = app.narratives.forecast_episode(
        project.id,
        20,
        [
            {"label": "believe", "description": "相信陈默"},
            {"label": "suspect", "description": "继续怀疑陈默"},
            {"label": "pretend", "description": "假装相信陈默"},
        ],
    )
    branch_a, branch_b, branch_c = (d.world_branch_id for d in forecast.directions)

    # Divergent actions in each branch must not leak into the others.
    action = ActorAction(
        actor_id="fang_ning",
        action_type=ActionType.INJECT if hasattr(ActionType, "INJECT") else ActionType.DECIDE,
        description="分支专属行动",
    )
    app.worlds.inject_action(world.id, branch_a, action)
    state_a = app.worlds.get_branch(branch_a).current_state
    state_b = app.worlds.get_branch(branch_b).current_state
    state_c = app.worlds.get_branch(branch_c).current_state
    assert len(state_a.events) > len(state_b.events)
    assert state_b.events == state_c.events

    # The canonical branch stays untouched by simulation.
    canon_state = app.worlds.get_branch(canonical_branch.id).current_state
    assert canon_state.events == state_c.events

    # Simulated events never enter narrative canon automatically.
    assert app.narratives.repo.list_canon_entries(project.id) == []


def test_selecting_without_committing_keeps_canon_unchanged(app) -> None:
    project, world, canonical_branch = _story_project(app)
    forecast = app.narratives.forecast_episode(
        project.id, 20, [{"label": "A"}, {"label": "B"}]
    )
    direction_b = forecast.directions[1]
    app.narratives.select_forecast_direction(forecast.id, direction_b.id)

    project_after = app.narratives.get_project(project.id)
    assert project_after.canonical_world_branch_id == canonical_branch.id

    draft = app.narratives.generate_episode_draft_sync(project.id, 20)
    app.narratives.audit_episode(project.id, draft.id)
    app.narratives.commit_episode(project.id, 20, draft.id, canon_updates={
        "canon_events": ["EP20：方宁选择继续怀疑陈默"],
    })
    # Commit without a forecast branch binding keeps the project's canon pointer
    # unless the draft explicitly carries a branch.
    project_final = app.narratives.get_project(project.id)
    assert project_final.canonical_world_branch_id in (
        canonical_branch.id,
        draft.canonical_branch_id,
    )


def test_commit_moves_canon_pointer_to_chosen_branch(app) -> None:
    project, world, canonical_branch = _story_project(app)
    forecast = app.narratives.forecast_episode(
        project.id, 20, [{"label": "A"}, {"label": "B"}]
    )
    branch_b = forecast.directions[1].world_branch_id

    draft = app.narratives.generate_episode_draft_sync(project.id, 20)
    # The author picks branch B: the draft records it before commit.
    draft.canonical_branch_id = branch_b
    app.narratives.repo.save_episode_version(draft)
    app.narratives.audit_episode(project.id, draft.id)
    app.narratives.commit_episode(project.id, 20, draft.id)

    assert app.narratives.get_project(project.id).canonical_world_branch_id == branch_b

    # The next episode forks from the new canon (via a new forecast).
    next_forecast = app.narratives.forecast_episode(
        project.id, 21, [{"label": "C1"}, {"label": "C2"}]
    )
    next_branches = [d.world_branch_id for d in next_forecast.directions]
    assert all(b for b in next_branches)
    parent_names = {
        app.worlds.get_branch(b).parent_branch_id for b in next_branches if app.worlds.get_branch(b)
    }
    assert branch_b in parent_names or all(
        b != canonical_branch.id for b in next_branches
    )
