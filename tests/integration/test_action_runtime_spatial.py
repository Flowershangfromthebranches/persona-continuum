from __future__ import annotations

import json
from datetime import datetime

import pytest

from persona_continuum.domain.scene import ActionEvent, RoomSceneState
from persona_continuum.room.models import ParticipantSlot
from persona_continuum.room.prompt_composer import PromptComposer
from persona_continuum.runtime.scene_runtime import SceneRuntime
from persona_continuum.runtime.turn_normalizer import semantic_experience


@pytest.fixture()
def persona(app):
    return app.personas.create_from_manifest(
        {
            "id": "action-probe",
            "display_name": "苏禾",
            "persona_type": "fictional_or_synthetic_person",
            "run_mode": "digital_continuation",
        }
    ).id


def evening():
    time = datetime.fromisoformat("2026-09-18T23:30:00+08:00")
    return RoomSceneState(scene_time=time, clock_wall_time=datetime.now(time.tzinfo))


def test_case_1_user_action_mode_approach(app, persona):
    """Case 1: User Action Mode approach creates ActionEvent, proximity changes, no dialogue."""
    scene = evening()
    channels, scene_events = SceneRuntime().accept_turn(
        scene,
        room_id="room-act-1",
        turn_id="t1",
        actor="user",
        raw_content="我向苏禾走过去。",
        input_mode="action",
    )
    action_events = [e.action_event for e in scene_events if e.action_event]
    assert channels.spoken_text == ""
    assert len(action_events) >= 1
    ae = action_events[0]
    assert ae.action_type == "approach"
    assert ae.target_id == "苏禾"
    assert ae.status == "completed"
    assert scene.spatial.get_proximity("user", "苏禾") == "near"
    assert "（" not in channels.spoken_text


def test_case_2_user_stand_up(app, persona):
    """Case 2: User '我站起来。' -> posture sitting -> standing."""
    scene = evening()
    scene.spatial.postures["user"] = "sitting"
    channels, scene_events = SceneRuntime().accept_turn(
        scene,
        room_id="room-act-2",
        turn_id="t1",
        actor="user",
        raw_content="我站起来。",
        input_mode="action",
    )
    action_events = [e.action_event for e in scene_events if e.action_event]
    assert scene.spatial.postures["user"] == "standing"
    assert action_events[0].action_type == "stand_up"


def test_case_3_user_hug_interactive_attempt(app, persona):
    """Case 3: User '我抱住她。' -> interactive Action attempt, not immediately completed."""
    scene = evening()
    channels, scene_events = SceneRuntime().accept_turn(
        scene,
        room_id="room-act-3",
        turn_id="t1",
        actor="user",
        raw_content="我抱住她。",
    )
    action_events = [e.action_event for e in scene_events if e.action_event]
    ae = next(a for a in action_events if a.action_type == "hug")
    assert ae.category == "interactive"
    assert ae.status == "attempted"
    # Physical contact is not forced until Persona resolves it
    assert "hugging" not in scene.spatial.get_contacts("user", "她")


def test_case_4_persona_output_speech_and_action(app, persona):
    """Case 4: Persona靠近User并说话 -> structured speech + action, not parenthetical."""
    raw = json.dumps(
        {
            "speech": "你今天怎么这么安静？",
            "actions": [
                {
                    "action_type": "approach",
                    "target_id": "user",
                    "parameters": {"proximity": "near"},
                }
            ],
        },
        ensure_ascii=False,
    )
    scene = evening()
    channels, scene_events = SceneRuntime().accept_turn(
        scene,
        room_id="room-act-4",
        turn_id="t1",
        actor="persona",
        raw_content=raw,
        advance_clock=False,
    )
    action_events = [e.action_event for e in scene_events if e.action_event]
    assert channels.spoken_text == "你今天怎么这么安静？"
    assert action_events[0].action_type == "approach"
    assert action_events[0].target_id == "user"
    assert scene.spatial.get_proximity("persona", "user") == "near"
    assert "（" not in channels.spoken_text


def test_case_5_prompt_never_renders_action_as_parenthetical_narration(app, persona):
    """Case 5: Next turn prompt does not render action as parenthetical narration."""
    scene = evening()
    scene.spatial.postures["user"] = "standing"
    scene.spatial.postures["persona"] = "sitting"
    scene.spatial.set_proximity("user", "persona", "near")

    session = app.sessions.start_session(persona)
    prepared = app.sessions.prepare_turn(persona, session.id, "你好")
    _, _, prompt = PromptComposer().compose_turn_prompt(
        slot=ParticipantSlot(participant_id="p", persona_id=persona),
        prepared=prepared,
        recent_transcript=[
            {
                "participant_id": "persona",
                "spoken_text": "在看书呢。",
                "actions": [{"action_type": "sit_down"}],
            }
        ],
        user_message="我走近你。",
        scene_state=scene,
        structured_actions=True,
    )
    assert "one-or-two-sentence quota" in prompt
    assert "1-2 action quota" in prompt
    assert "Current Physical Scene" in prompt
    assert "Postures: user: standing, persona: sitting" in prompt
    assert "Distances: persona:user: near" in prompt
    assert "（在看书呢）" not in prompt
    assert "（坐下）" not in prompt


def test_case_6_salience_filtering_fifty_actions(app, persona):
    """Case 6: 50 low-salience ActionEvents do not flood long-term memory."""
    low_actions = [
        ActionEvent(room_id="r", actor_id="user", action_type="sit_down", salience="low"),
        ActionEvent(room_id="r", actor_id="user", action_type="stand_up", salience="low"),
        ActionEvent(room_id="r", actor_id="user", action_type="walk", salience="low"),
    ] * 17  # 51 actions
    high_action = ActionEvent(
        room_id="r", actor_id="user", target_id="苏禾", action_type="hug", salience="high"
    )

    mem = semantic_experience(
        "我坐下又站起。",
        "怎么了这是？",
        counterpart="user",
        action_events=low_actions + [high_action],
    )
    # Only the high salience action enters long term semantic experience
    assert "重要互动: hug" in mem
    assert "sit_down" not in mem
    assert "stand_up" not in mem


def test_case_7_action_plus_time_continuity(app, persona):
    """Case 7: 23:30 lie down and sleep -> 8h later waking event seamlessly updates posture."""
    scene = evening()
    # 23:30 user lies down to sleep
    SceneRuntime().accept_turn(
        scene,
        room_id="r",
        turn_id="t1",
        actor="user",
        raw_content="我去睡觉了。",
    )
    assert scene.spatial.postures["user"] == "lying"
    assert scene.activities["user"] == "sleeping"

    # Next morning user wakes up
    SceneRuntime().accept_turn(
        scene,
        room_id="r",
        turn_id="t2",
        actor="user",
        raw_content="第二天早上我醒了，站起身伸了个懒腰。",
    )
    assert scene.activities["user"] == "awake"
    assert scene.spatial.postures["user"] in {"standing", "sitting"}
    assert scene.scene_time.day == 19
    assert scene.scene_time.hour == 8
