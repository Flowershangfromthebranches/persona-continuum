from __future__ import annotations

import base64

import pytest
from starlette.testclient import TestClient

from persona_continuum.agent.models import AgentTurn
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.protocols.openai_compatible import OpenAICompatibleAPIAdapter
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.room.models import ParticipantSlot
from persona_continuum.web.server import create_web_app

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_PNG_BASE64 = base64.b64encode(_PNG_BYTES).decode("ascii")


def _persona(continuum: PersonaContinuum, persona_id: str) -> None:
    continuum.personas.create_from_manifest(
        {
            "id": persona_id,
            "display_name": persona_id,
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )


def _room(app: PersonaContinuum) -> str:
    _persona(app, "alice")
    _persona(app, "bob")
    room = app.orchestrator.create_room(
        title="Attachment room",
        topic="attachments",
        participants=[
            ParticipantSlot(
                participant_id="slot_alice",
                persona_id="alice",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
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


@pytest.mark.anyio
async def test_upload_attachment_persists_file(app: PersonaContinuum) -> None:
    room_id = _room(app)
    record = app.orchestrator.upload_room_attachment(
        room_id, filename="photo.png", mime="image/png", content_base64=_PNG_BASE64
    )
    assert record["filename"] == "photo.png"
    assert record["mime"] == "image/png"
    assert record["size"] == len(_PNG_BYTES)
    assert record["kind"] == "image"
    assert record["room_id"] == room_id
    assert record["url"] == f"/api/room-attachments/{record['id']}"
    assert "stored_name" not in record

    stored = app.orchestrator.get_room_attachment(record["id"])
    assert stored is not None
    path = (
        app.config.room_uploads_dir / room_id / str(stored.get("stored_name"))
    )
    assert path.read_bytes() == _PNG_BYTES


@pytest.mark.anyio
async def test_upload_rejects_bad_base64_and_missing_room(app: PersonaContinuum) -> None:
    room_id = _room(app)
    with pytest.raises(ValueError, match="attachment_base64_invalid"):
        app.orchestrator.upload_room_attachment(
            room_id, filename="x.png", mime="image/png", content_base64="!!!"
        )
    with pytest.raises(KeyError):
        app.orchestrator.upload_room_attachment(
            "room_missing", filename="x.png", mime="image/png", content_base64=_PNG_BASE64
        )


@pytest.mark.anyio
async def test_inject_message_with_attachments(app: PersonaContinuum) -> None:
    room_id = _room(app)
    await app.orchestrator.start_room(room_id)
    record = app.orchestrator.upload_room_attachment(
        room_id, filename="photo.png", mime="image/png", content_base64=_PNG_BASE64
    )

    # Text + file travel together in one message.
    event = await app.orchestrator.inject_message(
        room_id,
        "看看这张图",
        client_message_id="msg-1",
        attachment_ids=[record["id"]],
    )
    message = event["message"]
    assert message["content"] == "看看这张图"
    assert message["attachments"] == [record]

    # File-only message is allowed; the transcript keeps both shapes.
    event2 = await app.orchestrator.inject_message(
        room_id, "", client_message_id="msg-2", attachment_ids=[record["id"]]
    )
    assert event2["message"]["content"] == ""

    state = app.orchestrator.get_room(room_id)
    assert state is not None
    assert state.transcript[-1]["attachments"] == [record]

    # Unknown attachment ids fail loudly instead of sending half a message.
    with pytest.raises(ValueError, match="attachment_not_found"):
        await app.orchestrator.inject_message(
            room_id, "hi", client_message_id="msg-3", attachment_ids=["ghost"]
        )


@pytest.mark.anyio
async def test_attachment_described_in_next_turn_prompt(app: PersonaContinuum) -> None:
    room_id = _room(app)
    await app.orchestrator.start_room(room_id)
    record = app.orchestrator.upload_room_attachment(
        room_id, filename="spec.pdf", mime="application/pdf", content_base64=_PNG_BASE64
    )
    await app.orchestrator.inject_message(
        room_id, "评审这份文档", client_message_id="msg-1", attachment_ids=[record["id"]]
    )

    events = []
    async for ev in app.orchestrator.step_turn(
        room_id, manual_speaker_id="slot_alice", user_message=""
    ):
        events.append(ev)
    assert any(e.get("event") == "turn_completed" for e in events)

    # Non-image files stay text-only: no multimodal message is built.
    state = app.orchestrator.get_room(room_id)
    assert state is not None
    messages, inline = app.orchestrator._turn_image_messages(state)
    assert messages == [] and inline == []
    assert app.orchestrator._turn_attachments(state)[0]["kind"] == "file"


@pytest.mark.anyio
async def test_image_attachment_builds_multimodal_message(app: PersonaContinuum) -> None:
    room_id = _room(app)
    await app.orchestrator.start_room(room_id)
    record = app.orchestrator.upload_room_attachment(
        room_id, filename="photo.png", mime="image/png", content_base64=_PNG_BASE64
    )
    await app.orchestrator.inject_message(
        room_id, "看看这张图", client_message_id="msg-1", attachment_ids=[record["id"]]
    )

    # Canonical path: the room hands metadata-only attachments to the turn.
    # No bytes are read in the room layer; the adapter materialises the
    # carrier (local path / native protocol / inline base64) at its boundary.
    state = app.orchestrator.get_room(room_id)
    assert state is not None
    canonical = app.orchestrator._turn_attachments(state)
    assert len(canonical) == 1
    assert canonical[0]["mime_type"] == "image/png"
    assert canonical[0]["local_path"].endswith(".png")
    assert "data:" not in canonical[0]["local_path"]

    # Legacy inline builder is preserved for backward compatibility.
    state = app.orchestrator.get_room(room_id)
    assert state is not None
    messages, inline = app.orchestrator._turn_image_messages(state)
    assert len(messages) == 1
    parts = messages[0]["content"]
    assert parts[0] == {"type": "text", "text": ""}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert inline and inline[0]["media_type"] == "image/png"


def test_prompt_renderer_emits_image_content_blocks() -> None:
    turn = AgentTurn(
        user_message="看看这张图",
        metadata={"inline_images": [{"media_type": "image/png", "data": _PNG_BASE64}]},
    )
    messages = AgentPromptRenderer.render_for_native_roles(turn)
    user_messages = [m for m in messages if m.get("role") == "user"]
    assert len(user_messages) == 1
    content = user_messages[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看看这张图"}
    assert content[1]["type"] == "image_url"

    # Plain turns keep the exact legacy string shape (CLI transports).
    plain = AgentPromptRenderer.render_for_native_roles(AgentTurn(user_message="hi"))
    assert plain[-1] == {"role": "user", "content": "hi"}


def test_provider_converters_anthropic_and_google() -> None:
    adapter = OpenAICompatibleAPIAdapter(
        adapter_id="api_test",
        base_url="http://127.0.0.1:1/v1",
    )
    turn = AgentTurn(
        user_message="看看这张图",
        metadata={"inline_images": [{"media_type": "image/png", "data": _PNG_BASE64}]},
    )
    native = AgentPromptRenderer.render_for_native_roles(turn)
    user_message = next(m for m in native if m.get("role") == "user")

    anthropic = adapter._anthropic_message(user_message, turn)
    assert anthropic["content"][0] == {"type": "text", "text": "看看这张图"}
    source = anthropic["content"][1]
    assert source["type"] == "image"
    assert source["source"]["media_type"] == "image/png"
    assert source["source"]["data"] == _PNG_BASE64

    google = adapter._google_parts(user_message, turn)
    assert google and google[0]["inline_data"]["mime_type"] == "image/png"
    assert google[0]["inline_data"]["data"] == _PNG_BASE64


def test_room_attachment_api(app: PersonaContinuum) -> None:
    room_id = _room(app)
    with TestClient(create_web_app(app)) as client:
        uploaded = client.post(
            f"/api/rooms/{room_id}/attachments",
            json={
                "filename": "photo.png",
                "mime": "image/png",
                "content_base64": _PNG_BASE64,
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        record = uploaded.json()["data"]
        assert record["kind"] == "image"

        by_id = client.get(record["url"])
        assert by_id.status_code == 200
        assert by_id.content == _PNG_BYTES

        bad = client.post(
            f"/api/rooms/{room_id}/attachments",
            json={"filename": "x.png", "content_base64": "!!!"},
        )
        assert bad.status_code == 400

        missing_room = client.post(
            "/api/rooms/room_missing/attachments",
            json={"filename": "x.png", "content_base64": _PNG_BASE64},
        )
        assert missing_room.status_code == 404

        await_started = client.post(f"/api/rooms/{room_id}/start")
        assert await_started.status_code == 200, await_started.text
        injected = client.post(
            f"/api/rooms/{room_id}/inject",
            json={
                "content": "看看这张图",
                "client_message_id": "api-msg-1",
                "attachments": [record["id"]],
            },
        )
        assert injected.status_code == 200, injected.text
        assert injected.json()["data"]["message"]["attachments"] == [record]

        fetched = client.get(f"/api/rooms/{room_id}")
        assert fetched.status_code == 200
        transcript = fetched.json()["data"]["transcript"]
        assert transcript[-1]["attachments"] == [record]
