from __future__ import annotations

import json
from types import MethodType

from persona_continuum.application.narrative_service import (
    OUTLINE_AUDIT_PAYLOAD_BUDGET_BYTES,
    OUTLINE_CHUNK_SIZE,
    NarrativeService,
)
from persona_continuum.domain.narrative import Beat, EpisodePlan
from tests.fixtures.narrative_runtime import install_narrative_responder, seed_project


def test_outline_calls_architect_chunk_writer_and_global_auditor(app, monkeypatch) -> None:
    adapter = install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=12)
    plans = app.narratives.generate_outline_sync(
        project.id, generation_mode="agent"
    )
    assert len(plans) == 12
    assert [plan.episode_number for plan in plans] == list(range(1, 13))
    assert all(plan.generation_mode.value == "agent" for plan in plans)
    assert plans[0].hook == "Hook 1"
    prompts = [turn.user_message for _, turn in adapter.sent_turns]
    assert any("Design the season/arc structure" in prompt for prompt in prompts)
    assert sum("Write structured episode plans" in prompt for prompt in prompts) == 2
    assert any("Audit this master outline" in prompt for prompt in prompts)
    audit_prompts = [prompt for prompt in prompts if "Audit this master outline" in prompt]
    assert audit_prompts
    assert all("runtime_trace" not in prompt for prompt in audit_prompts)


def _cjk_plan(episode: int) -> EpisodePlan:
    long_goal = "裁员通知把过去十年的因果一次性摊开，" * 8
    return EpisodePlan(
        project_id="proj",
        episode_number=episode,
        title=f"第{episode}集 十年后的回响",
        narrative_goal=long_goal,
        hook="邮箱里的未来通知让方正不敢点开附件。",
        beats=[
            Beat(
                order=1,
                title="发现",
                description="方正反复核对发件人时间戳，确认那是十年后的自己。" * 2,
            ),
            Beat(
                order=2,
                title="代价",
                description="同事开始用他尚未做出的选择来评价他此刻的沉默。" * 2,
            ),
        ],
        must_happen=["不能公开未来附件"],
        required_characters=["方正", "陈律"],
        plot_threads=["身份", "裁员"],
        clues_to_plant=["时间戳"],
        clues_to_echo=["工牌"],
        reveal_targets=["发件人是未来的方正"] if episode == 12 else [],
        forbidden_reveals=["最终裁员名单"],
        cliffhanger="下一条通知已经在输入中。",
        runtime_trace={"secret": "should-not-leak-into-audit"},
    )


def test_outline_audit_windows_stay_inside_argv_budget() -> None:
    plans = [_cjk_plan(number) for number in range(1, 61)]
    windows = NarrativeService._outline_windows(plans)
    assert len(windows) > 1
    assert all(len(window) <= OUTLINE_CHUNK_SIZE for window in windows)
    for window in windows:
        payload = [NarrativeService._compact_episode_for_audit(plan) for plan in window]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        assert "should-not-leak-into-audit" not in raw
        assert "runtime_trace" not in raw
        assert len(raw.encode("utf-8")) <= OUTLINE_AUDIT_PAYLOAD_BUDGET_BYTES


def test_outline_reports_nonzero_progress(app, monkeypatch) -> None:
    install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=12)
    seen: list[tuple[str, str, int]] = []

    def report(stage: str, label: str, percent: int) -> None:
        seen.append((stage, label, percent))

    plans = app.narratives._run_sync(
        app.narratives.generate_outline(
            project.id, generation_mode="agent", progress=report
        )
    )
    assert len(plans) == 12
    assert seen
    assert seen[0][0] == "outline_architecture"
    assert seen[0][2] > 0
    assert any(stage == "outline_chunk" for stage, _label, _percent in seen)
    assert any("正在写 EP" in label for _stage, label, _percent in seen)
    percents = [percent for _stage, _label, percent in seen]
    assert percents == sorted(percents)
    assert percents[-1] >= 76


def test_reclaim_orphaned_narrative_jobs(app) -> None:
    from datetime import UTC, datetime

    from persona_continuum.application._utils import dumps
    from persona_continuum.application.job_progress import JobProgress

    project, _ = seed_project(app, episodes=3)
    now = datetime.now(UTC).isoformat()
    row = {
        "id": "njob_orphan",
        "project_id": project.id,
        "kind": "outline",
        "status": "running",
        "payload": {},
        "progress": JobProgress(stage="running", label="Narrative job running").model_dump(
            mode="json"
        ),
        "result": {},
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    app.narratives._jobs[row["id"]] = row
    app.narratives._save_job_row(row)
    app.narratives.reclaim_orphaned_jobs()
    reclaimed = app.narratives.get_job(row["id"])
    assert reclaimed["status"] == "failed"
    assert "Process restarted" in str(reclaimed["error"] or dumps({}))


def test_sixty_episode_outline_audit_does_not_dump_full_plans(app, monkeypatch) -> None:
    adapter = install_narrative_responder(app, monkeypatch)
    project, _ = seed_project(app, episodes=60)
    plans = app.narratives.generate_outline_sync(project.id, generation_mode="agent")
    assert len(plans) == 60
    prompts = [turn.user_message for _, turn in adapter.sent_turns]
    audit_prompts = [prompt for prompt in prompts if "Audit this master outline" in prompt]
    assert any("continuity map" in prompt for prompt in audit_prompts)
    assert any("Window: EP" in prompt for prompt in audit_prompts)
    for prompt in audit_prompts:
        assert "runtime_trace" not in prompt
        assert len(prompt.encode("utf-8")) <= 57_344


def test_outline_saves_when_agent_reaudit_still_has_plot_issues(app, monkeypatch) -> None:
    adapter = install_narrative_responder(app, monkeypatch)
    inner = adapter._generate_mock_response

    def wrapped(self, session, turn):
        prompt = turn.user_message or ""
        if "Final pass: audit this repaired outline" in prompt:
            return json.dumps({"blocking_issues": ["arc still loops"], "warnings": []})
        if "Audit this master outline" in prompt:
            return json.dumps({"blocking_issues": ["needs repair"], "warnings": []})
        return inner(session, turn)

    adapter._generate_mock_response = MethodType(wrapped, adapter)
    project, _ = seed_project(app, episodes=3)
    plans = app.narratives.generate_outline_sync(project.id, generation_mode="agent")
    assert [plan.episode_number for plan in plans] == [1, 2, 3]
    stored = app.narratives.list_episode_plans(project.id)
    assert len(stored) == 3
    audit = (plans[0].runtime_trace or {}).get("global_audit") or {}
    assert audit.get("repaired") is True
    assert audit.get("unresolved_after_repair") == ["arc still loops"]
    assert "arc still loops" in audit.get("warnings", [])
