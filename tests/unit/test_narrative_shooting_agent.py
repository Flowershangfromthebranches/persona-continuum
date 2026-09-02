"""Unit tests for the Narrative Shooting Agent contract and service."""

from __future__ import annotations

import asyncio
import json
from types import MethodType
from typing import Any

import pytest

from persona_continuum.agent.structured_output import StructuredOutputEngine
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.narrative import ProductionAsset
from persona_continuum.narrative.director import (
    ACTION_REGISTRY as DIRECTOR_ACTION_REGISTRY,
)
from persona_continuum.narrative.director import HUMAN_ONLY_ACTIONS
from persona_continuum.narrative.shooting import (
    SHOOTING_ACTION_NOT_ALLOWED,
    SHOOTING_ACTION_REGISTRY,
    SHOOTING_DECISION_SCHEMA,
    ShootingActionRisk,
    build_shooting_context,
    normalize_shooting_action_arguments,
)
from tests.fixtures.narrative_runtime import (
    RUNTIME,
    install_narrative_responder,
    seed_project,
)

# Hardcoded expectation: the human-only action set must stay exactly this.
EXPECTED_HUMAN_ONLY_ACTIONS = (
    "commit_episode",
    "force_commit_episode",
    "generate_production_package",
    "delete_project",
    "delete_canon",
    "delete_persona",
    "modify_persona_base",
    "force_override_audit",
)


@pytest.fixture()
def app(tmp_path):
    continuum = PersonaContinuum(
        Config(data_dir=tmp_path / "shooting_agent"), include_fake_agent=True
    )
    continuum.init()
    yield continuum
    continuum.close()


def test_shooting_registry_excludes_director_story_actions() -> None:
    for action in (
        "patch_episode_plan",
        "revise_episode_draft",
        "commit_episode",
        "generate_production_package",
    ):
        assert action not in SHOOTING_ACTION_REGISTRY
    # The two registries must not share a single action: Shooting is the
    # production-side sibling, never a story/canon writer.
    assert not set(SHOOTING_ACTION_REGISTRY) & set(DIRECTOR_ACTION_REGISTRY)
    assert HUMAN_ONLY_ACTIONS == EXPECTED_HUMAN_ONLY_ACTIONS


def test_build_complete_production_guide_is_a_safe_write_action() -> None:
    """The Task C guide action joins the registry as SAFE_WRITE and keeps
    the zero-overlap invariant with the director's story registry."""
    spec = SHOOTING_ACTION_REGISTRY.get("build_complete_production_guide")
    assert spec is not None
    assert spec.risk == ShootingActionRisk.SAFE_WRITE
    assert "project_id" in spec.required
    assert "prompt_package_id" in spec.required
    assert "production_package_id" in spec.arguments
    # The registry-wide invariant still holds after the addition.
    assert not set(SHOOTING_ACTION_REGISTRY) & set(DIRECTOR_ACTION_REGISTRY)


def test_shooting_decision_schema_parses_valid_decision() -> None:
    result = StructuredOutputEngine().parse_and_validate(
        json.dumps(
            {
                "decision": "execute_action",
                "action": "create_model_prompt_package",
                "arguments": {
                    "production_package_id": "prod_1",
                    "profile_id": "veo_3_1",
                },
            }
        ),
        SHOOTING_DECISION_SCHEMA,
        phase="unit_test",
    )
    value = result.value
    assert value["decision"] == "execute_action"
    assert value["action"] == "create_model_prompt_package"
    assert value["arguments"]["profile_id"] == "veo_3_1"


def test_normalize_injects_session_scope_and_applies_aliases() -> None:
    normalized = normalize_shooting_action_arguments(
        "set_clip_reference_assets",
        {
            "package_id": "pkg_1",
            "reference_assets": ["past_a", "past_b"],
            "video_profile_id": "veo_3_1",
            "mode": "image_to_video",
            "note": "",
        },
        session_project_id="proj_9",
        session_episode_number=4,
    )
    assert normalized["prompt_package_id"] == "pkg_1"
    assert normalized["reference_asset_ids"] == ["past_a", "past_b"]
    assert normalized["profile_id"] == "veo_3_1"
    assert normalized["generation_mode"] == "image_to_video"
    # Host-injected scope: the model never supplies project_id.
    assert normalized["project_id"] == "proj_9"
    # Empty values are dropped, not carried into validation.
    assert "note" not in normalized


def test_normalize_defaults_and_coerces_episode_number() -> None:
    coerced = normalize_shooting_action_arguments(
        "get_canon_episode",
        {"episode_number": "2"},
        session_project_id="proj_9",
        session_episode_number=7,
    )
    assert coerced["episode_number"] == 2
    defaulted = normalize_shooting_action_arguments(
        "get_canon_episode",
        {},
        session_project_id="proj_9",
        session_episode_number=7,
    )
    assert defaulted["episode_number"] == 7


def test_normalize_project_id_is_host_authoritative() -> None:
    """F8: the session's project scope unconditionally wins — a model-
    supplied project_id can never widen or redirect the action's scope."""
    overridden = normalize_shooting_action_arguments(
        "set_clip_reference_assets",
        {
            "package_id": "pkg_1",
            "reference_assets": ["past_a"],
            "project_id": "proj_other",
        },
        session_project_id="proj_9",
        session_episode_number=4,
    )
    assert overridden["project_id"] == "proj_9"


def test_build_shooting_context_excludes_truth_markers_keeps_request() -> None:
    context = build_shooting_context(
        mode="agent",
        session={
            "id": "nsho_x",
            "project_id": "proj_1",
            "episode_number": 1,
            "status": "active",
        },
        user_request="请为 EP1 的镜头生成 clip 计划并编译提示词",
        allowed_actions=[
            {"name": "get_shot_list", "risk": "read_only", "description": "Read shots"}
        ],
        visual_bibles={
            "characters": [
                {"name": "Fang", "visual_description": "蓝色衬衫的年轻职员"}
            ]
        },
        production_assets=[
            {"id": "past_1", "name": "ref", "asset_type": "character_reference"}
        ],
    )
    # Production-scope context: story-bible truth markers never appear.
    assert "final_truth" not in context
    # The user request and the action registry survive assembly.
    assert "请为 EP1 的镜头生成 clip 计划并编译提示词" in context
    assert "get_shot_list" in context


def test_build_shooting_context_truncates_to_budget() -> None:
    long_request = "请求" * 3000
    context = build_shooting_context(
        session={"id": "nsho_x"},
        user_request=long_request,
        allowed_actions=[
            {"name": "get_shot_list", "risk": "read_only", "description": "d"}
        ],
        max_chars=800,
    )
    assert len(context) <= 800
    # The request prefix is kept; the oversized remainder is cut.
    assert long_request[:100] in context
    assert long_request not in context


def _seed_canon_project(app: Any) -> tuple[Any, Any]:
    """Outline → draft → audit → commit → canon production package."""
    project, _bible = seed_project(app, episodes=1)
    app.narratives.generate_outline_sync(project.id, episode_count=1)
    version = app.narratives.generate_episode_draft_sync(project.id, 1)
    app.narratives.audit_episode(project.id, version.id)
    app.narratives.commit_episode(project.id, 1, version.id)
    production = app.narratives.generate_production_package(project.id, 1)
    assert production.shot_list, "scripted Director turn must produce shots"
    return project, production


def test_fake_runtime_full_chain_without_native_tool_calling(
    app, monkeypatch
) -> None:
    install_narrative_responder(app, monkeypatch)
    project, production = _seed_canon_project(app)
    service = app.narrative_shooting

    # Spy on execute_structured: the Shooting Agent is a plain-CLI agent —
    # every model turn must go through the structured-output boundary with
    # the shooting decision schema, never native tool calling.
    executor = app.agent_runtime_executor
    original_execute = executor.execute_structured
    structured_calls: list[dict[str, Any]] = []

    async def spy_execute_structured(binding: Any, *args: Any, **kwargs: Any) -> Any:
        structured_calls.append(
            {
                "schema": kwargs.get("schema"),
                "phase": kwargs.get("phase"),
                "user_message": str(kwargs.get("user_message") or ""),
            }
        )
        return await original_execute(binding, *args, **kwargs)

    monkeypatch.setattr(executor, "execute_structured", spy_execute_structured)

    # Script the shooting turns: first a forbidden story-side attempt (must
    # be rejected), then the requested clip-plan creation, then finish.
    adapter = app.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    decisions: list[str] = [
        json.dumps(
            {
                "decision": "execute_action",
                "action": "patch_episode_plan",
                "arguments": {"episode_number": 1, "title": "story hijack attempt"},
            }
        ),
        json.dumps(
            {
                "decision": "execute_action",
                "action": "create_model_prompt_package",
                "arguments": {
                    "production_package_id": production.id,
                    "profile_id": "veo_3_1",
                },
            }
        ),
        json.dumps({"decision": "finish", "finish_summary": "clip plan created"}),
    ]
    base_responder = adapter._generate_mock_response

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
    snapshot = service.send_message(session.id, "为 EP1 的镜头创建 clip 计划")

    # Every scripted decision was consumed by exactly one shooting turn.
    assert not decisions

    actions = snapshot["actions"]
    assert [a["action"] for a in actions] == [
        "patch_episode_plan",
        "create_model_prompt_package",
    ]
    rejected = actions[0]
    assert rejected["status"] == "rejected"
    assert rejected["error_code"] == SHOOTING_ACTION_NOT_ALLOWED
    succeeded = actions[1]
    assert succeeded["status"] == "succeeded"
    package_id = succeeded["result"]["artifacts"]["prompt_package_id"]

    # The package was persisted with status ready and real clips.
    package = app.narratives.repo.get_model_prompt_package(package_id)
    assert package is not None
    assert package.status == "ready"
    assert package.clips
    assert package.target_profile_id == "veo_3_1"

    # Session messages (user + assistant) and actions are persisted.
    roles = [m["role"] for m in snapshot["messages"]]
    assert "user" in roles
    assert "assistant" in roles
    assert snapshot["session"]["id"] == session.id

    # The fake runtime was driven through execute_structured (plain CLI).
    shooting_calls = [
        call
        for call in structured_calls
        if call["schema"] is SHOOTING_DECISION_SCHEMA
        and "You are the Narrative Shooting Agent" in call["user_message"]
    ]
    assert len(shooting_calls) == 3

    # Turn-scoped leases: after the turn completes the service stores no
    # binding/session attribute beyond its four fixed collaborators, and the
    # session is no longer running. (Executor-level sessions are reclaimed by
    # the scheduler's idle timeout; the service itself must hold nothing.)
    assert set(service.__dict__) == {"continuum", "narratives", "repo", "_tasks"}
    assert session.id not in service._tasks
    assert snapshot["session"]["status"] != "running"


def test_circuit_breaker_after_three_consecutive_failures(app, monkeypatch) -> None:
    """Mark's gap + F10: three consecutive rejected actions trip the circuit
    breaker — the session lands in needs_human_guidance with the corrected
    wording (the condition is 3 consecutive failures of any action, not the
    same operation)."""
    install_narrative_responder(app, monkeypatch)
    project, _production = _seed_canon_project(app)
    service = app.narrative_shooting

    adapter = app.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    decisions = [
        json.dumps(
            {
                "decision": "execute_action",
                "action": "patch_episode_plan",
                "arguments": {"episode_number": 1, "title": f"story hijack {i}"},
            }
        )
        for i in range(3)
    ]
    base_responder = adapter._generate_mock_response

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
    snapshot = service.send_message(session.id, "修改剧本计划")

    actions = snapshot["actions"]
    assert [a["action"] for a in actions] == ["patch_episode_plan"] * 3
    assert all(a["status"] == "rejected" for a in actions)
    # Circuit breaker: the session needs human guidance after 3 failures.
    assert snapshot["session"]["status"] == "needs_human_guidance"
    assert any("连续 3 次操作失败" in m["content"] for m in snapshot["messages"])
    assert not decisions


def test_replan_retains_pinned_reference_assets(app, monkeypatch) -> None:
    """F5: a re-plan must migrate pinned reference assets from the old clips
    to the new clips whose source shots overlap, and rebuild the asset
    requirements against the fresh clip plan."""
    install_narrative_responder(app, monkeypatch)
    project, production = _seed_canon_project(app)
    service = app.narrative_shooting

    package = asyncio.run(
        app.narratives.generate_model_prompt_package_async(
            project.id, production.id, "veo_3_1"
        )
    )
    assert package.status == "ready"
    assert package.clips
    pinned_clip = package.clips[0]

    # Register a production asset and pin it on the clip (host patch path).
    asset = ProductionAsset(
        id="past_pin",
        project_id=project.id,
        asset_type="character_reference",
        name="钉住的参考图",
    )
    app.narratives.repo.save_production_asset(asset)
    patched = service.patch_clip(
        project.id, package.id, pinned_clip.id, {"reference_asset_ids": ["past_pin"]}
    )
    assert patched.reference_asset_ids == ["past_pin"]

    # Trigger a re-plan through the Shooting Agent (option change).
    adapter = app.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    decisions = [
        json.dumps(
            {
                "decision": "execute_action",
                "action": "revise_clip_plan",
                "arguments": {
                    "prompt_package_id": package.id,
                    "aspect_ratio": "9:16",
                    "instruction": "改为竖屏，保留参考图",
                },
            }
        ),
        json.dumps({"decision": "finish", "finish_summary": "replan done"}),
    ]
    base_responder = adapter._generate_mock_response

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
    snapshot = service.send_message(session.id, "把画面改成竖屏并保留参考图")

    actions = snapshot["actions"]
    assert [a["action"] for a in actions] == ["revise_clip_plan"]
    assert actions[0]["status"] == "succeeded"

    replanned = app.narratives.repo.get_model_prompt_package(package.id)
    assert replanned is not None
    assert replanned.aspect_ratio == "9:16"
    assert replanned.clips
    # Every new clip covering the pinned clip's source shots keeps the pin.
    old_shots = set(pinned_clip.source_shot_numbers)
    for new_clip in replanned.clips:
        if old_shots & set(new_clip.source_shot_numbers):
            assert "past_pin" in new_clip.reference_asset_ids
    assert any("past_pin" in c.reference_asset_ids for c in replanned.clips)
    # Asset requirements were rebuilt against the fresh clip plan (a pinned
    # registered asset needs no requirement entry).
    assets_by_id = {
        a.id: a for a in app.narratives.repo.list_production_assets(project.id)
    }
    assert replanned.asset_requirements == type(service)._rebuild_asset_requirements(
        replanned.clips, assets_by_id
    )
