from __future__ import annotations

import json

import pytest

from persona_continuum.application.narrative_service import (
    OUTLINE_AUDIT_PAYLOAD_BUDGET_BYTES,
    NarrativeService,
)
from persona_continuum.domain.narrative import ForecastDirection
from persona_continuum.narrative.runtime import (
    NARRATIVE_AGENT_GENERATION_FAILED,
    NarrativeAgentError,
)
from tests.fixtures.narrative_runtime import RUNTIME, install_narrative_responder, seed_project


@pytest.mark.anyio
async def test_agent_forecast_runs_persona_world_scenes(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=3)
    character = app.narratives.add_character(project.id, name="Fang", role="lead")
    app.narratives.create_missing_personas(project.id)
    app.narratives.generate_outline_sync(project.id, 3, generation_mode="deterministic")
    app.narratives.ensure_story_world(project.id)
    forecast = await app.narratives.forecast_episode_async(
        project.id,
        1,
        [{"label": "A", "description": "Trust Chen"}],
        horizon_episodes=2,
        runtime=RUNTIME,
        generation_mode="agent",
    )
    direction = forecast.directions[0]
    assert len(direction.steps) == 2
    assert all(step["scene_ids"] for step in direction.steps)
    assert all(step["events"] for step in direction.steps)
    assert direction.evaluation["emotional_intensity"] == 0.8
    assert (
        app.narratives.list_scenes(project.id, 1)[0].participants[0].character_id
        == character.id
    )


@pytest.mark.anyio
async def test_agent_forecast_auto_binds_unbound_story_characters(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=2)
    digital = app.narratives.add_character(
        project.id,
        id="char_digital_fang",
        name="数字方宁",
        role="悬念载体",
    )
    assert digital.persona_id is None
    app.narratives.generate_outline_sync(project.id, 2, generation_mode="deterministic")
    plan = app.narratives.get_episode_plan(project.id, 1)
    plan.required_characters = ["char_digital_fang"]
    app.narratives.save_episode_plan(plan)

    forecast = await app.narratives.forecast_episode_async(
        project.id,
        1,
        [{"label": "A", "description": "相信邮件"}],
        horizon_episodes=1,
        runtime=RUNTIME,
        generation_mode="agent",
    )
    assert forecast.directions
    bound = app.narratives.repo.get_character("char_digital_fang")
    assert bound is not None
    assert bound.persona_id
    persona = app.personas.get(bound.persona_id)
    assert persona.manifest.persona_type.value == "fictional_or_synthetic_person"
    assert app.narratives.list_scenes(project.id, 1)


def test_forecast_evaluation_payload_stays_inside_argv_budget() -> None:
    long_summary = "数字方宁在沙盒中穷举方宁的每一种失败，" * 80
    direction = ForecastDirection(
        label="A",
        description="方宁选择相信邮件并提前质询 HR。",
        summary=long_summary,
        steps=[
            {
                "episode_number": index,
                "summary": long_summary,
                "events": [long_summary, long_summary],
                "character_decisions": [{"text": long_summary}],
                "relationship_changes": [{"delta": long_summary}],
                "predicted_episode_beats": [long_summary],
            }
            for index in range(1, 6)
        ],
    )
    compact = NarrativeService._compact_forecast_direction_for_eval(direction)
    raw = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    assert len(raw.encode("utf-8")) <= OUTLINE_AUDIT_PAYLOAD_BUDGET_BYTES
    assert long_summary not in raw
    full = json.dumps(
        direction.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )
    assert len(raw.encode("utf-8")) < len(full.encode("utf-8"))


@pytest.mark.anyio
async def test_forecast_survives_evaluation_transport_limit(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=2)
    app.narratives.add_character(project.id, name="Fang", role="lead")
    app.narratives.generate_outline_sync(project.id, 2, generation_mode="deterministic")
    original = app.narratives._structured_call_async

    async def wrapped(*args, **kwargs):
        if kwargs.get("phase") == "forecast_evaluation":
            raise NarrativeAgentError(
                NARRATIVE_AGENT_GENERATION_FAILED,
                "PROMPT_TRANSPORT_LIMIT_EXCEEDED",
                stage="forecast_evaluation",
                original_error=RuntimeError("PROMPT_TRANSPORT_LIMIT_EXCEEDED"),
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.narratives, "_structured_call_async", wrapped)
    forecast = await app.narratives.forecast_episode_async(
        project.id,
        1,
        [{"label": "A", "description": "相信邮件"}],
        horizon_episodes=1,
        runtime=RUNTIME,
        generation_mode="agent",
    )
    assert forecast.status.value == "completed"
    assert forecast.directions
    assert forecast.directions[0].scores
    assert any("transport" in item.casefold() for item in forecast.directions[0].risks)
