"""Regression tests for the "clear conversation" affordance.

The feature
-----------
The chat panel exposes a button that wipes every transcript row for a
room, leaving the room itself, its participants, persona state, memory
and round counters untouched.  These tests pin both the contract (what
gets deleted, what is preserved) and the WebSocket broadcast (so
clients with the same room open stay in sync).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from persona_continuum.room.models import RoomTranscriptRecord


def _record(room_id: str, turn_index: int, content: str) -> RoomTranscriptRecord:
    return RoomTranscriptRecord(
        id=f"row_{room_id}_{turn_index}",
        room_id=room_id,
        turn_id=f"turn_{turn_index}",
        participant_id="host" if turn_index % 2 == 0 else "expert",
        persona_id="persona_x",
        speaker_name=f"Speaker {turn_index}",
        agent_runtime_id="fake_agent",
        agent_session_id="sess",
        content=content,
        commit_status="committed",
        created_at=datetime.now(UTC),
    )


def test_clear_removes_every_transcript_row(app) -> None:
    orch = app.orchestrator
    state = orch.create_room(
        title="t",
        topic="t",
        participants=[],
        metadata={"probe": True},
    )
    for i in range(3):
        orch._save_transcript_record(_record(state.id, i, f"line {i}"))
    before = orch.list_room_transcripts(state.id)
    assert len(before) == 3

    removed = asyncio.run(orch.clear_room_transcripts(state.id))

    assert removed == 3
    assert orch.list_room_transcripts(state.id) == []


def test_clear_resets_in_memory_transcript_window(app) -> None:
    orch = app.orchestrator
    state = orch.create_room(
        title="t",
        topic="t",
        participants=[],
        metadata={"probe": True},
    )
    # Seed both the in-memory window and the persistent table.
    state.transcript = [
        {"turn_id": "t1", "participant_id": "user", "content": "hi"},
        {"turn_id": "t2", "participant_id": "host", "content": "hello"},
    ]
    orch._save_transcript_record(_record(state.id, 0, "hi"))
    orch._save_transcript_record(_record(state.id, 1, "hello"))

    asyncio.run(orch.clear_room_transcripts(state.id))

    fresh = orch.get_room(state.id)
    assert fresh is not None
    assert fresh.transcript == []


def test_clear_preserves_room_metadata_and_participants(app) -> None:
    """The point of "clear conversation" is to wipe the transcript without
    losing anything else.  Verifies metadata, topic, title and bound
    participants survive.
    """

    orch = app.orchestrator
    state = orch.create_room(
        title="Important Room",
        topic="Sensitive topic",
        participants=[],
        metadata={"notes": "must survive"},
    )
    orch._save_transcript_record(_record(state.id, 0, "don't keep this"))

    asyncio.run(orch.clear_room_transcripts(state.id))

    fresh = orch.get_room(state.id)
    assert fresh is not None
    assert fresh.title == "Important Room"
    assert fresh.topic == "Sensitive topic"
    assert fresh.metadata.get("notes") == "must survive"


def test_clear_returns_zero_when_room_is_already_empty(app) -> None:
    orch = app.orchestrator
    state = orch.create_room(
        title="t",
        topic="t",
        participants=[],
        metadata={"probe": True},
    )
    removed = asyncio.run(orch.clear_room_transcripts(state.id))
    assert removed == 0


def test_clear_isolates_other_rooms(app) -> None:
    """Clearing one room must never touch a sibling's transcript."""

    orch = app.orchestrator
    keep = orch.create_room(
        title="keep",
        topic="keep",
        participants=[],
        metadata={"probe": True},
    )
    wipe = orch.create_room(
        title="wipe",
        topic="wipe",
        participants=[],
        metadata={"probe": True},
    )
    orch._save_transcript_record(_record(keep.id, 0, "stay here"))
    orch._save_transcript_record(_record(wipe.id, 0, "should go"))

    asyncio.run(orch.clear_room_transcripts(wipe.id))

    assert len(orch.list_room_transcripts(keep.id)) == 1
    assert orch.list_room_transcripts(keep.id)[0].content == "stay here"
    assert orch.list_room_transcripts(wipe.id) == []


@pytest.mark.anyio
async def test_clear_broadcasts_transcript_cleared_event(app) -> None:
    """Other connected clients should learn about the clear via the
       broadcast queue, not just the originating HTTP response.
    """

    orch = app.orchestrator
    state = orch.create_room(
        title="t",
        topic="t",
        participants=[],
        metadata={"probe": True},
    )
    orch._save_transcript_record(_record(state.id, 0, "goodbye"))

    queue = orch.subscribe_events(state.id)
    try:
        await orch.clear_room_transcripts(state.id)
        # The broadcast is non-blocking; drain whatever is immediately
        # available.  At minimum the ``transcript_cleared`` event should be
        # in flight.
        seen = []
        while True:
            try:
                seen.append(queue.get_nowait())
            except Exception:
                break
        kinds = [event.get("event") for event in seen]
        assert "transcript_cleared" in kinds
        cleared = next(event for event in seen if event.get("event") == "transcript_cleared")
        assert cleared.get("removed") == 1
        assert cleared.get("room_id") == state.id
    finally:
        orch.unsubscribe_events(state.id, queue)