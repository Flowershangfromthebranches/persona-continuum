"""Service-level tests for the video production guide pipeline.

No HTTP: these tests drive ``NarrativeService`` directly (the same way the
shooting-agent unit tests do) against a synthetic canon project seeded via
the scripted narrative responder. They cover the guide gates, idempotent
reuse, clip-plan drift supersede, stale propagation hooks and both the
deterministic fallback and the AGENT-mode LLM-refinement paths.
"""

from __future__ import annotations

import asyncio
import json
from types import MethodType
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.narrative.runtime import (
    VIDEO_GUIDE_SOURCE_NOT_READY,
    NarrativeAgentError,
)
from tests.fixtures.narrative_runtime import (
    RUNTIME,
    install_narrative_responder,
    seed_project,
)

PROFILE_ID = "generic"


@pytest.fixture()
def app(tmp_path):
    continuum = PersonaContinuum(
        Config(data_dir=tmp_path / "video_guide_pipeline"), include_fake_agent=True
    )
    continuum.init()
    yield continuum
    continuum.close()


def _seed_canon_project(app: Any) -> tuple[Any, Any]:
    """Outline → draft → audit → commit → canon production package.

    The base seed's bible carries no characters/locations, but the guide
    compiler resolves clip locations against the location visual bible
    fail-closed (SHOOTING_LOCATION_CONTEXT_MISSING), so the bible is extended
    with the scripted cast/setting before any artifact is generated.
    """
    project, _bible = seed_project(app, episodes=1)
    app.narratives.save_bible(
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
    app.narratives.generate_outline_sync(project.id, episode_count=1)
    version = app.narratives.generate_episode_draft_sync(project.id, 1)
    app.narratives.audit_episode(project.id, version.id)
    app.narratives.commit_episode(project.id, 1, version.id)
    production = app.narratives.generate_production_package(project.id, 1)
    assert production.shot_list, "scripted Director turn must produce shots"
    return project, production


def _seed_ready_package(app: Any, project: Any, production: Any) -> Any:
    package = asyncio.run(
        app.narratives.generate_model_prompt_package_async(
            project.id, production.id, PROFILE_ID
        )
    )
    assert package.status == "ready"
    assert package.clips
    return package


def test_guide_pipeline_happy_path_deterministic(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, production = _seed_canon_project(app)
    package = _seed_ready_package(app, project, production)

    guide = asyncio.run(
        app.narratives.generate_video_production_guide_async(project.id, package.id)
    )

    assert guide.status == "ready"
    assert guide.stale is False
    assert guide.prompt_package_id == package.id
    assert guide.production_package_id == production.id
    assert guide.episode_version_id == production.episode_version_id
    assert guide.target_profile_id == PROFILE_ID
    assert guide.markdown_document.strip()
    assert "一、制作目标" in guide.markdown_document
    assert "十三、最终检查清单" in guide.markdown_document
    assert guide.required_assets
    assert all(asset.generation_prompt for asset in guide.required_assets)
    assert guide.clip_workflows
    assert all(clip.copy_ready_prompt for clip in guide.clip_workflows)

    # No runtime_assignment entry for guide_compilation -> deterministic mode
    # with zero LLM refinement; every asset stays on the deterministic baseline.
    pipeline = guide.runtime_trace["pipeline"]
    assert pipeline["stage"] == "rendering_guide"
    assert pipeline["generation_mode"] == "deterministic"
    assert pipeline["refined_asset_count"] == 0
    assert pipeline["refined_clip_count"] == 0
    assert pipeline["fallback_asset_count"] == 0
    assert pipeline["fallback_clip_count"] == 0
    assert pipeline["clip_plan_fingerprint"]
    assert all(asset.provenance == "deterministic" for asset in guide.required_assets)

    # The guide was persisted and is discoverable from the repo.
    stored = app.narratives.repo.get_video_production_guide(guide.id)
    assert stored is not None
    assert stored.markdown_document == guide.markdown_document
    assert guide.id in {
        g.id for g in app.narratives.repo.list_video_production_guides(project.id)
    }


def test_guide_pipeline_is_idempotent_per_prompt_package(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, production = _seed_canon_project(app)
    package = _seed_ready_package(app, project, production)

    first = asyncio.run(
        app.narratives.generate_video_production_guide_async(project.id, package.id)
    )
    second = asyncio.run(
        app.narratives.generate_video_production_guide_async(project.id, package.id)
    )
    assert second.id == first.id
    stored = app.narratives.repo.list_video_production_guides(
        project.id, prompt_package_id=package.id
    )
    assert len(stored) == 1
    assert stored[0].stale is False


def test_guide_pipeline_rejects_unready_source_package(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, production = _seed_canon_project(app)
    planned = asyncio.run(
        app.narratives.generate_model_prompt_package_async(
            project.id, production.id, PROFILE_ID, stop_after_plan=True
        )
    )
    assert planned.status != "ready"

    with pytest.raises(NarrativeAgentError) as excinfo:
        asyncio.run(
            app.narratives.generate_video_production_guide_async(project.id, planned.id)
        )
    assert excinfo.value.code == VIDEO_GUIDE_SOURCE_NOT_READY
    assert excinfo.value.stage == "video_production_guide"


def test_guide_pipeline_supersedes_on_clip_plan_drift(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, production = _seed_canon_project(app)
    package = _seed_ready_package(app, project, production)
    first = asyncio.run(
        app.narratives.generate_video_production_guide_async(project.id, package.id)
    )
    assert first.revision_reason == ""

    # Drift: rename one clip id of the ready source package so the stored
    # clip_plan_fingerprint no longer matches the package's live plan.
    drifted = package.model_copy(deep=True)
    drifted.clips = [
        clip.model_copy(update={"id": "clip_moved_1"})
        if clip.id == package.clips[0].id
        else clip
        for clip in drifted.clips
    ]
    app.narratives.repo.save_model_prompt_package(drifted)

    second = asyncio.run(
        app.narratives.generate_video_production_guide_async(project.id, package.id)
    )
    assert second.id != first.id
    assert second.parent_guide_id == first.id
    assert "clip plan changed" in second.revision_reason
    assert second.stale is False

    old = app.narratives.repo.get_video_production_guide(first.id)
    assert old is not None
    assert old.stale is True


def test_guide_stale_propagates_from_prompt_package_hook(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, production = _seed_canon_project(app)
    package = _seed_ready_package(app, project, production)
    guide = asyncio.run(
        app.narratives.generate_video_production_guide_async(project.id, package.id)
    )
    assert guide.stale is False

    marked = app.narratives.repo.mark_video_production_guides_stale_for_prompt_package(
        project.id, package.id
    )
    assert marked == 1
    stored = app.narratives.repo.get_video_production_guide(guide.id)
    assert stored is not None
    assert stored.stale is True


def test_guide_stale_propagates_from_production_hook(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, production = _seed_canon_project(app)
    package = _seed_ready_package(app, project, production)
    guide = asyncio.run(
        app.narratives.generate_video_production_guide_async(project.id, package.id)
    )

    marked = app.narratives.repo.mark_video_production_guides_stale_for_production(
        project.id, production.id
    )
    assert marked == 1
    stored = app.narratives.repo.get_video_production_guide(guide.id)
    assert stored is not None
    assert stored.stale is True


def test_guide_pipeline_agent_refinement_overlays_and_counts(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, production = _seed_canon_project(app)
    package = _seed_ready_package(app, project, production)
    baseline_prompts = {clip.id: clip.copy_ready_prompt for clip in package.clips}

    # Flip guide_compilation into AGENT mode via the project runtime.
    assignment = dict(project.runtime_assignment)
    assignment["guide_compilation"] = dict(RUNTIME)
    app.narratives.update_project(project.id, {"runtime_assignment": assignment})

    guide = asyncio.run(
        app.narratives.generate_video_production_guide_async(project.id, package.id)
    )

    pipeline = guide.runtime_trace["pipeline"]
    assert pipeline["generation_mode"] == "agent"
    assert pipeline["batch_count"] >= 1
    assert pipeline["asset_count"] >= 1
    assert pipeline["refined_asset_count"] == pipeline["asset_count"]
    assert pipeline["fallback_asset_count"] == 0
    assert pipeline["refined_clip_count"] == pipeline["clip_count"]
    assert pipeline["fallback_clip_count"] == 0

    refined_assets = [
        asset for asset in guide.required_assets if asset.provenance == "llm_refined"
    ]
    assert len(refined_assets) == pipeline["refined_asset_count"]
    assert refined_assets
    assert all(
        asset.generation_prompt.startswith("Refined reference prompt")
        for asset in refined_assets
    )
    # Deterministic assets (if any survived) keep their provenance tag.
    assert all(
        asset.provenance in {"deterministic", "llm_refined"}
        for asset in guide.required_assets
    )
    assert guide.clip_workflows
    assert all(
        clip.copy_ready_prompt.startswith("Refined copy-ready guide prompt")
        for clip in guide.clip_workflows
    )

    # The source prompt package is never mutated by guide-layer refinement.
    fresh = app.narratives.repo.get_model_prompt_package(package.id)
    assert fresh is not None
    assert {clip.id: clip.copy_ready_prompt for clip in fresh.clips} == baseline_prompts


def test_guide_pipeline_agent_fallback_keeps_deterministic_baseline(
    app, monkeypatch
) -> None:
    adapter = install_narrative_responder(app, monkeypatch)
    project, production = _seed_canon_project(app)
    package = _seed_ready_package(app, project, production)

    # Wrap the responder: every guide asset-refinement turn answers with a
    # placeholder-only row so the sanitizer rejects it and the pipeline must
    # degrade to the deterministic baseline instead of failing the build.
    base_responder = adapter._generate_mock_response

    def rejecting_responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Reference Asset Prompt Compiler" in prompt:
            return json.dumps(
                {"assets": [{"asset_key": "@GHOST", "generation_prompt": "TBD"}]}
            )
        return base_responder(session, turn)

    adapter._generate_mock_response = MethodType(rejecting_responder, adapter)

    assignment = dict(project.runtime_assignment)
    assignment["guide_compilation"] = dict(RUNTIME)
    app.narratives.update_project(project.id, {"runtime_assignment": assignment})

    guide = asyncio.run(
        app.narratives.generate_video_production_guide_async(project.id, package.id)
    )

    pipeline = guide.runtime_trace["pipeline"]
    assert pipeline["generation_mode"] == "agent"
    assert pipeline["refined_asset_count"] == 0
    assert pipeline["fallback_asset_count"] == pipeline["asset_count"]
    # Every asset keeps the deterministic baseline prompt.
    assert all(
        asset.provenance == "deterministic" for asset in guide.required_assets
    )
    # Clip refinement still succeeded through the scripted responder.
    assert pipeline["refined_clip_count"] == pipeline["clip_count"]
    assert guide.clip_workflows
    assert all(clip.copy_ready_prompt for clip in guide.clip_workflows)
