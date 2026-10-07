from __future__ import annotations

from datetime import datetime

import pytest

from persona_continuum.domain.scene import RoomSceneState
from persona_continuum.room.models import ParticipantSlot, RoomMode
from persona_continuum.room.prompt_composer import PromptComposer
from persona_continuum.runtime.temporal_parser import parse_temporal_input


@pytest.fixture()
def persona(app):
    return app.personas.create_from_manifest(
        {
            "id": "time-probe",
            "display_name": "苏禾",
            "persona_type": "fictional_or_synthetic_person",
            "run_mode": "digital_continuation",
        }
    ).id


def evening():
    time = datetime.fromisoformat("2026-09-18T23:30:00+08:00")
    return RoomSceneState(scene_time=time, clock_wall_time=datetime.now(time.tzinfo))


def test_case_1_relative_duration_parser():
    """Case 1: 23:30 + 8小时 -> next day 07:30, elapsed 28800s."""
    ref = evening().scene_time
    parsed = parse_temporal_input("8小时", ref)
    assert parsed.kind == "relative"
    assert parsed.seconds == 28800.0
    assert parsed.target_scene_time.hour == 7
    assert parsed.target_scene_time.minute == 30
    assert parsed.target_scene_time.day == 19


def test_case_2_semantic_next_morning_parser():
    """Case 2: 23:30 + 第二天早上 -> next morning 08:00."""
    ref = evening().scene_time
    parsed = parse_temporal_input("第二天早上", ref)
    assert parsed.kind == "semantic"
    assert parsed.target_scene_time.hour == 8
    assert parsed.target_scene_time.minute == 0
    assert parsed.target_scene_time.day == 19
    assert parsed.seconds == 30600.0  # 8.5 hours


def test_case_3_semantic_tomorrow_specific_hour():
    """Case 3: 23:30 + 明天 09:30 -> elapsed calculated correctly."""
    ref = evening().scene_time
    parsed = parse_temporal_input("明天 09:30", ref)
    assert parsed.target_scene_time.day == 19
    assert parsed.target_scene_time.hour == 9
    assert parsed.target_scene_time.minute == 30
    assert parsed.seconds == 36000.0  # 10 hours


def test_case_4_absolute_iso_timestamp():
    """Case 4: 23:30 + 2026-09-20 14:00 -> absolute jump forward."""
    ref = evening().scene_time
    parsed = parse_temporal_input("2026-09-20 14:00", ref)
    assert parsed.kind == "absolute"
    assert parsed.target_scene_time.day == 20
    assert parsed.target_scene_time.hour == 14
    assert parsed.target_scene_time.minute == 0
    assert parsed.seconds > 86400.0


def test_case_5_invalid_input_fail_closed():
    """Case 5: '香蕉' -> validation error, fails closed."""
    ref = evening().scene_time
    with pytest.raises(ValueError) as exc:
        parse_temporal_input("香蕉", ref)
    assert "unrecognized_time_expression" in str(exc.value)


def test_case_10_time_cannot_rewind():
    """Case 10: Attempting to rewind time raises error."""
    ref = evening().scene_time
    with pytest.raises(ValueError) as exc:
        parse_temporal_input("2026-09-17 08:00", ref)
    assert "time_cannot_rewind" in str(exc.value)


@pytest.mark.anyio
async def test_time_mode_injection_advances_runtime_and_does_not_call_model(app, persona):
    """Cases 1, 6-9: Injecting in time mode updates scene and needs without dialogue."""
    room = app.orchestrator.create_room(
        participants=[
            ParticipantSlot(
                participant_id="p1",
                persona_id=persona,
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
            )
        ],
        mode=RoomMode.MANUAL,
        scene_state=evening(),
    )
    await app.orchestrator.start_room(room.id)

    # 1. Action: lie down to sleep
    await app.orchestrator.inject_message(
        room.id,
        "我躺下睡觉。",
        input_mode="action",
    )
    r = app.orchestrator.get_room(room.id)
    assert r.scene_state.spatial.postures["user"] == "lying"

    # 2. Time: +8小时
    await app.orchestrator.inject_message(
        room.id,
        "8小时",
        input_mode="time",
    )
    r = app.orchestrator.get_room(room.id)
    assert r.scene_state.scene_time.day == 19
    assert r.scene_state.scene_time.hour == 7
    assert r.scene_state.scene_time.minute == 30
    assert r.scene_state.elapsed_since_last_interaction == 28800.0

    # Ensure no dialogue turn was created for time injection
    last_turn = r.transcript[-1]
    assert last_turn["input_mode"] == "time"
    assert last_turn["spoken_text"] == ""
    assert "时间推进" in last_turn["content"]

    # 3. Model was NOT called (still 0 persona responses)
    persona_turns = [t for t in r.transcript if t.get("participant_id") != "user"]
    assert len(persona_turns) == 0

    # 4. Next speech turn: "老婆，早啊。"
    evs = [e async for e in app.orchestrator.step_turn(room.id, "p1", "老婆，早啊。")]
    assert any(e["event"] == "turn_completed" for e in evs)

    r = app.orchestrator.get_room(room.id)
    assert len(r.transcript) >= 3

    # Check Prompt composition: prompt contains scene time as morning
    session = app.sessions.list_sessions(persona)[0]
    cp = session.metadata.get("counterpart_id", "user")
    prep = app.sessions.prepare_turn(persona, session.id, "老婆，早啊。", counterpart_id=cp)
    _, _, full_prompt = PromptComposer().compose_turn_prompt(
        slot=ParticipantSlot(participant_id="p1", persona_id=persona),
        prepared=prep,
        recent_transcript=r.transcript,
        user_message="老婆，早啊。",
        scene_state=r.scene_state,
    )
    assert "Current Scene State" in full_prompt
    assert "09-19" in full_prompt
    assert "User: 8小时" not in full_prompt
