from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import pytest

from persona_continuum.room.models import (
    ParticipantSlot,
    RoomProtocolConfig,
    RoomProtocolType,
    RoomSharedContext,
)
from persona_continuum.room.protocol_runtime import ProtocolActionRequest, RoomProtocolRuntime
from persona_continuum.room.protocols import ProtocolRegistry


def _create_persona(app: Any, persona_id: str) -> None:
    app.personas.create_from_manifest(
        {
            "id": persona_id,
            "display_name": persona_id,
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )


def _participant(
    persona_id: str, role: str, specialties: list[str] | None = None
) -> ParticipantSlot:
    return ParticipantSlot(
        participant_id=f"slot_{persona_id}",
        persona_id=persona_id,
        display_name=persona_id,
        role=role,
        specialties=specialties or [],
        runtime_selection="fake_agent",
        model_selection="fake-gpt-5",
    )


def test_builtin_protocols_and_required_roles() -> None:
    registry = ProtocolRegistry()
    ids = [definition.id.value for definition in registry.list_definitions()]
    assert ids == [
        "free_discussion",
        "host_moderated",
        "expert_consultation",
        "debate",
        "committee",
    ]
    expert = registry.get(RoomProtocolType.EXPERT_CONSULTATION)
    with pytest.raises(ValueError, match="protocol_role_required"):
        registry.validate_participants(expert, [_participant("only_member", "member")])


@pytest.mark.anyio
async def test_expert_consultation_is_parallel_and_first_analysis_is_isolated(app: Any) -> None:
    for persona_id in ("host", "a", "b", "c"):
        _create_persona(app, persona_id)
    participants = [
        _participant("host", "host"),
        _participant("a", "expert", ["backend"]),
        _participant("b", "expert", ["frontend"]),
        _participant("c", "expert", ["security"]),
    ]
    room = app.orchestrator.create_room(
        title="consultation",
        topic="backend frontend security architecture",
        participants=participants,
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(min_experts=3, max_experts=3),
        shared_context=RoomSharedContext(background="shared only"),
    )
    runtime = RoomProtocolRuntime(app.orchestrator.protocol_repository)
    seen: dict[str, list[ProtocolActionRequest]] = defaultdict(list)
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def execute(request: ProtocolActionRequest) -> dict[str, Any]:
        nonlocal active, max_active
        seen[request.action].append(request)
        if request.action == "routing":
            return {
                "selected_participant_ids": ["slot_a", "slot_b", "slot_c"],
                "routing_reason": "all relevant",
            }
        if request.action == "analysis":
            assert request.peer_results == []
            async with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            async with lock:
                active -= 1
            return {
                "summary": f"analysis {request.participant.participant_id}",
                "key_findings": [request.participant.participant_id],
                "evidence": [],
                "uncertainties": [],
                "recommendation": "continue",
                "confidence": 0.8,
            }
        if request.action == "review":
            assert len(request.peer_results) == 2
            assert all(
                item["participant_id"] != request.participant.participant_id
                for item in request.peer_results
            )
            return {
                "agree": ["scope"],
                "disagree": [],
                "concerns": [],
                "changed_position": False,
                "updated_conclusion": None,
            }
        return {
            "summary": "synthesis",
            "consensus": ["shared"],
            "conflicts": [],
            "uncertainties": [],
            "recommendations": ["act"],
        }

    result = await runtime.run(room, room.topic or "", execute)
    assert result.status.value == "success"
    assert max_active == 3
    assert len(result.submissions) == 3
    assert len(result.reviews) == 3
    assert result.final_result and result.final_result["summary"] == "synthesis"


@pytest.mark.anyio
async def test_member_failure_finishes_partial_success(app: Any) -> None:
    for persona_id in ("host2", "good", "bad"):
        _create_persona(app, persona_id)
    room = app.orchestrator.create_room(
        title="partial",
        topic="test failure",
        participants=[
            _participant("host2", "host"),
            _participant("good", "expert"),
            _participant("bad", "expert"),
        ],
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(
            min_experts=2,
            max_experts=2,
            cross_review=False,
            continue_on_member_failure=True,
        ),
    )
    runtime = RoomProtocolRuntime(app.orchestrator.protocol_repository)

    async def execute(request: ProtocolActionRequest) -> dict[str, Any]:
        if request.action == "routing":
            return {
                "selected_participant_ids": ["slot_good", "slot_bad"],
                "routing_reason": "test",
            }
        if request.action == "analysis" and request.participant.participant_id == "slot_bad":
            raise TimeoutError("expert timeout")
        if request.action == "analysis":
            return {
                "summary": "usable",
                "key_findings": [],
                "evidence": [],
                "uncertainties": [],
                "recommendation": "continue",
                "confidence": 0.7,
            }
        return {"summary": "host used successful result"}

    result = await runtime.run(room, room.topic or "", execute)
    assert result.status.value == "partial_success"
    assert len(result.submissions) == 1
    assert any(task.error_type == "TimeoutError" for task in result.tasks)
    assert result.final_result and "successful" in result.final_result["summary"]


@pytest.mark.anyio
async def test_committee_records_votes_and_chair_result(app: Any) -> None:
    for persona_id in ("chair", "m1", "m2"):
        _create_persona(app, persona_id)
    room = app.orchestrator.create_room(
        title="committee",
        topic="approve proposal",
        participants=[
            _participant("chair", "chair"),
            _participant("m1", "member"),
            _participant("m2", "member"),
        ],
        protocol=RoomProtocolType.COMMITTEE,
    )
    runtime = RoomProtocolRuntime(app.orchestrator.protocol_repository)

    async def execute(request: ProtocolActionRequest) -> dict[str, Any]:
        if request.action == "analysis":
            return {
                "summary": "opinion",
                "key_findings": [],
                "evidence": [],
                "uncertainties": [],
                "recommendation": "approve",
                "confidence": 0.8,
            }
        if request.action == "review":
            return {
                "agree": [],
                "disagree": [],
                "concerns": [],
                "changed_position": False,
                "updated_conclusion": None,
            }
        if request.action == "vote":
            return {"vote": "approve", "reason": "sound", "confidence": 0.9}
        return {"summary": "approve with safeguards"}

    result = await runtime.run(room, room.topic or "", execute)
    assert result.status.value == "success"
    assert len(result.votes) == 3
    assert result.final_result
    assert result.final_result["vote_count"] == {"approve": 3.0}


def test_legacy_room_migration_and_template_snapshot(app: Any) -> None:
    now = "2026-01-01T00:00:00+00:00"
    app.database.conn.execute(
        "INSERT INTO rooms VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy_room",
            "active",
            "[]",
            "legacy",
            '{"id":"legacy_room","status":"active","participants":[]}',
            now,
            now,
        ),
    )
    app.database.conn.commit()
    app.database._ensure_room_protocol_state()
    migrated = app.orchestrator.get_room("legacy_room")
    assert migrated is not None
    assert migrated.protocol == RoomProtocolType.FREE_DISCUSSION
    assert migrated.shared_context.background == ""

    template = app.orchestrator.protocol_repository.create_template(
        {
            "name": "snapshot",
            "protocol": "free_discussion",
            "participants": [],
            "shared_context": {"background": "v1"},
        }
    )
    saved = app.orchestrator.protocol_repository.get_template(template.id)
    assert saved and saved.shared_context.background == "v1"
    app.orchestrator.protocol_repository.update_template(
        template.id, {"shared_context": {"background": "v2"}}
    )
    assert saved.shared_context.background == "v1"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("protocol", "roles", "expected_stages"),
    [
        (
            RoomProtocolType.HOST_MODERATED,
            ["host", "member"],
            {"host_analysis", "member_response", "host_final"},
        ),
        (
            RoomProtocolType.DEBATE,
            ["host", "pro", "con", "judge"],
            {"pro_argument", "con_argument", "cross_examination", "judge_review", "final"},
        ),
    ],
)
async def test_host_and_debate_protocols_follow_declared_stages(
    app: Any,
    protocol: RoomProtocolType,
    roles: list[str],
    expected_stages: set[str],
) -> None:
    participants = []
    for index, role in enumerate(roles):
        persona_id = f"{protocol.value}_{role}_{index}"
        _create_persona(app, persona_id)
        participants.append(_participant(persona_id, role))
    room = app.orchestrator.create_room(
        title=protocol.value,
        topic="question",
        participants=participants,
        protocol=protocol,
    )
    runtime = RoomProtocolRuntime(app.orchestrator.protocol_repository)
    seen_stages: set[str] = set()

    async def execute(request: ProtocolActionRequest) -> dict[str, Any]:
        seen_stages.add(request.stage)
        if request.action == "review":
            return {
                "agree": [],
                "disagree": [],
                "concerns": [],
                "changed_position": False,
                "updated_conclusion": None,
            }
        if request.action == "routing":
            return {"selected_participant_ids": [], "routing_reason": "continue"}
        return {"summary": f"completed {request.stage}"}

    result = await runtime.run(room, room.topic or "", execute)
    assert result.status.value == "success"
    assert expected_stages <= seen_stages


def test_synthesis_schema_is_gated_to_expert_consultation(app: Any) -> None:
    """The dense synthesis schema/density contract is expert_consultation-only.

    Committee (chair_synthesis) and host_moderated (host_final) reuse the
    "synthesis" action but must keep the permissive default schema and no
    density retry.
    """

    schema_for = app.orchestrator._protocol_output_schema
    expert = schema_for("synthesis", RoomProtocolType.EXPERT_CONSULTATION)
    assert expert["required"] == ["summary", "participant_positions", "final_judgment"]
    for protocol in (RoomProtocolType.HOST_MODERATED, RoomProtocolType.COMMITTEE, None):
        assert schema_for("synthesis", protocol)["required"] == ["summary"]
