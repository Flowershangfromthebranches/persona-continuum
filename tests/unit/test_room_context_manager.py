from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

from persona_continuum.room.context_manager import (
    RoomContextManager,
    render_summary_markdown,
)
from persona_continuum.room.static_kernel import StaticPersonaKernelCache


def _turn(i: int) -> dict[str, object]:
    return {"turn_id": f"t{i}", "speaker_name": f"S{i}", "content": f"message number {i}"}


class _Value(Enum):
    PUBLIC = "public"
    CHAT = "chat"


def test_static_kernel_cache_looks_up_revision_before_building() -> None:
    cache = StaticPersonaKernelCache()
    manifest = SimpleNamespace(
        id="persona_static",
        persona_type=_Value.PUBLIC,
        run_mode=_Value.CHAT,
        birth_date=None,
        death_date=None,
        aliases=[],
        updated_at="2026-08-28T00:00:00Z",
    )
    prepared = SimpleNamespace(
        identity_anchor=manifest,
        session_id="main",
        compiled_persona_context={
            "runtime_version": {
                "base_compile_version": 7,
                "continuation_versions": [],
                "active_branch_id": "main",
                "digital_runtime_revision": 3,
            },
            "by_key": {"values": ["truth"]},
        },
    )
    builds = {"count": 0}
    real_build = cache._build

    def counting_build(*args):  # type: ignore[no-untyped-def]
        builds["count"] += 1
        return real_build(*args)

    cache._build = counting_build  # type: ignore[method-assign]
    first = cache.get_or_build(prepared, display_name="Static")  # type: ignore[arg-type]
    second = cache.get_or_build(prepared, display_name="Static")  # type: ignore[arg-type]
    assert first is second
    assert builds["count"] == 1
    assert cache.snapshot()["hits"] == 1


def test_persistent_thread_gets_full_then_delta() -> None:
    mgr = RoomContextManager(cursor_enabled=True, raw_window=4)
    transcript = [_turn(i) for i in range(10)]

    cold = mgr.prepare(
        room_id="r", participant_id="p", transcript=transcript, persistent=True
    )
    assert cold.mode == "full"  # cold thread has seen nothing
    assert len(cold.turns) == 10
    mgr.commit("r", "p", cold)

    # Two new turns arrive; only those are sent to the persistent thread.
    transcript2 = [_turn(i) for i in range(12)]
    d = mgr.prepare(
        room_id="r", participant_id="p", transcript=transcript2, persistent=True
    )
    assert d.mode == "delta"
    assert [t["content"] for t in d.turns] == [
        "message number 10",
        "message number 11",
    ]
    assert d.cursor_before == 10
    mgr.commit("r", "p", d)

    # No new turns -> empty delta (prompt stays bounded, stable over time).
    empty = mgr.prepare(
        room_id="r", participant_id="p", transcript=transcript2, persistent=True
    )
    assert empty.mode == "delta"
    assert empty.turns == []


def test_reset_cursor_triggers_rehydration_window() -> None:
    mgr = RoomContextManager(cursor_enabled=True, raw_window=3)
    transcript = [_turn(i) for i in range(9)]
    first = mgr.prepare(
        room_id="r", participant_id="p", transcript=transcript, persistent=True
    )
    mgr.commit("r", "p", first)
    # Physical runtime died -> cursor reset.
    mgr.reset_cursor("r", "p")
    rehydr = mgr.prepare(
        room_id="r",
        participant_id="p",
        transcript=transcript,
        persistent=True,
        summary_block="## Room Long-term Summary",
    )
    assert rehydr.mode == "rehydrated"
    # Bounded recent window, not the whole history.
    assert len(rehydr.turns) == 3
    assert rehydr.turns[-1]["content"] == "message number 8"
    # After rehydration the cursor is restored, so the next turn is a delta.
    mgr.commit("r", "p", rehydr)
    nxt = mgr.prepare(
        room_id="r",
        participant_id="p",
        transcript=[_turn(i) for i in range(10)],
        persistent=True,
    )
    assert nxt.mode == "delta"
    assert [t["content"] for t in nxt.turns] == ["message number 9"]


def test_stateless_agent_uses_bounded_window_with_summary() -> None:
    mgr = RoomContextManager(cursor_enabled=True, raw_window=4)
    transcript = [_turn(i) for i in range(20)]
    win = mgr.prepare(
        room_id="r",
        participant_id="p",
        transcript=transcript,
        persistent=False,
        summary_block="## Room Long-term Summary",
    )
    assert win.mode == "windowed"
    assert len(win.turns) == 4
    assert win.summary_block == "## Room Long-term Summary"
    # Stateless participants never register a persistent cursor.
    assert mgr.cursor_for("r", "p") is None


def test_stateless_without_summary_falls_back_to_full() -> None:
    mgr = RoomContextManager(raw_window=4)
    transcript = [_turn(i) for i in range(3)]
    full = mgr.prepare(
        room_id="r", participant_id="p", transcript=transcript, persistent=False
    )
    assert full.mode == "full"
    assert len(full.turns) == 3


def test_cursor_disabled_uses_windowed_stateless_path() -> None:
    mgr = RoomContextManager(cursor_enabled=False, raw_window=4)
    transcript = [_turn(i) for i in range(12)]
    # Even a "persistent" participant is treated stateless when disabled.
    res = mgr.prepare(
        room_id="r",
        participant_id="p",
        transcript=transcript,
        persistent=True,
        summary_block="summary",
    )
    assert res.mode == "windowed"
    assert len(res.turns) == 4


def test_summary_markdown_preserves_all_required_sections() -> None:
    md = render_summary_markdown(
        {
            "key_facts": ["founded company"],
            "relationship_shifts": ["rivalry softened"],
            "commitments": ["deliver Q3"],
            "conflicts": ["pricing dispute"],
            "viewpoint_changes": ["now favors open ecosystem"],
            "positions": ["vertical integration"],
            "important_events": ["product launch"],
            "open_topics": ["antitrust"],
            "long_term_goals": ["hardware+software"],
            "known_user_info": ["prefers brevity"],
        }
    )
    for label in (
        "Key facts",
        "Relationship shifts",
        "Commitments",
        "Conflicts",
        "Viewpoint changes",
        "Positions",
        "Important events",
        "Open topics",
        "Long-term goals",
        "Known user information",
    ):
        assert label in md


def test_should_update_summary_threshold() -> None:
    mgr = RoomContextManager(raw_window=8, summary_every_turns=8)
    assert mgr.should_update_summary(3) is False  # below window
    assert mgr.should_update_summary(8) is True
    assert mgr.should_update_summary(9) is False
    assert mgr.should_update_summary(16) is True
