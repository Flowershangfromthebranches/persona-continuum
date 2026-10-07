"""Regression: the rolling summary must be a bounded, delta-based compression.

Pins down the P1a contract:
  * the summariser only ever sees dialogue that has LEFT the raw window
  * it folds forward from a checkpoint, oldest first, skipping nothing
  * its input is held under a token budget by stopping at a message boundary,
    never by falling back to the full transcript and never mid-string
  * its output respects both the character and token ceilings
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_continuum.room.context_manager import (  # noqa: E402
    RoomContextManager,
    estimate_tokens,
    render_summary_markdown,
)


def _entry(index: int, text: str | None = None) -> dict:
    return {
        "participant_id": "slot_0" if index % 2 else "user",
        "speaker_name": "苏禾" if index % 2 else "User",
        "content": text or f"第{index}条消息内容",
        "spoken_text": text or f"第{index}条消息内容",
        "scene_time": "2026-09-19T10:00:00+08:00",
        "actions": [],
        "scene_events": [],
    }


class Recorder:
    """Fake summariser: records payloads and returns a valid summary object."""

    def __init__(self, facts: list[str] | None = None) -> None:
        self.payloads: list[dict] = []
        self.facts = facts or ["fact"]

    async def __call__(self, payload: dict, schema: dict) -> dict:
        self.payloads.append(json.loads(json.dumps(payload)))
        return {"key_facts": list(self.facts), "open_topics": ["unresolved"]}


def _run(coro):
    return asyncio.run(coro)


def test_delta_excludes_the_raw_window() -> None:
    mgr = RoomContextManager(raw_window=4)
    transcript = [_entry(i) for i in range(12)]
    rec = Recorder()

    summary, checkpoint, report = _run(
        mgr.update_summary(
            transcript=transcript, previous_summary=None, summarize=rec, checkpoint=0
        )
    )

    assert summary is not None
    assert report["reason"] == "ok"
    # Boundary is len - raw_window; the last 4 entries are still verbatim in the
    # prompt and must NOT be handed to the summariser.
    assert report["eviction_boundary"] == 8
    sent = rec.payloads[0]["new_dialogue_since_last_summary"]
    assert len(sent) == 8
    assert all("第7条" not in json.dumps(m) for m in [sent[-1]])
    assert sent[0]["text"] == "第0条消息内容"


def test_checkpoint_advances_and_nothing_is_skipped() -> None:
    mgr = RoomContextManager(raw_window=2)
    transcript = [_entry(i) for i in range(10)]
    rec = Recorder()

    _, cp1, r1 = _run(
        mgr.update_summary(transcript=transcript, previous_summary=None,
                           summarize=rec, checkpoint=0)
    )
    assert r1["reason"] == "ok"
    assert cp1 == 8

    # Nothing new has left the window yet.
    _, cp2, r2 = _run(
        mgr.update_summary(transcript=transcript, previous_summary="s",
                           summarize=rec, checkpoint=cp1)
    )
    assert r2["reason"] == "no_new_delta"
    assert cp2 == cp1
    assert len(rec.payloads) == 1  # no extra model call


def test_input_budget_stops_at_a_message_boundary() -> None:
    mgr = RoomContextManager(raw_window=1, summary_input_max_tokens=700)
    big = "很长的对话内容。" * 200  # ~1600 CJK tokens, far past the budget
    transcript = [_entry(i, big) for i in range(6)]
    rec = Recorder()

    _, checkpoint, report = _run(
        mgr.update_summary(
            transcript=transcript, previous_summary=None, summarize=rec, checkpoint=0
        )
    )

    assert report["reason"] == "ok"
    # The ceiling holds even when a single message is larger than the budget.
    assert report["estimated_input_tokens"] <= 700
    assert report["delta_messages_clipped"] == 1
    assert report["delta_messages"] == 1
    # Oldest-first: the checkpoint advanced only past what was actually folded,
    # so the remainder is deferred rather than dropped.
    assert checkpoint == 1
    assert report["delta_messages_deferred"] > 0
    # Never the whole transcript.
    assert report["delta_messages"] < len(transcript) - mgr.raw_window


def test_previous_summary_is_never_dropped_for_delta() -> None:
    mgr = RoomContextManager(raw_window=1, summary_input_max_tokens=120)
    previous = "## Room Long-term Summary\n- Key facts: " + "既有事实；" * 40
    transcript = [_entry(i, "新对话。" * 40) for i in range(4)]
    rec = Recorder()

    summary, checkpoint, report = _run(
        mgr.update_summary(
            transcript=transcript, previous_summary=previous, summarize=rec, checkpoint=0
        )
    )
    # The fixed part (system prompt + previous summary) already exceeds this
    # deliberately tiny budget, so nothing may be folded: the previous summary
    # is never sacrificed to make room for the delta, and no call is made.
    assert report["reason"] == "delta_exceeds_budget"
    assert rec.payloads == []
    # The previous summary comes back untouched rather than being replaced or
    # dropped, and the checkpoint must not move.
    assert summary == previous
    assert checkpoint == 0


def test_no_new_eviction_yet_makes_no_model_call() -> None:
    mgr = RoomContextManager(raw_window=8)
    transcript = [_entry(i) for i in range(5)]
    rec = Recorder()

    summary, checkpoint, report = _run(
        mgr.update_summary(transcript=transcript, previous_summary=None,
                           summarize=rec, checkpoint=None)
    )
    assert report["reason"] == "nothing_evicted_yet"
    assert summary is None
    assert rec.payloads == []


def test_output_respects_char_and_token_caps() -> None:
    summary = {
        "key_facts": ["事实" * 400],
        "open_topics": ["话题" * 400],
        "commitments": ["承诺" * 400],
    }
    by_chars = render_summary_markdown(summary, max_chars=500)
    assert len(by_chars) <= 500
    by_tokens = render_summary_markdown(summary, max_tokens=100)
    assert estimate_tokens(by_tokens) <= 100
    both = render_summary_markdown(summary, max_chars=10000, max_tokens=60)
    assert estimate_tokens(both) <= 60
    # The header always survives so the block is recognisable.
    assert both.startswith("## Room Long-term Summary")


def test_estimator_counts_cjk_per_character() -> None:
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdefgh") == 2


@pytest.mark.parametrize("raw_window", [2, 4, 8])
def test_checkpoint_is_monotonic_across_repeated_calls(raw_window: int) -> None:
    mgr = RoomContextManager(raw_window=raw_window)
    rec = Recorder()
    transcript = [_entry(i) for i in range(4)]
    checkpoint = 0
    last = -1
    for _ in range(4):
        _, checkpoint, report = _run(
            mgr.update_summary(transcript=transcript, previous_summary="s",
                               summarize=rec, checkpoint=checkpoint)
        )
        if report["reason"] == "ok":
            assert checkpoint is not None and checkpoint >= last
            last = checkpoint
        transcript.append(_entry(len(transcript)))
