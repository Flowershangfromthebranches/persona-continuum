"""Transport-aware prompt packing for Room turns.

Why this exists
---------------
``PROMPT_TRANSPORT_LIMIT_EXCEEDED`` is *not* a model-context-window problem.
A 1M-token model behind a CLI that takes its prompt as ``-p <prompt>`` has a
transport budget of only ~64KB because a single argv element is capped by the
kernel (``MAX_ARG_STRLEN`` = 128KB on Linux, and macOS ARG_MAX is ~1MB shared
with the environment).  ``runtime_executor._prepare_turn()`` measures the
finished prompt and rejects it there -- which is correct, but far too late:
by then the prompt is already assembled and the turn fails.

This module packs *before* assembly.  Every section carries an explicit
priority; the packer fills sections from the highest priority down and drops
or truncates the rest until the prompt fits the transport budget.  Sections
are measured in UTF-8 bytes, never in Python characters, because CJK text
costs three bytes per character and a char-count budget under-counts by 3x.

Priority order (never dropped):
    1. system safety / role
    2. persona identity core
    3. current user task
    4. RoomCaseState
    5. current protocol stage instructions
    6. current expert task

Then (dropped/truncated under pressure, in this order):
    7. relevant structured results
    8. compact room summary
    9. recent dialogue

First to go: older raw transcript, duplicated public expert messages,
verbose protocol event history, repeated shared context.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from persona_continuum.agent.prompt_transport import (
    PromptTransportCapability,
    PromptTransportMode,
)

# The transport capability already carries a 10% safety margin.  Room prompts
# additionally carry JSON-escaped payloads and chat scaffolding that are added
# *after* the sections below were measured, so take a second margin on top:
# 57344 (ARGV safe bytes) * 0.85 ~= 48.7KB, inside the 48-52KB target band.
PACK_SAFETY_RATIO = 0.85
# Never plan below this even for a pathological transport declaration; below
# ~16KB the prompt stops being usable for real expert work.
ABSOLUTE_FLOOR_BYTES = 16 * 1024

# Priority ladder.  Lower number = kept longer.
PRIORITY_SYSTEM = 10
PRIORITY_IDENTITY = 20
PRIORITY_TASK = 30
PRIORITY_CASE_STATE = 40
PRIORITY_STAGE = 50
PRIORITY_EXPERT_TASK = 60
PRIORITY_RESULTS = 70
PRIORITY_SUMMARY = 80
PRIORITY_DIALOGUE = 90
PRIORITY_HISTORY = 100


def byte_length(value: Any) -> int:
    """UTF-8 byte length of any prompt fragment. Never count characters."""

    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def budget_for(capability: PromptTransportCapability | None) -> int:
    """Byte budget the packer must respect for one transport capability."""

    if capability is None:
        return ABSOLUTE_FLOOR_BYTES
    safe = int(capability.safe_prompt_bytes)
    return max(ABSOLUTE_FLOOR_BYTES, int(safe * PACK_SAFETY_RATIO))


def conservative_budget() -> int:
    """Smallest budget that must always work (unknown / ARGV transports)."""

    capability = PromptTransportCapability.model_validate(
        {"transport_mode": PromptTransportMode.ARGV.value}
    )
    return budget_for(capability)


class PackedSection(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    key: str
    priority: int
    content: Any
    bytes: int
    truncated: bool = False
    dropped: bool = False
    #: When False the section may be truncated but never removed entirely.
    droppable: bool = True


class PackedPrompt(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sections: list[PackedSection] = Field(default_factory=list)
    budget_bytes: int = 0
    used_bytes: int = 0
    dropped: list[str] = Field(default_factory=list)
    truncated: list[str] = Field(default_factory=list)

    @property
    def fits(self) -> bool:
        return self.used_bytes <= self.budget_bytes

    def payload(self) -> dict[str, Any]:
        """The sections that survived, keyed by section name."""

        return {
            section.key: section.content
            for section in self.sections
            if not section.dropped
        }

    def report(self) -> dict[str, Any]:
        return {
            "budget_bytes": self.budget_bytes,
            "used_bytes": self.used_bytes,
            "fits": self.fits,
            "dropped": list(self.dropped),
            "truncated": list(self.truncated),
            "sections": [
                {
                    "key": section.key,
                    "priority": section.priority,
                    "bytes": section.bytes,
                    "truncated": section.truncated,
                    "dropped": section.dropped,
                }
                for section in self.sections
            ],
        }


class ContextPacker:
    """Fill a transport budget with prompt sections, highest priority first."""

    def __init__(
        self,
        capability: PromptTransportCapability | None = None,
        *,
        budget_bytes: int | None = None,
    ) -> None:
        self.capability = capability
        # An explicit budget overrides the capability-derived one: callers
        # that reserve headroom (e.g. for a synthesis repair retry) compute
        # the effective budget themselves.
        self.budget = budget_bytes if budget_bytes is not None else budget_for(capability)

    # -- section construction ---------------------------------------------
    @staticmethod
    def _section(
        key: str,
        priority: int,
        content: Any,
        *,
        droppable: bool = True,
    ) -> PackedSection:
        return PackedSection(
            key=key,
            priority=priority,
            content=content,
            bytes=byte_length(content),
            droppable=droppable,
        )

    def pack(
        self,
        *,
        system: str = "",
        persona_context: Any = None,
        # Structured task payloads (dict) are allowed: byte_length and _clip
        # handle mappings, and keeping keys whole beats shattering a JSON
        # string under pressure.
        current_task: Any = "",
        case_state: Any = None,
        stage_instruction: str = "",
        expert_task: Any = None,
        structured_results: Any = None,
        room_summary: str = "",
        recent_dialogue: Any = None,
        shared_context: Any = None,
        older_transcript: Any = None,
    ) -> PackedPrompt:
        """Assemble sections and trim them until they fit the budget."""

        sections: list[PackedSection] = []
        if system:
            sections.append(
                self._section(
                    "system", PRIORITY_SYSTEM, system, droppable=False
                )
            )
        if persona_context not in (None, "", [], {}):
            sections.append(
                self._section(
                    "persona_context",
                    PRIORITY_IDENTITY,
                    persona_context,
                    droppable=False,
                )
            )
        if current_task:
            sections.append(
                self._section(
                    "current_task", PRIORITY_TASK, current_task, droppable=False
                )
            )
        if case_state not in (None, "", [], {}):
            sections.append(
                self._section(
                    "case_state", PRIORITY_CASE_STATE, case_state, droppable=False
                )
            )
        if stage_instruction:
            sections.append(
                self._section(
                    "stage_instruction",
                    PRIORITY_STAGE,
                    stage_instruction,
                    droppable=False,
                )
            )
        if expert_task not in (None, "", [], {}):
            sections.append(
                self._section(
                    "expert_task", PRIORITY_EXPERT_TASK, expert_task, droppable=False
                )
            )
        if shared_context not in (None, "", [], {}):
            sections.append(self._section("shared_context", PRIORITY_STAGE, shared_context))
        if structured_results not in (None, "", [], {}):
            sections.append(
                self._section(
                    "structured_results", PRIORITY_RESULTS, structured_results
                )
            )
        if room_summary:
            sections.append(self._section("room_summary", PRIORITY_SUMMARY, room_summary))
        if recent_dialogue not in (None, "", [], {}):
            sections.append(self._section("recent_dialogue", PRIORITY_DIALOGUE, recent_dialogue))
        if older_transcript not in (None, "", [], {}):
            sections.append(self._section("older_transcript", PRIORITY_HISTORY, older_transcript))

        sections.sort(key=lambda item: item.priority)
        used = sum(section.bytes for section in sections if not section.dropped)
        dropped: list[str] = []
        truncated: list[str] = []

        # 1. Drop whole sections from the lowest priority up.
        for section in reversed(sections):
            if used <= self.budget:
                break
            if not section.droppable:
                continue
            section.dropped = True
            used -= section.bytes
            dropped.append(section.key)

        # 2. Still over budget: truncate the surviving droppable sections,
        #    lowest priority first, so a mandatory section is never clipped
        #    before an optional one.
        for section in reversed(sections):
            if used <= self.budget:
                break
            if section.dropped or not section.droppable:
                continue
            excess = used - self.budget
            keep = max(0, section.bytes - excess)
            if keep <= 0:
                section.dropped = True
                used -= section.bytes
                dropped.append(section.key)
                continue
            section.content = _clip(section.content, keep)
            section.bytes = byte_length(section.content)
            section.truncated = True
            truncated.append(section.key)
            used = sum(item.bytes for item in sections if not item.dropped)

        # 3. Last resort: a mandatory section alone can exceed the budget
        #    (huge system prompt).  Clip it too -- a clipped prompt at least
        #    runs, whereas PROMPT_TRANSPORT_LIMIT_EXCEEDED kills the turn.
        for section in reversed(sections):
            if used <= self.budget:
                break
            excess = used - self.budget
            keep = max(0, section.bytes - excess)
            section.content = _clip(section.content, keep)
            section.bytes = byte_length(section.content)
            section.truncated = True
            if section.key not in truncated:
                truncated.append(section.key)
            used = sum(item.bytes for item in sections if not item.dropped)

        return PackedPrompt(
            sections=sections,
            budget_bytes=self.budget,
            used_bytes=used,
            dropped=dropped,
            truncated=truncated,
        )


def _clip(value: Any, keep_bytes: int) -> Any:
    """Truncate a fragment to roughly ``keep_bytes`` UTF-8 bytes."""

    if keep_bytes <= 0:
        return "" if isinstance(value, str) else []
    if isinstance(value, str):
        encoded = value.encode("utf-8")[:keep_bytes]
        return encoded.decode("utf-8", errors="ignore")
    if isinstance(value, list):
        kept: list[Any] = []
        used = 0
        for item in value:
            cost = byte_length(item)
            if used + cost > keep_bytes:
                break
            kept.append(item)
            used += cost
        return kept
    if isinstance(value, dict):
        kept_dict: dict[str, Any] = {}
        used = 0
        # Preserve key order: callers rely on it for readability.
        for key, item in value.items():
            cost = byte_length(item)
            if used + cost > keep_bytes:
                break
            kept_dict[key] = item
            used += cost
        return kept_dict
    return value


# --- transcript compaction -------------------------------------------------


def compact_transcript(
    transcript: list[dict[str, Any]],
    *,
    keep_recent: int = 6,
    per_message_chars: int = 400,
) -> list[dict[str, Any]]:
    """Trim a raw transcript into a transport-cheap dialogue section.

    Only the fields the model needs to follow the conversation survive;
    long bodies are clipped per message.
    """

    window = list(transcript or [])[-max(1, keep_recent) :]
    compacted: list[dict[str, Any]] = []
    for item in window:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if len(content) > per_message_chars:
            content = content[:per_message_chars] + "…"
        entry: dict[str, Any] = {
            "speaker": item.get("speaker_name") or item.get("participant_id") or "",
            "content": content,
        }
        kind = item.get("message_kind")
        if kind:
            entry["kind"] = kind
        compacted.append(entry)
    return compacted


def dedupe_public_messages(
    transcript: list[dict[str, Any]],
    structured_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Drop raw expert prose whose structured conclusion is already present.

    The same information was reaching the prompt three times: as a raw
    transcript row, as the rendered public chat message, and as the
    structured peer result.  The structured form is strictly richer, so the
    raw duplicates go.
    """

    results = structured_results or []
    covered: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            continue
        participant = str(result.get("participant_id") or "").strip()
        if participant:
            covered.add(participant)
    if not covered:
        return list(transcript or [])

    def _message_kind(item: dict[str, Any]) -> str:
        # Transcript rows carry message_kind under ``metadata`` (see the
        # orchestrator's protocol public recorder); older callers may pass it
        # at the top level.  Missing either way means "not a protocol row".
        kind = str(item.get("message_kind") or "")
        if not kind:
            metadata = item.get("metadata")
            if isinstance(metadata, dict):
                kind = str(metadata.get("message_kind") or "")
        return kind

    return [
        item
        for item in (transcript or [])
        if not (
            isinstance(item, dict)
            and str(item.get("participant_id") or "") in covered
            and _message_kind(item).startswith("protocol_")
        )
    ]


def extract_older_user_facts(transcript: list[dict[str, Any]], *, limit: int = 12) -> list[str]:
    """Deterministically salvage earlier user statements from old turns.

    Used when the rolling summary is unavailable: the raw window cannot be
    carried, but what the *user* actually said is small and is exactly what
    must not be lost.
    """

    facts: list[str] = []
    for item in reversed(transcript or []):
        if not isinstance(item, dict):
            continue
        if item.get("participant_id") != "user":
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        facts.append(content[:300])
        if len(facts) >= limit:
            break
    facts.reverse()
    return facts


__all__ = [
    "ABSOLUTE_FLOOR_BYTES",
    "PACK_SAFETY_RATIO",
    "PRIORITY_CASE_STATE",
    "PRIORITY_DIALOGUE",
    "PRIORITY_EXPERT_TASK",
    "PRIORITY_HISTORY",
    "PRIORITY_IDENTITY",
    "PRIORITY_RESULTS",
    "PRIORITY_STAGE",
    "PRIORITY_SUMMARY",
    "PRIORITY_SYSTEM",
    "PRIORITY_TASK",
    "ContextPacker",
    "PackedPrompt",
    "PackedSection",
    "budget_for",
    "byte_length",
    "compact_transcript",
    "conservative_budget",
    "dedupe_public_messages",
    "extract_older_user_facts",
]
