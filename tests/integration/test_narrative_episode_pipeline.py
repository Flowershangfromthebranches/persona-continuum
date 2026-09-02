"""Full episode pipeline integration test: PREPARE → DRAFT → AUDIT → COMMIT.

Runs against the real container and real storage; the fake agent only stands
in for the model inside the Room runtime (Fake Agents are test-only).
"""

from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config


@pytest.fixture()
def app(tmp_path):
    continuum = PersonaContinuum(Config(data_dir=tmp_path / "pipe"), include_fake_agent=True)
    continuum.init()
    yield continuum
    continuum.close()


@pytest.mark.anyio
async def test_full_episode_pipeline(app) -> None:
    project = app.narratives.create_project(
        title="裁员通知来自十年后", logline="一封来自2036年的裁员邮件", format="micro_drama"
    )
    app.narratives.generate_story_bible_sync(project.id)
    bible_updates = app.narratives.save_bible(
        project.id,
        {
            "core_question": "如果你知道自己的死亡时间，你会怎么活？",
            "final_truth": ["邮件并非时间旅行，而是来自ORACLE模拟中的数字方宁"],
            "characters": [
                {"id": "fang_ning", "name": "方宁", "role": "主角", "description": "广告公司职员"}
            ],
        },
    )
    assert bible_updates.version == 2
    app.narratives.add_fact(
        project.id, "邮件来自ORACLE模拟中的数字方宁", secret=True
    )
    app.narratives.generate_outline_sync(project.id, episode_count=3)
    character = app.narratives.add_character(project.id, name="方宁", role="主角")
    app.narratives.create_missing_personas(project.id)
    bound = app.narratives.repo.get_character(character.id)
    assert bound.persona_id

    # Enter the story world (creates a Parallel World from the bible)
    bound_project = app.narratives.ensure_story_world(project.id)
    assert bound_project.story_world_id
    assert bound_project.canonical_world_branch_id

    # PREPARE
    context = app.narratives.prepare_episode(project.id, 1)
    assert context["context_fingerprint"]
    assert context["episode_plan"]["episode_number"] == 1

    # SIMULATE a scene (uses SceneEngine → Room runtime → fake agent)
    scene = app.narratives.create_scene(
        project.id,
        {
            "episode_number": 1,
            "order": 1,
            "location": "公司工位",
            "scene_goal": "方宁收到2036年的裁员邮件",
            "participants": [
                {"character_id": character.id, "name": "方宁", "goal": "弄清邮件真伪"}
            ],
        },
    )
    simulated = await app.narratives.simulate_scene(project.id, scene)
    assert simulated.status.value == "completed"
    assert simulated.summary
    assert simulated.relationship_delta  # multi-dimensional, never a single affinity
    assert "affinity" not in simulated.relationship_delta[0]

    # DRAFT
    version = app.narratives.generate_episode_draft_sync(project.id, 1)
    assert version.version == 1
    assert "EP01" in version.screenplay
    assert version.beat_sheet

    # AUDIT
    report = app.narratives.audit_episode(project.id, version.id)
    assert report.passed, [f.message for f in report.findings]

    # COMMIT
    revision_before = app.narratives.get_project(project.id).revision
    result = app.narratives.commit_episode(project.id, 1, version.id)
    assert result["committed"] is True
    assert app.narratives.get_project(project.id).revision > revision_before
    assert app.narratives.repo.list_canon_entries(project.id)
    assert app.narratives.get_episode_plan(project.id, 1).status.value == "canon"


@pytest.mark.anyio
async def test_background_pipeline_job(app) -> None:
    project = app.narratives.create_project(title="后台任务", format="series")
    app.narratives.generate_story_bible_sync(project.id)
    app.narratives.generate_outline_sync(project.id, episode_count=2)
    job = app.narratives.create_job(
        "episode_pipeline",
        project.id,
        {"episode_number": 1, "steps": ["prepare", "draft", "audit"]},
    )
    import asyncio

    final = app.narratives.get_job(job["id"])
    for _ in range(50):
        final = app.narratives.get_job(job["id"])
        if final["status"] in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.1)
    assert final["status"] == "completed", final.get("error")
    assert final["result"]["audit"]["passed"] is True

    # pause / cancel semantics exist and don't crash on a terminal job
    app.narratives.pause_job(job["id"])
    app.narratives.cancel_job(job["id"])
