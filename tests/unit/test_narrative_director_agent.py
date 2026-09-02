"""Narrative Director Agent — permissions, action loop, gates, and safety."""

from __future__ import annotations

import json
from types import MethodType
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from tests.fixtures.narrative_runtime import install_narrative_responder, seed_project

DRAFT_JSON = {
    "episode_number": 1,
    "title": "AI Draft",
    "duration": 90,
    "hook": "A message arrives",
    "scenes": [
        {
            "scene_number": 1,
            "location": "Office",
            "time": "18:29",
            "characters": ["fang"],
            "duration": 45,
            "action": ["Fang opens the message"],
            "dialogue": [{"speaker": "Fang", "text": "This cannot be real."}],
            "visual_direction": "Phone glow",
            "narrative_function": "Hook",
        }
    ],
    "reveals": [],
    "clues_planted": ["timestamp"],
    "clues_echoed": [],
    "relationship_changes": [],
    "knowledge_changes": [],
    "cliffhanger": "Do not trust future me",
}


@pytest.fixture()
def app(tmp_path):
    continuum = PersonaContinuum(Config(data_dir=tmp_path / "director"), include_fake_agent=True)
    continuum.init()
    yield continuum
    continuum.close()


def _decide(**kwargs: Any) -> str:
    return json.dumps(kwargs)


def install_director_responder(app: Any, monkeypatch: Any, decisions: list[dict[str, Any]]) -> Any:
    """Script Director decisions (consumed in order); other stages keep defaults."""
    adapter = install_narrative_responder(app, monkeypatch)
    queue = list(decisions)
    original = adapter._generate_mock_response

    def responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Narrative Director Agent" in prompt:
            assert queue, "Director decision script exhausted"
            return queue.pop(0)
        if "Revise the existing structured episode draft" in prompt:
            return json.dumps(dict(DRAFT_JSON, title="AI Draft V2"))
        return original(session, turn)

    monkeypatch.setattr(adapter, "_generate_mock_response", MethodType(responder, adapter))
    return adapter


def _prepared_episode(app: Any, monkeypatch: Any) -> tuple[Any, Any, Any]:
    """Project + bible + outline plans + EP01 V1 draft, all via the fake runtime."""
    install_narrative_responder(app, monkeypatch)
    project, _bible = seed_project(app)
    app.narratives.generate_outline_sync(project.id, episode_count=3)
    version = app.narratives.generate_episode_draft_sync(project.id, 1)
    return project, _bible, version


def _actions(app: Any, session_id: str) -> list[Any]:
    return app.narrative_repo.list_director_actions(session_id)


def test_human_only_actions_are_not_registered(app: Any) -> None:
    from persona_continuum.narrative.director import ACTION_REGISTRY, HUMAN_ONLY_ACTIONS

    for name in HUMAN_ONLY_ACTIONS:
        assert name not in ACTION_REGISTRY


def test_permission_rejects_commit_and_production(app: Any, monkeypatch: Any) -> None:
    project, _bible, _version = _prepared_episode(app, monkeypatch)
    install_director_responder(
        app,
        monkeypatch,
        [
            _decide(decision="execute_action", action="commit_episode", arguments={}),
            _decide(decision="execute_action", action="generate_production_package", arguments={}),
            _decide(decision="stop", message="无法提交正史。"),
        ],
    )
    session = app.narrative_director.create_session(project.id, episode_number=1)
    app.narrative_director.send_message(session.id, "帮我直接提交正史")

    rejected = [a for a in _actions(app, session.id) if a.status.value == "rejected"]
    assert {a.action for a in rejected} == {"commit_episode", "generate_production_package"}
    assert all(a.error_code == "DIRECTOR_ACTION_NOT_ALLOWED" for a in rejected)
    # Nothing was committed and no production package exists.
    assert app.narrative_repo.get_canon_episode_version(project.id, 1) is None
    assert app.narrative_repo.list_production_packages(project.id) == []


def test_discuss_mode_blocks_write_actions(app: Any, monkeypatch: Any) -> None:
    project, _bible, _version = _prepared_episode(app, monkeypatch)
    install_director_responder(
        app,
        monkeypatch,
        [
            _decide(
                decision="execute_action",
                action="patch_episode_plan",
                arguments={
                    "project_id": project.id,
                    "episode_number": 1,
                    "patch": {"cliffhanger": "hacked"},
                    "expected_project_revision": 1,
                    "reason": "should not run",
                },
            ),
            _decide(decision="answer", message="我的建议是……"),
        ],
    )
    session = app.narrative_director.create_session(
        project.id, episode_number=1, mode="discuss"
    )
    app.narrative_director.send_message(session.id, "你觉得结尾怎么样？")

    rejected = [a for a in _actions(app, session.id) if a.status.value == "rejected"]
    assert len(rejected) == 1
    plan = app.narratives.get_episode_plan(project.id, 1)
    assert plan.cliffhanger != "hacked"


def test_agent_mode_full_revision_loop_reaches_canon_gate(app: Any, monkeypatch: Any) -> None:
    project, _bible, v1 = _prepared_episode(app, monkeypatch)
    revision = app.narratives.get_project(project.id).revision
    install_director_responder(
        app,
        monkeypatch,
        [
            _decide(decision="execute_action", action="get_pipeline_state",
                    arguments={"project_id": project.id, "episode_number": 1}),
            _decide(
                decision="execute_action",
                action="patch_episode_plan",
                arguments={
                    "project_id": project.id,
                    "episode_number": 1,
                    "patch": {"cliffhanger": "The accident happens at 19:17"},
                    "expected_project_revision": revision,
                    "reason": "用户要求 19:17 车祸在本集发生",
                },
            ),
            _decide(
                decision="execute_action",
                action="revise_episode_draft",
                arguments={
                    "project_id": project.id,
                    "episode_number": 1,
                    "base_version_id": v1.id,
                    "instructions": ["删除提前暴露的模拟系统名称", "19:17 事故在本集兑现"],
                    "revision_mode": "local",
                },
            ),
            _decide(decision="execute_action", action="audit_episode",
                    arguments={"project_id": project.id, "episode_number": 1}),
            _decide(decision="stop", message="EP01 已完成修改并通过审核。"),
        ],
    )
    session = app.narrative_director.create_session(project.id, episode_number=1, mode="agent")
    app.narrative_director.send_message(session.id, "按上面的要求修改 EP01，不要提交正史")

    # V2 created from V1; V1 untouched.
    versions = app.narratives.get_episode_versions(project.id, 1)
    assert [v.version for v in versions] == [2, 1]
    v2 = versions[0]
    assert v2.parent_version_id == v1.id
    assert v2.revision_mode == "local"
    assert "删除提前暴露的模拟系统名称" in v2.revision_instructions
    assert v1.screenplay == app.narratives.get_episode_version(v1.id).screenplay
    assert v2.created_by.startswith("director:")

    # Plan patched and project revision bumped.
    plan = app.narratives.get_episode_plan(project.id, 1)
    assert plan.cliffhanger == "The accident happens at 19:17"
    assert app.narratives.get_project(project.id).revision > revision

    # Canon Gate: audit passed → session parked before human approval.
    refreshed = app.narrative_repo.get_director_session(session.id)
    assert refreshed is not None
    assert refreshed.status.value == "waiting_for_canon_approval"
    succeeded = {a.action for a in _actions(app, session.id) if a.status.value == "succeeded"}
    assert succeeded == {
        "get_pipeline_state",
        "patch_episode_plan",
        "revise_episode_draft",
        "audit_episode",
    }
    # No canon was committed by the Director.
    assert app.narrative_repo.get_canon_episode_version(project.id, 1) is None


def test_patch_episode_plan_rejects_immutable_fields(app: Any, monkeypatch: Any) -> None:
    project, _bible, _version = _prepared_episode(app, monkeypatch)
    revision = app.narratives.get_project(project.id).revision
    with pytest.raises(ValueError, match="not patchable"):
        app.narratives.patch_episode_plan(
            project.id,
            1,
            {"id": "hacked"},
            expected_project_revision=revision,
            reason="test",
        )



def test_patch_episode_plan_bumps_revision_and_stales_draft(app: Any, monkeypatch: Any) -> None:
    project, _bible, v1 = _prepared_episode(app, monkeypatch)
    revision = app.narratives.get_project(project.id).revision
    updated = app.narratives.patch_episode_plan(
        project.id,
        1,
        {"must_not_happen": ["MindCore-Prometheus-Sim 提前暴露"]},
        expected_project_revision=revision,
        reason="泄漏太早",
    )
    assert "MindCore-Prometheus-Sim 提前暴露" in updated.must_not_happen
    project_now = app.narratives.get_project(project.id)
    assert project_now.revision == revision + 1
    v1_after = app.narratives.get_episode_version(v1.id)
    assert v1_after.stale is True
    assert v1_after.screenplay == v1.screenplay  # data preserved, only flagged


def test_state_conflict_refuses_stale_write(app: Any, monkeypatch: Any) -> None:
    project, _bible, _version = _prepared_episode(app, monkeypatch)
    install_director_responder(
        app,
        monkeypatch,
        [
            _decide(
                decision="execute_action",
                action="patch_episode_plan",
                arguments={
                    "project_id": project.id,
                    "episode_number": 1,
                    "patch": {"cliffhanger": "stale write"},
                    "expected_project_revision": 999,
                    "reason": "conflict",
                },
            ),
            _decide(decision="answer", message="检测到状态冲突，已重新读取。"),
        ],
    )
    session = app.narrative_director.create_session(project.id, episode_number=1)
    app.narrative_director.send_message(session.id, "修改EP01")

    failed = [a for a in _actions(app, session.id) if a.status.value == "failed"]
    assert len(failed) == 1
    assert failed[0].error_code == "NARRATIVE_DIRECTOR_STATE_CONFLICT"
    plan = app.narratives.get_episode_plan(project.id, 1)
    assert plan.cliffhanger != "stale write"


def test_high_impact_write_requires_user_confirmation(app: Any, monkeypatch: Any) -> None:
    project, _bible, _version = _prepared_episode(app, monkeypatch)
    install_director_responder(
        app,
        monkeypatch,
        [
            _decide(
                decision="execute_action",
                action="patch_story_bible",
                arguments={
                    "project_id": project.id,
                    "patch": {"world_rules": ["未来邮件来自物理时间旅行"]},
                    "reason": "核心设定变更",
                },
            ),
            _decide(
                decision="execute_action",
                action="patch_story_bible",
                arguments={
                    "project_id": project.id,
                    "patch": {"world_rules": ["未来邮件来自物理时间旅行"]},
                    "reason": "核心设定变更",
                    "user_confirmed": True,
                },
            ),
            _decide(decision="stop", message="Story Bible 已更新。"),
        ],
    )
    session = app.narrative_director.create_session(project.id, episode_number=1)
    app.narrative_director.send_message(session.id, "我要改核心设定")

    # First attempt only asks for confirmation.
    pending = [a for a in _actions(app, session.id) if a.status.value == "pending"]
    assert len(pending) == 1
    mid = app.narrative_repo.get_director_session(session.id)
    assert mid is not None and mid.status.value == "waiting_for_user"
    assert mid.pending_action.get("action") == "patch_story_bible"
    bible_before = app.narratives.get_bible(project.id)

    # User confirms → the second scripted action is allowed to run.
    app.narrative_director.send_message(session.id, "确认")
    after = app.narratives.get_bible(project.id)
    assert after is not None and bible_before is not None
    assert after.version == bible_before.version + 1
    assert "未来邮件来自物理时间旅行" in after.world_rules
    final = app.narrative_repo.get_director_session(session.id)
    assert final is not None and final.pending_action == {}


def test_continue_after_canon_gate_creates_v3(app: Any, monkeypatch: Any) -> None:
    project, _bible, v1 = _prepared_episode(app, monkeypatch)
    install_director_responder(
        app,
        monkeypatch,
        [
            _decide(
                decision="execute_action",
                action="revise_episode_draft",
                arguments={
                    "project_id": project.id,
                    "episode_number": 1,
                    "base_version_id": v1.id,
                    "instructions": ["砍短第二场"],
                    "revision_mode": "local",
                },
            ),
            _decide(decision="execute_action", action="audit_episode",
                    arguments={"project_id": project.id, "episode_number": 1}),
        ],
    )
    session = app.narrative_director.create_session(project.id, episode_number=1)
    app.narrative_director.send_message(session.id, "第二场太拖了")
    mid = app.narrative_repo.get_director_session(session.id)
    assert mid is not None and mid.status.value == "waiting_for_canon_approval"
    assert len(app.narratives.get_episode_versions(project.id, 1)) == 2

    # User keeps iterating after the gate: V3 with V2 as parent.
    v2 = app.narratives.get_episode_versions(project.id, 1)[0]
    install_director_responder(
        app,
        monkeypatch,
        [
            _decide(
                decision="execute_action",
                action="revise_episode_draft",
                arguments={
                    "project_id": project.id,
                    "episode_number": 1,
                    "base_version_id": v2.id,
                    "instructions": ["再砍 20%"],
                    "revision_mode": "local",
                },
            ),
            _decide(decision="execute_action", action="audit_episode",
                    arguments={"project_id": project.id, "episode_number": 1}),
        ],
    )
    app.narrative_director.send_message(session.id, "第二场还是太拖，再砍20%")
    versions = app.narratives.get_episode_versions(project.id, 1)
    assert [v.version for v in versions] == [3, 2, 1]
    assert versions[0].parent_version_id == v2.id
    final = app.narrative_repo.get_director_session(session.id)
    assert final is not None and final.status.value == "waiting_for_canon_approval"


def _revise_audit_script(project: Any, base_version_id: str) -> list[dict[str, Any]]:
    revise = _decide(
        decision="execute_action",
        action="revise_episode_draft",
        arguments={
            "project_id": project.id,
            "episode_number": 1,
            "base_version_id": base_version_id,
            "instructions": ["fix it"],
            "revision_mode": "local",
        },
    )
    audit = _decide(decision="execute_action", action="audit_episode",
                    arguments={"project_id": project.id, "episode_number": 1})
    return [revise, audit, revise, audit, revise, audit]


def test_audit_repair_loop_stops_after_two_rounds(app: Any, monkeypatch: Any) -> None:
    project, _bible, v1 = _prepared_episode(app, monkeypatch)
    # AGENT reviewer keeps failing the audit with a BLOCKING finding.
    adapter = app.agent_registry.get_adapter("fake_agent")
    queue: list[str] = _revise_audit_script(project, v1.id)
    original = adapter._generate_mock_response

    def responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Narrative Director Agent" in prompt:
            return queue.pop(0)
        if "Revise the existing structured episode draft" in prompt:
            return json.dumps(dict(DRAFT_JSON, title=f"AI Draft V{2 + queue.count('x')}"))
        if "Review this AI episode draft" in prompt:
            return json.dumps(
                {"findings": [{"severity": "blocking", "code": "TB", "message": "still broken"}]}
            )
        return original(session, turn)

    monkeypatch.setattr(adapter, "_generate_mock_response", MethodType(responder, adapter))
    session = app.narrative_director.create_session(project.id, episode_number=1)
    app.narrative_director.send_message(session.id, "修复所有 BLOCKING")

    final = app.narrative_repo.get_director_session(session.id)
    assert final is not None
    assert final.status.value == "needs_human_guidance"
    audits = [a for a in _actions(app, session.id) if a.action == "audit_episode"]
    # Initial audit + exactly 2 automatic repair rounds; no infinite loop.
    assert len(audits) == 3
    assert app.narratives.get_episode_versions(project.id, 1)[0].version == 4  # V1 + 3 revisions


def test_warning_findings_do_not_block_canon_gate(app: Any, monkeypatch: Any) -> None:
    project, _bible, v1 = _prepared_episode(app, monkeypatch)
    adapter = app.agent_registry.get_adapter("fake_agent")
    queue: list[str] = _revise_audit_script(project, v1.id)[:2]
    original = adapter._generate_mock_response

    def responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "You are the Narrative Director Agent" in prompt:
            return queue.pop(0)
        if "Revise the existing structured episode draft" in prompt:
            return json.dumps(dict(DRAFT_JSON, title="AI Draft V2"))
        if "Review this AI episode draft" in prompt:
            return json.dumps(
                {"findings": [{"severity": "warning", "code": "TW", "message": "minor pacing"}]}
            )
        return original(session, turn)

    monkeypatch.setattr(adapter, "_generate_mock_response", MethodType(responder, adapter))
    session = app.narrative_director.create_session(project.id, episode_number=1)
    app.narrative_director.send_message(session.id, "继续修改")

    final = app.narrative_repo.get_director_session(session.id)
    assert final is not None
    # BLOCKING 0 / WARNING > 0 still parks at the canon gate — WARNINGs are
    # never chased to zero automatically.
    assert final.status.value == "waiting_for_canon_approval"
    audit_action = [a for a in _actions(app, session.id) if a.action == "audit_episode"][0]
    assert audit_action.result["artifacts"]["blocking_count"] == 0
    assert audit_action.result["artifacts"]["warning_count"] >= 1


def test_orphaned_running_session_is_reclaimed_and_reusable(app: Any, monkeypatch: Any) -> None:
    """A RUNNING session left by a dead process must not block forever."""
    from persona_continuum.domain.narrative import (
        NarrativeDirectorAction,
        NarrativeDirectorActionStatus,
        NarrativeDirectorSessionStatus,
    )

    project, _bible, _version = _prepared_episode(app, monkeypatch)
    session = app.narrative_director.create_session(project.id, episode_number=1)
    # Simulate a crash mid-loop: status running, action running, no live task.
    session.status = NarrativeDirectorSessionStatus.RUNNING
    app.narrative_repo.save_director_session(session)
    action = NarrativeDirectorAction(
        session_id=session.id,
        action="revise_episode_draft",
        status=NarrativeDirectorActionStatus.RUNNING,
    )
    app.narrative_repo.save_director_action(action)

    # Startup reclaim parks it at WAITING_FOR_USER and cancels in-flight actions.
    recovered = app.narrative_director.reclaim_orphaned_sessions()
    assert recovered == 1
    refreshed = app.narrative_repo.get_director_session(session.id)
    assert refreshed is not None
    assert refreshed.status.value == "waiting_for_user"
    assert app.narrative_repo.list_director_actions(session.id)[0].status.value == "cancelled"

    # The recovered session accepts a new message (no busy error).
    install_director_responder(app, monkeypatch, [_decide(decision="answer", message="恢复。")])
    app.narrative_director.send_message(session.id, "继续")
    final = app.narrative_repo.get_director_session(session.id)
    assert final is not None and final.status.value == "active"


def test_busy_guard_still_rejects_live_loop(app: Any, monkeypatch: Any) -> None:
    from persona_continuum.application.narrative_director_service import DirectorSessionBusyError
    from persona_continuum.domain.narrative import NarrativeDirectorSessionStatus

    project, _bible, _version = _prepared_episode(app, monkeypatch)
    session = app.narrative_director.create_session(project.id, episode_number=1)
    session.status = NarrativeDirectorSessionStatus.RUNNING
    app.narrative_repo.save_director_session(session)

    # A live (not-done) task must keep the busy guard active.
    class StubTask:
        def done(self) -> bool:
            return False

    app.narrative_director._tasks[session.id] = StubTask()  # type: ignore[assignment]
    try:
        with pytest.raises(DirectorSessionBusyError):
            app.narrative_director.send_message(session.id, "再发一条")
    finally:
        app.narrative_director._tasks.pop(session.id, None)


def test_argument_normalization_and_missing_argument_error(app: Any) -> None:
    from persona_continuum.narrative.director import (
        missing_required_arguments,
        normalize_action_arguments,
    )

    # Alias mapping + single-string instructions wrapping.
    normalized = normalize_action_arguments(
        "revise_episode_draft",
        {
            "episode_version_id": "",
            "base_version_id": "epv_x",
            "instruction": "砍短第二场",
            "mode": "local",
        },
        session_project_id="nproj_test",
        session_episode_number=1,
    )
    assert normalized["base_version_id"] == "epv_x"
    assert normalized["project_id"] == "nproj_test"
    assert normalized["instructions"] == ["砍短第二场"]
    assert normalized["revision_mode"] == "local"
    assert normalized["episode_number"] == 1  # session-bound default

    # Missing hard-required field produces an explicit error list.
    director_module = __import__(
        "persona_continuum.narrative.director", fromlist=["ACTION_REGISTRY"]
    )
    spec = director_module.ACTION_REGISTRY["revise_episode_draft"]
    missing = missing_required_arguments(
        spec, {"base_version_id": "epv_x", "project_id": "nproj_x"}
    )
    assert missing == ["instructions"]
    # With the host-injected defaults applied nothing is missing.
    complete = normalize_action_arguments(
        "revise_episode_draft",
        {"instruction": "x"},
        session_project_id="nproj_x",
        session_episode_number=1,
    )
    assert missing_required_arguments(spec, complete) == []


def test_consecutive_failures_trip_circuit_breaker(app: Any, monkeypatch: Any) -> None:
    project, _bible, v1 = _prepared_episode(app, monkeypatch)
    bad_revise = _decide(
        decision="execute_action",
        action="revise_episode_draft",
        arguments={"instructions": "x"},  # valid after normalization; will fail on purpose below
    )
    # Force failures by pointing base_version_id at a nonexistent version.
    bad_revise = json.dumps(
        {
            "decision": "execute_action",
            "action": "revise_episode_draft",
            "arguments": {"base_version_id": "epv_missing", "instructions": ["x"]},
        }
    )
    install_director_responder(
        app,
        monkeypatch,
        [bad_revise, bad_revise, bad_revise, _decide(decision="stop", message="不应到达")],
    )
    session = app.narrative_director.create_session(project.id, episode_number=1)
    app.narrative_director.send_message(session.id, "继续")

    final = app.narrative_repo.get_director_session(session.id)
    assert final is not None
    assert final.status.value == "needs_human_guidance"
    failed = [a for a in _actions(app, session.id) if a.status.value == "failed"]
    assert len(failed) == 3  # breaker stopped after 3, not the full 12-turn budget
