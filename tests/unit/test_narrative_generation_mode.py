from __future__ import annotations

from tests.fixtures.narrative_runtime import RUNTIME, install_narrative_responder


def test_deterministic_mode_never_calls_agent(app, monkeypatch) -> None:
    adapter = install_narrative_responder(app, monkeypatch)
    project = app.narratives.create_project(title="Offline", logline="rules")
    bible = app.narratives.generate_story_bible_sync(
        project.id, runtime=RUNTIME, generation_mode="deterministic"
    )
    assert bible.generation_mode.value == "deterministic"
    assert adapter.sent_turns == []


def test_auto_uses_agent_when_runtime_is_configured(app, monkeypatch) -> None:
    adapter = install_narrative_responder(app, monkeypatch)
    project = app.narratives.create_project(title="Auto", logline="agent")
    bible = app.narratives.generate_story_bible_sync(
        project.id, runtime=RUNTIME, generation_mode="auto"
    )
    assert bible.generation_mode.value == "agent"
    assert bible.premise == "AI premise"
    assert len(adapter.sent_turns) == 1
