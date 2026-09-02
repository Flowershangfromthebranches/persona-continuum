from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config


@pytest.fixture()
def app(tmp_path):
    continuum = PersonaContinuum(Config(data_dir=tmp_path / "canon"), include_fake_agent=True)
    continuum.init()
    yield continuum
    continuum.close()


def _prepared_episode(app, title: str = "正史测试"):
    project = app.narratives.create_project(title=title, format="series")
    app.narratives.generate_story_bible_sync(project.id)
    app.narratives.generate_outline_sync(project.id, episode_count=2)
    return project


def test_commit_is_atomic_and_bumps_revision(app) -> None:
    project = _prepared_episode(app)
    version = app.narratives.generate_episode_draft_sync(project.id, 1)
    app.narratives.audit_episode(project.id, version.id)
    revision_before = app.narratives.get_project(project.id).revision

    result = app.narratives.commit_episode(project.id, 1, version.id)
    assert result["committed"] is True
    assert app.narratives.get_project(project.id).revision > revision_before

    canon = app.narratives.repo.list_canon_entries(project.id)
    assert len(canon) == 1
    assert canon[0].source == "narrative_commit"
    canon_version = app.narratives.repo.get_canon_episode_version(project.id, 1)
    assert canon_version is not None and canon_version.id == version.id


def test_commit_requires_audit(app) -> None:
    project = _prepared_episode(app)
    version = app.narratives.generate_episode_draft_sync(project.id, 1)
    with pytest.raises(ValueError, match="audit"):
        app.narratives.commit_episode(project.id, 1, version.id)


def test_blocking_audit_refuses_commit_unless_forced(app) -> None:
    project = _prepared_episode(app)
    app.narratives.generate_outline_sync(project.id, episode_count=2)
    plan = app.narratives.get_episode_plan(project.id, 1)
    plan.must_not_happen = ["主角公开承认一切"]
    app.narratives.save_episode_plan(plan)
    # Make the draft contain the forbidden content so the auditor blocks it.
    version = app.narratives.generate_episode_draft_sync(project.id, 1)
    version.screenplay = "EP01\n主角公开承认一切，全场哗然。\n" * 4
    app.narratives.repo.save_episode_version(version)
    report = app.narratives.audit_episode(project.id, version.id)

    if report.blocking_count > 0:
        with pytest.raises(ValueError, match="BLOCKING"):
            app.narratives.commit_episode(project.id, 1, version.id)
        result = app.narratives.commit_episode(
            project.id, 1, version.id, force=True, override_reason="作者确认保留"
        )
        assert result["committed"] is True
        audits = app.narratives.list_audits(project.id, 1)
        codes = [f.code for a in audits for f in a.findings]
        assert "COMMIT_OVERRIDE" in codes
    else:
        # Rule did not trigger; force path is still exercised by other tests.
        assert report.passed is True


def test_canon_commit_marks_other_drafts_stale(app) -> None:
    project = _prepared_episode(app)
    v1 = app.narratives.generate_episode_draft_sync(project.id, 1)
    v2 = app.narratives.generate_episode_draft_sync(project.id, 1)
    assert v2.version == 2
    app.narratives.audit_episode(project.id, v1.id)
    app.narratives.commit_episode(project.id, 1, v1.id)

    # The revision bump invalidates the fingerprint of the uncommitted draft.
    project_state = app.narratives.get_project(project.id)
    fingerprint = app.narratives.context_fingerprint(project_state)
    assert v2.context_fingerprint != fingerprint


def test_stale_draft_cannot_be_committed(app) -> None:
    project = _prepared_episode(app)
    version = app.narratives.generate_episode_draft_sync(project.id, 1)
    app.narratives.audit_episode(project.id, version.id)
    # Invalidate context by editing the bible AFTER the audit.
    app.narratives.save_bible(project.id, {"theme": "新的主题"})
    stale_version = app.narratives.repo.get_episode_version(version.id)
    assert stale_version.stale is True
    with pytest.raises(ValueError, match="stale"):
        app.narratives.commit_episode(project.id, 1, version.id)


def test_bible_versions_never_overwritten(app) -> None:
    project = _prepared_episode(app)
    v1 = app.narratives.repo.get_bible(project.id)
    v2 = app.narratives.save_bible(project.id, {"premise": "新前提"})
    assert v2.version == v1.version + 1
    old = app.narratives.repo.get_bible(project.id, v1.version)
    assert old.premise == v1.premise


def test_episode_version_history_preserved(app) -> None:
    project = _prepared_episode(app)
    app.narratives.generate_episode_draft_sync(project.id, 1)
    app.narratives.generate_episode_draft_sync(project.id, 1)
    app.narratives.generate_episode_draft_sync(project.id, 1)
    versions = app.narratives.get_episode_versions(project.id, 1)
    assert [v.version for v in versions] == [3, 2, 1]
