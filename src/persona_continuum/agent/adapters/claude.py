from __future__ import annotations

from persona_continuum.agent.context_capability import default_model_capability_registry
from persona_continuum.agent.models import ModelCapability, SelectionStrategy
from persona_continuum.agent.protocols.streaming_json_cli import StreamingJsonCliAdapter


def _claude_fallback_model(
    model_id: str,
    display_name: str,
    *,
    efforts: list[str],
    default_effort: str,
    retired: bool = False,
) -> ModelCapability:
    record = default_model_capability_registry().lookup(model_id)
    return ModelCapability(
        id=model_id,
        display_name=display_name,
        provider="anthropic",
        supported_reasoning_efforts=efforts,
        default_reasoning_effort=default_effort,
        context_window=record.native_context_window if record is not None else None,
        source="official_capability_table",
        reasoning_selection=SelectionStrategy.STARTUP,
        selectable=not retired,
    )


class ClaudeCodeAdapter(StreamingJsonCliAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="claude_code",
            name="Claude Code",
            binary_candidates=[
                "claude",
                "~/.npm-global/bin/claude",
                "~/.claude/bin/claude",
                "~/.local/bin/claude",
                "/opt/homebrew/bin/claude",
                "/usr/local/bin/claude",
            ],
            version_args=["--version"],
            exec_args=["--stream", "json"],
            default_models=[
                _claude_fallback_model(
                    "claude-sonnet-5",
                    "Claude Sonnet 5 (Hybrid Reasoning)",
                    efforts=["none", "low", "medium", "high", "max"],
                    default_effort="high",
                ),
                _claude_fallback_model(
                    "claude-opus-5",
                    "Claude Opus 5 (Deep Reasoning)",
                    efforts=["none", "low", "medium", "high", "max"],
                    default_effort="high",
                ),
                _claude_fallback_model(
                    "claude-haiku-4-5",
                    "Claude Haiku 4.5",
                    efforts=["none", "low", "medium"],
                    default_effort="medium",
                ),
                _claude_fallback_model(
                    "claude-3-7-sonnet-20250219",
                    "Claude 3.7 Sonnet (retired fallback)",
                    efforts=["none", "low", "medium", "high", "max"],
                    default_effort="high",
                    retired=True,
                ),
                _claude_fallback_model(
                    "claude-3-5-haiku-20241022",
                    "Claude 3.5 Haiku (retired fallback)",
                    efforts=["none"],
                    default_effort="none",
                    retired=True,
                ),
            ],
            model_flag="--model",
            reasoning_flag="--max-thinking-tokens",
        )
