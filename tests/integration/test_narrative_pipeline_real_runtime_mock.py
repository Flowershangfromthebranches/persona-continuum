from __future__ import annotations

import pytest

from tests.fixtures.narrative_runtime import RUNTIME, install_narrative_responder, seed_project


@pytest.mark.anyio
async def test_real_runtime_mock_pipeline_records_agent_calls_without_fallback(
    app, monkeypatch
) -> None:
    install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=2)
    character = app.narratives.add_character(project.id, name="Fang", role="lead")
    app.narratives.create_missing_personas(project.id)
    plans = await app.narratives.generate_outline(
        project.id, 2, runtime=RUNTIME, generation_mode="agent"
    )
    app.narratives.ensure_story_world(project.id)
    forecast = await app.narratives.forecast_episode_async(
        project.id,
        1,
        [{"label": "A", "description": "Trust"}],
        horizon_episodes=1,
        runtime=RUNTIME,
        generation_mode="agent",
    )
    app.narratives.select_forecast_direction(forecast.id, forecast.directions[0].id)
    version = await app.narratives.generate_episode_draft(
        project.id, 1, runtime=RUNTIME, generation_mode="agent"
    )
    report = await app.narratives.audit_episode_async(
        project.id, version.id, runtime=RUNTIME, generation_mode="agent"
    )
    package = await app.narratives.generate_production_package_async(
        project.id, 1, version.id, runtime=RUNTIME, generation_mode="agent", is_preview=True
    )
    assert plans[0].generation_mode.value == "agent"
    assert len(forecast.directions[0].steps) == 1
    assert version.generation_mode.value == "agent"
    assert report.passed
    assert package.generation_mode.value == "agent"
    assert character.persona_id is None  # persisted binding lives on repository copy
    assert all(trace.get("failure") is None for trace in app.narratives._runtime_traces)
