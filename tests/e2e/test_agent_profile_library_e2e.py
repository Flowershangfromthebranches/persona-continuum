"""Agent/Profile Library UI and World classification contracts.

These tests are written with the feature and intentionally remain unexecuted
until the user explicitly authorizes validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WEB_ROOT = Path("src/persona_continuum/web")


def _read(relative_path: str) -> str:
    return (WEB_ROOT / relative_path).read_text(encoding="utf-8")


@pytest.mark.e2e
def test_agent_profile_library_page_has_structured_filters() -> None:
    html = _read("static/index.html")
    assert "人物 / 档案库" in html
    assert "profile-type-filter" in html
    assert "profile-status-filter" in html
    assert "创建档案" in html


@pytest.mark.e2e
def test_profile_cards_show_summary_and_enrichment_action() -> None:
    script = _read("static/app.js")
    assert "data-enrich-profile" in script
    assert "summary" in script
    assert "/api/profiles/" in script
    assert "使命" in script
    assert "制度目标" in script


@pytest.mark.e2e
def test_world_entity_classification_shows_agents_and_non_agents() -> None:
    html = _read("static/index.html")
    script = _read("static/app.js")
    assert "world-agent-classification-container" in html
    assert "world-non-agent-entities-container" in html
    assert "Actor 补全引擎" in html
    assert "/api/worlds/actor-completion/confirm" in script


@pytest.mark.e2e
def test_profile_enrichment_uses_async_job_api() -> None:
    script = _read("static/app.js")
    assert "requested_scope" in script
    assert "/enrich" in script
    assert "旧版本仍保留可追溯" in script
