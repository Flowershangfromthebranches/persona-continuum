"""Integration tests for the Narrative Director REST API surface."""

from __future__ import annotations

import json
import time
from types import MethodType
from typing import Any

import pytest
from starlette.testclient import TestClient

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.web.server import create_web_app
from tests.fixtures.narrative_runtime import (
    install_narrative_responder,
    seed_project,
)


@pytest.fixture()
def client(tmp_path):
    continuum = PersonaContinuum(
        Config(data_dir=tmp_path / "dir_api"), include_fake_agent=True
    )
    continuum.init()
    with TestClient(create_web_app(continuum)) as test_client:
        yield test_client, continuum
    continuum.close()


def _decide(**kwargs: Any) -> str:
    return json.dumps(kwargs)


def test_director_session_flow_over_http(client, monkeypatch) -> None:
    test_client, continuum = client
    install_narrative_responder(continuum, monkeypatch)
    project, _bible = seed_project(continuum)
    adapter = continuum.agent_registry.get_adapter("fake_agent")
    queue = [
        _decide(decision="execute_action", action="get_project", arguments={}),
        _decide(decision="answer", message="项目状态正常。"),
    ]
    original = adapter._generate_mock_response

    def responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Narrative Director Agent" in prompt:
            return queue.pop(0)
        return original(session, turn)

    adapter._generate_mock_response = MethodType(responder, adapter)

    # Create a session bound to EP01.
    created = test_client.post(
        f"/api/narratives/{project.id}/director/sessions",
        json={"episode_number": 1, "mode": "agent"},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["data"]["id"]

    listed = test_client.get(f"/api/narratives/{project.id}/director/sessions")
    assert listed.status_code == 200
    assert any(s["id"] == session_id for s in listed.json()["data"])

    # Send a message; the loop runs on the app event loop.
    sent = test_client.post(
        f"/api/narratives/{project.id}/director/sessions/{session_id}/messages",
        json={"content": "看一下项目状态"},
    )
    assert sent.status_code == 202, sent.text

    # Poll until the loop finishes (bounded).
    deadline = time.time() + 10
    snapshot: dict[str, Any] = {}
    while time.time() < deadline:
        got = test_client.get(
            f"/api/narratives/{project.id}/director/sessions/{session_id}"
        )
        assert got.status_code == 200
        snapshot = got.json()["data"]
        if snapshot["session"]["status"] != "running":
            break
        time.sleep(0.05)

    assert snapshot["session"]["status"] == "active"
    roles = [m["role"] for m in snapshot["messages"]]
    assert roles[0] == "user" and "assistant" in roles
    actions = test_client.get(
        f"/api/narratives/{project.id}/director/sessions/{session_id}/actions"
    )
    assert actions.status_code == 200
    data = actions.json()["data"]
    assert len(data) == 1
    assert data[0]["action"] == "get_project"
    assert data[0]["status"] == "succeeded"

    # Mode update + pause/cancel endpoints respond.
    patched = test_client.patch(
        f"/api/narratives/{project.id}/director/sessions/{session_id}",
        json={"mode": "discuss"},
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["mode"] == "discuss"
    cancelled = test_client.post(
        f"/api/narratives/{project.id}/director/sessions/{session_id}/cancel"
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"


def test_director_session_unknown_id_404(client) -> None:
    test_client, _continuum = client
    res = test_client.get("/api/narratives/nproj_missing/director/sessions/ndir_missing")
    assert res.status_code == 404
