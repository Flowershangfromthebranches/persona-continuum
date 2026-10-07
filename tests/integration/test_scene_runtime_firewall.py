from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from persona_continuum.application._utils import dumps
from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.scene import RoomSceneState
from persona_continuum.room.models import ParticipantSlot, RoomMode
from persona_continuum.room.prompt_composer import PromptComposer
from persona_continuum.runtime.scene_runtime import SceneRuntime
from persona_continuum.runtime.turn_normalizer import normalize_turn, normalize_turn_for_prompt
from persona_continuum.storage.temporal_migration import VERSION, migrate_temporal_history


@pytest.fixture()
def persona(app):
    return app.personas.create_from_manifest(
        {
            "id": "scene-probe",
            "display_name": "Scene Probe",
            "persona_type": "fictional_or_synthetic_person",
            "run_mode": "digital_continuation",
        }
    ).id


def evening():
    time = datetime.fromisoformat("2026-09-18T23:30:00+08:00")
    return RoomSceneState(scene_time=time, clock_wall_time=time)


def accept(scene, text, seconds=0, actor="user", turn="t1"):
    return SceneRuntime().accept_turn(
        scene,
        room_id="room-test",
        turn_id=turn,
        actor=actor,
        raw_content=text,
        wall_time=scene.clock_wall_time + timedelta(seconds=seconds),
    )


@pytest.mark.parametrize("seconds,hour", [(8 * 3600, 7), (10, 23)])
def test_sleep_uses_elapsed_time_without_offscreen_invention(seconds, hour):
    scene = evening()
    accept(scene, "我去睡觉了。")
    assert scene.activities["user"] == "sleeping"
    assert scene.scene_time.hour == 23
    accept(scene, "对了，还有件事。", seconds, turn="t2")
    assert scene.scene_time.hour == hour
    assert scene.elapsed_since_last_interaction == seconds
    assert [e.type for e in scene.recent_events] == ["activity_start"]
    assert scene.activities["user"] == "sleeping"  # Duration does not invent waking.


@pytest.mark.parametrize("text", ["第二天早上我醒了。", "（时间很快过去，现在第二天早上）我醒了。"])
def test_explicit_morning_and_sleep_end(text):
    scene = evening()
    accept(scene, "我去睡觉了。")
    channels, events = accept(scene, text, 10, turn="t2")
    assert scene.scene_time.day == 19 and scene.scene_time.hour == 8
    assert channels.spoken_text == "我醒了。"
    assert scene.activities["user"] == "awake"
    assert [e.type for e in events] == ["time_advance", "activity_end", "activity_start"]
    assert all(e.created_at.day == 18 for e in events)


@pytest.mark.parametrize(
    "text", ["如果第二天早上我醒了怎么办？", "我不去睡觉了。", "她说：我去睡觉了。"]
)
def test_hypothetical_quoted_and_negative_are_not_confirmed_events(text):
    assert normalize_turn(text).scene_events == []


def test_action_and_unknown_directions_never_become_voice_examples():
    raw = "(镜头拉远)\n（抱住她）\n晚安。\n（她陷入了梦境）"
    normalized = normalize_turn_for_prompt({"content": raw, "participant_id": "persona"})
    assert normalized["spoken_text"] == "晚安。"
    assert normalized["actions"][0]["payload"]["action"] == "hug"
    assert "镜头" not in json.dumps(normalized, ensure_ascii=False)


def test_structured_action_separate_from_speech():
    scene = evening()
    raw = json.dumps(
        {
            "speech": "晚安。",
            "actions": [{"type": "physical_action", "action": "hug", "target": "user"}],
            "scene_updates": [],
        },
        ensure_ascii=False,
    )
    channels, events = accept(scene, raw, actor="persona")
    assert channels.spoken_text == "晚安。"
    assert events[0].payload == {"action": "hug", "target": "user"}
    view = normalize_turn_for_prompt({**channels.model_dump(), "participant_id": "persona"})
    assert view["spoken_text"] == "晚安。"
    assert "（" not in json.dumps(view, ensure_ascii=False)


def test_twelve_hours_prepare_commit_and_later_wall_reads_never_rewind(app, persona):
    session = app.sessions.start_session(persona)
    wall = datetime.now(UTC)
    app.sessions.prepare_turn(persona, session.id, "你好", current_time=wall)
    app.motivation.update_needs(persona, {"intimacy": -0.3}, "probe", now=wall)
    app.affect.update_emotions(persona, {"joy": 0.9}, "probe", now=wall)
    future = wall + timedelta(hours=12)
    prepared = app.sessions.prepare_turn(persona, session.id, "我醒了", current_time=future)
    assert next(e for e in prepared.current_emotions if e.name == "joy").intensity < 0.9
    result = app.sessions.commit_turn(
        persona,
        session.id,
        user_message="我醒了",
        persona_response="（抱住你）早安。",
        state_patch={"needs": {"intimacy": -0.05}, "affect_delta": {"joy": 0.05}},
    )
    memory = app.memories.get_memory(result["memory_id"])
    assert memory.occurred_at == future
    assert memory.written_at < future
    assert memory.retrieval_role == "event"
    assert "（" not in memory.content
    assert all(n.updated_at >= future for n in app.motivation.get_needs(persona, now=wall))
    assert all(e.updated_at >= future for e in app.affect.get_emotions(persona, now=wall))
    archived = app.database.conn.execute(
        "SELECT * FROM session_turns WHERE id=?", (result["turn_id"],)
    ).fetchone()
    assert archived["persona_response"] == "（抱住你）早安。"
    assert datetime.fromisoformat(archived["created_at"]) < future


def test_raw_archive_and_voice_roles_are_separate(app, persona):
    archived = app.memories.add_memory(
        persona, content="（舞台调度）晚安。", memory_type=MemoryType.DIGITAL_EXPERIENCE
    )
    assert archived.retrieval_role == "raw_archive"
    assert not app.memories.search_memories(persona, "晚安")
    app.memories.rebuild_index()
    assert not app.database.conn.execute(
        "SELECT 1 FROM memories_fts WHERE memory_id=?", (archived.id,)
    ).fetchone()


def test_migration_retains_30_raw_turns_and_runtime_state(app, persona):
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
    )
    session = app.sessions.start_session(persona, room_id=room.id)
    raw = "（抱住你）\n晚安。\n（镜头拉远）"
    for i in range(30):
        result = app.sessions.commit_turn(
            persona, session.id, user_message="我要睡了", persona_response=raw
        )
        app.database.conn.execute(
            "UPDATE memories SET metadata_json=? WHERE id=?",
            (dumps({"turn_id": result["turn_id"], "session_id": session.id}), result["memory_id"]),
        )
        app.database.conn.execute(
            "INSERT INTO room_transcripts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"r{i}",
                room.id,
                result["turn_id"],
                "p1",
                persona,
                "Probe",
                "fake",
                None,
                None,
                None,
                raw,
                None,
                "[]",
                "committed",
                "{}",
                f"2026-09-18T23:{i:02d}:00+08:00",
            ),
        )
        app.database.conn.commit()
    raw_before = [
        tuple(r)
        for r in app.database.conn.execute("SELECT id,content,created_at FROM room_transcripts")
    ]
    runtime_before = {
        table: [tuple(r) for r in app.database.conn.execute(f"SELECT * FROM {table}")]
        for table in ("relationships", "affect_states", "needs", "sessions", "session_turns")
    }
    app.database.conn.execute("DELETE FROM runtime_data_migrations WHERE version=?", (VERSION,))
    app.database.conn.commit()
    report = migrate_temporal_history(app.database.conn)
    assert report["turns"] == 30 and report["archived_memories"] == 30
    assert report["semantic_memories"] == 30
    assert raw_before == [
        tuple(r)
        for r in app.database.conn.execute("SELECT id,content,created_at FROM room_transcripts")
    ]
    for table, expected in runtime_before.items():
        assert expected == [tuple(r) for r in app.database.conn.execute(f"SELECT * FROM {table}")]
    assert migrate_temporal_history(app.database.conn)["already_applied"]
    found = app.memories.search_memories(persona, "晚安", limit=40)
    assert len(found) == 30
    assert all(m.retrieval_role == "event" and "（" not in m.content for m in found)
    state = json.loads(
        app.database.conn.execute("SELECT state_json FROM rooms WHERE id=?", (room.id,)).fetchone()[
            0
        ]
    )
    assert "（" not in state["metadata"].get("rolling_summary", "")
    assert state["metadata"]["voice_reset_participants"] == ["p1"]


@pytest.mark.anyio
async def test_room_turn_persists_channels_scene_time_and_duplicate_injection(app, persona):
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
    )
    await app.orchestrator.start_room(room.id)
    first = await app.orchestrator.inject_message(
        room.id, "（第二天早上）我醒了。", client_message_id="morning"
    )
    room = app.orchestrator.get_room(room.id)
    time = room.scene_state.scene_time
    second = await app.orchestrator.inject_message(
        room.id, "（第二天早上）我醒了。", client_message_id="morning"
    )
    assert second["duplicate"] and room.scene_state.scene_time == time
    assert first["message"]["spoken_text"] == "我醒了。"
    events = [e async for e in app.orchestrator.step_turn(room.id, manual_speaker_id="p1")]
    assert (
        next(i for i, e in enumerate(events) if e["event"] == "recall_started")
        < next(i for i, e in enumerate(events) if e["event"] == "recall_completed")
        < next(i for i, e in enumerate(events) if e["event"] == "agent_started")
    )
    assert any(e["event"] == "turn_completed" for e in events)
    memory = app.memories.list_memories(persona)[0]
    assert memory.occurred_at >= time
    assert app.orchestrator.get_room(room.id).transcript[-1]["raw_content"]
    assert (
        app.database.conn.execute(
            "SELECT count(*) FROM room_scene_events WHERE room_id=?", (room.id,)
        ).fetchone()[0]
        >= 3
    )


def test_prompt_does_not_expose_legacy_sandwich(app, persona):
    session = app.sessions.start_session(persona)
    prepared = app.sessions.prepare_turn(persona, session.id, "你好")
    raw = "（抱住你）\n晚安。\n（镜头拉远）"
    _, _, prompt = PromptComposer().compose_turn_prompt(
        slot=ParticipantSlot(participant_id="p", persona_id=persona),
        prepared=prepared,
        recent_transcript=[{"participant_id": "p", "content": raw}] * 30,
        user_message="（第二天早上）我醒了。",
        scene_state=evening(),
    )
    assert "（" not in prompt
    assert "镜头拉远" not in prompt
    assert "hug" in prompt and "Current Scene State" in prompt
    assert "晚安。" in prompt


def test_prefaced_json_is_archived_but_never_spoken():
    raw = '先核对 JSON 契约。{"speech":"早呀。","actions":[{"action":"hug","target":"user"}]}'
    channels = normalize_turn(raw, actor="persona")
    assert channels.spoken_text == "早呀。"
    assert channels.non_voice_context == ["先核对 JSON 契约。"]
    assert channels.actions[0]["payload"]["action"] == "hug"
    assert channels.raw_content == raw


def test_question_after_temporal_statement_does_not_cancel_intent():
    scene = evening()
    channels, _ = accept(scene, "第二天早上我醒了。早呀，你想聊什么？")
    assert scene.scene_time.day == 19
    assert scene.activities["user"] == "awake"
    assert channels.spoken_text == "我醒了。早呀，你想聊什么？"


@pytest.mark.anyio
async def test_direct_step_input_updates_authoritative_scene(app, persona):
    room = app.orchestrator.create_room(
        participants=[
            ParticipantSlot(
                participant_id="p",
                persona_id=persona,
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
            )
        ],
        mode=RoomMode.MANUAL,
        scene_state=evening(),
    )
    await app.orchestrator.start_room(room.id)
    room = app.orchestrator.get_room(room.id)
    room.scene_state.time_mode = "narrative"
    app.orchestrator._save_room_state(room, force=True)
    events = [e async for e in app.orchestrator.step_turn(room.id, "p", "第二天早上我醒了。")]
    assert any(e["event"] == "turn_completed" for e in events)
    room = app.orchestrator.get_room(room.id)
    assert room.scene_state.scene_time.hour == 8
    assert room.scene_state.scene_time.day == 19
    assert room.transcript[0]["participant_id"] == "user"
    assert app.memories.list_memories(persona)[0].occurred_at == room.scene_state.scene_time
