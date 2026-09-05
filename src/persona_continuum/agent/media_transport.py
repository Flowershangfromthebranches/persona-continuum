"""Adapter-side media transport: how one attachment reaches one runtime.

The Room layer resolves uploads into canonical ``AgentAttachment`` records
(id/kind/mime/size + a store-local path).  It never decides the carrier.
Each adapter declares, per attachment kind, one ``MediaInputMode``:

- LOCAL_PATH: the runtime reads the file from disk (CLI ``@path`` / file
  read).  The wire prompt carries only a short path reference.
- NATIVE_PROTOCOL: the wire protocol has a native image item (Codex
  app-server ``localImage``).  The item carries a path, never base64.
- INLINE_BASE64: the provider HTTP API wants inline base64 content blocks.
  Only HTTP adapters may use this.
- EXTRACTED_CONTENT: the binary is unusable, but extracted text may travel.
- UNSUPPORTED: fail loudly with MEDIA_INPUT_UNSUPPORTED, never drop.

The transport guard measures only the actual wire text.  Attachment bytes
are governed by adapter media budgets, never by the argv/stdin text budget.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from persona_continuum.agent.models import (
    AgentAttachment,
    AgentCapabilityFlags,
    MediaInputMode,
)
from persona_continuum.agent.response_collector import (
    AttachmentAccessDeniedError,
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    MediaInputUnsupportedError,
)
from persona_continuum.security.paths import ensure_child_path

#: Per-image ceiling for inline base64 carriers (HTTP providers).  CLI
#: adapters never inline, so this does not gate local-path delivery.
INLINE_IMAGE_MAX_BYTES = 20 * 1024 * 1024

#: Extractable text/document suffixes: content is parsed with the existing
#: ingestion pipeline (same parsers as persona sources) and travels as
#: structured text.  Everything else binary is EXTRACTED_CONTENT only when
#: text was actually produced, otherwise UNSUPPORTED at the adapter.
EXTRACTABLE_SUFFIXES = frozenset(
    {".txt", ".md", ".json", ".jsonl", ".csv", ".html", ".htm", ".docx", ".pdf"}
)

#: Cap for extracted attachment text per file on the wire.
EXTRACTED_TEXT_MAX_CHARS = 20_000


def media_input_mode_for(
    capabilities: AgentCapabilityFlags | None,
    kind: str,
) -> str:
    """Resolve the carrier for one attachment kind on one adapter."""

    modes = dict((capabilities.media_input_modes if capabilities else {}) or {})
    raw = str(modes.get(str(kind or "").lower()) or "").strip().lower()
    valid = {mode.value for mode in MediaInputMode}
    if raw in valid:
        return raw
    if kind == "image" and bool(getattr(capabilities, "images", False)):
        return MediaInputMode.INLINE_BASE64.value
    return MediaInputMode.UNSUPPORTED.value


def resolve_attachment_path(
    attachment: AgentAttachment,
    uploads_root: Path,
) -> Path:
    """Resolve a canonical attachment to its store file, contained.

    ``local_path`` may be absolute (preferred: the Room layer writes the
    absolute store path) or store-relative (legacy records).  Only files
    inside the attachment store are addressable: traversal, absolute
    escapes and missing files fail loudly instead of reaching a CLI
    command line.
    """

    raw_path = str(attachment.local_path or "").strip()
    if not raw_path:
        raise AttachmentNotFoundError(
            f"attachment_missing_path:{attachment.id}",
            phase="session_prompt",
            diagnostics={"attachment_id": attachment.id},
        )
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = uploads_root / candidate
    try:
        resolved = ensure_child_path(uploads_root, candidate)
    except Exception as exc:
        raise AttachmentAccessDeniedError(
            f"attachment_path_denied:{attachment.id}",
            phase="session_prompt",
            diagnostics={"attachment_id": attachment.id},
        ) from exc
    if not resolved.is_file():
        raise AttachmentNotFoundError(
            f"attachment_not_found:{attachment.id}",
            phase="session_prompt",
            diagnostics={"attachment_id": attachment.id},
        )
    return resolved


def check_inline_budget(attachment: AgentAttachment) -> None:
    if int(attachment.size_bytes or 0) > INLINE_IMAGE_MAX_BYTES:
        raise AttachmentTooLargeError(
            f"attachment_too_large:{attachment.id}",
            phase="session_prompt",
            diagnostics={
                "attachment_id": attachment.id,
                "size_bytes": int(attachment.size_bytes or 0),
                "limit_bytes": INLINE_IMAGE_MAX_BYTES,
            },
        )


def read_attachment_base64(
    attachment: AgentAttachment,
    uploads_root: Path,
) -> str:
    check_inline_budget(attachment)
    return base64.b64encode(resolve_attachment_path(attachment, uploads_root).read_bytes()).decode(
        "ascii"
    )


def local_path_reference(attachment: AgentAttachment) -> str:
    """Short wire-safe reference for LOCAL_PATH carriers (CLI @path)."""

    return f"@{attachment.local_path}"


def describe_unconsumed_attachment(
    attachment: AgentAttachment,
    mode: str,
    *,
    reason: str = "",
) -> str:
    name = attachment.filename or "attachment"
    suffix = f"（{reason}）" if reason else ""
    if mode == MediaInputMode.UNSUPPORTED.value:
        return (
            f"[用户上传{attachment.kind or '附件'}: {name} "
            f"({attachment.mime_type})，当前模型通道不支持此类型{suffix}]"
        )
    return (
        f"[用户上传{attachment.kind or '附件'}: {name} "
        f"({attachment.mime_type})，内容未随本轮发送{suffix}]"
    )


def require_consumable_or_raise(
    attachment: AgentAttachment,
    mode: str,
    *,
    adapter_id: str = "",
) -> None:
    if mode == MediaInputMode.UNSUPPORTED.value:
        raise MediaInputUnsupportedError(
            f"media_input_unsupported:{attachment.kind}:{attachment.mime_type}",
            phase="session_prompt",
            diagnostics={
                "adapter": adapter_id,
                "attachment_id": attachment.id,
                "kind": attachment.kind,
                "mime_type": attachment.mime_type,
            },
        )


def prepared_media_summary(attachments: list[AgentAttachment]) -> dict[str, Any]:
    return {
        "count": len(attachments),
        "kinds": sorted({str(a.kind or "file") for a in attachments}),
        "bytes": sum(int(a.size_bytes or 0) for a in attachments),
    }


def extract_attachment_text(
    attachment: AgentAttachment,
    uploads_root: Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
) -> str | None:
    """Extract readable text from a document attachment, or None.

    Reuses the existing ingestion parsers (txt/md/json/csv/html/docx/pdf):
    no new dependency, same ``unsupported_source_type`` contract as persona
    sources.  Returns None when the file has no extractable text (binary
    media, unknown suffix, empty parse) so the caller falls through to the
    adapter's native carrier or a loud UNSUPPORTED error -- never a fake
    "model received the file" claim.
    """

    from persona_continuum.ingestion.loader import SourceLoader

    suffix = Path(attachment.filename or "").suffix.lower()
    if suffix not in EXTRACTABLE_SUFFIXES:
        return None
    try:
        path = resolve_attachment_path(attachment, uploads_root)
    except Exception:
        return None
    try:
        docs = SourceLoader(max_bytes=max_bytes).load(path)
    except Exception:
        return None
    text = "\n\n".join(doc.content.strip() for doc in docs if doc.content.strip())
    text = text.strip()
    if not text:
        return None
    if len(text) > EXTRACTED_TEXT_MAX_CHARS:
        text = text[:EXTRACTED_TEXT_MAX_CHARS] + "\n…[内容过长已截断]"
    return text
