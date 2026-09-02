"""Narrative Studio UX completion: settings, binding, world auto-ensure, routing."""

from __future__ import annotations

import pytest

from persona_continuum.domain.persona import PersonaType, RunMode


def test_add_character_with_persona_id_binds_in_one_step(app) -> None:
    project = app.narratives.create_project(title="一步绑定", format="micro_drama")
    fang = app.personas.create_from_manifest(
        {
            "id": "fang_ning_full",
            "display_name": "方宁",
            "persona_type": PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON.value,
            "run_mode": RunMode.COUNTERFACTUAL_CONTINUATION.value,
            "summary": "广告公司职员",
        }
    )
    character = app.narratives.add_character(
        project.id,
        name=fang.display_name,
        role="主角",
        description="当前作品中的女主角",
        persona_id=fang.id,
    )
    assert character.name == "方宁"
    assert character.persona_id == fang.id
    assert character.description == "当前作品中的女主角"
    assert not hasattr(character, "temperament")
    bindings = app.narratives.repo.list_bindings(project.id)
    assert len(bindings) == 1
    assert bindings[0].persona_id == fang.id


def test_extra_character_stays_unbound(app) -> None:
    project = app.narratives.create_project(title="路人", format="micro_drama")
    extra = app.narratives.add_character(
        project.id, name="HR主管", role="功能角色", description="通知裁员"
    )
    assert extra.persona_id is None
    assert extra.name == "HR主管"


def test_unbind_character_clears_persona_not_library(app) -> None:
    project = app.narratives.create_project(title="解绑", format="micro_drama")
    fang = app.personas.create_from_manifest(
        {
            "id": "fang_ning_full",
            "display_name": "方宁",
            "persona_type": PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON.value,
            "run_mode": RunMode.COUNTERFACTUAL_CONTINUATION.value,
        }
    )
    character = app.narratives.add_character(
        project.id, name="方宁", role="主角", persona_id=fang.id
    )
    unbound = app.narratives.unbind_character(project.id, character.id)
    assert unbound.persona_id is None
    assert app.personas.get(fang.id).display_name == "方宁"
    assert app.narratives.repo.list_bindings(project.id) == []


def test_runtime_source_roundtrip_on_project(app) -> None:
    project = app.narratives.create_project(
        title="Runtime Source",
        runtime_assignment={
            "default": {
                "runtime_source": "api",
                "agent_id": "api_x",
                "model_id": "model_x",
                "reasoning_effort": "high",
            }
        },
    )
    loaded = app.narratives.get_project(project.id)
    saved = loaded.runtime_assignment["default"]
    assert saved["runtime_source"] == "api"
    assert saved["agent_id"] == "api_x"
    updated = app.narratives.update_project(
        project.id,
        {
            "runtime_assignment": {
                "default": {
                    "source": "local_cli",
                    "agent_id": "qoder",
                    "model_id": "glm",
                    "reasoning_effort": "medium",
                }
            }
        },
    )
    from persona_continuum.narrative.runtime import normalize_runtime

    normalized = normalize_runtime(updated.runtime_assignment["default"])
    assert normalized["runtime_source"] == "local_cli"
    assert normalized["agent_id"] == "qoder"


def test_bind_existing_persona_keeps_unbound_characters(app) -> None:
    project = app.narratives.create_project(
        title="绑定测试", logline="绑定已有人格", format="micro_drama"
    )
    fang = app.personas.create_from_manifest(
        {
            "id": "fang_ning_full",
            "display_name": "方宁",
            "persona_type": PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON.value,
            "run_mode": RunMode.COUNTERFACTUAL_CONTINUATION.value,
        }
    )
    lead = app.narratives.add_character(
        project.id, name="方宁", role="主角", description="广告公司职员"
    )
    extra = app.narratives.add_character(project.id, name="前台", role="路人")
    assert lead.persona_id is None
    assert extra.persona_id is None
    binding = app.narratives.bind_character(project.id, lead.id, persona_id=fang.id)
    assert binding.persona_id == fang.id
    characters = {item.name: item for item in app.narratives.list_characters(project.id)}
    assert characters["方宁"].persona_id == fang.id
    assert characters["方宁"].persona_origin == "existing_persona"
    assert characters["前台"].persona_id is None


def test_forecast_and_simulation_auto_ensure_story_world(app) -> None:
    project = app.narratives.create_project(
        title="自动世界",
        logline="系统应自动初始化故事世界",
        format="micro_drama",
        planned_episode_count=3,
    )
    app.narratives.generate_story_bible_sync(project.id, generation_mode="deterministic")
    lead = app.narratives.add_character(project.id, name="方宁", role="主角")
    assert app.narratives.get_project(project.id).story_world_id is None

    forecast = app.narratives.forecast_episode(
        project.id,
        1,
        [{"label": "A", "description": "方宁选择打开邮件"}],
        horizon_episodes=1,
        generation_mode="deterministic",
    )
    bound = app.narratives.get_project(project.id)
    assert bound.story_world_id
    assert bound.canonical_world_branch_id
    assert forecast.directions
    again = app.narratives.ensure_story_world(project.id)
    assert again.story_world_id == bound.story_world_id

    scene = app.narratives.create_scene(
        project.id,
        {
            "episode_number": 1,
            "order": 1,
            "location": "办公室",
            "scene_goal": "打开邮件",
            "participants": [{"character_id": lead.id, "name": lead.name, "goal": "求证"}],
        },
    )
    simulated = app.narratives._run_sync(
        app.narratives.simulate_scene(project.id, scene, generation_mode="deterministic")
    )
    assert simulated.status.value == "completed"
    assert simulated.runtime_trace.get("stage") == "scene_actor"


@pytest.mark.anyio
async def test_scene_actor_stage_runtime_is_used_for_rehearsal(app) -> None:
    project = app.narratives.create_project(
        title="Scene Actor",
        runtime_assignment={
            "default": {
                "agent_id": "fake_agent",
                "model_id": "fake-gpt-5",
                "reasoning_effort": "low",
            },
            "scene_actor": {
                "agent_id": "fake_agent",
                "model_id": "fake-claude-4",
                "reasoning_effort": "medium",
            },
            "forecast_simulator": {
                "agent_id": "fake_agent",
                "model_id": "fake-gpt-5",
                "reasoning_effort": "high",
            },
        },
    )
    app.narratives.generate_story_bible_sync(project.id, generation_mode="deterministic")
    lead = app.narratives.add_character(project.id, name="方宁", role="主角")
    scene = app.narratives.create_scene(
        project.id,
        {
            "episode_number": 1,
            "order": 1,
            "location": "地下停车场",
            "scene_goal": "对质",
            "participants": [{"character_id": lead.id, "name": lead.name, "goal": "求证"}],
        },
    )
    simulated = await app.narratives.simulate_scene(
        project.id, scene, generation_mode="deterministic"
    )
    assert simulated.runtime_trace["stage"] == "scene_actor"
    resolved = app.narratives._resolve_stage_runtime(
        app.narratives.get_project(project.id), "scene_actor", None
    )
    assert resolved["model_id"] == "fake-claude-4"
    forecast_runtime = app.narratives._resolve_stage_runtime(
        app.narratives.get_project(project.id), "forecast_simulator", None
    )
    assert forecast_runtime["model_id"] == "fake-gpt-5"
