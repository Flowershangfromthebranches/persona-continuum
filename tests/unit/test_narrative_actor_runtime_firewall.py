from __future__ import annotations

import pytest

from tests.fixtures.narrative_runtime import RUNTIME, install_narrative_responder, seed_project


@pytest.mark.anyio
async def test_final_runtime_prompt_is_actor_scoped(app, monkeypatch) -> None:
    adapter = install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=1)
    fang = app.narratives.add_character(project.id, name="Fang", role="lead")
    ceo = app.narratives.add_character(project.id, name="CEO", role="antagonist")
    app.narratives.create_missing_personas(project.id)
    fact = app.narratives.add_fact(project.id, "Digital Fang is the real source", secret=True)
    app.narratives.set_character_knowledge(project.id, fang.id, fact.id, "unknown")
    app.narratives.set_character_knowledge(
        project.id,
        ceo.id,
        fact.id,
        "known",
        fact_text="Digital Fang is the real source",
    )
    app.narratives.ensure_story_world(project.id)

    fang_scene = app.narratives.create_scene(
        project.id,
        {
            "episode_number": 1,
            "scene_goal": "Fang investigates the message",
            "participants": [{"character_id": fang.id, "name": "Fang"}],
        },
    )
    await app.narratives.simulate_scene(
        project.id,
        fang_scene,
        runtime=RUNTIME,
        generation_mode="agent",
        max_turns=1,
    )
    fang_prompt = next(
        turn.user_message
        for _, turn in reversed(adapter.sent_turns)
        if "SCOPED RUNTIME CONTEXT" in turn.user_message
    )
    assert "Digital Fang is the real source" not in fang_prompt
    assert "SCOPED RUNTIME CONTEXT" in fang_prompt

    ceo_scene = app.narratives.create_scene(
        project.id,
        {
            "episode_number": 1,
            "scene_goal": "CEO decides what to conceal",
            "participants": [{"character_id": ceo.id, "name": "CEO"}],
        },
    )
    await app.narratives.simulate_scene(
        project.id,
        ceo_scene,
        runtime=RUNTIME,
        generation_mode="agent",
        max_turns=1,
    )
    ceo_prompt = next(
        turn.user_message
        for _, turn in reversed(adapter.sent_turns)
        if "SCOPED RUNTIME CONTEXT" in turn.user_message
    )
    assert "Digital Fang is the real source" in ceo_prompt
    assert fang_scene.runtime_trace["agent_calls"] >= 1
