from persona_continuum.agent.adapters.claude import ClaudeCodeAdapter
from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.adapters.command_code import CommandCodeAdapter

try:
    from persona_continuum.agent.adapters.cursor import (  # type: ignore[import-not-found]
        CursorAdapter,
    )
except ImportError:  # Cursor adapter module not present yet; register nothing.
    CursorAdapter = None
from persona_continuum.agent.adapters.fake import FakeAgentAdapter
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

__all__ = [
    "ClaudeCodeAdapter",
    "CodeBuddyAdapter",
    "CodexAdapter",
    "CommandCodeAdapter",
    "CopilotAdapter",
    "CursorAdapter",
    "DeepSeekHarnessAdapter",
    "FakeAgentAdapter",
    "GeminiCliAdapter",
    "GrokBuildAdapter",
    "KimiAdapter",
    "OpenCodeAdapter",
    "QoderAdapter",
    "QwenAdapter",
    "WorkBuddyAdapter",
]
