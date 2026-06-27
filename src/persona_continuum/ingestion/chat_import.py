from __future__ import annotations

from typing import Any


def normalize_chat_row(
    row: dict[str, Any], field_map: dict[str, str] | None = None
) -> dict[str, Any]:
    mapping = field_map or {}
    return {
        "timestamp": row.get(mapping.get("timestamp", "timestamp")),
        "sender": row.get(mapping.get("sender", "sender")),
        "recipient": row.get(mapping.get("recipient", "recipient")),
        "content": row.get(mapping.get("content", "content")),
        "conversation_id": row.get(mapping.get("conversation_id", "conversation_id")),
    }
