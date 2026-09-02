"""Requirement §32-adjacent: transport-aware ContextPacker packing rules.

Covers:

- priority drop order: recent_dialogue goes first, mandatory sections stay
- CJK text is measured in UTF-8 bytes, never in Python characters
- ARGV transport budget lands in the ~48.7KB target band (57344 * 0.85)
- ``_clip`` never splits a UTF-8 codepoint in half
"""

from __future__ import annotations

import json

from persona_continuum.agent.prompt_transport import (
    DEFAULT_ARGV_MAX_PROMPT_BYTES,
    PromptTransportCapability,
    PromptTransportMode,
    capability_for_mode,
    default_prompt_transport_capability,
)
from persona_continuum.room.context_packer import (
    ABSOLUTE_FLOOR_BYTES,
    PACK_SAFETY_RATIO,
    ContextPacker,
    _clip,
    budget_for,
    byte_length,
    conservative_budget,
)


def _argv_capability() -> PromptTransportCapability:
    return capability_for_mode(PromptTransportMode.ARGV)


# --- budget ----------------------------------------------------------------


def test_argv_safe_bytes_use_single_argument_ceiling() -> None:
    capability = _argv_capability()
    assert capability.transport_mode == PromptTransportMode.ARGV.value
    assert capability.max_prompt_bytes == DEFAULT_ARGV_MAX_PROMPT_BYTES == 64 * 1024
    # 10% safety margin, floored at 8KB: 65536 - 8192.
    assert capability.safe_prompt_bytes == 57344


def test_budget_for_argv_lands_in_target_band() -> None:
    budget = budget_for(_argv_capability())
    # 57344 * 0.85 = 48742.4 bytes ~= 48.7KB, inside the 48-52KB band.
    assert budget == int(57344 * PACK_SAFETY_RATIO)
    assert 48_000 <= budget <= 52_000


def test_unknown_transport_falls_back_to_conservative_floor() -> None:
    assert budget_for(None) == ABSOLUTE_FLOOR_BYTES
    assert budget_for(default_prompt_transport_capability()) == conservative_budget()
    assert conservative_budget() == budget_for(_argv_capability())


# --- priority drop order -----------------------------------------------------


def test_over_budget_drops_recent_dialogue_first_keeps_mandatory_sections() -> None:
    packer = ContextPacker(_argv_capability())
    packed = packer.pack(
        system="SYSTEM-PROMPT",
        case_state={"intent": "the case anchor"},
        current_task={"question": "what should I do"},
        stage_instruction="STAGE-INSTRUCTION",
        expert_task={"duty": "expert duty"},
        structured_results=[{"summary": "R" * 30_000}],
        room_summary="S" * 20_000,
        recent_dialogue=[{"speaker": "user", "content": "D" * 60_000}],
        older_transcript=[{"speaker": "user", "content": "O" * 60_000}],
    )

    assert packed.fits is True
    # Lowest priority drops first: history, then recent dialogue, then summary.
    assert packed.dropped == ["older_transcript", "recent_dialogue", "room_summary"]
    # Mandatory / case-critical sections are never dropped.
    for kept in (
        "system",
        "case_state",
        "current_task",
        "stage_instruction",
        "expert_task",
        "structured_results",
    ):
        assert kept not in packed.dropped
    payload = packed.payload()
    assert payload["case_state"] == {"intent": "the case anchor"}
    assert payload["stage_instruction"] == "STAGE-INSTRUCTION"
    assert "recent_dialogue" not in payload


def test_dialogue_only_overflow_is_dropped_not_truncated_into_half_sections() -> None:
    packer = ContextPacker(_argv_capability())
    packed = packer.pack(
        system="SYSTEM",
        current_task="TASK",
        recent_dialogue=[{"speaker": "user", "content": "话" * 20_000}],
    )
    # One CJK-heavy dialogue entry (~60KB) alone busts the budget.
    assert packed.dropped == ["recent_dialogue"]
    assert packed.fits is True
    assert packed.payload()["current_task"] == "TASK"


def test_mandatory_section_alone_over_budget_is_clipped_not_dropped() -> None:
    packer = ContextPacker(_argv_capability())
    packed = packer.pack(system="X" * 100_000)

    section = packed.sections[0]
    assert section.key == "system"
    assert section.dropped is False
    assert section.truncated is True
    assert packed.fits is True
    assert packed.used_bytes <= packed.budget_bytes


# --- CJK byte accounting -----------------------------------------------------


def test_cjk_text_is_measured_in_utf8_bytes_not_characters() -> None:
    assert byte_length("中") == 3
    assert byte_length("abc") == 3
    assert byte_length(None) == 0
    payload = ["中文"]
    assert byte_length(payload) == len(
        json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    )

    packer = ContextPacker(_argv_capability())
    # 20k CJK chars = 60k UTF-8 bytes: over budget even though the char
    # count (20k) is far below it.  Char-count budgeting would pass here.
    packed = packer.pack(
        system="s" * 40_000,
        current_task="t",
        recent_dialogue=[{"speaker": "user", "content": "汉" * 20_000}],
    )
    assert packed.budget_bytes < 60_000
    assert "recent_dialogue" in packed.dropped
    assert packed.fits is True


# --- clipping ----------------------------------------------------------------


def test_clip_never_splits_utf8_codepoints() -> None:
    text = "中文内容测试" * 50
    for keep in (1, 2, 3, 4, 5, 7, 11, 37, 100, 1234):
        clipped = _clip(text, keep)
        # Must still be decodable round-trip: no half codepoints, no U+FFFD.
        encoded = clipped.encode("utf-8")
        assert len(encoded) <= keep
        assert "\ufffd" not in clipped
        assert clipped == text.encode("utf-8")[:keep].decode("utf-8", errors="ignore")


def test_clip_handles_containers_and_zero_keep() -> None:
    assert _clip("中文", 0) == ""
    assert _clip(["a", "中文"], 0) == []
    # List clip keeps whole items only.
    assert _clip(["ab", "中文", "more"], 8) == ["ab", "中文"]
    # Dict clip preserves key order and drops whole values.
    # Budget 7: "a"=1 + "中文"=6 bytes fit; "x"=1 would exceed (7+1>7).
    assert _clip({"a": "1", "b": "中文", "c": "x"}, 7) == {"a": "1", "b": "中文"}
