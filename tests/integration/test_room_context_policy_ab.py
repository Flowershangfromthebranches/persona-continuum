"""A/B: one room, one persona, one Memory DB -- two Context Policies.

This is the acceptance instrument for the decoupling work.  Switching between
``local_constrained`` and ``quality`` must change ONLY how the current prompt is
assembled: the Memory Store must be byte-for-byte the same set of records
afterwards, because context policy decides what this model sees this turn, not
what the persona remembers.
"""

from __future__ import annotations

from typing import Any

import pytest

from persona_continuum.domain.memory import MemoryRecord, MemoryType
from persona_continuum.room.models import ParticipantSlot, RoomStatus

QUERY = "我们之前关于重庆的讨论，你还记得吗？"

PROFILE_FIELDS = (
    "context_profile",
    "effective_context_budget",
    "prompt_target_tokens",
    "prompt_hard_tokens",
    "raw_window",
    "recent_dialogue_token_budget",
    "memory_top_k",
    "context_window",
)


def _store_snapshot(app: Any) -> dict[str, tuple[Any, ...]]:
    """Content-level view of the Memory Store (retrieval bookkeeping excluded).

    ``access_count`` / ``last_accessed_at`` are updated by retrieval itself and
    by both profiles equally, so they are not part of the invariant.  Everything
    that decides *what the persona remembers* is.
    """

    rows = app.database.conn.execute(
        "SELECT id, content, type, validity, importance, written_at, branch_id "
        "FROM memories ORDER BY id"
    ).fetchall()
    return {str(row["id"]): tuple(row) for row in rows}


def _fts_count(app: Any) -> int:
    row = app.database.conn.execute("SELECT COUNT(*) AS c FROM memories_fts").fetchone()
    return int(row["c"])


def _seed_memories(app: Any, persona_id: str, count: int = 40) -> None:
    for index in range(count):
        app.memories.add_memory(
            MemoryRecord(
                id=f"mem_cq_{index:03d}",
                persona_id=persona_id,
                type=MemoryType.EPISODIC if index % 3 else MemoryType.SEMANTIC,
                source_kind="seed",
                importance=0.5 + (index % 10) / 100,
                content=(
                    f"关于重庆的讨论第 {index} 条：用户提到过考研、数学焦虑和以后想去的城市，"
                    "苏禾记得当时的情绪。"
                ),
            )
        )


def _synthetic_transcript(count: int, *, body_repeat: int = 6) -> list[dict[str, Any]]:
    """A long-enough shared history to distinguish the two working-memory budgets.

    Bodies are sized so the whole transcript still FITS the transport budget
    (otherwise the binding constraint would be bytes-per-ARGV, not the token
    budget under test) while comfortably exceeding the local token budget.
    """

    turns: list[dict[str, Any]] = []
    for index in range(count):
        user = index % 2 == 0
        turns.append(
            {
                "participant_id": "user" if user else "su_he",
                "speaker_name": "用户" if user else "苏禾",
                "persona_id": "" if user else "su_he",
                "message_kind": "user_message" if user else "persona_reply",
                "content": f"第 {index} 轮的对话内容，讨论重庆旅行计划与考研安排。" * body_repeat,
            }
        )
    return turns


async def _create_room(app: Any) -> str:
    app.personas.create_from_manifest(
        {
            "id": "su_he",
            "display_name": "苏禾",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    _seed_memories(app, "su_he")
    room = app.orchestrator.create_room(
        title="su-he",
        topic="日常",
        participants=[
            ParticipantSlot(
                participant_id="slot_su_he",
                persona_id="su_he",
                display_name="苏禾",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
                reasoning_selection="none",
            )
        ],
    )
    await app.orchestrator.start_room(room.id)
    return room.id


async def _run_turn(app: Any, room_id: str, strategy: str) -> dict[str, Any]:
    state = app.orchestrator.get_room(room_id)
    assert state is not None
    state.metadata["context_strategy"] = strategy
    state.transcript = _synthetic_transcript(30)
    app.orchestrator._save_room_state(state, force=True)
    report: dict[str, Any] = {}
    async for event in app.orchestrator.step_turn(
        room_id,
        manual_speaker_id="slot_su_he",
        user_message=QUERY,
    ):
        if event.get("event") == "room_context_report":
            report = dict(event)
    assert report, "the turn must publish a Context Assembly Report"
    return report


@pytest.mark.anyio
async def test_two_profiles_share_one_memory_store(app: Any) -> None:
    room_id = await _create_room(app)
    before = _store_snapshot(app)
    fts_before = _fts_count(app)

    local = await _run_turn(app, room_id, "local_constrained")
    quality = await _run_turn(app, room_id, "quality")

    # --- the memory store is untouched by the policy switch ------------------
    # Committing a turn legitimately appends new ``digital_experience`` records;
    # what may NOT happen is any existing record changing or disappearing.
    after = _store_snapshot(app)
    missing = set(before) - set(after)
    assert not missing, f"Context Policy deleted memories: {sorted(missing)}"
    for memory_id, row in before.items():
        assert after[memory_id] == row, f"Context Policy modified memory {memory_id}"
    assert _fts_count(app) >= fts_before

    # --- but the assembled prompt is a different shape -----------------------
    assert local["context_profile"] == "local_constrained"
    assert quality["context_profile"] == "remote_quality"
    assert local["prompt_hard_tokens"] == 7000
    assert quality["prompt_hard_tokens"] > 100_000
    assert local["effective_context_budget"] == 7000
    assert quality["effective_context_budget"] > 100_000
    assert quality["memory_top_k"] > local["memory_top_k"]
    assert quality["raw_window"] > local["raw_window"]
    assert quality["recent_dialogue_token_budget"] > local["recent_dialogue_token_budget"]
    for field in PROFILE_FIELDS:
        assert field in local and field in quality


@pytest.mark.anyio
async def test_remote_quality_sees_more_of_the_same_history(app: Any) -> None:
    room_id = await _create_room(app)
    local = await _run_turn(app, room_id, "local_constrained")
    # Recreate the same history for the second measurement.
    quality = await _run_turn(app, room_id, "quality")

    assert local["transcript_in_prompt"] <= local["raw_window"]
    assert quality["transcript_in_prompt"] > local["transcript_in_prompt"]
    assert quality["memory_selected"] >= local["memory_selected"]
    assert local["memory_selected"] <= local["memory_top_k"]

    # local_constrained keeps its single-rung-friendly behaviour: it is allowed
    # to trim, remote_quality has a single rung and never degrades memories.
    assert local["prompt_budget_stages_available"] > quality["prompt_budget_stages_available"]
    assert quality["prompt_budget_stages_available"] == 1


@pytest.mark.anyio
async def test_local_profile_never_reopens_the_metal_risk(app: Any) -> None:
    room_id = await _create_room(app)
    local = await _run_turn(app, room_id, "local_constrained")
    assert local["local_safety_ceiling_applied"] is True
    assert local["effective_context_budget"] == 7000
    assert local["prompt_target_tokens"] == 6000
    assert local["memory_top_k"] == 8
    assert local["raw_window"] == 8


@pytest.mark.anyio
async def test_room_status_is_unaffected_by_the_policy(app: Any) -> None:
    room_id = await _create_room(app)
    await _run_turn(app, room_id, "quality")
    state = app.orchestrator.get_room(room_id)
    assert state is not None
    assert state.status in {RoomStatus.READY, RoomStatus.DISCUSSING, RoomStatus.ACTIVE}
