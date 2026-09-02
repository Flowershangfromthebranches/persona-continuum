from __future__ import annotations

from tests.fixtures.narrative_runtime import RUNTIME, install_narrative_responder, seed_project


def test_production_planner_agent_refines_generic_package(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=1)
    app.narratives.generate_outline_sync(project.id, 1, generation_mode="deterministic")
    version = app.narratives.generate_episode_draft_sync(
        project.id, 1, generation_mode="deterministic"
    )
    package = app.narratives.generate_production_package(
        project.id,
        1,
        version.id,
        runtime=RUNTIME,
        generation_mode="agent",
        is_preview=True,
    )
    assert package.generation_mode.value == "agent"
    assert package.shot_list[0].camera == "eye-level"
    assert package.video_generation_prompts == ["slow push toward phone"]
    assert "vendor" in package.continuity_notes[-1]
