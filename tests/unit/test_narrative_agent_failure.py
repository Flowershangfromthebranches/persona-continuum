from __future__ import annotations

import pytest

from persona_continuum.narrative.runtime import NarrativeAgentError
from tests.fixtures.narrative_runtime import RUNTIME


def test_agent_failure_is_terminal_and_sanitized(app) -> None:
    adapter = app.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    adapter.simulate_crash_on_turn = 1
    project = app.narratives.create_project(title="Fail Closed")
    with pytest.raises(NarrativeAgentError) as caught:
        app.narratives.generate_story_bible_sync(
            project.id, runtime=RUNTIME, generation_mode="agent"
        )
    details = caught.value.to_dict()
    assert details["stage"] == "story_bible"
    assert details["agent"] == "fake_agent"
    assert details["requested_model"] == "fake-gpt-5"
    assert details["original_error_class"]
    assert "secret" not in str(details).casefold()
    assert app.narratives.get_bible(project.id) is None


def test_agent_mode_without_runtime_is_explicitly_unavailable(app) -> None:
    project = app.narratives.create_project(title="No Runtime")
    with pytest.raises(NarrativeAgentError) as caught:
        app.narratives.generate_story_bible_sync(
            project.id, generation_mode="agent"
        )
    assert caught.value.code == "NARRATIVE_AGENT_RUNTIME_UNAVAILABLE"


def test_story_bible_surfaces_cli_stderr_and_retries_exit_without_output(
    app, monkeypatch
) -> None:
    from persona_continuum.agent.response_collector import AgentReportedError

    calls = {"count": 0}

    async def boom(*_args, **_kwargs):
        calls["count"] += 1
        raise AgentReportedError(
            "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT: CLI exited with status 1",
            code="AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
            phase="story_bible",
            diagnostics={
                "stderr_tail": (
                    "Gemini API rejected the request: user location is not supported"
                )
            },
            retriable=True,
        )

    monkeypatch.setattr(app.agent_runtime_executor, "execute_structured", boom)
    project = app.narratives.create_project(title="Location Fail")
    with pytest.raises(NarrativeAgentError) as caught:
        app.narratives.generate_story_bible_sync(
            project.id, runtime=RUNTIME, generation_mode="agent"
        )
    details = caught.value.to_dict()
    assert calls["count"] == 3
    assert caught.value.retryable is True
    assert "user location is not supported" in details["message"].casefold()
    assert "secret" not in str(details).casefold()
