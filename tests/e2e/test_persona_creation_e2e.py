"""Persona Creation UI/API contract tests.

These tests are intentionally added with the implementation and are not part
of the current validation run.  They become executable after explicit
validation approval and a configured ResearchToolBroker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WEB_ROOT = Path("src/persona_continuum/web")


def _read(relative_path: str) -> str:
    return (WEB_ROOT / relative_path).read_text(encoding="utf-8")


@pytest.mark.e2e
def test_persona_page_create_persona_opens_runtime_wizard() -> None:
    assert "创建人格" in _read("static/index.html")


@pytest.mark.e2e
def test_persona_creator_only_lists_ready_cli() -> None:
    script = _read("static/app.js")
    assert "getSelectableLocalAgents" in script
    assert "persona-creator-source" in _read("static/index.html")


@pytest.mark.e2e
def test_persona_creator_only_lists_connected_api() -> None:
    script = _read("static/app.js")
    assert "getSelectableApiAgents" in script
    assert "getSelectableAgentsForSource" in script


@pytest.mark.e2e
def test_persona_creator_model_dynamic() -> None:
    script = _read("static/app.js")
    assert "getRuntimeModels" in script
    assert "reasoning_capability" in script
    assert "supported_efforts" in script


@pytest.mark.e2e
def test_persona_creator_reasoning_dynamic() -> None:
    script = _read("static/app.js")
    assert "getRuntimeReasoningOptions" in script
    assert "default_reasoning_effort" in script
    assert "effortPattern" in script
    assert 'new Set(["none", "low", "medium", "high", "xhigh", "max"])' not in script


@pytest.mark.e2e
def test_api_reasoning_probe_renders_runtime_discovery() -> None:
    script = _read("static/app.js")
    assert "apiProfileReasoningSummary" in script
    assert "apiProfileRuntimeAgent" in script
    assert "正在探测模型与 Reasoning 能力" in script
    assert "Reasoning ${summary.reportedCount}/${summary.modelCount}" in script
    load_agents_body = script.split("async function loadAgents", 1)[1].split(
        "async function revalidateAgentResearch", 1
    )[0]
    assert "renderApiProfiles();" in load_agents_body


@pytest.mark.e2e
def test_runtime_model_options_expose_reasoning_capability() -> None:
    script = _read("static/app.js")
    assert "runtimeModelOptionLabel" in script
    assert "runtimeReasoningNotice" in script
    assert "同一 Provider 有" in script


@pytest.mark.e2e
def test_parallel_world_missing_persona_confirmation_flow() -> None:
    script = _read("static/app.js")
    assert "/api/worlds/persona-completion/confirm" in script
    assert "requires_persona_completion_confirmation" in _read("api.py")
