"""Automatic prompt-transport audit for every first-party adapter."""

from __future__ import annotations

from persona_continuum.agent.adapters.claude import ClaudeCodeAdapter
from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.adapters.command_code import CommandCodeAdapter
from persona_continuum.agent.adapters.gemini import GeminiCliAdapter
from persona_continuum.agent.adapters.grok import GrokBuildAdapter
from persona_continuum.agent.adapters.opencode import OpenCodeAdapter
from persona_continuum.agent.adapters.other_vendors import (
    CodeBuddyAdapter,
    CopilotAdapter,
    DeepSeekHarnessAdapter,
    KimiAdapter,
    QoderAdapter,
    QwenAdapter,
    WorkBuddyAdapter,
)
from persona_continuum.agent.prompt_transport import resolve_prompt_transport_capability

# Expected modes from inspecting send()/spawn.  Gemini streaming is stdin when
# the resolved binary is agy; without a binary the class still declares argv
# via exec_args=["-p"].
EXPECTED = {
    "codex": "stdin",
    "grok": "stream",
    "claude_code": "stdin",
    "gemini_cli": "argv",  # overridden below when a local agy binary is resolved
    "opencode": "stream",
    "command_code": "argv",
    "qwen": "stdin",
    "kimi": "stdin",
    "copilot": "stdin",
    "qoder": "argv",
    "codebuddy": "argv",
    "workbuddy": "argv",
    "deepseek_harness": "stdin",
}


def _adapters() -> list[object]:
    return [
        CodexAdapter(),
        GrokBuildAdapter(),
        ClaudeCodeAdapter(),
        GeminiCliAdapter(),
        OpenCodeAdapter(),
        CommandCodeAdapter(),
        QwenAdapter(),
        KimiAdapter(),
        CopilotAdapter(),
        QoderAdapter(),
        CodeBuddyAdapter(),
        WorkBuddyAdapter(),
        DeepSeekHarnessAdapter(),
    ]


def test_prompt_transport_audit_table() -> None:
    rows = []
    for adapter in _adapters():
        cap = resolve_prompt_transport_capability(adapter)
        adapter_id = str(getattr(adapter, "adapter_id", type(adapter).__name__))
        rows.append(
            {
                "adapter": adapter_id,
                "prompt_transport_mode": cap.transport_mode,
                "max_prompt_bytes": cap.max_prompt_bytes,
                "safe_prompt_bytes": cap.safe_prompt_bytes,
                "supports_large_prompt": cap.supports_large_prompt,
                "source": cap.source,
            }
        )
        expected = EXPECTED[adapter_id]
        if adapter_id == "gemini_cli" and getattr(adapter, "_is_agy_binary", lambda: False)():
            expected = "stdin"
        assert cap.transport_mode == expected, rows[-1]
        if cap.transport_mode in {"stdin", "stream", "rpc"}:
            assert cap.supports_large_prompt is True
        if cap.transport_mode == "argv":
            assert cap.supports_large_prompt is False
            assert cap.safe_prompt_bytes <= 64 * 1024
    assert {row["adapter"] for row in rows} == set(EXPECTED)


def test_agy_binary_switches_gemini_to_stdin() -> None:
    adapter = GeminiCliAdapter()
    adapter._resolved_binary = "/synthetic/agy"
    cap = resolve_prompt_transport_capability(adapter)
    assert cap.transport_mode == "stdin"
    assert cap.supports_large_prompt is True
