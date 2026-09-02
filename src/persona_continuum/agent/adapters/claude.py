from __future__ import annotations

from persona_continuum.agent.models import ModelCapability, SelectionStrategy
from persona_continuum.agent.protocols.streaming_json_cli import StreamingJsonCliAdapter


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
                ModelCapability(
                    id="claude-sonnet-5",
                    display_name="Claude Sonnet 5 (Hybrid Reasoning)",
                    provider="anthropic",
                    supported_reasoning_efforts=["none", "low", "medium", "high", "max"],
                    default_reasoning_effort="high",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="claude-opus-5",
                    display_name="Claude Opus 5 (Deep Reasoning)",
                    provider="anthropic",
                    supported_reasoning_efforts=["none", "low", "medium", "high", "max"],
                    default_reasoning_effort="high",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="claude-haiku-4-5",
                    display_name="Claude Haiku 4.5",
                    provider="anthropic",
                    supported_reasoning_efforts=["none", "low", "medium"],
                    default_reasoning_effort="medium",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="claude-3-7-sonnet-20250219",
                    display_name="Claude 3.7 Sonnet (Hybrid Reasoning)",
                    provider="anthropic",
                    supported_reasoning_efforts=["none", "low", "medium", "high", "max"],
                    default_reasoning_effort="high",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="claude-3-5-haiku-20241022",
                    display_name="Claude 3.5 Haiku",
                    provider="anthropic",
                    supported_reasoning_efforts=["none"],
                    default_reasoning_effort="none",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
            ],
            model_flag="--model",
            reasoning_flag="--max-thinking-tokens",
        )
