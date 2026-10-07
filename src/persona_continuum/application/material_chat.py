"""Large Conversation Pipeline V2 primitives: speaker roles and turns.

This module is deterministic and local-first.  It never deletes or rewrites
raw EvidenceUnits: ConversationTurn is an analysis-layer view that packs
consecutive same-speaker chat messages into one unit of Agent attention while
every underlying message id stays traceable through evidence_unit_ids.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Semantic pipeline states persisted per raw EvidenceUnit.  These values live
# in EvidenceUnit.metadata["semantic_status"] and are the single source of
# truth for progress accounting: context-only messages must never make a
# classification job look permanently pending.
SEMANTIC_TARGET_PENDING = "target_pending"
SEMANTIC_CONTEXT_ONLY = "context_only"
SEMANTIC_REVIEWED_NO_EVIDENCE = "reviewed_no_independent_evidence"
SEMANTIC_EVIDENCE_EXTRACTED = "evidence_extracted"

ROLE_TARGET = "target_persona"
ROLE_EXPORTER = "exporter"
ROLE_UNKNOWN = "unknown"

CHAT_SOURCE_KINDS = {"chat", "chat_import", "guided_interview"}

# Header declarations such as: # 对方 = 目标 Persona / # 我 = 聊天记录导出者
_HEADER_ROLE_LINE = re.compile(r"^#\s*([^=＃#]{1,40}?)\s*[=＝]\s*(.+?)\s*$")
_TARGET_HINT = re.compile(r"目标|persona", re.IGNORECASE)
_EXPORTER_HINT = re.compile(r"导出|发送者本人|发送者|exporter|self|owner", re.IGNORECASE)


def parse_speaker_role_map(text: str) -> dict[str, str]:
    """Map declared speaker names to target_persona / exporter.

    Only an explicit header assigns a role; anything ambiguous stays out of
    the map so the caller keeps the conservative (still-analyzed) behavior.
    """

    mapping: dict[str, str] = {}
    for line in str(text or "").splitlines()[:64]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            break
        match = _HEADER_ROLE_LINE.match(stripped)
        if match is None:
            continue
        name = match.group(1).strip()
        declaration = match.group(2)
        if not name:
            continue
        if _TARGET_HINT.search(declaration):
            mapping[name] = ROLE_TARGET
        elif _EXPORTER_HINT.search(declaration):
            mapping[name] = ROLE_EXPORTER
    return mapping


def conversation_id_of(unit: Any) -> str:
    metadata = dict(getattr(unit, "metadata", None) or {})
    row = dict(metadata.get("row") or {})
    locator = dict(getattr(unit, "source_locator", None) or {})
    return str(
        row.get("conversation_id")
        or metadata.get("conversation_id")
        or row.get("thread_id")
        or metadata.get("thread_id")
        or locator.get("conversation_id")
        or "default"
    )


def parse_turn_time(value: Any) -> datetime | None:
    if not value:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return None


class ConversationTurn(BaseModel):
    """Non-destructive analysis layer over raw EvidenceUnits (never a replacement)."""

    id: str
    anchor_id: str
    source_id: str
    conversation_id: str | None = None
    speaker: str | None = None
    speaker_role: str = ROLE_UNKNOWN
    # evidence_source turns are classified; context_only turns ride in the
    # prompt to explain neighbours but never produce independent persona
    # evidence of their own.
    semantic_role: str = "evidence_source"
    start_time: str | None = None
    end_time: str | None = None
    text: str
    evidence_unit_ids: list[str] = Field(default_factory=list)
    source_kind: str = "user_provided"

    @property
    def timestamp(self) -> str | None:
        """Window packing reads timestamp; for a turn that is its end time."""

        return self.end_time

    @property
    def is_target(self) -> bool:
        return self.semantic_role != "context_only"

    @property
    def member_ids(self) -> list[str]:
        return list(self.evidence_unit_ids)


def turn_from_units(members: Sequence[Any]) -> ConversationTurn:
    anchor = members[0]
    ids = [str(item.id) for item in members]
    timestamps = [str(item.timestamp) for item in members if getattr(item, "timestamp", None)]
    speaker = str(getattr(anchor, "speaker", None) or "") or None
    roles = {str(getattr(item, "speaker_role", None) or "") for item in members}
    declared = {ROLE_TARGET, ROLE_EXPORTER} & {value for value in roles if value}
    speaker_role = next(iter(declared)) if len(declared) == 1 else ROLE_UNKNOWN
    semantic_statuses = {
        str((getattr(item, "metadata", None) or {}).get("semantic_status") or "")
        for item in members
    }
    context_only = semantic_statuses == {SEMANTIC_CONTEXT_ONLY}
    text = "\n".join(str(item.text) for item in members)
    source_kind = str(getattr(anchor, "source_kind", None) or "user_provided")
    chat_speaker = speaker if source_kind in CHAT_SOURCE_KINDS else None
    # The turn id IS the anchor raw unit id, so window packing, checkpoint
    # accounting and Agent output rows carry real ledger ids end to end.
    return ConversationTurn(
        id=ids[0],
        anchor_id=ids[0],
        source_id=str(anchor.source_id),
        conversation_id=conversation_id_of(anchor) if chat_speaker else None,
        speaker=chat_speaker,
        speaker_role=speaker_role,
        semantic_role="context_only" if context_only else "evidence_source",
        start_time=min(timestamps) if timestamps else None,
        end_time=max(timestamps) if timestamps else None,
        text=text,
        evidence_unit_ids=ids,
        source_kind=source_kind,
    )


def fold_conversation_turns(
    units: Sequence[Any],
    *,
    gap_seconds: int = 90,
) -> list[ConversationTurn]:
    """Pack consecutive same-speaker chat messages into analysis turns.

    A turn flushes on speaker change, conversation/source change, a gap
    longer than gap_seconds, a missing timestamp, or non-chat material.
    Every raw unit id survives inside exactly one turn; nothing is dropped.
    Input order (source, segment_index) must be the conversation order.
    """

    turns: list[ConversationTurn] = []
    bucket: list[Any] = []
    bucket_key: tuple[str, str, str] | None = None
    bucket_end: datetime | None = None

    def merge_key(unit: Any) -> tuple[str, str, str] | None:
        if str(getattr(unit, "source_kind", "") or "") not in CHAT_SOURCE_KINDS:
            return None
        speaker = str(getattr(unit, "speaker", None) or "")
        if not speaker:
            return None
        return (str(unit.source_id), conversation_id_of(unit), speaker)

    def flush() -> None:
        nonlocal bucket, bucket_key, bucket_end
        if bucket:
            turns.append(turn_from_units(bucket))
        bucket = []
        bucket_key = None
        bucket_end = None

    for unit in units:
        key = merge_key(unit)
        current_time = parse_turn_time(getattr(unit, "timestamp", None))
        if key is None:
            flush()
            turns.append(turn_from_units([unit]))
            continue
        mergeable = (
            bucket
            and bucket_key == key
            and bucket_end is not None
            and current_time is not None
            and (current_time - bucket_end).total_seconds() <= max(0, int(gap_seconds))
            and (current_time - bucket_end).total_seconds() >= 0
        )
        if bucket and not mergeable:
            flush()
        bucket.append(unit)
        bucket_key = key
        if current_time is not None:
            bucket_end = current_time
    flush()
    return turns


def merge_turns(
    carry: ConversationTurn | None,
    nxt: ConversationTurn,
    *,
    gap_seconds: int = 90,
) -> ConversationTurn | None:
    """Extend a trailing open turn with the next turn when still contiguous.

    The streaming classification loop uses this so one same-speaker burst is
    never split at an arbitrary checkpoint-batch boundary.  Returns ``None``
    when the turns must stay separate (caller flushes ``carry`` first).
    """

    if carry is None:
        return None
    if (
        carry.source_kind not in CHAT_SOURCE_KINDS
        or nxt.source_kind not in CHAT_SOURCE_KINDS
        or carry.source_id != nxt.source_id
        or carry.speaker is None
        or carry.speaker != nxt.speaker
        or carry.conversation_id != nxt.conversation_id
        or not carry.end_time
        or not nxt.start_time
    ):
        return None
    carry_end = parse_turn_time(carry.end_time)
    next_start = parse_turn_time(nxt.start_time)
    if carry_end is None or next_start is None:
        return None
    delta = (next_start - carry_end).total_seconds()
    if delta < 0 or delta > max(0, int(gap_seconds)):
        return None
    context_only = carry.semantic_role == "context_only" and (nxt.semantic_role == "context_only")
    return carry.model_copy(
        update={
            "evidence_unit_ids": [*carry.evidence_unit_ids, *nxt.evidence_unit_ids],
            "text": carry.text + "\n" + nxt.text,
            "end_time": max(str(carry.end_time), str(nxt.end_time)),
            "semantic_role": "context_only" if context_only else "evidence_source",
        }
    )


__all__ = [
    "CHAT_SOURCE_KINDS",
    "ConversationTurn",
    "ROLE_EXPORTER",
    "ROLE_TARGET",
    "ROLE_UNKNOWN",
    "SEMANTIC_CONTEXT_ONLY",
    "SEMANTIC_EVIDENCE_EXTRACTED",
    "SEMANTIC_REVIEWED_NO_EVIDENCE",
    "SEMANTIC_TARGET_PENDING",
    "conversation_id_of",
    "fold_conversation_turns",
    "merge_turns",
    "parse_speaker_role_map",
    "parse_turn_time",
    "turn_from_units",
]
