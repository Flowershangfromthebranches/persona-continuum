from __future__ import annotations

import base64
import json

import pytest

from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.adapters.command_code import CommandCodeAdapter
from persona_continuum.agent.contracts import adapter_wire_text
from persona_continuum.agent.media_transport import (
    media_input_mode_for,
    resolve_attachment_path,
)
from persona_continuum.agent.models import (
    AgentAttachment,
    AgentCapabilityFlags,
    AgentTurn,
    MediaInputMode,
)
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.prompt_transport import (
    resolve_prompt_transport_capability,
)
from persona_continuum.agent.response_collector import (
    AttachmentAccessDeniedError,
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    MediaInputUnsupportedError,
)
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.room.models import ParticipantSlot

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_PNG_BASE64 = base64.b64encode(_PNG_BYTES).decode("ascii")
_BIG_BYTES = bytes(((i * 2654435761) >> 16) & 0xFF for i in range(6 * 1024 * 1024))
_BIG_BASE64 = base64.b64encode(_BIG_BYTES).decode("ascii")


def _attachment(**overrides) -> AgentAttachment:
    payload = {
        "id": "att_1",
        "kind": "image",
        "mime_type": "image/png",
        "filename": "photo.png",
        "size_bytes": len(_PNG_BYTES),
        "local_path": "/store/room_1/photo.png",
        "url": "/api/room-attachments/att_1",
    }
    payload.update(overrides)
    return AgentAttachment.model_validate(payload)


def _persona(app: PersonaContinuum, persona_id: str) -> None:
    app.personas.create_from_manifest(
        {
            "id": persona_id,
            "display_name": persona_id,
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )


def _room_id(app: PersonaContinuum) -> str:
    _persona(app, "alice")
    _persona(app, "bob")
    room = app.orchestrator.create_room(
        title="Media matrix room",
        topic="media",
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


def test_cli_text_wire_has_no_base64() -> None:
    adapter = CommandCodeAdapter()
    turn = AgentTurn(
        user_message="看看这张图",
        attachments=[_attachment()],
    )
    wire = adapter_wire_text(adapter, turn)
    assert "data:image/" not in wire
    assert ";base64," not in wire
    assert _attachment().local_path in wire
    assert resolve_prompt_transport_capability(adapter).transport_mode == "argv"


def test_command_code_builds_local_path_block() -> None:
    adapter = CommandCodeAdapter()
    turn = AgentTurn(
        user_message="看看这张图", attachments=[_attachment(), _attachment(id="att_2")]
    )
    block = adapter._attachment_prompt_block(turn)
    assert block.count("@") == 0  # stable carrier is the plain store path
    assert "/store/room_1/photo.png" in block
    assert "data:image/" not in block
    assert ";base64," not in block


def test_codex_app_server_builds_local_image_items() -> None:
    adapter = CodexAdapter()
    turn = AgentTurn(user_message="看看这张图", attachments=[_attachment()])
    items = adapter._attachment_inputs(turn)
    assert items == [{"type": "localImage", "path": "/store/room_1/photo.png"}]
    payload = json.dumps(items)
    assert "base64" not in payload


def test_codex_rejects_non_image_loudly() -> None:
    adapter = CodexAdapter()
    turn = AgentTurn(user_message="读文档", attachments=[_attachment(kind="file")])
    with pytest.raises(MediaInputUnsupportedError) as excinfo:
        adapter._attachment_inputs(turn)
    assert excinfo.value.code == "MEDIA_INPUT_UNSUPPORTED"


def test_unsupported_adapter_reports_unsupported() -> None:
    from persona_continuum.agent.media_transport import require_consumable_or_raise

    with pytest.raises(MediaInputUnsupportedError) as excinfo:
        require_consumable_or_raise(
            _attachment(kind="video"),
            MediaInputMode.UNSUPPORTED.value,
            adapter_id="plain",
        )
    assert excinfo.value.code == "MEDIA_INPUT_UNSUPPORTED"


def test_path_traversal_rejected(tmp_path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    evil = _attachment(local_path="../../etc/passwd")
    with pytest.raises(AttachmentAccessDeniedError) as excinfo:
        resolve_attachment_path(evil, uploads)
    assert excinfo.value.code == "ATTACHMENT_ACCESS_DENIED"


def test_missing_attachment_file_rejected(tmp_path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    ghost = _attachment(local_path="room_1/ghost.png")
    with pytest.raises(AttachmentNotFoundError) as excinfo:
        resolve_attachment_path(ghost, uploads)
    assert excinfo.value.code == "ATTACHMENT_NOT_FOUND"


def test_missing_local_path_rejected(tmp_path) -> None:
    with pytest.raises(AttachmentNotFoundError):
        resolve_attachment_path(_attachment(local_path=""), tmp_path)


def test_inline_budget_rejects_oversize() -> None:
    from persona_continuum.agent.media_transport import check_inline_budget

    huge = _attachment(size_bytes=21 * 1024 * 1024)
    with pytest.raises(AttachmentTooLargeError) as excinfo:
        check_inline_budget(huge)
    assert excinfo.value.code == "ATTACHMENT_TOO_LARGE"


def test_extract_attachment_text_txt_and_unsupported(tmp_path) -> None:
    from persona_continuum.agent.media_transport import extract_attachment_text

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    doc = uploads / "note.txt"
    doc.write_text("hello attachment world", encoding="utf-8")
    text_attachment = _attachment(
        id="att_txt",
        kind="file",
        mime_type="text/plain",
        filename="note.txt",
        size_bytes=doc.stat().st_size,
        local_path=str(doc),
    )
    assert extract_attachment_text(text_attachment, uploads) == "hello attachment world"

    binary = _attachment(
        id="att_bin", kind="file", mime_type="application/octet-stream", filename="a.bin"
    )
    assert extract_attachment_text(binary, uploads) is None


def test_extract_attachment_text_stays_outside_cli_budget(tmp_path) -> None:
    from persona_continuum.agent.media_transport import (
        EXTRACTED_TEXT_MAX_CHARS,
        extract_attachment_text,
    )

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    big = uploads / "big.txt"
    big.write_text("x" * (EXTRACTED_TEXT_MAX_CHARS + 1000), encoding="utf-8")
    attachment = _attachment(
        id="att_big",
        kind="file",
        mime_type="text/plain",
        filename="big.txt",
        size_bytes=big.stat().st_size,
        local_path=str(big),
    )
    text = extract_attachment_text(attachment, uploads)
    assert text is not None
    assert len(text) <= EXTRACTED_TEXT_MAX_CHARS + 32


def test_media_mode_resolution_prefers_declared_map() -> None:
    caps = AgentCapabilityFlags(
        images=True,
        media_input_modes={"image": "local_path", "file": "extracted_content"},
    )
    assert media_input_mode_for(caps, "image") == "local_path"
    assert media_input_mode_for(caps, "file") == "extracted_content"
    assert media_input_mode_for(caps, "video") == "unsupported"
    assert media_input_mode_for(None, "image") == "unsupported"
    # Legacy images=True without a declared map keeps the HTTP inline path.
    assert media_input_mode_for(AgentCapabilityFlags(images=True), "image") == (
        "inline_base64"
    )


def test_shared_renderer_stays_byte_free_with_canonical_attachments() -> None:
    turn = AgentTurn(user_message="看看这张图", attachments=[_attachment()])
    single = AgentPromptRenderer.render_for_single_prompt(turn)
    assert "data:image/" not in single
    assert ";base64," not in single
    native = AgentPromptRenderer.render_for_native_roles(turn)
    assert native[-1] == {"role": "user", "content": "看看这张图"}


@pytest.mark.anyio
async def test_multiple_images_text_plus_image_room_turn(app: PersonaContinuum) -> None:
    room_id = _room_id(app)
    await app.orchestrator.start_room(room_id)
    first = app.orchestrator.upload_room_attachment(
        room_id, filename="a.png", mime="image/png", content_base64=_PNG_BASE64
    )
    second = app.orchestrator.upload_room_attachment(
        room_id, filename="b.png", mime="image/png", content_base64=_PNG_BASE64
    )
    await app.orchestrator.inject_message(
        room_id,
        "对比这两张图",
        client_message_id="multi-1",
        attachment_ids=[first["id"], second["id"]],
    )
    state = app.orchestrator.get_room(room_id)
    assert state is not None
    canonical = app.orchestrator._turn_attachments(state)
    assert len(canonical) == 2

    events = []
    async for ev in app.orchestrator.step_turn(
        room_id, manual_speaker_id="slot_alice", user_message=""
    ):
        events.append(ev)
    assert any(e.get("event") == "turn_completed" for e in events)


@pytest.mark.anyio
async def test_history_does_not_reinline_old_images(app: PersonaContinuum) -> None:
    room_id = _room_id(app)
    await app.orchestrator.start_room(room_id)
    record = app.orchestrator.upload_room_attachment(
        room_id, filename="a.png", mime="image/png", content_base64=_PNG_BASE64
    )
    await app.orchestrator.inject_message(
        room_id, "第一张", client_message_id="h1", attachment_ids=[record["id"]]
    )
    events = []
    async for ev in app.orchestrator.step_turn(
        room_id, manual_speaker_id="slot_alice", user_message=""
    ):
        events.append(ev)
    assert any(e.get("event") == "turn_completed" for e in events)

    # A later text-only user message must not re-attach the old image: only
    # the newest user message's attachments travel.
    await app.orchestrator.inject_message(room_id, "继续聊", client_message_id="h2")
    state = app.orchestrator.get_room(room_id)
    assert state is not None
    assert app.orchestrator._turn_attachments(state) == []


@pytest.mark.anyio
async def test_guard_measures_cli_wire_text_not_raw_bytes(app: PersonaContinuum) -> None:
    from persona_continuum.agent.contracts import adapter_wire_text

    room_id = _room_id(app)
    await app.orchestrator.start_room(room_id)
    record = app.orchestrator.upload_room_attachment(
        room_id, filename="big.png", mime="image/png", content_base64=_BIG_BASE64
    )
    await app.orchestrator.inject_message(
        room_id, "大图", client_message_id="g1", attachment_ids=[record["id"]]
    )
    state = app.orchestrator.get_room(room_id)
    assert state is not None
    canonical = app.orchestrator._turn_attachments(state)
    adapter = CommandCodeAdapter()
    turn = AgentTurn(user_message="大图", attachments=canonical)
    wire = adapter_wire_text(adapter, turn)
    assert len(wire.encode("utf-8")) < 64 * 1024
    assert len(wire.encode("utf-8")) < len(_BIG_BYTES)


@pytest.mark.anyio
async def test_session_resume_keeps_attachment_reference(app: PersonaContinuum) -> None:
    room_id = _room_id(app)
    await app.orchestrator.start_room(room_id)
    record = app.orchestrator.upload_room_attachment(
        room_id, filename="a.png", mime="image/png", content_base64=_PNG_BASE64
    )
    await app.orchestrator.inject_message(
        room_id, "记住这张图", client_message_id="r1", attachment_ids=[record["id"]]
    )
    resumed = await app.orchestrator.resume_from_storage(room_id)
    canonical = app.orchestrator._turn_attachments(resumed)
    assert len(canonical) == 1
    assert canonical[0]["id"] == record["id"]
