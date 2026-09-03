from __future__ import annotations

import json

from starlette.testclient import TestClient

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.web.server import create_web_app


def _create_manifest_persona(continuum: PersonaContinuum, persona_id: str) -> None:
    continuum.personas.create_from_manifest(
        {
            "id": persona_id,
            "display_name": persona_id,
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )


def _room_participants(*pairs: tuple[str, str, str]) -> list[dict[str, object]]:
    return [
        {
            "participant_id": participant_id,
            "persona_id": persona_id,
            "role": role,
            "runtime_selection": "fake_agent",
            "model_selection": "fake-gpt-5",
        }
        for participant_id, persona_id, role in pairs
    ]


def test_room_protocol_template_and_run_api(app) -> None:
    for persona_id in ("api_host", "api_expert"):
        app.personas.create_from_manifest(
            {
                "id": persona_id,
                "display_name": persona_id,
                "persona_type": "fictional",
                "run_mode": "continuation",
            }
        )
    with TestClient(create_web_app(app)) as client:
        protocols = client.get("/api/room-protocols")
        assert protocols.status_code == 200
        assert {item["id"] for item in protocols.json()["data"]} >= {
            "free_discussion",
            "host_moderated",
            "expert_consultation",
            "debate",
            "committee",
        }

        templates = client.get("/api/room-templates")
        assert templates.status_code == 200
        assert any(
            item["name"] == "太卜阁 · 术数综合会诊" for item in templates.json()["data"]
        )

        created_template = client.post(
            "/api/room-templates",
            json={
                "name": "API template",
                "protocol": "expert_consultation",
                "participants": [
                    {"participant_id": "host", "persona_id": "api_host", "role": "host"},
                    {
                        "participant_id": "expert",
                        "persona_id": "api_expert",
                        "role": "expert",
                    },
                ],
                "shared_context": {"background": "template background"},
            },
        )
        assert created_template.status_code == 201
        template_id = created_template.json()["data"]["id"]

        created = client.post(
            "/api/rooms",
            json={
                "title": "Protocol API",
                "topic": "review architecture",
                "protocol": "expert_consultation",
                "protocol_config": {"min_experts": 1, "max_experts": 1},
                "shared_context": {"background": "room copy"},
                "initialize_async": False,
                "participants": [
                    {
                        "participant_id": "host",
                        "persona_id": "api_host",
                        "role": "host",
                        "runtime_selection": "fake_agent",
                        "model_selection": "fake-gpt-5",
                    },
                    {
                        "participant_id": "expert",
                        "persona_id": "api_expert",
                        "role": "expert",
                        "specialties": ["architecture"],
                        "runtime_selection": "fake_agent",
                        "model_selection": "fake-gpt-5",
                    },
                ],
            },
        )
        assert created.status_code == 201
        room_id = created.json()["data"]["id"]

        patched = client.patch(
            f"/api/rooms/{room_id}",
            json={"description": "patched", "shared_context": {"background": "patched copy"}},
        )
        assert patched.status_code == 200
        assert patched.json()["data"]["shared_context"]["background"] == "patched copy"

        run = client.post(
            f"/api/rooms/{room_id}/run",
            json={"question": "review architecture", "background": False},
        )
        assert run.status_code == 200, run.text
        assert run.json()["data"]["protocol_state"]["status"] == "success"
        events = client.get(f"/api/rooms/{room_id}/events")
        assert events.status_code == 200
        assert events.json()["data"][-1]["event_type"] == "final_response"
        finalized = client.post(f"/api/rooms/{room_id}/finalize")
        assert finalized.status_code == 200, finalized.text
        assert finalized.json()["data"]["protocol_state"]["status"] == "success", finalized.text
        assert finalized.json()["data"]["protocol_state"]["source_run_id"] is not None
        runs = client.get(f"/api/room-runs?room_id={room_id}")
        assert runs.status_code == 200
        run_id = runs.json()["data"][0]["id"]
        detail = client.get(f"/api/room-runs/{run_id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["tasks"][0]["task_type"] == "room_run"
        assert all(
            task["parent_task_id"] == detail.json()["data"]["tasks"][0]["id"]
            for task in detail.json()["data"]["tasks"][1:]
        )
        removed_run = client.delete(f"/api/room-runs/{run_id}")
        assert removed_run.status_code == 200
        assert removed_run.json()["data"]["deleted"] is True
        for historical in client.get(f"/api/room-runs?room_id={room_id}").json()["data"]:
            assert client.delete(f"/api/room-runs/{historical['id']}").status_code == 200

        deleted = client.delete(f"/api/room-templates/{template_id}")
        assert deleted.status_code == 200
        assert deleted.json()["data"]["deleted"] is True


def test_expert_consultation_protocol_persists_across_restart(tmp_path) -> None:
    """Test C: expert_consultation protocol survives a full app restart."""

    data_dir = tmp_path / "pc-data"
    room_id = ""
    first = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=True)
    first.init()
    try:
        _create_manifest_persona(first, "restart_host")
        _create_manifest_persona(first, "restart_expert")
        with TestClient(create_web_app(first)) as client:
            created = client.post(
                "/api/rooms",
                json={
                    "title": "Restart protocol",
                    "protocol": "expert_consultation",
                    "initialize_async": False,
                    "participants": _room_participants(
                        ("host", "restart_host", "host"),
                        ("expert", "restart_expert", "expert"),
                    ),
                },
            )
            assert created.status_code == 201, created.text
            room_id = created.json()["data"]["id"]
            assert created.json()["data"]["protocol"] == "expert_consultation"
        # Guarantee the row is durable before simulating the restart.
        first.orchestrator.flush_room_state(room_id)
    finally:
        first.close()

    second = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=True)
    second.init()
    try:
        with TestClient(create_web_app(second)) as client:
            got = client.get(f"/api/rooms/{room_id}")
            assert got.status_code == 200, got.text
            assert got.json()["data"]["protocol"] == "expert_consultation"
    finally:
        second.close()


def test_legacy_room_without_protocol_defaults_to_free_discussion(tmp_path) -> None:
    """Legacy rows persisted before the protocol field must still open."""

    data_dir = tmp_path / "pc-data"
    room_id = ""
    first = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=True)
    first.init()
    try:
        _create_manifest_persona(first, "legacy_host")
        _create_manifest_persona(first, "legacy_expert")
        with TestClient(create_web_app(first)) as client:
            created = client.post(
                "/api/rooms",
                json={
                    "title": "Legacy row",
                    "protocol": "free_discussion",
                    "initialize_async": False,
                    "participants": _room_participants(
                        ("a", "legacy_host", "member"),
                        ("b", "legacy_expert", "member"),
                    ),
                },
            )
            assert created.status_code == 201, created.text
            room_id = created.json()["data"]["id"]
        first.orchestrator.flush_room_state(room_id)
    finally:
        first.close()

    second = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=True)
    second.init()
    try:
        # Simulate a legacy row persisted before the protocol key existed.
        # Done on the fresh instance so no in-process cache re-persists it.
        row = second.database.conn.execute(
            "SELECT state_json FROM rooms WHERE id = ?", (room_id,)
        ).fetchone()
        assert row is not None
        state_data = json.loads(row["state_json"])
        state_data.pop("protocol", None)
        second.database.conn.execute(
            "UPDATE rooms SET state_json = ? WHERE id = ?",
            (json.dumps(state_data), room_id),
        )
        second.database.conn.commit()
        with TestClient(create_web_app(second)) as client:
            got = client.get(f"/api/rooms/{room_id}")
            assert got.status_code == 200, got.text
            assert got.json()["data"]["protocol"] == "free_discussion"
    finally:
        second.close()


def test_create_room_without_explicit_protocol_is_rejected(app) -> None:
    """Missing protocol + no template must fail loudly, not create a room."""

    _create_manifest_persona(app, "noproto_host")
    _create_manifest_persona(app, "noproto_expert")
    with TestClient(create_web_app(app)) as client:
        res = client.post(
            "/api/rooms",
            json={
                "title": "No protocol",
                "initialize_async": False,
                "participants": _room_participants(
                    ("host", "noproto_host", "host"),
                    ("expert", "noproto_expert", "expert"),
                ),
            },
        )
        assert res.status_code == 400
        assert res.json()["error"] == "请选择房间协作模式。"


def test_create_room_without_participants_is_rejected_instead_of_auto_binding(app) -> None:
    _create_manifest_persona(app, "must_not_auto_bind")
    with TestClient(create_web_app(app)) as client:
        res = client.post(
            "/api/rooms",
            json={
                "title": "Blank participant list",
                "protocol": "free_discussion",
                "initialize_async": False,
                "participants": [],
            },
        )
    assert res.status_code == 400
    assert res.json()["error"] == "请至少添加并配置一个参与者席位。"


def test_create_room_missing_expert_role_returns_friendly_error(app) -> None:
    """protocol_role_required errors map to a friendly Chinese message."""

    _create_manifest_persona(app, "rolecheck_host")
    _create_manifest_persona(app, "rolecheck_expert")
    with TestClient(create_web_app(app)) as client:
        res = client.post(
            "/api/rooms",
            json={
                "title": "Missing expert",
                "protocol": "expert_consultation",
                "initialize_async": False,
                "participants": _room_participants(
                    ("host", "rolecheck_host", "host"),
                    ("critic", "rolecheck_expert", "critic"),
                ),
            },
        )
        assert res.status_code == 400
        assert res.json()["error"] == "专家会诊至少需要 1 位主持人与 1 位专家。"


def _create_free_discussion_room(client: TestClient, host_id: str, expert_id: str) -> str:
    created = client.post(
        "/api/rooms",
        json={
            "title": "Upgrade me",
            "topic": "convert protocol",
            "protocol": "free_discussion",
            "initialize_async": False,
            "participants": _room_participants(
                ("slot_host", host_id, "host"),
                ("slot_m1", expert_id, "member"),
            ),
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]["id"]


def test_convert_room_protocol_migrates_free_discussion(app) -> None:
    _create_manifest_persona(app, "conv_host")
    _create_manifest_persona(app, "conv_expert")
    with TestClient(create_web_app(app)) as client:
        room_id = _create_free_discussion_room(client, "conv_host", "conv_expert")
        converted = client.post(
            f"/api/rooms/{room_id}/protocol/convert",
            json={
                "target_protocol": "expert_consultation",
                "role_mapping": {"slot_host": "host", "slot_m1": "expert"},
            },
        )
        assert converted.status_code == 200, converted.text
        data = converted.json()["data"]
        assert data["protocol"] == "expert_consultation"
        assert data["host_participant_id"] == "slot_host"
        roles = {p["participant_id"]: p["role"] for p in data["participants"]}
        assert roles == {"slot_host": "host", "slot_m1": "expert"}
        # Live run state is reset; history keeps in the event store.
        assert data["protocol_state"]["status"] == "pending"
        assert data["protocol_state"]["current_stage"] == "waiting_user"
        # Transcript etc. are untouched (empty room: still no entries).
        assert data["transcript"] == []
        # Audit event recorded against a valid run anchor.
        events = client.get(f"/api/rooms/{room_id}/events")
        assert events.status_code == 200
        assert events.json()["data"][-1]["event_type"] == "protocol_converted"
        # The converted room can immediately run the target protocol.
        run = client.post(
            f"/api/rooms/{room_id}/run",
            json={"question": "review architecture", "background": False},
        )
        assert run.status_code == 200, run.text
        assert run.json()["data"]["protocol_state"]["status"] == "success"


def test_convert_room_protocol_guards(app) -> None:
    _create_manifest_persona(app, "guard_host")
    _create_manifest_persona(app, "guard_expert")
    with TestClient(create_web_app(app)) as client:
        # 404: unknown room.
        missing = client.post(
            "/api/rooms/room_missing/protocol/convert",
            json={
                "target_protocol": "expert_consultation",
                "role_mapping": {"slot_host": "host"},
            },
        )
        assert missing.status_code == 404
        assert missing.json()["details"]["code"] == "room_not_found"

        room_id = _create_free_discussion_room(client, "guard_host", "guard_expert")

        # 400: unsupported target protocol.
        wrong_target = client.post(
            f"/api/rooms/{room_id}/protocol/convert",
            json={"target_protocol": "debate", "role_mapping": {"slot_host": "host"}},
        )
        assert wrong_target.status_code == 400
        assert wrong_target.json()["details"]["code"] == "unsupported_protocol_conversion"

        # 400: mapping without exactly one host.
        no_host = client.post(
            f"/api/rooms/{room_id}/protocol/convert",
            json={
                "target_protocol": "expert_consultation",
                "role_mapping": {"slot_host": "expert", "slot_m1": "expert"},
            },
        )
        assert no_host.status_code == 400
        assert no_host.json()["details"]["code"] == "invalid_role_mapping"

        # 400: mapping does not cover every enabled participant.
        incomplete = client.post(
            f"/api/rooms/{room_id}/protocol/convert",
            json={"target_protocol": "expert_consultation", "role_mapping": {"slot_host": "host"}},
        )
        assert incomplete.status_code == 400
        assert incomplete.json()["details"]["code"] == "invalid_role_mapping"
        assert "slot_m1" in incomplete.json()["error"]

        # 409: room no longer lifecycle-active after stop.
        stopped = client.post(f"/api/rooms/{room_id}/stop")
        assert stopped.status_code == 200, stopped.text
        settled = client.post(
            f"/api/rooms/{room_id}/protocol/convert",
            json={
                "target_protocol": "expert_consultation",
                "role_mapping": {"slot_host": "host", "slot_m1": "expert"},
            },
        )
        assert settled.status_code == 409
        assert settled.json()["details"]["code"] == "room_status_not_convertible"


def test_convert_room_protocol_rejects_already_converted(app) -> None:
    _create_manifest_persona(app, "again_host")
    _create_manifest_persona(app, "again_expert")
    with TestClient(create_web_app(app)) as client:
        room_id = _create_free_discussion_room(client, "again_host", "again_expert")
        first = client.post(
            f"/api/rooms/{room_id}/protocol/convert",
            json={
                "target_protocol": "expert_consultation",
                "role_mapping": {"slot_host": "host", "slot_m1": "expert"},
            },
        )
        assert first.status_code == 200, first.text
        second = client.post(
            f"/api/rooms/{room_id}/protocol/convert",
            json={
                "target_protocol": "expert_consultation",
                "role_mapping": {"slot_host": "host", "slot_m1": "expert"},
            },
        )
        # Only free_discussion -> expert_consultation is supported.
        assert second.status_code == 400
        assert second.json()["details"]["code"] == "unsupported_protocol_conversion"
