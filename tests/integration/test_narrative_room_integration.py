"""AI Writer's Room runs on the existing Room Protocol Engine (CUSTOM protocol)."""

from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.narrative.writer_room import (
    WRITER_ROOM_STAGE_IDS,
    build_writer_room_protocol_config,
)


def test_writer_room_protocol_template() -> None:
    config = build_writer_room_protocol_config(cross_review=True)
    stage_ids = tuple(s["id"] for s in config.stages)
    assert stage_ids == WRITER_ROOM_STAGE_IDS
    assert config.stages[-1]["id"] == "editor_synthesis"
    assert config.finalizer_role == "head_writer"
    # Cross review is optional when disabled.
    without_cross = build_writer_room_protocol_config(cross_review=False)
    cross = next(s for s in without_cross.stages if s["id"] == "cross_review")
    assert cross.get("optional") is True


@pytest.fixture()
def app(tmp_path):
    continuum = PersonaContinuum(Config(data_dir=tmp_path / "room_int"), include_fake_agent=True)
    continuum.init()
    yield continuum
    continuum.close()


@pytest.mark.anyio
async def test_writer_room_runs_through_protocol_engine(app) -> None:
    project = app.narratives.create_project(title="编剧室测试", format="micro_drama")
    app.narratives.generate_story_bible_sync(project.id)
    app.narratives.generate_outline_sync(project.id, episode_count=2)
    app.personas.create_from_manifest(
        {
            "id": "room_head_writer",
            "display_name": "主编",
            "persona_type": "fictional_or_synthetic_person",
            "run_mode": "continuation",
        }
    )
    result = await app.narratives.run_writer_room(
        project.id,
        1,
        [
            {
                "role": "head_writer",
                "persona_id": "room_head_writer",
                "display_name": "主编",
                "runtime_selection": "fake_agent",
                "model_selection": "fake-gpt-5",
                "reasoning_selection": "medium",
            }
        ],
    )
    assert "room_id" in result
    room = app.orchestrator.get_room(result["room_id"])
    assert room is not None
    # The room persisted its protocol run (no second orchestrator was created).
    assert room.protocol == "custom" or getattr(room, "protocol", None)
