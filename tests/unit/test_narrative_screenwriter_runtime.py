from __future__ import annotations

from persona_continuum.domain.narrative import WriterRoomSynthesis
from tests.fixtures.narrative_runtime import RUNTIME, install_narrative_responder, seed_project


def test_screenwriter_agent_consumes_writer_room_synthesis(app, monkeypatch) -> None:
    adapter = install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=1)
    app.narratives.generate_outline_sync(project.id, 1, generation_mode="deterministic")
    app.narratives.repo.save_writer_room_synthesis(
        WriterRoomSynthesis(
            project_id=project.id,
            episode_number=1,
            room_id="room_test",
            final_writer_instruction="UNIQUE_HEAD_WRITER_INSTRUCTION",
            stage_outputs=[{"raw_protocol_payload": "OMIT_FROM_WRITER_PROMPT" * 10_000}],
        )
    )
    version = app.narratives.generate_episode_draft_sync(
        project.id, 1, runtime=RUNTIME, generation_mode="agent"
    )
    assert version.generation_mode.value == "agent"
    assert version.structured_draft["title"] == "AI Draft"
    prompt = adapter.sent_turns[-1][1].user_message
    assert "UNIQUE_HEAD_WRITER_INSTRUCTION" in prompt
    assert "OMIT_FROM_WRITER_PROMPT" not in prompt
    assert len(prompt) < 40_000
    assert version.writer_room_synthesis["room_id"] == "room_test"
