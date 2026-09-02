"""Shared contracts for the Room Tool Broker provider registry.

``tool_permissions`` and ``tool_availability`` are different facts and the
registry keeps them apart:

- *permission* is a room policy: what this participant is allowed to call.
- *availability* is a runtime fact: what a provider can actually execute.

Only ``availability AND permission`` may reach a model, and only that same
intersection may be executed.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

# --- structured tool error codes -------------------------------------------
# Every code is distinct on purpose.  A persona that cannot tell "no such
# tool" from "you may not use it" from "the engine is down" will read all
# three as a refusal and abandon the consultation.
TOOL_NOT_AVAILABLE = "tool_not_available"
TOOL_PERMISSION_DENIED = "tool_permission_denied"
TOOL_PROVIDER_UNHEALTHY = "tool_provider_unhealthy"
TOOL_EXECUTION_FAILED = "tool_execution_failed"
TOOL_TIMEOUT = "tool_timeout"
INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"

TOOL_ERROR_CODES: frozenset[str] = frozenset(
    {
        TOOL_NOT_AVAILABLE,
        TOOL_PERMISSION_DENIED,
        TOOL_PROVIDER_UNHEALTHY,
        TOOL_EXECUTION_FAILED,
        TOOL_TIMEOUT,
        INVALID_TOOL_ARGUMENTS,
    }
)

# Provider status vocabulary shared by preflight, health and the Room UI.
PROVIDER_READY = "ready"
PROVIDER_STARTING = "starting"
PROVIDER_DISABLED = "disabled"
PROVIDER_UNAVAILABLE = "unavailable"
PROVIDER_ERROR = "error"

_INVALID_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_tool_name(name: str) -> str:
    """Map an arbitrary provider tool name onto ``[A-Za-z0-9_-]``.

    Agent wire protocols reject function names outside that set, so a provider
    may advertise a sanitized name while still executing the original one.
    """

    return _INVALID_TOOL_NAME.sub("_", str(name or "")).strip("_") or "unnamed_tool"


def tool_error(
    code: str,
    *,
    tool: str = "",
    message: str = "",
    provider: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Build the one structured error payload every provider returns.

    ``error``/``tool`` are kept at the top level so existing adapters and
    transcript renderers keep working unchanged.
    """

    payload: dict[str, Any] = {
        "ok": False,
        "error": code,
        "tool": tool,
        "message": message or code,
    }
    if provider:
        payload["provider"] = provider
    payload.update(extra)
    return payload


def is_tool_error(payload: Any) -> bool:
    return isinstance(payload, dict) and str(payload.get("error") or "") in TOOL_ERROR_CODES


@dataclass(slots=True)
class ToolExecutionContext:
    """Who is calling a tool, carried from the room down to the provider."""

    persona_id: str = ""
    participant_id: str = ""
    room_id: str = ""
    branch_id: str = "main"
    # Participant policy, used by the broker before dispatch.  Providers may
    # read it for audit logging but must never widen their own permissions.
    allowed_tools: tuple[str, ...] = ()


@dataclass(slots=True)
class ProviderHealth:
    """Runtime availability of one provider, safe to show in the Room UI."""

    provider_id: str
    display_name: str = ""
    status: str = PROVIDER_UNAVAILABLE
    # True when the provider can be started at all (binary present, enabled).
    available: bool = False
    # True when a live session is currently usable.
    healthy: bool = False
    tool_count: int = 0
    tools: list[str] = field(default_factory=list)
    # Human readable reason; shown verbatim in the UI when unavailable.
    reason: str | None = None
    detail: str = ""

    @property
    def is_ready(self) -> bool:
        return bool(self.available and self.healthy)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ToolProvider(ABC):
    """One pluggable tool source behind the aggregated Room Tool Broker."""

    provider_id: str = "abstract"
    display_name: str = ""
    # First-party providers expose a persona's own self-inspection tools and
    # bypass the external permission allowlist.  Third-party providers
    # (external MCP sources, etc.) require an explicit permission entry.
    builtin: bool = False

    @abstractmethod
    def is_available(self) -> bool:
        """Whether this provider can run at all in the current environment."""

    @abstractmethod
    async def list_tools(self) -> list[str]:
        """Names of the tools this provider can currently execute."""

    @abstractmethod
    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Agent-protocol tool definitions for the currently available tools."""

    @abstractmethod
    def can_handle(self, tool_name: str) -> bool:
        """Whether this provider owns ``tool_name`` (synchronous routing test)."""

    @abstractmethod
    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> Any:
        """Execute ``tool_name`` and return structured content or a tool error."""

    @abstractmethod
    async def health(self) -> ProviderHealth:
        """Current health; may lazily start the provider when configured to."""

    async def preflight(self) -> ProviderHealth:
        """Environment-level readiness check used before a room starts."""

        return await self.health()

    async def shutdown(self) -> None:
        """Release provider resources.  Safe to call more than once."""

        return None


def openai_function_definition(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one OpenAI-compatible function tool definition."""

    schema = parameters if isinstance(parameters, dict) and parameters else {
        "type": "object",
        "properties": {},
    }
    return {
        "type": "function",
        "function": {
            "name": sanitize_tool_name(name),
            "description": str(description or name),
            "parameters": schema,
        },
    }
