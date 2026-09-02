"""Director-invoked Persona rehearsal must respect the Knowledge Firewall.

The Director (author layer) may read Story Truth / final_truth, but rehearsal
scenes go through the existing firewall-scoped actor runtime: character
prompts must never contain author-only secrets the character does not know.
"""

from __future__ import annotations

import asyncio
from types import MethodType
from typing import Any

import pytest

from tests.fixtures.narrative_runtime import (
    install_narrative_responder,
    seed_project,
)
from tests.unit.test_narrative_director_agent import _decide


@pytest.mark.anyio
async def test_director_rehearsal_does_not_leak_final_truth(app, monkeypatch) -> None:
    adapter = install_narrative_responder(app, monkeypatch)
    project, _bible = seed_project(app, episodes=1)
    fang = app.narratives.add_character(project.id, name="Fang", role="lead")
    app.narratives.create_missing_personas(project.id)
    app.narratives.ensure_story_world(project.id)

    install_director = app.narrative_director
    queue = [
        _decide(
            decision="execute_action",
            action="run_persona_rehearsal",
            arguments={
                "project_id": project.id,
                "episode_number": 1,
                "character_ids": [fang.id],
                "goal": "Rehearse the message scene",
                "max_turns": 1,
            },
        ),
        _decide(decision="stop", message="排练完成。"),
    ]
    original = adapter._generate_mock_response

    def responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Narrative Director Agent" in prompt:
            return queue.pop(0)
        return original(session, turn)

    monkeypatch.setattr(adapter, "_generate_mock_response", MethodType(responder, adapter))

    session = install_director.create_session(project.id, episode_number=1)
    install_director.send_message(session.id, "这场互动感觉不自然，排练一下。")

    # The loop runs as a background task on the running event loop.
    task = install_director._tasks.get(session.id)
    if task is not None:
        await asyncio.shield(task)
    current = app.narrative_repo.get_director_session(session.id)
    assert current is not None and current.status.value == "active"
    rehearsal_actions = app.narrative_repo.list_director_actions(session.id)
    assert any(
        a.action == "run_persona_rehearsal" and a.status.value == "succeeded"
        for a in rehearsal_actions
    )

    # The scene was simulated through the firewall-scoped actor runtime.
    actor_prompt = next(
        turn.user_message
        for _, turn in reversed(adapter.sent_turns)
        if "SCOPED RUNTIME CONTEXT" in turn.user_message
    )
    assert "Digital Fang is the real source" not in actor_prompt
    rehearsal = [s for s in app.narratives.list_scenes(project.id, 1) if s.dialogue]
    assert rehearsal, "rehearsal scene should have dialogue"
