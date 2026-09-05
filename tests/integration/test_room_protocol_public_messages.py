from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from persona_continuum.room.models import (
    ParticipantSlot,
    RoomMode,
    RoomProtocolConfig,
    RoomProtocolType,
    RoomStatus,
)


def _manifest(persona_id: str) -> dict[str, str]:
    return {
        "id": persona_id,
        "display_name": persona_id,
        "persona_type": "fictional",
        "run_mode": "continuation",
    }


def _slot(persona_id: str, role: str, specialties: list[str] | None = None) -> ParticipantSlot:
    return ParticipantSlot(
        participant_id=f"slot_{persona_id}",
        persona_id=persona_id,
        display_name=persona_id,
        role=role,
        specialties=specialties or [],
        runtime_selection="fake_agent",
        model_selection="fake-gpt-5",
        reasoning_selection="low",
    )


def _protocol_message_kinds(transcript: list[dict[str, Any]]) -> list[str]:
    return [
        str(entry.get("metadata", {}).get("message_kind"))
        for entry in transcript
        if entry.get("commit_status") == "protocol_public"
    ]


def _mark_ready(app: Any, room_id: str) -> None:
    state = app.orchestrator.get_room(room_id)
    assert state is not None
    state.status = RoomStatus.READY
    app.orchestrator._save_room_state(state, force=True)


@pytest.mark.anyio
async def test_expert_consultation_writes_public_messages_to_transcript(app: Any) -> None:
    """Structured model output must surface as public chat transcript entries."""
    for persona_id in ("pub_host", "pub_expert_a", "pub_expert_b"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title="public messages",
        topic="backend and frontend",
        participants=[
            _slot("pub_host", "host"),
            _slot("pub_expert_a", "expert", ["backend"]),
            _slot("pub_expert_b", "expert", ["frontend"]),
        ],
        host_participant_id="slot_pub_host",
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(min_experts=2, max_experts=2),
    )
    await app.orchestrator.start_room(room.id)
    completed = await app.orchestrator.run_protocol(room.id, room.topic or "")

    # The backend condition the frontend polling fallback relies on: the run
    # reached a terminal protocol state and the room returned to ready.
    assert completed.status.value == "ready"
    assert completed.protocol_state.status.value in {"success", "partial_success"}

    fresh = app.orchestrator.get_room(room.id)
    assert fresh is not None
    kinds = _protocol_message_kinds(fresh.transcript)
    # Host analysis, routing, one independent analysis per expert, reviews
    # and a synthesis/final answer — in that relative order.
    assert "protocol_host_analysis" in kinds
    assert "protocol_routing" in kinds
    analysis_entries = [
        entry
        for entry in fresh.transcript
        if entry.get("metadata", {}).get("message_kind") == "protocol_analysis"
    ]
    assert len(analysis_entries) == 2
    analysis_ids = {entry["participant_id"] for entry in analysis_entries}
    assert analysis_ids == {"slot_pub_expert_a", "slot_pub_expert_b"}
    assert "protocol_review" in kinds
    final_kinds = [kind for kind in kinds if kind in {"protocol_synthesis", "protocol_final"}]
    assert len(final_kinds) == 1
    for entry in fresh.transcript:
        if entry.get("commit_status") == "protocol_public":
            assert str(entry.get("content") or "").strip()
            assert entry["metadata"]["run_id"] == completed.protocol_state.run_id
            assert entry["metadata"]["task_id"]
    # No hidden chain-of-thought leaks: fake adapter thinking strings never
    # appear in the transcript.
    assert not any("[Reasoning" in str(entry.get("content")) for entry in fresh.transcript)

    # Refresh / page reload reads the same transcript through the API path.
    reloaded = app.orchestrator.get_room(room.id)
    assert _protocol_message_kinds(reloaded.transcript) == kinds

    # The append-only transcript store is the authority across restarts.
    records = app.orchestrator.list_room_transcripts(room.id)
    assert sum(1 for r in records if r.commit_status == "protocol_public") == len(kinds)
    public_records = [r for r in records if r.commit_status == "protocol_public"]
    assert all(r.metadata.get("message_kind") for r in public_records)


@pytest.mark.anyio
async def test_protocol_public_message_recorder_is_idempotent(app: Any) -> None:
    """run_id + task_id + message_kind dedups across WS replays and retries."""
    for persona_id in ("idem_host", "idem_expert"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title="idempotent",
        topic="dedup",
        participants=[_slot("idem_host", "host"), _slot("idem_expert", "expert")],
        host_participant_id="slot_idem_host",
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(min_experts=1, max_experts=1),
    )
    _mark_ready(app, room.id)
    state = app.orchestrator.get_room(room.id)
    payload = {
        "_room": state,
        "room_id": room.id,
        "run_id": "roomrun_x",
        "task_id": "roomtask_y",
        "stage": "independent_analysis",
        "action": "analysis",
        "message_kind": "protocol_analysis",
        "participant_id": "slot_idem_expert",
        "persona_id": "idem_expert",
        "speaker_name": "idem_expert",
        "content": "同一条公开分析",
    }
    await app.orchestrator._record_protocol_public_message(dict(payload))
    await app.orchestrator._record_protocol_public_message(dict(payload))
    fresh = app.orchestrator.get_room(room.id)
    matches = [
        entry
        for entry in fresh.transcript
        if entry.get("turn_id") == "protocol:roomrun_x:roomtask_y:protocol_analysis"
    ]
    assert len(matches) == 1
    rows = app.orchestrator.list_room_transcripts(room.id)
    dupes = [
        r
        for r in rows
        if r.turn_id == "protocol:roomrun_x:roomtask_y:protocol_analysis"
    ]
    assert len(dupes) == 1


@pytest.mark.anyio
async def test_protocol_final_is_suppressed_after_synthesis_even_when_text_differs(
    app: Any,
) -> None:
    """Terminal summary text must not create a second final-answer card."""
    for persona_id in ("summary_host", "summary_expert"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title="synthesis dedup",
        topic="q",
        participants=[_slot("summary_host", "host"), _slot("summary_expert", "expert")],
        host_participant_id="slot_summary_host",
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
    )
    _mark_ready(app, room.id)
    state = app.orchestrator.get_room(room.id)
    common = {
        "_room": state,
        "room_id": room.id,
        "run_id": "roomrun_summary",
        "participant_id": "slot_summary_host",
        "persona_id": "summary_host",
        "speaker_name": "summary_host",
    }
    await app.orchestrator._record_protocol_public_message(
        {
            **common,
            "task_id": "synthesis_task",
            "stage": "host_synthesis",
            "action": "synthesis",
            "message_kind": "protocol_synthesis",
            "content": "综合结论\n\n共识：A；B\n\n建议：C",
        }
    )
    await app.orchestrator._record_protocol_public_message(
        {
            **common,
            "_room": app.orchestrator.get_room(room.id),
            "task_id": "root_task",
            "stage": "final",
            "action": "finalize",
            "message_kind": "protocol_final",
            "content": "综合结论",
        }
    )
    fresh = app.orchestrator.get_room(room.id)
    assert _protocol_message_kinds(fresh.transcript) == ["protocol_synthesis"]


def test_room_transcripts_turn_id_unique_index(app: Any) -> None:
    app.orchestrator.protocol_repository.ensure_builtin_templates()
    with pytest.raises(sqlite3.IntegrityError):
        for _ in range(2):
            app.database.conn.execute(
                """
                INSERT INTO room_transcripts (
                  id, room_id, turn_id, participant_id, persona_id, speaker_name,
                  agent_runtime_id, content, created_at
                ) VALUES ('x1', 'roomA', 'dup-turn', 'user', 'user', 'User', '', 'hi', 'now')
                """
            )
        app.database.conn.commit()


@pytest.mark.anyio
async def test_inject_message_client_id_is_idempotent(app: Any) -> None:
    """Retrying the same client_message_id never duplicates a user message."""
    for persona_id in ("inject_host", "inject_expert"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title="inject idempotent",
        topic="q",
        participants=[_slot("inject_host", "host"), _slot("inject_expert", "expert")],
        host_participant_id="slot_inject_host",
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
    )
    _mark_ready(app, room.id)
    started: list[str] = []

    async def fake_run(room_id: str, question: str) -> Any:
        started.append(room_id)

    def fake_start(room_id: str, question: str) -> Any:
        started.append(room_id)
        return asyncio.get_running_loop().create_task(asyncio.sleep(0))

    app.orchestrator.start_protocol_background = fake_start  # type: ignore[method-assign]
    first = await app.orchestrator.inject_message(
        room.id, "我的问题", client_message_id="client-1"
    )
    second = await app.orchestrator.inject_message(
        room.id, "我的问题", client_message_id="client-1"
    )
    assert first["message"]["turn_id"] == "user_msg:client-1"
    assert second["duplicate"] is True
    fresh = app.orchestrator.get_room(room.id)
    user_entries = [entry for entry in fresh.transcript if entry.get("participant_id") == "user"]
    assert len(user_entries) == 1
    # The protocol run is triggered once for the injected message.
    assert started.count(room.id) == 1
    # A different client id is a genuinely new message.
    third = await app.orchestrator.inject_message(
        room.id, "第二个问题", client_message_id="client-2"
    )
    assert third.get("duplicate") is not True
    fresh = app.orchestrator.get_room(room.id)
    assert len([e for e in fresh.transcript if e.get("participant_id") == "user"]) == 2


@pytest.mark.anyio
async def test_inject_never_starts_autonomous_for_protocol_rooms(app: Any) -> None:
    for persona_id in ("iso_host", "iso_expert"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title="isolation",
        topic="q",
        participants=[_slot("iso_host", "host"), _slot("iso_expert", "expert")],
        host_participant_id="slot_iso_host",
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
    )
    _mark_ready(app, room.id)
    calls: list[str] = []
    protocol_calls: list[str] = []

    def fake_autonomous(room_id: str, **kwargs: Any) -> None:
        calls.append(room_id)
        raise AssertionError("start_autonomous_discussion must not run for protocol rooms")

    def fake_protocol(room_id: str, question: str) -> Any:
        protocol_calls.append(room_id)
        return asyncio.get_running_loop().create_task(asyncio.sleep(0))

    app.orchestrator.start_autonomous_discussion = fake_autonomous  # type: ignore[method-assign]
    app.orchestrator.start_protocol_background = fake_protocol  # type: ignore[method-assign]
    event = await app.orchestrator.inject_message(room.id, "请会诊")
    assert event["message"]["participant_id"] == "user"
    assert calls == []
    assert protocol_calls == [room.id]
    assert app.orchestrator._autonomous_tasks.get(room.id) is None


@pytest.mark.anyio
async def test_inject_free_discussion_autonomous_still_starts(app: Any) -> None:
    for persona_id in ("fd_a", "fd_b"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title="free discussion",
        topic="chat",
        participants=[_slot("fd_a", "member"), _slot("fd_b", "member")],
        protocol=RoomProtocolType.FREE_DISCUSSION,
        mode=RoomMode.AUTONOMOUS,
    )
    _mark_ready(app, room.id)
    calls: list[str] = []
    protocol_calls: list[str] = []

    def fake_autonomous(room_id: str, **kwargs: Any) -> None:
        calls.append(room_id)

    def fake_protocol(room_id: str, question: str) -> Any:
        protocol_calls.append(room_id)
        raise AssertionError("protocol runtime must not start for free_discussion rooms")

    app.orchestrator.start_autonomous_discussion = fake_autonomous  # type: ignore[method-assign]
    app.orchestrator.start_protocol_background = fake_protocol  # type: ignore[method-assign]
    await app.orchestrator.inject_message(room.id, "开始讨论")
    assert calls == [room.id]
    assert protocol_calls == []
    # Injection success is never masked as failure by a later start error.
    app.orchestrator.start_autonomous_discussion = (  # type: ignore[method-assign]
        lambda room_id, **kwargs: (_ for _ in ()).throw(ValueError("boom"))
    )
    event = await app.orchestrator.inject_message(
        room.id, "再来一条", client_message_id="fd-1"
    )
    assert event["message"]["participant_id"] == "user"


@pytest.mark.anyio
async def test_direct_chat_accepts_one_persona_and_starts_one_reply(app: Any) -> None:
    app.personas.create_from_manifest(_manifest("direct_persona"))
    room = app.orchestrator.create_room(
        title="direct chat",
        participants=[_slot("direct_persona", "member")],
        protocol=RoomProtocolType.FREE_DISCUSSION,
        mode=RoomMode.DIRECT_CHAT,
    )
    _mark_ready(app, room.id)
    calls: list[tuple[str, str]] = []

    def fake_direct_reply(room_id: str, user_message: str) -> None:
        calls.append((room_id, user_message))

    app.orchestrator.start_direct_reply = fake_direct_reply  # type: ignore[method-assign]
    event = await app.orchestrator.inject_message(room.id, "你好")

    assert event["message"]["participant_id"] == "user"
    assert calls == [(room.id, "你好")]


def test_direct_chat_rejects_multiple_personas(app: Any) -> None:
    for persona_id in ("direct_a", "direct_b"):
        app.personas.create_from_manifest(_manifest(persona_id))

    with pytest.raises(ValueError, match="direct_chat_requires_exactly_one_participant"):
        app.orchestrator.create_room(
            title="invalid direct chat",
            participants=[_slot("direct_a", "member"), _slot("direct_b", "member")],
            protocol=RoomProtocolType.FREE_DISCUSSION,
            mode=RoomMode.DIRECT_CHAT,
        )


@pytest.mark.anyio
async def test_final_answer_appears_exactly_once(app: Any) -> None:
    """Synthesis produces the final answer; the terminal event never doubles it."""
    for persona_id in ("once_host", "once_expert"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title="once",
        topic="q",
        participants=[_slot("once_host", "host"), _slot("once_expert", "expert")],
        host_participant_id="slot_once_host",
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(min_experts=1, max_experts=1),
    )
    await app.orchestrator.start_room(room.id)
    completed = await app.orchestrator.run_protocol(room.id, "q")
    fresh = app.orchestrator.get_room(room.id)
    synthesis = [
        entry
        for entry in fresh.transcript
        if entry.get("metadata", {}).get("message_kind") == "protocol_synthesis"
    ]
    finals = [
        entry
        for entry in fresh.transcript
        if entry.get("metadata", {}).get("message_kind") == "protocol_final"
    ]
    # The synthesis answer is displayed once; a deterministic final answer is
    # only appended when no synthesis message exists (never both).
    assert len(synthesis) + len(finals) == 1
    events = app.orchestrator.protocol_repository.list_events(
        room.id, completed.protocol_state.run_id
    )
    final_events = [event for event in events if event.event_type == "final_response"]
    assert len(final_events) == 1
    synthesis_events = [event for event in events if event.event_type == "synthesis_submitted"]
    assert len(synthesis_events) == 1


@pytest.mark.anyio
async def test_protocol_transcript_never_contains_legacy_host_cards(app: Any) -> None:
    """Test D: protocol rooms own their cards; free-discussion host cards never leak."""
    for persona_id in ("nohostcard_host", "nohostcard_expert"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title="no host cards",
        topic="backend",
        participants=[
            _slot("nohostcard_host", "host"),
            _slot("nohostcard_expert", "expert", ["backend"]),
        ],
        host_participant_id="slot_nohostcard_host",
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(min_experts=1, max_experts=1),
    )
    await app.orchestrator.start_room(room.id)
    completed = await app.orchestrator.run_protocol(room.id, room.topic or "")
    assert completed.protocol_state.status.value in {"success", "partial_success"}

    fresh = app.orchestrator.get_room(room.id)
    assert fresh.transcript
    for entry in fresh.transcript:
        assert entry.get("speaker_name") != "Host"
        assert entry.get("commit_status") != "host_message"


@pytest.mark.anyio
async def test_protocol_prompts_taibu_free_and_synthesis_has_public_payload(app: Any) -> None:
    """Test H: no removed tool-provider wording in prompts; synthesis ships payload."""
    for persona_id in ("clean_host", "clean_expert"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title="prompt hygiene",
        topic="backend architecture",
        participants=[
            _slot("clean_host", "host"),
            _slot("clean_expert", "expert", ["backend"]),
        ],
        host_participant_id="slot_clean_host",
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(min_experts=1, max_experts=1),
    )
    await app.orchestrator.start_room(room.id)
    completed = await app.orchestrator.run_protocol(room.id, room.topic or "")
    assert completed.protocol_state.status.value in {"success", "partial_success"}

    # The fake adapter records every turn it receives; each captured system
    # prompt must be free of the removed tool-provider vocabulary.
    adapter = app.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    sent_turns = list(getattr(adapter, "sent_turns", []) or [])
    assert sent_turns, "fake adapter must capture protocol prompts"
    forbidden = ("Taibu", "精确排盘工具当前不可用", "术数工具不可用", "知识解释模式")
    for _session_id, turn in sent_turns:
        system_prompt = str(getattr(turn, "system_prompt", "") or "")
        for needle in forbidden:
            assert needle not in system_prompt

    fresh = app.orchestrator.get_room(room.id)
    synthesis = [
        entry
        for entry in fresh.transcript
        if entry.get("metadata", {}).get("message_kind") == "protocol_synthesis"
    ]
    assert len(synthesis) == 1
    payload = synthesis[0].get("metadata", {}).get("public_payload")
    assert isinstance(payload, dict)
    for key in ("summary", "participant_positions", "final_judgment"):
        assert key in payload
