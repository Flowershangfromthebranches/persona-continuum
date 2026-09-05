from __future__ import annotations

import base64
import json

import pytest

from persona_continuum.agent.adapters.command_code import CommandCodeAdapter
from persona_continuum.agent.models import AgentTurn
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.prompt_transport import (
    resolve_prompt_transport_capability,
)
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.room.models import ParticipantSlot

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_PNG_BASE64 = base64.b64encode(_PNG_BYTES).decode("ascii")

# ~6MB of incompressible-ish bytes: base64 expands to ~8MB, which must never
# ride the CLI text transport.
_BIG_BYTES = bytes(((i * 2654435761) >> 16) & 0xFF for i in range(6 * 1024 * 1024))
_BIG_BASE64 = base64.b64encode(_BIG_BYTES).decode("ascii")


def _persona(continuum: PersonaContinuum, persona_id: str) -> None:
    continuum.personas.create_from_manifest(
        {
            "id": persona_id,
            "display_name": persona_id,
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )


def _room_id(app: PersonaContinuum, runtime: str, model: str) -> str:
    _persona(app, "alice")
    _persona(app, "bob")
    room = app.orchestrator.create_room(
        title="Transport probe room",
        topic="transport",
        participants=[
            ParticipantSlot(
                participant_id="slot_alice",
                persona_id="alice",
                runtime_selection=runtime,
                model_selection=model,
                reasoning_selection="high",
            ),
            ParticipantSlot(
                participant_id="slot_bob",
                persona_id="bob",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
                reasoning_selection="high",
            ),
        ],
    )
    return room.id


def _estimate_wire_bytes(turn: AgentTurn) -> int:
    total = len(
        ((turn.system_prompt or "") + (turn.full_prompt or turn.user_message or "")).encode(
            "utf-8"
        )
    )
    for message in turn.messages or []:
        total += len(json.dumps(message, ensure_ascii=False, default=str).encode("utf-8"))
    return total


@pytest.mark.anyio
async def test_cli_pure_text_turn_passes_guard(app: PersonaContinuum) -> None:
    room_id = _room_id(app, "fake_agent", "fake-gpt-5")
    await app.orchestrator.start_room(room_id)
    await app.orchestrator.inject_message(room_id, "hello world", client_message_id="t1")

    events = []
    async for ev in app.orchestrator.step_turn(
        room_id, manual_speaker_id="slot_alice", user_message=""
    ):
        events.append(ev)
    assert any(e.get("event") == "turn_completed" for e in events)


@pytest.mark.anyio
async def test_small_image_cli_does_not_inline_base64(app: PersonaContinuum) -> None:
    room_id = _room_id(app, "fake_agent", "fake-gpt-5")
    await app.orchestrator.start_room(room_id)
    record = app.orchestrator.upload_room_attachment(
        room_id, filename="photo.png", mime="image/png", content_base64=_PNG_BASE64
    )
    await app.orchestrator.inject_message(
        room_id, "看看这张图", client_message_id="m1", attachment_ids=[record["id"]]
    )

    events = []
    async for ev in app.orchestrator.step_turn(
        room_id, manual_speaker_id="slot_alice", user_message=""
    ):
        events.append(ev)
    assert any(e.get("event") == "turn_completed" for e in events)

    adapter = app.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    sent_turns = getattr(adapter, "sent_turns", [])
    assert sent_turns, "fake adapter must have received the turn"
    _session_id, turn = sent_turns[-1]
    rendered = AgentPromptRenderer.render_for_single_prompt(turn)
    assert "data:image/" not in rendered
    assert ";base64," not in rendered


@pytest.mark.anyio
async def test_large_image_does_not_trip_cli_text_transport(app: PersonaContinuum) -> None:
    room_id = _room_id(app, "fake_agent", "fake-gpt-5")
    await app.orchestrator.start_room(room_id)
    record = app.orchestrator.upload_room_attachment(
        room_id, filename="big.png", mime="image/png", content_base64=_BIG_BASE64
    )
    await app.orchestrator.inject_message(
        room_id, "看看这张大图", client_message_id="m1", attachment_ids=[record["id"]]
    )

    # Canonical attachments travel as metadata: the CLI text transport sees
    # only a short path reference, so a 6MB image never trips the text
    # budget (previously: ~8MB base64 inlined into turn.messages).
    events = []
    async for ev in app.orchestrator.step_turn(
        room_id, manual_speaker_id="slot_alice", user_message=""
    ):
        events.append(ev)
    errors = [
        e
        for e in events
        if e.get("event") in {"agent_error", "turn_failed", "room_error"}
    ]
    assert not errors, f"large image tripped the transport guard: {errors!r}"
    assert any(e.get("event") == "turn_completed" for e in events)


def test_command_code_declares_argv_transport() -> None:
    adapter = CommandCodeAdapter()
    capability = resolve_prompt_transport_capability(adapter)
    # build_exec_argv puts the prompt in `-p <prompt>`: the diagnostics must
    # say argv, not stdin/unknown.
    assert capability.transport_mode == "argv"


def test_inline_base64_never_reaches_cli_render() -> None:
    turn = AgentTurn(
        user_message="看看这张图",
        metadata={"inline_images": [{"media_type": "image/png", "data": _PNG_BASE64}]},
    )
    rendered = AgentPromptRenderer.render_for_single_prompt(turn)
    assert "data:image/" not in rendered
    assert ";base64," not in rendered


def test_guard_counts_actual_wire_payload_not_raw_attachment() -> None:
    turn = AgentTurn(user_message="看看这张图 " * 100)
    assert _estimate_wire_bytes(turn) < 8 * 1024 * 1024
    # Raw attachment bytes must never be part of this estimate: the guard
    # measures the wire payload, while attachment policy lives elsewhere.
    assert _estimate_wire_bytes(turn) < len(_BIG_BYTES)
