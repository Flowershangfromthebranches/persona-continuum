"""Recent-dialogue selection is a TOKEN budget, with message count as a cap.

"嗯" and a 3000-character essay are both one message, so a message count can
neither protect a weak model nor describe a strong one.  These tests pin the
selection function, the equivalence with the historical count-only behaviour,
and the orphan-turn guarantee (prepare and the summary boundary agree).
"""

from __future__ import annotations

from typing import Any

from persona_continuum.room.context_manager import RoomContextManager, recent_window_start


def _turn(speaker: str, content: str) -> dict[str, Any]:
    return {"participant_id": speaker, "speaker_name": speaker, "content": content}


def _transcript(count: int, *, body: str = "这是一条普通长度的消息。") -> list[dict[str, Any]]:
    return [
        _turn("用户" if index % 2 == 0 else "苏禾", f"{body}#{index}") for index in range(count)
    ]


def test_no_token_budget_keeps_the_historical_count_only_behaviour() -> None:
    transcript = _transcript(40)
    assert recent_window_start(transcript, message_window=8, token_budget=None) == 32
    assert recent_window_start(_transcript(5), message_window=8, token_budget=None) == 0
    assert recent_window_start([], message_window=8, token_budget=None) == 0


def test_message_count_is_a_cap_even_with_a_large_token_budget() -> None:
    transcript = _transcript(40)
    assert recent_window_start(transcript, message_window=6, token_budget=10_000_000) == 34


def test_token_budget_stops_before_the_cap_when_the_dialogue_is_long() -> None:
    transcript = _transcript(40, body="这是一条明显更长的消息，包含足够多的字符来消耗预算。" * 8)
    start = recent_window_start(transcript, message_window=256, token_budget=600)
    assert 0 < start < len(transcript)
    assert len(transcript) - start < 256
    # The window it selected really does fit the budget.
    from persona_continuum.room.context_manager import estimate_tokens

    kept = transcript[start:]
    total = sum(
        estimate_tokens(str(turn["content"])) + estimate_tokens(str(turn["speaker_name"]))
        for turn in kept[1:]
    )
    assert total <= 600


def test_newest_message_is_kept_even_when_it_alone_exceeds_the_budget() -> None:
    transcript = _transcript(10, body="短")
    transcript.append(_turn("用户", "很长的一条消息" * 200))
    start = recent_window_start(transcript, message_window=8, token_budget=50)
    assert start == len(transcript) - 1


def test_a_larger_budget_selects_strictly_more_history() -> None:
    transcript = _transcript(60, body="中等长度的对话内容。")
    small = recent_window_start(transcript, message_window=256, token_budget=100)
    large = recent_window_start(transcript, message_window=256, token_budget=5000)
    assert large < small


def test_prepare_uses_the_token_budget_and_reports_windowing() -> None:
    manager = RoomContextManager(raw_window=8)
    transcript = _transcript(60, body="中等长度的对话内容，用于确认窗口选择。" * 4)
    ctx = manager.prepare(
        room_id="room",
        participant_id="p",
        transcript=transcript,
        persistent=False,
        summary_block="## Room Long-term Summary\n- Key facts: x",
        message_window=256,
        recent_token_budget=400,
    )
    assert ctx.mode == "windowed"
    assert len(ctx.turns) < 60
    assert ctx.turns[-1]["content"].endswith("#59")


def test_prepare_keeps_the_historical_window_when_no_budget_is_given() -> None:
    manager = RoomContextManager(raw_window=4)
    transcript = _transcript(10)
    ctx = manager.prepare(
        room_id="room", participant_id="p", transcript=transcript, persistent=False
    )
    assert ctx.mode == "windowed"
    assert len(ctx.turns) == 4


def test_prepare_sends_the_whole_transcript_when_it_is_inside_the_window() -> None:
    manager = RoomContextManager(raw_window=8)
    transcript = _transcript(3)
    ctx = manager.prepare(
        room_id="room",
        participant_id="p",
        transcript=transcript,
        persistent=False,
        message_window=16,
        recent_token_budget=10_000,
    )
    assert ctx.mode == "full"
    assert len(ctx.turns) == 3


def test_eviction_boundary_matches_the_window_prepare_sends() -> None:
    """If these disagreed, a turn could leave the window without being summarised."""

    manager = RoomContextManager(raw_window=8)
    transcript = _transcript(80, body="用于核对驱逐边界与发送窗口一致性的内容。" * 3)
    boundary = manager.eviction_boundary(
        transcript, message_window=256, recent_token_budget=900
    )
    ctx = manager.prepare(
        room_id="room",
        participant_id="p",
        transcript=transcript,
        persistent=False,
        message_window=256,
        recent_token_budget=900,
    )
    assert boundary == len(transcript) - len(ctx.turns)


def test_summary_boundary_override_is_honoured() -> None:
    manager = RoomContextManager(raw_window=8)
    # A wide profile: nothing has left the window yet, so no summary is due.
    assert manager.should_update_summary(30, has_summary=False, eviction_boundary=0) is False
    # A narrow profile: history has left the window, so the first summary is due.
    assert manager.should_update_summary(30, has_summary=False, eviction_boundary=10) is True


def test_summary_boundary_override_survives_the_legacy_gate() -> None:
    """The ``turn_index < raw_window`` guard must not block a token-budget run."""

    manager = RoomContextManager(raw_window=64)
    assert manager.should_update_summary(5, has_summary=False, eviction_boundary=2) is True
    assert manager.should_update_summary(5, has_summary=False) is False
