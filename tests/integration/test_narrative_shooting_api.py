"""Integration tests for the Narrative Shooting / Video Production API."""

from __future__ import annotations

import asyncio
import io
import json
import re
import time
import zipfile
from types import MethodType
from typing import Any

import pytest
from starlette.testclient import TestClient

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.narrative import (
    ProductionPackage,
    ShootingSessionStatus,
)
from persona_continuum.narrative.video_profile_registry import get_profile
from persona_continuum.web.server import create_web_app
from tests.fixtures.narrative_runtime import (
    RUNTIME,
    install_narrative_responder,
    seed_project,
)

_PIPELINE_STAGES = (
    "planning_clips",
    "planning_assets",
    "compiling_prompts",
    "validating",
)


@pytest.fixture()
def client(tmp_path):
    continuum = PersonaContinuum(
        Config(data_dir=tmp_path / "shooting_api"), include_fake_agent=True
    )
    continuum.init()
    with TestClient(create_web_app(continuum)) as test_client:
        yield test_client, continuum
    continuum.close()


@pytest.fixture()
def canon_env(client, monkeypatch):
    """Seeded canon project: outline → draft → audit → commit → master."""
    test_client, continuum = client
    install_narrative_responder(continuum, monkeypatch)
    project, _bible = seed_project(continuum, episodes=1)
    continuum.narratives.generate_outline_sync(project.id, episode_count=1)
    version = continuum.narratives.generate_episode_draft_sync(project.id, 1)
    continuum.narratives.audit_episode(project.id, version.id)
    continuum.narratives.commit_episode(project.id, 1, version.id)
    production = continuum.narratives.generate_production_package(project.id, 1)
    assert production.shot_list, "scripted Director turn must produce shots"
    return test_client, continuum, project, production


def _wait_for_job(test_client: TestClient, job_id: str) -> dict[str, Any]:
    job: dict[str, Any] = {}
    for _ in range(120):
        job = test_client.get(f"/api/narrative-jobs/{job_id}").json()["data"]
        if job["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert job["status"] == "completed", job
    return job


def _create_ready_package(
    test_client: TestClient, project: Any, production: Any, profile_id: str = "veo_3_1"
) -> dict[str, Any]:
    res = test_client.post(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages",
        json={"profile_id": profile_id},
    )
    assert res.status_code == 202, res.text
    _wait_for_job(test_client, res.json()["data"]["id"])
    listing = test_client.get(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages"
    )
    packages = listing.json()["data"]
    assert packages
    return packages[0]


def test_video_model_profiles_api(client) -> None:
    test_client, _continuum = client
    res = test_client.get("/api/narratives/video-model-profiles")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert len(data) == 6
    assert {p["id"] for p in data} == {
        "generic",
        "veo_3_1",
        "runway_gen_4_5",
        "hailuo",
        "seedance",
        "kling",
    }
    single = test_client.get("/api/narratives/video-model-profiles/veo_3_1")
    assert single.status_code == 200, single.text
    veo = single.json()["data"]
    assert veo["display_name"]
    assert veo["verification_status"] == "verified"
    assert veo["official_sources"]
    assert veo["supported_durations_seconds"] == [4.0, 6.0, 8.0]
    missing = test_client.get("/api/narratives/video-model-profiles/not_a_model")
    assert missing.status_code == 404, missing.text


def test_prompt_package_requires_canon_master(client) -> None:
    test_client, continuum = client
    project, _bible = seed_project(continuum, episodes=1)
    # A production package that does not reference the canon episode version:
    # the shooting pipeline must refuse it (canon gate, fail fast on POST).
    orphan = ProductionPackage(project_id=project.id, episode_number=1)
    continuum.narratives.repo.save_production_package(orphan)
    res = test_client.post(
        f"/api/narratives/{project.id}/production/{orphan.id}/prompt-packages",
        json={"profile_id": "veo_3_1"},
    )
    assert res.status_code == 409, res.text
    assert "NARRATIVE_PRODUCTION_CANON_REQUIRED" in res.text


def test_prompt_package_rejects_preview_source(canon_env) -> None:
    test_client, continuum, project, production = canon_env
    preview = ProductionPackage(
        project_id=project.id,
        episode_number=1,
        episode_version_id=production.episode_version_id,
        is_preview=True,
    )
    continuum.narratives.repo.save_production_package(preview)
    res = test_client.post(
        f"/api/narratives/{project.id}/production/{preview.id}/prompt-packages",
        json={"profile_id": "veo_3_1"},
    )
    assert res.status_code == 409, res.text
    assert "SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED" in res.text


def test_prompt_package_job_completes_with_honest_progress(
    canon_env, monkeypatch
) -> None:
    test_client, continuum, project, production = canon_env
    stages_seen: list[tuple[str | None, int | None]] = []
    original_save = continuum.narratives._save_job_row

    def spy_save_job_row(row: dict[str, Any]) -> None:
        progress = row.get("progress") or {}
        stages_seen.append((progress.get("stage"), progress.get("percent")))
        original_save(row)

    monkeypatch.setattr(continuum.narratives, "_save_job_row", spy_save_job_row)

    res = test_client.post(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages",
        json={"profile_id": "veo_3_1"},
    )
    assert res.status_code == 202, res.text
    job_id = res.json()["data"]["id"]
    job: dict[str, Any] = {}
    for _ in range(120):
        job = test_client.get(f"/api/narrative-jobs/{job_id}").json()["data"]
        if job["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert job["status"] == "completed", job

    # All four pipeline stages were reported, without a fabricated percent:
    # the pipeline itself never invents one (0 until completion).
    stage_names = [stage for stage, _percent in stages_seen]
    for expected in _PIPELINE_STAGES:
        assert expected in stage_names, stages_seen
    pipeline_percents = [
        percent for stage, percent in stages_seen if stage in _PIPELINE_STAGES
    ]
    assert all(percent in (None, 0) for percent in pipeline_percents), stages_seen
    completed_rows = [
        (stage, percent) for stage, percent in stages_seen if stage == "completed"
    ]
    assert completed_rows and completed_rows[-1][1] == 100

    listing = test_client.get(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages"
    )
    assert listing.status_code == 200, listing.text
    pkg = listing.json()["data"][0]
    assert pkg["status"] == "ready"
    assert pkg["clips"]
    # Every master shot maps into a generation clip (single-shot fixture:
    # count equality is legitimate, coverage is the invariant).
    covered = {n for c in pkg["clips"] for n in c["source_shot_numbers"]}
    assert covered == {s.shot_number for s in production.shot_list}
    # Profile version pinning: stored version == registry current, and the
    # derived update-available flag is false.
    assert pkg["target_profile_id"] == "veo_3_1"
    assert pkg["target_profile_version"] == str(get_profile("veo_3_1").profile_version)
    assert pkg["profile_update_available"] is False

    single = test_client.get(
        f"/api/narratives/{project.id}/prompt-packages/{pkg['id']}"
    )
    assert single.status_code == 200, single.text
    assert single.json()["data"]["id"] == pkg["id"]


def test_patch_clip_validations(canon_env) -> None:
    test_client, _continuum, project, production = canon_env
    pkg = _create_ready_package(test_client, project, production)
    clip = pkg["clips"][0]
    base = (
        f"/api/narratives/{project.id}/prompt-packages/{pkg['id']}"
        f"/clips/{clip['id']}"
    )
    ok = test_client.patch(base, json={"generation_mode": "image_to_video"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["generation_mode"] == "image_to_video"
    unsupported = test_client.patch(base, json={"duration_seconds": 15})
    assert unsupported.status_code == 422, unsupported.text
    assert "VIDEO_PROFILE_DURATION_UNSUPPORTED" in unsupported.text
    unknown_ref = test_client.patch(
        base, json={"reference_asset_ids": ["past_missing"]}
    )
    assert unknown_ref.status_code in (400, 404), unknown_ref.text


def test_stale_propagation_on_master_regeneration(canon_env) -> None:
    test_client, continuum, project, production = canon_env
    pkg = _create_ready_package(test_client, project, production)
    assert pkg["stale"] is False
    # Regenerating the production master creates a NEW master and flags the
    # packages derived from the previous one as stale.
    regenerated = continuum.narratives.generate_production_package(project.id, 1)
    assert regenerated.id != production.id
    listing = test_client.get(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages"
    )
    stale_pkg = listing.json()["data"][0]
    assert stale_pkg["stale"] is True


def test_profile_update_available_derived_at_read_time(canon_env) -> None:
    test_client, continuum, project, production = canon_env
    pkg = _create_ready_package(test_client, project, production)
    stored = continuum.narratives.repo.get_model_prompt_package(pkg["id"])
    assert stored is not None
    drifted = stored.model_copy(deep=True, update={"target_profile_version": "0"})
    continuum.narratives.repo.save_model_prompt_package(drifted)
    res = test_client.get(
        f"/api/narratives/{project.id}/prompt-packages/{pkg['id']}"
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["profile_update_available"] is True


def test_production_assets_round_trip_and_unprepared_semantics(canon_env) -> None:
    test_client, _continuum, project, production = canon_env
    res = test_client.post(
        f"/api/narratives/{project.id}/production-assets",
        json={
            "asset_type": "character_reference",
            "name": "方宁参考图",
            "episode_number": 1,
            "production_package_id": production.id,
        },
    )
    assert res.status_code == 201, res.text
    asset = res.json()["data"]
    # No source_uri / local_path: registered but not prepared yet.
    assert asset["source_uri"] is None
    assert asset["local_path"] is None
    listing = test_client.get(
        f"/api/narratives/{project.id}/production-assets"
        f"?production_package_id={production.id}"
    )
    assert listing.status_code == 200, listing.text
    ids = [a["id"] for a in listing.json()["data"]]
    assert asset["id"] in ids


def test_shooting_sessions_api_lifecycle(client, monkeypatch) -> None:
    test_client, continuum = client
    install_narrative_responder(continuum, monkeypatch)
    project, _bible = seed_project(continuum, episodes=1)

    # Single-turn finish script: the session settles back to ACTIVE.
    adapter = continuum.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    base_responder = adapter._generate_mock_response

    def finishing_responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Narrative Shooting Agent" in prompt:
            return json.dumps(
                {"decision": "finish", "finish_summary": "当前没有待办事项"}
            )
        return base_responder(session, turn)

    adapter._generate_mock_response = MethodType(finishing_responder, adapter)

    created = test_client.post(
        f"/api/narratives/{project.id}/shooting/sessions",
        json={"episode_number": 1, "mode": "agent"},
    )
    assert created.status_code == 201, created.text
    session = created.json()["data"]

    sent = test_client.post(
        f"/api/narratives/{project.id}/shooting/sessions/{session['id']}/messages",
        json={"content": "看看当前可用的视频模型"},
    )
    assert sent.status_code == 202, sent.text

    # The API turn runs on the app loop: poll until the session settles.
    snapshot: dict[str, Any] = {}
    for _ in range(120):
        snapshot = test_client.get(
            f"/api/narratives/{project.id}/shooting/sessions/{session['id']}"
        ).json()["data"]
        if snapshot["session"]["status"] != "running":
            break
        time.sleep(0.05)
    assert snapshot["session"]["status"] == "active", snapshot["session"]
    assert any(m["role"] == "user" for m in snapshot["messages"])
    assert any(m["role"] == "assistant" for m in snapshot["messages"])

    # Pause only interrupts a RUNNING loop; for a settled session it is an
    # idempotent no-op.
    paused = test_client.post(
        f"/api/narratives/{project.id}/shooting/sessions/{session['id']}/pause"
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["data"]["status"] in ("active", "paused")
    # A paused session resumes through the API back to active.
    target = continuum.narrative_shooting.get_session(session["id"])
    target.status = ShootingSessionStatus.PAUSED
    continuum.narratives.repo.save_shooting_session(target)
    resumed = test_client.post(
        f"/api/narratives/{project.id}/shooting/sessions/{session['id']}/resume"
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["status"] == "active"
    cancelled = test_client.post(
        f"/api/narratives/{project.id}/shooting/sessions/{session['id']}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"

    missing = test_client.get(
        f"/api/narratives/{project.id}/shooting/sessions/does_not_exist"
    )
    assert missing.status_code == 404, missing.text


def test_profile_version_bump_creates_superseding_revision(canon_env) -> None:
    """F1: a pinned-profile version drift must NOT silently reuse the old
    package — a fresh superseding revision is created with the parent chain
    set, while the old row is preserved (stale) and the derived banner
    clears on the new revision."""
    test_client, continuum, project, production = canon_env
    pkg = _create_ready_package(test_client, project, production)
    # Simulate a registry version bump by drifting the stored pin.
    stored = continuum.narratives.repo.get_model_prompt_package(pkg["id"])
    assert stored is not None
    drifted = stored.model_copy(deep=True, update={"target_profile_version": "0"})
    continuum.narratives.repo.save_model_prompt_package(drifted)

    res = test_client.post(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages",
        json={"profile_id": "veo_3_1"},
    )
    assert res.status_code == 202, res.text
    _wait_for_job(test_client, res.json()["data"]["id"])

    listing = test_client.get(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages"
    ).json()["data"]
    assert len(listing) == 2
    by_id = {item["id"]: item for item in listing}
    old = by_id[pkg["id"]]
    fresh = next(item for item in listing if item["id"] != pkg["id"])
    # Fresh revision: current registry version, parent chain, honest reason.
    assert str(fresh["target_profile_version"]) == str(
        get_profile("veo_3_1").profile_version
    )
    assert fresh["parent_package_id"] == pkg["id"]
    assert "profile 0→1 update" in fresh["revision_reason"]
    assert fresh["profile_update_available"] is False
    # Old row preserved (never deleted) and marked stale.
    assert old["stale"] is True


def test_aspect_ratio_change_creates_superseding_revision(canon_env) -> None:
    """F1: an options change (16:9 → 9:16) supersedes the old package with a
    fresh revision instead of returning the stale-shaped existing one."""
    test_client, _continuum, project, production = canon_env
    pkg = _create_ready_package(test_client, project, production)
    assert pkg["aspect_ratio"] == "16:9"

    res = test_client.post(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages",
        json={"profile_id": "veo_3_1", "aspect_ratio": "9:16"},
    )
    assert res.status_code == 202, res.text
    _wait_for_job(test_client, res.json()["data"]["id"])

    listing = test_client.get(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages"
    ).json()["data"]
    assert len(listing) == 2
    fresh = next(item for item in listing if item["id"] != pkg["id"])
    assert fresh["aspect_ratio"] == "9:16"
    assert fresh["parent_package_id"] == pkg["id"]
    assert "options changed: aspect_ratio 16:9→9:16" in fresh["revision_reason"]
    old = next(item for item in listing if item["id"] == pkg["id"])
    assert old["stale"] is True


def test_clip_plan_then_compile_prompts_two_step_flow(canon_env) -> None:
    """F3: step 1 (clip-plan) stops at the clip_planned checkpoint with the
    deterministic baseline prompts; step 2 (compile-prompts) resumes the
    SAME package from the checkpoint to ready; a repeated step 2 on a ready
    package is an idempotent reuse (no new row)."""
    test_client, _continuum, project, production = canon_env
    base = f"/api/narratives/{project.id}"

    # Step 1: plan-only run.
    res = test_client.post(
        f"{base}/production/{production.id}/clip-plan",
        json={"profile_id": "veo_3_1"},
    )
    assert res.status_code == 202, res.text
    _wait_for_job(test_client, res.json()["data"]["id"])
    listing = test_client.get(
        f"{base}/production/{production.id}/prompt-packages"
    ).json()["data"]
    assert len(listing) == 1
    planned = listing[0]
    assert planned["status"] == "clip_planned"
    assert planned["clips"]
    # Deterministic baseline only: the LLM refinement has not run yet.
    assert planned["clips"][0]["prompt"]
    assert "Refined cinematic prompt" not in planned["clips"][0]["prompt"]

    # Step 2: compile prompts from the checkpoint.
    res2 = test_client.post(f"{base}/prompt-packages/{planned['id']}/compile-prompts")
    assert res2.status_code == 202, res2.text
    _wait_for_job(test_client, res2.json()["data"]["id"])
    single = test_client.get(f"{base}/prompt-packages/{planned['id']}").json()["data"]
    assert single["status"] == "ready"
    assert "Refined cinematic prompt" in single["clips"][0]["prompt"]

    # Idempotent: compiling again on a ready package reuses it.
    res3 = test_client.post(f"{base}/prompt-packages/{planned['id']}/compile-prompts")
    assert res3.status_code == 202, res3.text
    _wait_for_job(test_client, res3.json()["data"]["id"])
    listing2 = test_client.get(
        f"{base}/production/{production.id}/prompt-packages"
    ).json()["data"]
    assert len(listing2) == 1
    assert listing2[0]["status"] == "ready"


def test_get_clip_plan_route_returns_latest_package(canon_env) -> None:
    """GET clip-plan resolves the latest non-stale clip_planned package with
    the same payload shape as GET prompt-packages/{id}; a production package
    without a plan returns 404."""
    test_client, _continuum, project, production = canon_env
    base = f"/api/narratives/{project.id}"

    # Step 1: plan-only run, then read it back via the GET route.
    res = test_client.post(
        f"{base}/production/{production.id}/clip-plan",
        json={"profile_id": "veo_3_1"},
    )
    assert res.status_code == 202, res.text
    _wait_for_job(test_client, res.json()["data"]["id"])

    got = test_client.get(f"{base}/production/{production.id}/clip-plan")
    assert got.status_code == 200, got.text
    planned = got.json()["data"]
    assert planned["status"] == "clip_planned"
    assert planned["clips"]
    assert planned["production_package_id"] == production.id
    assert planned["stale"] is False

    # A production package with no clip plan resolves to 404.
    missing = test_client.get(f"{base}/production/no-such-prod-pkg/clip-plan")
    assert missing.status_code == 404, missing.text
    assert "Clip plan not found" in missing.text


def test_supersede_propagation_reaches_interposed_master(canon_env) -> None:
    """F4: a preview package interposed between the previous master and the
    regeneration must not shield the previous master's prompt packages from
    the stale cascade."""
    test_client, continuum, project, production = canon_env
    pkg = _create_ready_package(test_client, project, production)
    assert pkg["stale"] is False

    preview = ProductionPackage(
        project_id=project.id,
        episode_number=1,
        episode_version_id=production.episode_version_id,
        is_preview=True,
    )
    continuum.narratives.repo.save_production_package(preview)
    regenerated = continuum.narratives.generate_production_package(project.id, 1)
    assert regenerated.id not in {production.id, preview.id}

    listing = test_client.get(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages"
    ).json()["data"]
    assert listing and listing[0]["stale"] is True


def test_patch_clip_rejects_empty_prompt_and_gates_negative_prompt(
    canon_env,
) -> None:
    """F6: the host patch path mirrors the refinement merge — an empty prompt
    is rejected, and negative_prompt is gated by the profile's documented
    negative-prompt support."""
    test_client, _continuum, project, production = canon_env
    runway_pkg = _create_ready_package(
        test_client, project, production, profile_id="runway_gen_4_5"
    )
    runway_base = (
        f"/api/narratives/{project.id}/prompt-packages/{runway_pkg['id']}"
        f"/clips/{runway_pkg['clips'][0]['id']}"
    )
    # Empty / whitespace-only prompt is never a valid clip prompt.
    empty = test_client.patch(runway_base, json={"prompt": "   "})
    assert empty.status_code in (400, 422), empty.text
    # Runway documents supports_negative_prompt=false: the value is forced
    # to None instead of reaching the model.
    gated = test_client.patch(runway_base, json={"negative_prompt": "模糊，低质量"})
    assert gated.status_code == 200, gated.text
    assert gated.json()["data"]["negative_prompt"] is None

    # Veo documents supports_negative_prompt=true: the value is preserved.
    veo_pkg = _create_ready_package(test_client, project, production)
    veo_base = (
        f"/api/narratives/{project.id}/prompt-packages/{veo_pkg['id']}"
        f"/clips/{veo_pkg['clips'][0]['id']}"
    )
    kept = test_client.patch(veo_base, json={"negative_prompt": "模糊，低质量"})
    assert kept.status_code == 200, kept.text
    assert kept.json()["data"]["negative_prompt"] == "模糊，低质量"


def test_refinement_merges_recommended_settings_over_baseline(
    canon_env, monkeypatch
) -> None:
    """F6: the refinement merge is {**deterministic_baseline, **recommended}
    — a model-supplied override must never wipe the planner's baseline."""
    test_client, continuum, project, production = canon_env
    adapter = continuum.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    base_responder = adapter._generate_mock_response

    def settings_responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Video Prompt Compiler for one target video model" in prompt:
            clip_ids = re.findall(r'"clip_id":\s*"([^"]+)"', prompt)
            # Echo only real clip ids (never the output_contract example).
            clip_ids = list(
                dict.fromkeys(
                    cid for cid in clip_ids if re.fullmatch(r"[\w\-]+", cid)
                )
            )
            return json.dumps(
                {
                    "clips": [
                        {
                            "clip_id": clip_id,
                            "prompt": f"Refined cinematic prompt for clip {index + 1}",
                            "recommended_settings": {"seed": 42},
                        }
                        for index, clip_id in enumerate(clip_ids)
                    ]
                }
            )
        return base_responder(session, turn)

    adapter._generate_mock_response = MethodType(settings_responder, adapter)

    pkg = _create_ready_package(test_client, project, production)
    clip = pkg["clips"][0]
    settings = clip["recommended_settings"]
    # Baseline planner recommendations survive the merge... (the single
    # duration source of truth is actual_generation_duration, task #29)
    assert settings["actual_generation_duration"] == clip["duration_seconds"]
    assert settings["aspect_ratio"] == clip["aspect_ratio"]
    # ... and the model-supplied override is merged in, not replacing it.
    assert settings["seed"] == 42


def test_shooting_session_busy_guard_returns_409(client, monkeypatch) -> None:
    """Mark's gap: a second concurrent POST while a shooting turn is running
    must fail fast with 409 SHOOTING_SESSION_BUSY."""
    test_client, continuum = client
    install_narrative_responder(continuum, monkeypatch)
    project, _bible = seed_project(continuum, episodes=1)
    service = continuum.narrative_shooting

    # A single terminating scripted turn: once the gate is released, the
    # blocked turn returns "finish" and the session settles back cleanly.
    adapter = continuum.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    base_responder = adapter._generate_mock_response

    def finishing_responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Narrative Shooting Agent" in prompt:
            return json.dumps(
                {"decision": "finish", "finish_summary": "释放后收尾"}
            )
        return base_responder(session, turn)

    adapter._generate_mock_response = MethodType(finishing_responder, adapter)

    # Block the first turn inside the app loop: the loop task stays RUNNING
    # until released, giving the second POST a real concurrency window.
    cell: dict[str, Any] = {}
    original_turn = service._shooting_turn

    async def blocked_turn(project_arg: Any, session_arg: Any, context_arg: Any) -> Any:
        loop = asyncio.get_running_loop()
        cell["loop"] = loop
        gate = cell.get("gate")
        if gate is None:
            gate = asyncio.Event()
            cell["gate"] = gate
        await gate.wait()
        return await original_turn(project_arg, session_arg, context_arg)

    monkeypatch.setattr(service, "_shooting_turn", blocked_turn)

    created = test_client.post(
        f"/api/narratives/{project.id}/shooting/sessions",
        json={"episode_number": 1, "mode": "agent"},
    )
    assert created.status_code == 201, created.text
    session = created.json()["data"]
    session_url = f"/api/narratives/{project.id}/shooting/sessions/{session['id']}"
    messages_url = f"{session_url}/messages"

    first = test_client.post(messages_url, json={"content": "第一条消息"})
    assert first.status_code == 202, first.text
    snapshot: dict[str, Any] = {}
    for _ in range(120):
        snapshot = test_client.get(session_url).json()["data"]
        if snapshot["session"]["status"] == "running" and "loop" in cell:
            break
        time.sleep(0.05)
    assert snapshot["session"]["status"] == "running"

    second = test_client.post(messages_url, json={"content": "并发第二条消息"})
    assert second.status_code == 409, second.text
    assert "SHOOTING_SESSION_BUSY" in second.text

    # Release the gate; the loop drains and the session settles back.
    cell["loop"].call_soon_threadsafe(cell["gate"].set)
    for _ in range(240):
        snapshot = test_client.get(session_url).json()["data"]
        if snapshot["session"]["status"] != "running":
            break
        time.sleep(0.05)
    assert snapshot["session"]["status"] == "active", snapshot["session"]


def test_export_prompt_package_zip(canon_env) -> None:
    test_client, _continuum, project, production = canon_env
    pkg = _create_ready_package(test_client, project, production)
    res = test_client.get(
        f"/api/narratives/{project.id}/production/{production.id}"
        f"/prompt-packages/export"
    )
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/zip"
    disposition = res.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="clips-ep01-')
    assert disposition.endswith('.zip"')
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = zf.namelist()
        assert "README.md" in names
        assert "manifest.json" in names
        clip_files = sorted(n for n in names if n.endswith(".txt"))
        assert len(clip_files) == len(pkg["clips"])
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["production_package_id"] == production.id
        assert manifest["packages"][0]["clip_count"] == len(pkg["clips"])
        # Each prompt sheet carries the compiled prompt text.
        sheet = zf.read(clip_files[0]).decode("utf-8")
        assert pkg["clips"][0]["prompt"] in sheet


def test_export_prompt_package_zip_empty_is_404(canon_env) -> None:
    test_client, _continuum, project, production = canon_env
    res = test_client.get(
        f"/api/narratives/{project.id}/production/{production.id}"
        f"/prompt-packages/export"
    )
    assert res.status_code == 404, res.text


# ----------------------------------------------------------------------
# Executable video production guide (API + shooting action)
# ----------------------------------------------------------------------
def _regen_master_with_bible(continuum: Any, project: Any, production: Any) -> Any:
    """Rebuild the canon master with a location/character visual bible.

    The scripted seed bible carries no locations, and the guide compiler
    resolves clip locations fail-closed (SHOOTING_LOCATION_CONTEXT_MISSING).
    ``save_bible`` creates a new version (which stales the old master via the
    supersede cascade), then a fresh master is regenerated from that bible.
    Returns the rebuilt production package.
    """
    del production  # the old master stays behind, superseded and stale
    continuum.narratives.save_bible(
        project.id,
        {
            "characters": [
                {
                    "id": "fang",
                    "name": "Fang",
                    "role": "lead",
                    "visual": {
                        "description": "Short black hair, grey hoodie, carries a phone",
                        "color_palette": ["灰", "黑"],
                    },
                }
            ],
            "locations": [
                {
                    "id": "office",
                    "name": "Office",
                    "visual": {
                        "description": "Open-plan office with a flickering ceiling light",
                        "color_palette": ["冷白"],
                    },
                }
            ],
        },
    )
    rebuilt = continuum.narratives.generate_production_package(project.id, 1)
    assert rebuilt.shot_list, "scripted Director turn must produce shots"
    return rebuilt


def _build_guide_via_api(
    test_client: TestClient, project: Any, production: Any
) -> dict[str, Any]:
    created = test_client.post(
        f"/api/narratives/{project.id}/production/{production.id}/production-guide",
        json={},
    )
    assert created.status_code == 202, created.text
    job = _wait_for_job(test_client, created.json()["data"]["id"])
    assert job["kind"] == "video_production_guide"
    return (
        test_client.get(
            f"/api/narratives/{project.id}/production/{production.id}/production-guide"
        )
        .json()["data"]
    )


def test_production_guide_full_flow_export_and_payload(canon_env) -> None:
    test_client, continuum, project, production = canon_env
    production = _regen_master_with_bible(continuum, project, production)
    pkg = _create_ready_package(test_client, project, production)

    payload = _build_guide_via_api(test_client, project, production)

    assert payload["status"] == "ready"
    assert payload["stale"] is False
    assert payload["prompt_package_id"] == pkg["id"]
    assert payload["production_package_id"] == production.id
    markdown = payload["markdown_document"]
    assert markdown.strip()
    assert "一、制作目标" in markdown
    assert "检查清单" in markdown

    guide_id = payload["id"]
    exported = test_client.get(
        f"/api/narratives/{project.id}/production-guides/{guide_id}/export"
    )
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"] == "text/markdown; charset=utf-8"
    disposition = exported.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="guide-ep01-')
    assert disposition.endswith('.md"')
    # The export serves the persisted markdown verbatim (no re-render).
    assert exported.text == markdown

    single = test_client.get(
        f"/api/narratives/{project.id}/production-guides/{guide_id}"
    )
    assert single.status_code == 200, single.text
    assert single.json()["data"]["id"] == guide_id


def test_production_guide_requires_ready_prompt_package(canon_env) -> None:
    test_client, _continuum, project, production = canon_env
    res = test_client.post(
        f"/api/narratives/{project.id}/production/{production.id}/production-guide",
        json={},
    )
    assert res.status_code == 409, res.text
    assert "VIDEO_GUIDE_SOURCE_NOT_READY" in res.text


def test_shooting_action_builds_complete_production_guide(canon_env) -> None:
    test_client, continuum, project, production = canon_env
    production = _regen_master_with_bible(continuum, project, production)
    pkg = _create_ready_package(test_client, project, production)
    service = continuum.narrative_shooting

    adapter = continuum.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    base_responder = adapter._generate_mock_response
    decisions = [
        json.dumps(
            {
                "decision": "execute_action",
                "action": "build_complete_production_guide",
                "arguments": {"prompt_package_id": pkg["id"]},
            }
        ),
        json.dumps({"decision": "finish", "finish_summary": "guide built"}),
    ]

    def queued_responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Narrative Shooting Agent" in prompt:
            assert decisions, "more shooting turns than scripted decisions"
            return decisions.pop(0)
        return base_responder(session, turn)

    adapter._generate_mock_response = MethodType(queued_responder, adapter)

    session = service.create_session(
        project.id, episode_number=1, mode="agent", runtime=dict(RUNTIME)
    )
    snapshot = service.send_message(session.id, "生成完整制作手册")

    actions = snapshot["actions"]
    assert [a["action"] for a in actions] == ["build_complete_production_guide"]
    succeeded = actions[0]
    assert succeeded["status"] == "succeeded", succeeded
    artifacts = succeeded["result"]["artifacts"]
    assert artifacts["production_guide_id"]
    assert artifacts["status"] == "ready"
    assert artifacts["asset_count"] >= 1
    assert artifacts["clip_count"] >= 1

    guide = continuum.narratives.repo.get_video_production_guide(
        artifacts["production_guide_id"]
    )
    assert guide is not None
    assert guide.markdown_document.strip()


def test_production_guide_stale_after_prompt_package_supersede(canon_env) -> None:
    test_client, continuum, project, production = canon_env
    production = _regen_master_with_bible(continuum, project, production)
    _create_ready_package(test_client, project, production)
    payload = _build_guide_via_api(test_client, project, production)
    assert payload["stale"] is False

    # Drift the generation options: the superseding package invalidates the
    # old package AND every guide built on it (stale cascade).
    res = test_client.post(
        f"/api/narratives/{project.id}/production/{production.id}/prompt-packages",
        json={"profile_id": "veo_3_1", "aspect_ratio": "9:16"},
    )
    assert res.status_code == 202, res.text
    _wait_for_job(test_client, res.json()["data"]["id"])

    payload_after = (
        test_client.get(
            f"/api/narratives/{project.id}/production/{production.id}/production-guide"
        )
        .json()["data"]
    )
    assert payload_after["stale"] is True


def test_shooting_action_surfaces_location_context_missing(canon_env) -> None:
    """The seed bible has no locations: the guide compiler fails closed and
    the shooting action must surface SHOOTING_LOCATION_CONTEXT_MISSING."""
    test_client, continuum, project, production = canon_env
    pkg = _create_ready_package(test_client, project, production)
    service = continuum.narrative_shooting

    adapter = continuum.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    base_responder = adapter._generate_mock_response
    decisions = [
        json.dumps(
            {
                "decision": "execute_action",
                "action": "build_complete_production_guide",
                "arguments": {"prompt_package_id": pkg["id"]},
            }
        ),
        json.dumps({"decision": "finish", "finish_summary": "done despite failure"}),
    ]

    def queued_responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Narrative Shooting Agent" in prompt:
            assert decisions, "more shooting turns than scripted decisions"
            return decisions.pop(0)
        return base_responder(session, turn)

    adapter._generate_mock_response = MethodType(queued_responder, adapter)

    session = service.create_session(
        project.id, episode_number=1, mode="agent", runtime=dict(RUNTIME)
    )
    snapshot = service.send_message(session.id, "生成完整制作手册")

    actions = snapshot["actions"]
    assert [a["action"] for a in actions] == ["build_complete_production_guide"]
    failed = actions[0]
    assert failed["status"] == "failed", failed
    assert failed["error_code"] == "SHOOTING_LOCATION_CONTEXT_MISSING"
    assert failed["result"]["code"] == "SHOOTING_LOCATION_CONTEXT_MISSING"
    # The session survives the failed action and settles cleanly.
    assert snapshot["session"]["status"] != "running"


# ----------------------------------------------------------------------
# One-click complete AI video production plan (task #5/#6/#7)
# ----------------------------------------------------------------------
def test_complete_video_production_one_click_flow(canon_env) -> None:
    """ONE POST (complete-plan) runs clip plan + prompt package + production
    guide as a single job whose stages are reported in creator language."""
    test_client, continuum, project, production = canon_env
    production = _regen_master_with_bible(continuum, project, production)

    created = test_client.post(
        f"/api/narratives/{project.id}/production/{production.id}/complete-plan",
        json={"target_profile_id": "veo_3_1", "aspect_ratio": "9:16"},
    )
    assert created.status_code == 202, created.text
    job = _wait_for_job(test_client, created.json()["data"]["id"])
    assert job["kind"] == "complete_video_production"
    # The final result carries the guide — the only deliverable the user needs.
    guide_id = job["result"]["production_guide_id"]
    assert guide_id
    assert job["result"]["model_prompt_package_id"]

    guide = continuum.narratives.repo.get_video_production_guide(guide_id)
    assert guide is not None and guide.status == "ready"
    markdown = guide.markdown_document
    assert markdown.startswith("# EP01《")
    assert "# Google Veo 3.1 AI视频完整制作方案" in markdown
    for section in (
        "## 一、制作目标与基础设置",
        "## 二、先建立永久角色参考素材",
        "## 三、本集需要建立的场景参考素材",
        "## 五、整集视频结构",
        "## 使用方式",
        "### Start Frame",
        "### Ingredients / References",
        "、尾帧接首帧完整流程",
        "、字幕时间轴",
        "、BGM 完整生成 Prompt",
        "、最终检查清单",
    ):
        assert section in markdown, section
    # Job progress never leaks developer jargon (task #7).
    progress_dump = json.dumps(job.get("progress") or {}, ensure_ascii=False)
    assert "model_prompt_package" not in progress_dump
    assert "clip_fingerprint" not in progress_dump


def test_complete_video_production_validates_inputs(canon_env) -> None:
    test_client, _continuum, project, production = canon_env
    missing_profile = test_client.post(
        f"/api/narratives/{project.id}/production/{production.id}/complete-plan",
        json={"target_profile_id": "not_a_model"},
    )
    assert missing_profile.status_code == 422, missing_profile.text

    no_profile = test_client.post(
        f"/api/narratives/{project.id}/production/{production.id}/complete-plan",
        json={},
    )
    assert no_profile.status_code == 400, no_profile.text

    unknown_package = test_client.post(
        f"/api/narratives/{project.id}/production/pkg_does_not_exist/complete-plan",
        json={"target_profile_id": "veo_3_1"},
    )
    assert unknown_package.status_code == 404, unknown_package.text
