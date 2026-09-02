from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
INDEX = ROOT / "src/persona_continuum/web/static/index.html"
APP = ROOT / "src/persona_continuum/web/static/app.js"


def test_persona_creation_progress_dialog_has_real_progress_bar() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="pc-progress-bar"' in html
    assert 'id="pc-stage"' in html


def test_profile_enrichment_progress_has_failure_surface() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="profile-enrich-progress-result"' in html
    assert 'id="profile-enrich-progress-close"' in html


def test_task_center_restores_both_background_job_types() -> None:
    html = INDEX.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    assert 'id="profile-task-center"' in html
    assert "/api/persona-creation/jobs" in app
    assert "/api/profile-enrichment/jobs" in app


def test_close_does_not_call_cancel_endpoint() -> None:
    app = APP.read_text(encoding="utf-8")
    assert (
        'onClick("#profile-enrich-progress-close", () => $("#dlg-profile-enrich-progress").close())'
        in app
    )


def test_polling_has_no_fixed_120_second_limit() -> None:
    app = APP.read_text(encoding="utf-8")
    assert "attempt < 240" not in app


def test_task_center_can_reopen_a_background_job() -> None:
    app = APP.read_text(encoding="utf-8")
    assert 'data-background-action="view"' in app
    assert "openPersonaCreationProgressDialog(job)" in app
    assert "openProfileEnrichmentProgress(job)" in app


def test_retryable_failure_exposes_current_stage_retry_controls() -> None:
    html = INDEX.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    assert 'id="pc-retry"' in html
    assert 'id="profile-enrich-progress-retry"' in html
    assert "/api/persona-creation/jobs/${encodeURIComponent(job.id)}/retry" in app
    assert "/api/profile-enrichment/jobs/${encodeURIComponent(job.id)}/retry" in app


def test_quality_gate_failure_is_terminal_retryable_and_not_cancellable() -> None:
    app = APP.read_text(encoding="utf-8")
    assert '"failed_quality_gate",' in app
    assert 'const BACKGROUND_FAILED = new Set(["failed", "failed_quality_gate"]);' in app
    assert 'retry.hidden = !BACKGROUND_FAILED.has(job.status)' in app
    assert '$("#pc-cancel").hidden = BACKGROUND_TERMINAL.has(job.status);' in app
    assert 'if (!job || !BACKGROUND_FAILED.has(job.status)) return;' in app


def test_task_center_uses_effective_retryability_for_legacy_failures() -> None:
    app = APP.read_text(encoding="utf-8")
    assert 'job.retry_available === true || failure.retriable === true' in app
    assert 'if (filter === "failed") return BACKGROUND_FAILED.has(job.status);' in app
    assert 'status === "failed_quality_gate"' in app
    assert 'job.status === "failed_quality_gate"' in app
    assert '"质量门禁未通过"' in app


def test_profile_progress_has_explicit_pause_resume_cancel_controls() -> None:
    html = INDEX.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    for element_id in (
        "profile-enrich-progress-pause",
        "profile-enrich-progress-resume",
        "profile-enrich-progress-cancel",
    ):
        assert f'id="{element_id}"' in html
    assert "/api/profile-enrichment/jobs/${encodeURIComponent(job.id)}/cancel" in app


def test_profile_enrichment_submit_exposes_busy_and_error_feedback() -> None:
    html = INDEX.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    assert 'id="profile-enrich-submit-status"' in html
    assert 'submit.textContent = "正在创建升级任务…"' in app
    assert 'submit.setAttribute("aria-busy", "true")' in app
    assert 'error.textContent = `升级失败：${message}`' in app
    assert 'submit.removeAttribute("aria-busy")' in app
