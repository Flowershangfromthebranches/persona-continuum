from __future__ import annotations

from tests.fixtures.narrative_runtime import install_narrative_responder


def test_stage_specific_model_and_reasoning_reach_runtime(app, monkeypatch) -> None:
    adapter = install_narrative_responder(app, monkeypatch)
    project = app.narratives.create_project(
        title="Routing",
        runtime_assignment={
            "story_architect": {
                "agent": "fake_agent", "model": "fake-gpt-5", "reasoning": "high"
            },
            "screenwriter": {
                "agent": "fake_agent", "model": "fake-claude-4", "reasoning": "medium"
            },
            "reviewer": {
                "agent": "fake_agent", "model": "fake-grok-4", "reasoning": "max"
            },
        },
    )
    app.narratives.generate_story_bible_sync(project.id, generation_mode="agent")
    app.narratives.generate_outline_sync(project.id, 1, generation_mode="deterministic")
    version = app.narratives.generate_episode_draft_sync(
        project.id, 1, generation_mode="agent"
    )
    app.narratives.audit_episode(project.id, version.id, generation_mode="agent")
    # Narrative calls close their physical sessions turn-by-turn, so use sent
    # turn session ids and the runtime traces as durable routing evidence.
    traces = app.narratives._runtime_traces
    assert any(t["requested_model"] == "fake-gpt-5" and t["reasoning"] == "high" for t in traces)
    assert any(
        t["requested_model"] == "fake-claude-4" and t["reasoning"] == "medium"
        for t in traces
    )
    assert any(t["requested_model"] == "fake-grok-4" and t["reasoning"] == "max" for t in traces)
    assert adapter.sent_turns
