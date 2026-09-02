"""Integration tests for the Narrative Studio REST API surface."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.web.server import create_web_app


@pytest.fixture()
def client(tmp_path):
    continuum = PersonaContinuum(
        Config(data_dir=tmp_path / "narr_api"), include_fake_agent=True
    )
    continuum.init()
    with TestClient(create_web_app(continuum)) as test_client:
        yield test_client
    continuum.close()


def test_project_full_settings_create_and_edit(client) -> None:
    res = client.post(
        "/api/narratives",
        json={
            "title": "裁员通知来自十年后",
            "logline": "方宁收到一封来自2036年的裁员通知邮件",
            "description": "广告公司职员在普通工作日收到来自十年后的邮件。",
            "format": "micro_drama",
            "genre": ["悬疑", "都市"],
            "tone": ["克制", "紧张"],
            "target_audience": "25-40 职场观众",
            "planned_episode_count": 60,
            "episode_duration_seconds_min": 90,
            "episode_duration_seconds_max": 120,
        },
    )
    assert res.status_code == 201, res.text
    project = res.json()["data"]
    assert project["format"] == "micro_drama"
    assert project["planned_episode_count"] == 60
    assert project["episode_duration_seconds_min"] == 90
    assert project["episode_duration_seconds_max"] == 120
    assert project["genre"] == ["悬疑", "都市"]
    assert project["tone"] == ["克制", "紧张"]
    assert project["target_audience"] == "25-40 职场观众"

    patched = client.patch(
        f"/api/narratives/{project['id']}",
        json={
            "title": "裁员通知来自十年后",
            "logline": "更新后的 logline",
            "description": "更新后的简介",
            "format": "micro_drama",
            "genre": ["悬疑"],
            "tone": ["冷静"],
            "target_audience": "都市短剧观众",
            "planned_episode_count": 48,
            "episode_duration_seconds_min": 80,
            "episode_duration_seconds_max": 110,
        },
    )
    assert patched.status_code == 200, patched.text
    data = patched.json()["data"]
    assert data["logline"] == "更新后的 logline"
    assert data["planned_episode_count"] == 48
    assert data["episode_duration_seconds_min"] == 80
    assert data["genre"] == ["悬疑"]


def test_runtime_assignment_persists_runtime_source(client) -> None:
    project = client.post(
        "/api/narratives", json={"title": "Runtime persist", "format": "micro_drama"}
    ).json()["data"]
    patched = client.patch(
        f"/api/narratives/{project['id']}",
        json={
            "runtime_assignment": {
                "default": {
                    "runtime_source": "api",
                    "agent_id": "api_x",
                    "model_id": "model_x",
                    "reasoning_effort": "high",
                }
            }
        },
    )
    assert patched.status_code == 200, patched.text
    saved = patched.json()["data"]["runtime_assignment"]["default"]
    assert saved["runtime_source"] == "api"
    assert saved["agent_id"] == "api_x"
    reloaded = client.get(f"/api/narratives/{project['id']}").json()["data"]
    assert reloaded["runtime_assignment"]["default"]["runtime_source"] == "api"


def test_project_crud(client) -> None:
    res = client.post(
        "/api/narratives",
        json={
            "title": "裁员通知来自十年后",
            "logline": "一封来自未来的裁员邮件",
            "format": "micro_drama",
        },
    )
    assert res.status_code == 201, res.text
    project = res.json()["data"]
    assert project["title"] == "裁员通知来自十年后"

    listed = client.get("/api/narratives").json()["data"]
    assert len(listed) == 1

    patched = client.patch(
        f"/api/narratives/{project['id']}", json={"logline": "更新后的一句话简介"}
    ).json()["data"]
    assert patched["logline"] == "更新后的一句话简介"

    assert client.delete(f"/api/narratives/{project['id']}").json()["ok"] is True
    assert client.get(f"/api/narratives/{project['id']}").status_code == 404


def test_bible_characters_knowledge_flow(client) -> None:
    project = client.post(
        "/api/narratives", json={"title": "信息差测试", "format": "series"}
    ).json()["data"]
    pid = project["id"]

    bible = client.post(f"/api/narratives/{pid}/bible/generate", json={}).json()["data"]
    assert bible["version"] == 1

    updated = client.patch(
        f"/api/narratives/{pid}/bible", json={"theme": "亲情与救赎"}
    ).json()["data"]
    assert updated["version"] == 2
    versions = client.get(f"/api/narratives/{pid}/bible/versions").json()["data"]
    assert len(versions) == 2

    character = client.post(
        f"/api/narratives/{pid}/characters", json={"name": "方宁", "role": "主角"}
    ).json()["data"]
    assert character["persona_id"] is None
    created = client.post(
        f"/api/narratives/{pid}/characters/create-missing", json={}
    ).json()["data"]
    assert len(created) == 1
    assert created[0]["persona_id"]

    fact = client.post(
        f"/api/narratives/{pid}/facts", json={"text": "邮件来自数字方宁", "secret": True}
    ).json()["data"]

    client.post(
        f"/api/narratives/{pid}/knowledge",
        json={"character_id": character["id"], "fact_id": fact["id"], "state": "unknown"},
    )
    matrix = client.get(f"/api/narratives/{pid}/knowledge").json()["data"]
    assert matrix["character_knowledge"][fact["id"]][character["id"]] == "unknown"
    assert matrix["audience_knowledge"][fact["id"]] == "hidden"

    client.post(
        f"/api/narratives/{pid}/audience-knowledge",
        json={"fact_id": fact["id"], "state": "partial"},
    )
    audience = client.get(f"/api/narratives/{pid}/audience-knowledge").json()["data"]
    assert audience[fact["id"]] == "partial"


def test_episode_pipeline_via_api(client) -> None:
    project = client.post(
        "/api/narratives",
        json={"title": "流水线测试", "format": "micro_drama", "planned_episode_count": 5},
    ).json()["data"]
    pid = project["id"]

    client.post(f"/api/narratives/{pid}/bible/generate", json={})
    outline = client.post(
        f"/api/narratives/{pid}/outline/generate", json={"episode_count": 3}
    ).json()["data"]
    assert [e["episode_number"] for e in outline] == [1, 2, 3]

    prepared = client.post(f"/api/narratives/{pid}/episodes/1/prepare", json={})
    assert prepared.status_code == 200
    assert len(prepared.json()["data"]["context_fingerprint"]) == 64

    version = client.post(f"/api/narratives/{pid}/episodes/1/draft", json={}).json()["data"]
    assert version["version"] == 1
    assert "EP01" in version["screenplay"]

    audit = client.post(
        f"/api/narratives/{pid}/episodes/1/audit", json={"version_id": version["id"]}
    ).json()["data"]
    assert audit["passed"] is True
    episodes = client.get(f"/api/narratives/{pid}/episodes").json()["data"]
    assert episodes[0]["latest_audit"]["id"] == audit["id"]
    assert client.get(f"/api/narratives/{pid}/arcs").status_code == 200

    commit = client.post(
        f"/api/narratives/{pid}/episodes/1/commit",
        json={"version_id": version["id"]},
    )
    assert commit.status_code == 200
    assert commit.json()["data"]["committed"] is True

    canon = client.get(f"/api/narratives/{pid}/canon")
    assert canon.status_code == 200

    package = client.post(f"/api/narratives/{pid}/episodes/1/production", json={})
    assert package.status_code == 200
    assert len(package.json()["data"]["shot_list"]) >= 1

    jobs = client.get(f"/api/narratives/{pid}/jobs").json()["data"]
    assert isinstance(jobs, list)
