from __future__ import annotations

import base64
import binascii
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from persona_continuum.security.paths import ensure_child_path

_CHUNK_CHARS = 1024 * 1024

_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}

_VIDEO_MIMES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
}


def attachment_kind(mime: str) -> str:
    normalized = (mime or "").lower().split(";")[0].strip()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "audio"
    return "file"


def attachment_supports_inline_vision(mime: str) -> bool:
    return (mime or "").lower().split(";")[0].strip() in _IMAGE_MIMES


def attachment_supports_inline_video(mime: str) -> bool:
    return (mime or "").lower().split(";")[0].strip() in _VIDEO_MIMES


def decode_base64_payload(content_base64: str) -> bytes:
    raw = "".join(str(content_base64 or "").split())
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("attachment_base64_invalid") from exc


def store_room_attachment(
    uploads_root: Path,
    room_id: str,
    filename: str,
    mime: str,
    raw_bytes: bytes,
) -> dict[str, Any]:
    safe_name = Path(str(filename or "attachment")).name or "attachment"
    room_dir = uploads_root / str(room_id)
    room_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{safe_name}"
    stored_path = ensure_child_path(room_dir, room_dir / stored_name)
    stored_path.write_bytes(raw_bytes)
    guessed = (mime or "").strip() or mimetypes.guess_type(safe_name)[0] or (
        "application/octet-stream"
    )
    return {
        "id": uuid.uuid4().hex,
        "filename": safe_name,
        "stored_name": stored_name,
        "mime": guessed,
        "size": len(raw_bytes),
        "kind": attachment_kind(guessed),
    }


def attachment_public_url(attachment_id: str) -> str:
    return f"/api/room-attachments/{attachment_id}"


def attachment_download_url(room_id: str, stored_name: str) -> str:
    return f"/api/rooms/{room_id}/attachments/{stored_name}"


def describe_attachment_for_prompt(attachment: dict[str, Any]) -> str:
    name = str(attachment.get("filename") or "attachment")
    mime = str(attachment.get("mime") or "unknown")
    size = attachment.get("size")
    size_text = f"{int(size)} bytes" if isinstance(size, int) else "unknown size"
    inline = bool(attachment.get("inline"))
    if inline:
        return (
            f"[用户上传{attachment.get('kind') or '附件'}: {name} "
            f"({mime}, {size_text})，内容已附在本消息中，请直接查看]"
        )
    return (
        f"[用户上传{attachment.get('kind') or '附件'}: {name} "
        f"({mime}, {size_text})，二进制内容未内联传输]"
    )
