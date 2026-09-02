from __future__ import annotations

import pytest

from persona_continuum.domain.persona import PersonaType, RunMode
from tests.fixtures.narrative_runtime import install_narrative_responder, seed_project


def _writer_participants(app):
    roles = [
        "head_writer",
        "story_architect",
        "character_editor",
        "mystery_editor",
        "continuity_editor",
        "commercial_editor",
    ]
    result = []
    for role in roles:
        persona_id = f"writer_{role}"
        app.personas.create_from_manifest(
            {
                "id": persona_id,
                "display_name": role,
                "persona_type": PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON.value,
                "run_mode": RunMode.COUNTERFACTUAL_CONTINUATION.value,
            }
        )
        result.append(
            {
                "role": role,
                "persona_id": persona_id,
                "runtime_selection": "fake_agent",
                "model_selection": "fake-gpt-5",
            }
        )
    return result


@pytest.mark.anyio
async def test_writer_room_persists_head_writer_synthesis(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=1)
    app.narratives.generate_outline_sync(project.id, 1, generation_mode="deterministic")
    result = await app.narratives.run_writer_room(
        project.id, 1, _writer_participants(app), cross_review=True
    )
    assert result["protocol_state"] == "success"
    assert result["stage_outputs"]
    synthesis = app.narratives.get_writer_room_synthesis(project.id, 1)
    assert synthesis is not None
    assert synthesis.final_writer_instruction
    assert synthesis.room_id == result["room_id"]
