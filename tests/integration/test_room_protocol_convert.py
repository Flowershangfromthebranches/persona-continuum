"""Requirement §32: free_discussion -> expert_consultation conversion persistence.

The guard matrix (unknown room / unsupported target / invalid mapping /
non-convertible status / double conversion) is covered in
``test_room_protocol_api.py``; this module focuses on what the migration must
PRESERVE and on durability across a restart.
"""

from __future__ import annotations

from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.room.models import ParticipantSlot, RoomProtocolType

CASE_ANCHOR = "CONVERT_ANCHOR：我想咨询明年的投资方向，我是男性，1990年5月1日出生于杭州。"


def _manifest(persona_id: str) -> dict[str, str]:
    return {
        "id": persona_id,
        "display_name": persona_id,
        "persona_type": "fictional",
        "run_mode": "continuation",
    }


def _slot(participant_id: str, persona_id: str, role: str) -> ParticipantSlot:
    return ParticipantSlot(
        participant_id=participant_id,
        persona_id=persona_id,
        display_name=participant_id,
        role=role,
        runtime_selection="fake_agent",
        model_selection="fake-gpt-5",
    )


async def _seed_free_discussion_room(app: PersonaContinuum, suffix: str) -> Any:
    """Create a free_discussion room with transcript, case state and one run."""

    for persona_id in (f"cv_{suffix}_host", f"cv_{suffix}_m1"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title=f"convert-{suffix}",
        topic="investment consultation",
        participants=[
            _slot("slot_host", f"cv_{suffix}_host", "host"),
            _slot("slot_m1", f"cv_{suffix}_m1", "member"),
        ],
        protocol=RoomProtocolType.FREE_DISCUSSION,
    )
    await app.orchestrator.start_room(room.id)

    # A real historical run (room_runs row + case_state anchor) before migration.
    await app.orchestrator.run_protocol(room.id, CASE_ANCHOR)

    # Give the room a transcript worth preserving across the migration.
    state = app.orchestrator.get_room(room.id)
    assert state is not None
    state.transcript.append(
        {
            "participant_id": "user",
            "speaker_name": "用户",
            "message_kind": "user_message",
            "content": CASE_ANCHOR,
        }
    )
    app.orchestrator._save_room_state(state, force=True)
    return state


def _room_run_ids(app: PersonaContinuum, room_id: str) -> list[str]:
    rows = app.database.conn.execute(
        "SELECT id FROM room_runs WHERE room_id = ? ORDER BY started_at",
        (room_id,),
    ).fetchall()
    return [row["id"] for row in rows]


def _has_converted_event(app: PersonaContinuum, room_id: str) -> bool:
    row = app.database.conn.execute(
        "SELECT COUNT(*) AS n FROM room_protocol_events "
        "WHERE room_id = ? AND event_type = 'protocol_converted'",
        (room_id,),
    ).fetchone()
    return bool(row and row["n"])


@pytest.mark.anyio
async def test_convert_preserves_history_and_resets_live_state(app: Any) -> None:
    state = await _seed_free_discussion_room(app, "a")
    room_id = state.id
    old_run_id = state.protocol_state.run_id
    assert old_run_id
    transcript_before = [dict(item) for item in state.transcript]
    case_state_before = state.case_state.model_dump(mode="json")
    assert state.case_state.intent  # seeded anchor

    converted = await app.orchestrator.convert_room_protocol(
        room_id,
        "expert_consultation",
        {"slot_host": "host", "slot_m1": "expert"},
    )

    # Roles: host stays host, member becomes expert; host pointer preserved.
    assert converted.protocol == RoomProtocolType.EXPERT_CONSULTATION
    assert converted.host_participant_id == "slot_host"
    roles = {p.participant_id: p.role for p in converted.participants}
    assert roles == {"slot_host": "host", "slot_m1": "expert"}

    # Live run state is reset to the waiting-for-user stage.
    assert converted.protocol_state.status.value == "pending"
    assert converted.protocol_state.current_stage == "waiting_user"
    assert converted.protocol_state.run_id != old_run_id

    # Nothing user-facing is lost: transcript, case state, bindings.
    assert converted.transcript == transcript_before
    assert converted.case_state.model_dump(mode="json") == case_state_before
    assert sorted(converted.binding_snapshots) == ["slot_host", "slot_m1"]

    # Old run history survives in room_runs / room_protocol_events.
    run_ids = _room_run_ids(app, room_id)
    assert old_run_id in run_ids
    assert _has_converted_event(app, room_id) is True

    # A GET (live mirror) still reports the converted protocol.
    fetched = app.orchestrator.get_room(room_id)
    assert fetched is not None
    assert fetched.protocol == RoomProtocolType.EXPERT_CONSULTATION

    # The converted room can immediately run the target protocol.
    rerun = await app.orchestrator.run_protocol(room_id, "请在专家会诊模式下继续")
    assert rerun.protocol_state.status.value == "success"


@pytest.mark.anyio
async def test_converted_room_persists_across_restart(tmp_path: Any) -> None:
    data_dir = tmp_path / "pc-data"
    room_id = ""

    first = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=True)
    first.init()
    try:
        state = await _seed_free_discussion_room(first, "restart")
        room_id = state.id
        old_run_id = state.protocol_state.run_id
        converted = await first.orchestrator.convert_room_protocol(
            room_id,
            "expert_consultation",
            {"slot_host": "host", "slot_m1": "expert"},
        )
        transcript_before = [dict(item) for item in converted.transcript]
        case_before = converted.case_state.model_dump(mode="json")
        roles_before = {p.participant_id: p.role for p in converted.participants}
    finally:
        first.close()

    second = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=True)
    second.init()
    try:
        reloaded = second.orchestrator.get_room(room_id)
        assert reloaded is not None
        # §32 protocol persistence: the conversion survives the restart.
        assert reloaded.protocol == RoomProtocolType.EXPERT_CONSULTATION
        assert reloaded.host_participant_id == "slot_host"
        assert {p.participant_id: p.role for p in reloaded.participants} == roles_before
        assert reloaded.protocol_state.status.value == "pending"
        assert reloaded.protocol_state.current_stage == "waiting_user"
        # Preserved payload also survives the round-trip.
        assert reloaded.case_state.model_dump(mode="json") == case_before
        assert [dict(item) for item in reloaded.transcript] == transcript_before
        assert old_run_id in _room_run_ids(second, room_id)
        assert _has_converted_event(second, room_id) is True
    finally:
        second.close()
