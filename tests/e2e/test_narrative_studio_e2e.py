"""Narrative Studio end-to-end acceptance.

Covers the full acceptance chain from the task definition:
create a project → story bible → characters (auto fictional personas) →
story world → outline → per-episode forecast with isolated branches →
scene simulation on real persona knowledge → draft → audit → canon commit →
next episode continues from new canon → production package.
"""

from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config


@pytest.fixture()
def app(tmp_path):
    continuum = PersonaContinuum(
        Config(data_dir=tmp_path / "e2e_narr"), include_fake_agent=True
    )
    continuum.init()
    yield continuum
    continuum.close()


def _setup_project(app) -> str:
    project = app.narratives.create_project(
        title="裁员通知来自十年后",
        logline="沈清收到一封来自2036年的裁员通知邮件",
        format="micro_drama",
        planned_episode_count=60,
        episode_duration_seconds_min=60,
        episode_duration_seconds_max=90,
    )
    app.narratives.generate_story_bible_sync(project.id)
    app.narratives.save_bible(
        project.id,
        {
            "premise": "广告公司职员沈清在2036年1月的一个工作日收到一封来自十年后的邮件",
            "core_question": "如果预言无法逃避，人还能改变什么？",
            "final_truth": ["邮件并非时间旅行，而是ORACLE模拟中的数字沈清发出"],
            "characters": [
                {"id": "shen_qing", "name": "沈清", "role": "主角",
                 "description": "30岁广告公司职员"},
                {"id": "gu_yan", "name": "顾言", "role": "同事",
                 "description": "沈清的上司，隐瞒真相"},
                {"id": "ceo", "name": "CEO", "role": "反派", "description": "知道ORACLE实验真相"},
            ],
        },
    )
    app.narratives.generate_outline_sync(project.id, episode_count=3)
    for spec in (
        {"id": "shen_qing", "name": "沈清", "role": "主角"},
        {"id": "gu_yan", "name": "顾言", "role": "同事"},
        {"id": "ceo", "name": "CEO", "role": "反派"},
    ):
        app.narratives.add_character(project.id, **spec)
    app.narratives.create_missing_personas(project.id)
    app.narratives.add_fact(project.id, "邮件来自ORACLE模拟中的数字沈清", secret=True)
    app.narratives.add_fact(project.id, "CEO从第一集就知道实验真相", secret=True)
    return project.id


@pytest.mark.anyio
async def test_narrative_studio_full_acceptance(app) -> None:
    pid = _setup_project(app)

    # 1. Characters are bound to auto-created fictional personas.
    characters = app.narratives.list_characters(pid)
    assert {c.name for c in characters} == {"沈清", "顾言", "CEO"}
    assert all(c.persona_id for c in characters)
    assert all(c.provenance == "fictional_author_defined" for c in characters)
    for c in characters:
        persona = app.personas.get(c.persona_id)
        assert persona.manifest.persona_type.value == "fictional_or_synthetic_person"

    # 2. The story enters a Parallel World with explicit actors only.
    project = app.narratives.ensure_story_world(pid)
    world_actors = app.worlds.list_actors(project.story_world_id, project.canonical_world_branch_id)
    assert {a.id for a in world_actors} >= {"shen_qing", "gu_yan", "ceo"}

    # 3. Knowledge matrix: EP01 strict separation.
    shen_id = next(c.id for c in characters if c.name == "沈清")
    ceo_id = next(c.id for c in characters if c.name == "CEO")
    facts = app.narratives.list_facts(pid)
    oracle_fact = next(f for f in facts if "ORACLE" in f.text)
    ceo_fact = next(f for f in facts if "CEO" in f.text)
    app.narratives.set_character_knowledge(
        pid, ceo_id, oracle_fact.id, "known", learned_episode=1, fact_text=oracle_fact.text
    )
    app.narratives.set_character_knowledge(
        pid, ceo_id, ceo_fact.id, "known", learned_episode=1, fact_text=ceo_fact.text
    )
    app.narratives.set_audience_knowledge(pid, oracle_fact.id, "hidden")
    matrix = app.narratives.get_knowledge_matrix(pid, episode_number=1)
    assert matrix["character_knowledge"][oracle_fact.id][ceo_id] == "known"
    assert shen_id not in matrix["character_knowledge"].get(oracle_fact.id, {})
    assert matrix["audience_knowledge"][oracle_fact.id] == "hidden"

    # 4. Knowledge firewall: Shen Qing's prompt never contains the secret.

    shen_char = next(c for c in characters if c.id == shen_id)
    knowledge = app.narratives.repo.list_knowledge(pid)
    view = app.narratives.firewall.build_character_context(
        shen_char,
        app.narratives.get_bible(pid),
        facts,
        knowledge,
        [],
        episode_number=1,
    )
    blob = "\n".join(view["prompt_blocks"])
    assert "ORACLE" not in blob
    assert "CEO从第一集就知道实验真相" not in blob

    # 5. Forecast with three isolated directions for EP20-style decision.
    forecast = app.narratives.forecast_episode(
        pid,
        2,
        [
            {"label": "believe", "description": "沈清选择相信顾言"},
            {"label": "suspect", "description": "沈清继续怀疑顾言"},
            {"label": "pretend", "description": "沈清假装相信顾言"},
        ],
        horizon_episodes=5,
    )
    direction_ids = [d.world_branch_id for d in forecast.directions]
    assert len(set(direction_ids)) == 3
    canon_state = app.worlds.get_branch(project.canonical_world_branch_id).current_state
    suspect_state = app.worlds.get_branch(direction_ids[1]).current_state
    assert suspect_state is not canon_state
    assert app.narratives.repo.list_canon_entries(pid) == []  # simulation is non-canon

    # 6. Scene simulation: characters act on what THEY know.
    scene = app.narratives.create_scene(
        pid,
        {
            "episode_number": 1,
            "order": 1,
            "location": "地下停车场",
            "scene_goal": "沈清更加怀疑顾言",
            "participants": [
                {
                    "character_id": shen_id,
                    "name": "沈清",
                    "goal": "得到真相",
                    "knowledge_fact_ids": [],
                },
                {
                    "character_id": ceo_id,
                    "name": "顾言",
                    "goal": "保护沈清",
                    "must_not_reveal": ["数字沈清"],
                },
            ],
        },
    )
    simulated = await app.narratives.simulate_scene(pid, scene)
    assert simulated.status.value == "completed"
    assert simulated.summary
    assert simulated.relationship_delta

    # 7. Draft + audit + commit.
    version = app.narratives.generate_episode_draft_sync(pid, 1)
    report = app.narratives.audit_episode(pid, version.id)
    assert report.passed, [f.message for f in report.findings]
    commit = app.narratives.commit_episode(
        pid, 1, version.id,
        canon_updates={"canon_events": ["EP01：沈清收到2036年的裁员邮件，预言应验"]},
    )
    assert commit["committed"] is True

    # 8. Canon exists; next episode plan continues from the new revision.
    canon = app.narratives.repo.list_canon_entries(pid)
    assert any("EP01" in e.text for e in canon)
    project_after = app.narratives.get_project(pid)
    assert project_after.revision > project.revision
    ep2_context = app.narratives.prepare_episode(pid, 2)
    assert ep2_context["recent_episode_summaries"]
    assert ep2_context["recent_episode_summaries"][-1]["episode"] == 1

    # 9. Production package for the micro drama.
    package = app.narratives.generate_production_package(pid, 1)
    assert package.shot_list
    assert package.screenplay
    assert package.video_generation_prompts
    assert package.character_visual_bible
    assert package.dialogue_track or package.subtitle_track
