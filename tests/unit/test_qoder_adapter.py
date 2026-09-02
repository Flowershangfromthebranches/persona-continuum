from __future__ import annotations

import pytest

from persona_continuum.agent.adapters import other_vendors
from persona_continuum.agent.adapters.other_vendors import QoderAdapter
from persona_continuum.agent.models import ReasoningCapabilityMode, ResearchVerificationStatus

SAMPLE_LISTING = """MODEL
Auto
Qwen3.8-Max
Qwen3.8-Flash
DeepSeek-V4-Pro
GLM-5.3
Kimi-K2.7-Code
MiniMax-M2.7
"""


def test_qoder_parse_models_skips_header_and_infers_provider() -> None:
    models = QoderAdapter()._parse_models(SAMPLE_LISTING)
    ids = [m.id for m in models]
    assert "MODEL" not in ids
    assert ids == [
        "Auto",
        "Qwen3.8-Max",
        "Qwen3.8-Flash",
        "DeepSeek-V4-Pro",
        "GLM-5.3",
        "Kimi-K2.7-Code",
        "MiniMax-M2.7",
    ]
    providers = {m.id: m.provider for m in models}
    assert providers["Qwen3.8-Max"] == "alibaba"
    assert providers["DeepSeek-V4-Pro"] == "deepseek"
    assert providers["GLM-5.3"] == "zhipu"
    assert providers["Kimi-K2.7-Code"] == "moonshot"
    assert providers["MiniMax-M2.7"] == "minimax"
    assert providers["Auto"] == "qoder"
    # Official CLI listings must classify as native startup-selection
    # evidence; "dynamic" would fail closed and hide the effort ladder.
    assert all(m.source == "official_cli" for m in models)
    assert all(
        m.reasoning_capability.mode == ReasoningCapabilityMode.NATIVE_EFFORT
        and m.reasoning_capability.verified
        and m.reasoning_capability.supported_efforts == ["none", "low", "medium", "high"]
        for m in models
    )


@pytest.mark.anyio
async def test_qoder_list_models_merges_logged_in_editions_and_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(argv: list[str], **_kwargs: object) -> tuple[int, str, str]:
        binary = argv[0]
        if binary.endswith("qodercli"):
            # International edition: not authenticated.
            return 1, "", "Not logged in. Run " + "qodercli login" + " to authenticate."
        return 0, SAMPLE_LISTING, ""

    monkeypatch.setattr(other_vendors, "safe_exec_cmd", fake_exec)
    adapter = QoderAdapter()
    monkeypatch.setattr(
        adapter,
        "_candidate_binaries",
        lambda: ["/bin/qodercli", "/bin/qoderclicn"],
    )

    models = await adapter.list_models()
    assert [m.id for m in models] == [
        "Auto",
        "Qwen3.8-Max",
        "Qwen3.8-Flash",
        "DeepSeek-V4-Pro",
        "GLM-5.3",
        "Kimi-K2.7-Code",
        "MiniMax-M2.7",
    ]
    # Sessions must run on the edition that actually served the listing.
    assert adapter._resolved_binary == "/bin/qoderclicn"


@pytest.mark.anyio
async def test_qoder_list_models_falls_back_when_no_edition_is_logged_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(argv: list[str], **_kwargs: object) -> tuple[int, str, str]:
        return 1, "", "Not logged in. Run " + "qodercli login" + " to authenticate."

    monkeypatch.setattr(other_vendors, "safe_exec_cmd", fake_exec)
    adapter = QoderAdapter()
    monkeypatch.setattr(adapter, "_candidate_binaries", lambda: ["/bin/qodercli"])

    models = await adapter.list_models()
    ids = {m.id for m in models}
    assert adapter._resolved_binary is None
    # The stale 4-model scaffold must be gone: current verified catalog.
    assert {"Qwen3.8-Max", "GLM-5.3", "Kimi-K2.7-Code", "MiniMax-M2.7"} <= ids
    assert all(m.source == "config" for m in models)


def test_qoder_declares_native_web_research() -> None:
    adapter = QoderAdapter()
    research = adapter._research_capability
    assert research.mode == "native_cli"
    assert research.verification_status == ResearchVerificationStatus.DECLARED
    assert research.discovers_sources is True
    assert research.reads_sources is True
