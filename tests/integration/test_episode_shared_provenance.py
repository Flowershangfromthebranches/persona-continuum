"""Phase 3.1: shared room user turns, provenance ownership, deletion lifecycle.

In a multi-persona room the user speaks ONCE.  That single raw turn must be
reachable from every persona's Episode that answered it -- without copying it,
and without letting one persona's reply leak into another persona's Episode.
Deletion is the other half: an explicit delete must leave either no Episodes or
an explicit availability marker, never a silently unresolvable link.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.room.models import ParticipantSlot

LIN = "su_he"
CHEN = "gu_yan"
SHARED_MESSAGE = "我下个月准备去重庆。"


@pytest.fixture()
def room_app(tmp_path) -> Any:
    config = Config(data_dir=tmp_path / "pc-shared")
    app = PersonaContinuum(config, include_fake_agent=True)
    app.init()
    for persona_id, name in ((LIN, "苏禾"), (CHEN, "顾言")):
        app.personas.create_from_manifest(
            {
                "id": persona_id,
                "display_name": name,
                "persona_type": "fictional",
                "run_mode": "continuation",
            }
        )
    yield app
    app.close()


def _slot(participant_id: str, persona_id: str, name: str) -> ParticipantSlot:
    return ParticipantSlot(
        participant_id=participant_id,
        persona_id=persona_id,
        display_name=name,
        runtime_selection="fake_agent",
        model_selection="fake-gpt-5",
        reasoning_selection="none",
    )


async def _two_persona_room(app: PersonaContinuum) -> str:
    room = app.orchestrator.create_room(
        title="双人房",
        topic="日常",
        participants=[
            _slot("slot_lin", LIN, "苏禾"),
            _slot("slot_chen", CHEN, "顾言"),
        ],
    )
    await app.orchestrator.start_room(room.id)
    return room.id


async def _turn(
    app: PersonaContinuum, room_id: str, speaker: str, message: str | None = None
) -> list[dict[str, Any]]:
    kwargs = {"user_message": message} if message else {}
    return [
        event
        async for event in app.orchestrator.step_turn(
            room_id, manual_speaker_id=speaker, **kwargs
        )
    ]


async def _both_reply(app: PersonaContinuum, room_id: str) -> None:
    """Both personas answer the same shared message, so both own an Episode."""

    await _turn(app, room_id, "slot_lin", SHARED_MESSAGE)
    await _turn(app, room_id, "slot_chen")


def _episode_of(app: PersonaContinuum, persona_id: str) -> Any:
    episodes = app.episodes.list_episodes(persona_id=persona_id)
    assert episodes, f"no episode for {persona_id}"
    return episodes[0]


@pytest.mark.anyio
async def test_shared_user_turn_is_provenance_of_every_persona(room_app: PersonaContinuum) -> None:
    room_id = await _two_persona_room(room_app)
    await _turn(room_app, room_id, "slot_lin", SHARED_MESSAGE)
    await _turn(room_app, room_id, "slot_chen")

    # One raw event, not one copy per persona.
    user_rows = room_app.database.conn.execute(
        "SELECT turn_id, content FROM room_transcripts "
        "WHERE room_id = ? AND participant_id = 'user'",
        (room_id,),
    ).fetchall()
    assert len(user_rows) == 1
    shared_turn_id = str(user_rows[0]["turn_id"])

    for persona_id in (LIN, CHEN):
        episode = _episode_of(room_app, persona_id)
        sources = room_app.episodes.episode_turns(episode.id)
        kinds = [item.source_kind for item in sources]
        assert kinds == ["shared_user", "session_turn"]
        assert sources[0].turn_id == shared_turn_id
        assert sources[0].speaker == "user"
        # The raw user text is reachable from BOTH Episodes.
        assert SHARED_MESSAGE in room_app.episodes.resolve_turn_text(sources[0])

    coverage = room_app.episodes.coverage()
    assert coverage["shared_user_sources"] == 2
    assert coverage["orphaned"] == 0
    assert coverage["unavailable_sources"] == 0


@pytest.mark.anyio
async def test_persona_reply_never_enters_another_persona_episode(
    room_app: PersonaContinuum,
) -> None:
    room_id = await _two_persona_room(room_app)
    await _turn(room_app, room_id, "slot_lin", SHARED_MESSAGE)
    await _turn(room_app, room_id, "slot_chen")

    lin_episode = _episode_of(room_app, LIN)
    chen_episode = _episode_of(room_app, CHEN)
    lin_turns = {item.turn_id for item in room_app.episodes.episode_turns(lin_episode.id)}
    chen_turns = {item.turn_id for item in room_app.episodes.episode_turns(chen_episode.id)}

    # The only thing they share is the user's own turn.
    assert len(lin_turns & chen_turns) == 1
    shared = next(iter(lin_turns & chen_turns))
    assert shared == str(
        room_app.database.conn.execute(
            "SELECT turn_id FROM room_transcripts "
            "WHERE room_id = ? AND participant_id = 'user'",
            (room_id,),
        ).fetchone()["turn_id"]
    )
    # Persona replies stay private to their own Episode.
    lin_session_turns = {
        item.turn_id
        for item in room_app.episodes.episode_turns(lin_episode.id)
        if item.source_kind == "session_turn"
    }
    chen_session_turns = {
        item.turn_id
        for item in room_app.episodes.episode_turns(chen_episode.id)
        if item.source_kind == "session_turn"
    }
    assert not lin_session_turns & chen_session_turns
    # And the Episodes are scoped to their own persona.
    assert lin_episode.persona_id == LIN
    assert chen_episode.persona_id == CHEN
    # turn_count counts the Episode's OWN turns, not the shared reference.
    assert lin_episode.turn_count == 1
    assert chen_episode.turn_count == 1


@pytest.mark.anyio
async def test_shared_turn_is_attached_once_per_episode(room_app: PersonaContinuum) -> None:
    """Several replies inside ONE Episode must not duplicate the source row."""

    session_id = room_app.sessions.start_session(
        persona_id=LIN, title="t", counterpart_id="user"
    ).id
    now = datetime.now(UTC)
    for index in range(2):
        room_app.episodes.assign_turn(
            persona_id=LIN,
            session_id=session_id,
            turn_id=f"turn_shared_{index}",
            counterpart_id="user",
            branch_id="main",
            room_id="room_demo",
            occurred_at=now + timedelta(minutes=index),
            user_message="我在想考研的事",
            persona_response=f"第{index}次回应",
            shared_user_turn_id="user_msg_shared",
            shared_user_text=SHARED_MESSAGE,
            shared_user_occurred_at=now,
        )
    episode = _episode_of(room_app, LIN)
    sources = room_app.episodes.episode_turns(episode.id)
    assert [item.source_kind for item in sources] == [
        "shared_user",
        "session_turn",
        "session_turn",
    ]
    # turn_count counts the Episode's own turns; the shared reference is extra.
    assert episode.turn_count == 2
    assert [item.position for item in sources] == [0, 1, 2]
    shared = [item for item in sources if item.source_kind == "shared_user"]
    assert len(shared) == 1
    assert room_app.episodes.coverage()["shared_user_sources"] == 1


@pytest.mark.anyio
async def test_reply_to_another_persona_is_a_separate_counterpart_scope(
    room_app: PersonaContinuum,
) -> None:
    """A persona answering another persona belongs to THAT relationship.

    The room keeps one Episode per counterpart, so 苏禾's reply addressed to the
    user and her later reply addressed to 顾言 are different memories.  This is
    scope isolation doing its job, not a lost turn.
    """

    room_id = await _two_persona_room(room_app)
    await _turn(room_app, room_id, "slot_lin", SHARED_MESSAGE)
    await _turn(room_app, room_id, "slot_chen")
    await _turn(room_app, room_id, "slot_lin")  # addressed to 顾言, not the user

    lin_episodes = room_app.episodes.list_episodes(persona_id=LIN)
    counterparts = sorted(episode.counterpart_id for episode in lin_episodes)
    assert counterparts == ["gu_yan", "user"]
    for episode in lin_episodes:
        assert episode.turn_count == 1
        assert any(
            item.source_kind == "shared_user"
            for item in room_app.episodes.episode_turns(episode.id)
        )
    coverage = room_app.episodes.coverage()
    assert coverage["orphaned"] == 0
    assert coverage["assigned_to_episode"] == 3  # 2 苏禾 turns + 1 顾言 turn


@pytest.mark.anyio
async def test_delete_session_removes_derived_episodes(room_app: PersonaContinuum) -> None:
    room_id = await _two_persona_room(room_app)
    await _both_reply(room_app, room_id)
    episode = _episode_of(room_app, LIN)
    session_id = episode.session_id

    assert (
        room_app.sessions.delete_session(LIN, session_id, delete_derived_memories=True) is True
    )

    assert room_app.episodes.get_episode(episode.id) is None
    assert room_app.episodes.episode_turns(episode.id) == []
    coverage = room_app.episodes.coverage()
    # No turn was left behind, and no provenance points at a deleted turn.
    assert coverage["orphaned"] == 0
    assert coverage["dangling_provenance"] == 0
    assert coverage["sessionless_turns"] == 0
    # The other persona's Episode is untouched.
    assert _episode_of(room_app, CHEN).persona_id == CHEN
    # Lineage for the deleted Episode is gone too.
    assert (
        room_app.database.conn.execute(
            "SELECT COUNT(*) AS c FROM lineage WHERE child_type = 'episode' AND child_id = ?",
            (episode.id,),
        ).fetchone()["c"]
        == 0
    )


@pytest.mark.anyio
async def test_delete_session_keep_derived_marks_provenance_unavailable(
    room_app: PersonaContinuum,
) -> None:
    room_id = await _two_persona_room(room_app)
    await _both_reply(room_app, room_id)
    episode = _episode_of(room_app, LIN)
    session_id = episode.session_id
    assert room_app.episodes.inspect_episode(episode.id)["sources"][1]["resolvable"] is True

    assert (
        room_app.sessions.delete_session(LIN, session_id, delete_derived_memories=False) is True
    )

    # The Episode survives (the caller asked to keep derived memory) but says so.
    # The persona's own turn is gone with the session; the room-level user turn
    # it answered is still there, so availability is explicitly PARTIAL.
    kept = room_app.episodes.get_episode(episode.id)
    assert kept is not None
    assert kept.metadata["source_availability"] == "partial"
    assert kept.metadata["unavailable_source_count"] == 1
    inspected = room_app.episodes.inspect_episode(episode.id)
    by_kind = {source["source_kind"]: source for source in inspected["sources"]}
    assert by_kind["session_turn"]["resolvable"] is False
    assert by_kind["shared_user"]["resolvable"] is True

    coverage = room_app.episodes.coverage()
    assert coverage["unavailable_sources"] >= 1  # explicit, not silent
    assert coverage["orphaned"] == 0  # nothing was left without a home
    assert coverage["committed_turns"] == 1  # the other persona's turn survives


@pytest.mark.anyio
async def test_delete_room_keeps_persona_episodes_and_marks_shared_sources(
    room_app: PersonaContinuum,
) -> None:
    room_id = await _two_persona_room(room_app)
    await _turn(room_app, room_id, "slot_lin", SHARED_MESSAGE)
    episode = _episode_of(room_app, LIN)
    assert episode.metadata.get("source_availability") in (None, "complete")

    assert room_app.orchestrator.delete_room(room_id) is True

    kept = room_app.episodes.get_episode(episode.id)
    assert kept is not None  # persona memory survives a room deletion
    assert kept.metadata["source_availability"] == "partial"
    sources = room_app.episodes.episode_turns(episode.id)
    shared = [item for item in sources if item.source_kind == "shared_user"]
    assert shared and room_app.episodes.resolve_turn_text(shared[0]) == ""
    # The persona's own turn still resolves: session_turns outlive the room.
    own = [item for item in sources if item.source_kind == "session_turn"]
    assert own and room_app.episodes.resolve_turn_text(own[0]) != ""
    coverage = room_app.episodes.coverage()
    assert coverage["unavailable_sources"] == 1
    assert coverage["orphaned"] == 0


@pytest.mark.anyio
async def test_single_persona_room_is_unaffected(room_app: PersonaContinuum) -> None:
    """A one-persona room keeps the simpler shape: just its own turn."""

    room = room_app.orchestrator.create_room(
        title="单人房",
        topic="日常",
        participants=[_slot("slot_lin", LIN, "苏禾")],
    )
    await room_app.orchestrator.start_room(room.id)
    await _turn(room_app, room.id, "slot_lin", SHARED_MESSAGE)
    episode = _episode_of(room_app, LIN)
    sources = room_app.episodes.episode_turns(episode.id)
    assert [item.source_kind for item in sources] == ["shared_user", "session_turn"]
    assert episode.turn_count == 1
    assert room_app.episodes.coverage()["orphaned"] == 0


@pytest.mark.anyio
async def test_shared_provenance_is_idempotent_across_restart(tmp_path) -> None:
    config = Config(data_dir=tmp_path / "pc-restart-shared")
    app = PersonaContinuum(config, include_fake_agent=True)
    app.init()
    for persona_id, name in ((LIN, "苏禾"), (CHEN, "顾言")):
        app.personas.create_from_manifest(
            {
                "id": persona_id,
                "display_name": name,
                "persona_type": "fictional",
                "run_mode": "continuation",
            }
        )
    room_id = await _two_persona_room(app)
    await _both_reply(app, room_id)
    before = {
        persona: [
            (item.source_kind, item.turn_id)
            for item in app.episodes.episode_turns(_episode_of(app, persona).id)
        ]
        for persona in (LIN, CHEN)
    }
    app.close()

    reopened = PersonaContinuum(config, include_fake_agent=True)
    reopened.init()
    after = {
        persona: [
            (item.source_kind, item.turn_id)
            for item in reopened.episodes.episode_turns(_episode_of(reopened, persona).id)
        ]
        for persona in (LIN, CHEN)
    }
    assert after == before
    assert reopened.episodes.coverage()["shared_user_sources"] == 2
    assert reopened.episodes.coverage()["orphaned"] == 0
    reopened.close()


def test_old_episodes_report_complete_availability(room_app: PersonaContinuum) -> None:
    """An untouched Episode must not be marked unavailable by default."""

    session_id = room_app.sessions.start_session(
        persona_id=LIN, title="t", counterpart_id="user"
    ).id
    room_app.sessions.commit_turn(
        persona_id=LIN,
        session_id=session_id,
        user_message="第一句",
        persona_response="苏禾回应",
        occurred_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    episode = _episode_of(room_app, LIN)
    assert "source_availability" not in episode.metadata
    assert room_app.episodes.coverage()["unavailable_sources"] == 0
