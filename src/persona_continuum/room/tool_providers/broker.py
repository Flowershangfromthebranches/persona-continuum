"""Aggregated Room Tool Broker.

One registry fronts every tool source a room can reach:

    RoomToolBroker
    |-- PersonaBuiltinToolProvider   (memory / relationship / runtime state)
    `-- FutureExternalToolProvider   (anything else, same contract)

Two independent facts decide what a model may see:

    provider tool exists        -> availability
    participant.tool_permissions -> permission
    participant.allow_agent_tools -> master switch

Only the intersection is advertised, and only the intersection can execute.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from persona_continuum.room.tool_providers.base import (
    TOOL_NOT_AVAILABLE,
    TOOL_PERMISSION_DENIED,
    ToolExecutionContext,
    ToolProvider,
    tool_error,
)
from persona_continuum.room.tool_providers.persona_builtin import (
    BUILTIN_TOOL_NAMES,
    PersonaBuiltinToolProvider,
)

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum


def _tool_name(definition: dict[str, Any]) -> str:
    function = definition.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(definition.get("name") or "")


class RoomToolBroker:
    """Aggregate every tool provider behind one availability + permission gate."""

    def __init__(
        self,
        continuum: PersonaContinuum | None = None,
        providers: list[ToolProvider] | None = None,
    ) -> None:
        self.continuum = continuum
        self._providers: list[ToolProvider] = list(providers) if providers else []
        if not self._providers and continuum is not None:
            self._providers = [PersonaBuiltinToolProvider(continuum)]
        self._by_id: dict[str, ToolProvider] = {
            provider.provider_id: provider for provider in self._providers
        }

    # -- registry ----------------------------------------------------------
    @property
    def providers(self) -> list[ToolProvider]:
        return list(self._providers)

    def register(self, provider: ToolProvider) -> None:
        existing = self._by_id.get(provider.provider_id)
        if existing is not None:
            self._providers = [p for p in self._providers if p is not existing]
        self._providers.append(provider)
        self._by_id[provider.provider_id] = provider

    def get_provider(self, provider_id: str) -> ToolProvider | None:
        return self._by_id.get(provider_id)

    # -- discovery ---------------------------------------------------------
    async def _definitions_for(self, provider: ToolProvider) -> list[dict[str, Any]]:
        try:
            return list(await provider.get_tool_definitions())
        except Exception:
            # A provider that cannot start simply contributes nothing; the
            # advertised list stays exactly the set that can execute.
            return []

    async def list_available_tool_definitions(
        self,
        *,
        tool_permissions: list[str] | None = None,
        allow_agent_tools: bool = True,
    ) -> list[dict[str, Any]]:
        """Definitions a participant may actually call this turn.

        ``tool_permissions`` semantics:

        - ``None``  -> no allowlist configured, every available tool is offered
        - ``[]``    -> only first-party persona built-ins are offered
        - ``[...]`` -> built-ins plus the named external tools
        """

        if not allow_agent_tools:
            return []
        allowed = None if tool_permissions is None else set(tool_permissions)
        definitions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for provider in self._providers:
            try:
                available = provider.is_available()
            except Exception:
                available = False
            if not available:
                continue
            for definition in await self._definitions_for(provider):
                name = _tool_name(definition)
                if not name or name in seen:
                    continue
                if not provider.builtin and allowed is not None and name not in allowed:
                    continue
                seen.add(name)
                definitions.append(definition)
        return definitions

    async def available_tool_names(self) -> list[str]:
        names: list[str] = []
        for provider in self._providers:
            if not provider.is_available():
                continue
            with contextlib.suppress(Exception):
                names.extend(await provider.list_tools())
        return names

    async def tool_availability_report(
        self,
        *,
        tool_permissions: list[str] | None = None,
        allow_agent_tools: bool = True,
    ) -> dict[str, Any]:
        """Structured availability for the Room UI.

        Distinguishes "the room allows it" from "the provider can run it", so
        the UI can say *why* a tool is missing instead of letting the model
        discover it by failing.
        """

        allowed = None if tool_permissions is None else set(tool_permissions)
        granted: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        seen: set[str] = set()
        available_names = set(await self.available_tool_names())
        for provider in self._providers:
            health = await self._safe_health(provider)
            for name in health.tools or []:
                if name in seen:
                    continue
                seen.add(name)
                permitted = provider.builtin or allowed is None or name in (allowed or set())
                entry = {
                    "name": name,
                    "provider": provider.provider_id,
                    "permitted": bool(permitted) and allow_agent_tools,
                    "available": name in available_names,
                    "provider_status": health.status,
                    "reason": None if name in available_names else health.reason,
                }
                if entry["permitted"] and entry["available"]:
                    granted.append(entry)
                elif permitted:
                    missing.append(entry)

        # A provider that cannot start reports no tools at all, which would
        # leave a granted-but-dead tool silently absent from this report.  The
        # room knows what it granted, so explain every one of those explicitly
        # (§5): the persona must be able to say "this tool is unavailable
        # because its provider is down", not discover it by failing a call.
        for name in sorted(allowed or ()):
            if name in seen or name in available_names:
                continue
            owner = self._provider_for(name)
            health = await self._safe_health(owner) if owner is not None else None
            missing.append(
                {
                    "name": name,
                    "provider": owner.provider_id if owner is not None else "",
                    "permitted": bool(allow_agent_tools),
                    "available": False,
                    "provider_status": health.status if health is not None else "unknown",
                    "reason": (
                        (health.reason if health is not None else None)
                        or "no registered provider implements this tool"
                    ),
                }
            )
        return {
            "allow_agent_tools": bool(allow_agent_tools),
            "granted": granted,
            "missing": missing,
            "granted_names": [item["name"] for item in granted],
            "missing_names": [item["name"] for item in missing],
        }

    # -- execution ---------------------------------------------------------
    async def execute_tool(
        self,
        persona_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        branch_id: str = "main",
        participant_id: str = "",
        room_id: str = "",
        tool_permissions: list[str] | None = None,
        allow_agent_tools: bool = True,
    ) -> Any:
        """Execute one tool through the same gate that built the definitions."""

        arguments = dict(arguments or {})
        context = ToolExecutionContext(
            persona_id=str(persona_id or ""),
            participant_id=str(participant_id or ""),
            room_id=str(room_id or ""),
            branch_id=str(branch_id or "main"),
            allowed_tools=tuple(tool_permissions or ()),
        )
        if not allow_agent_tools:
            return tool_error(
                TOOL_PERMISSION_DENIED,
                tool=tool_name,
                message="This participant has agent tools disabled.",
            )

        provider = self._provider_for(tool_name)
        if provider is None:
            return tool_error(
                TOOL_NOT_AVAILABLE,
                tool=tool_name,
                message=f"No registered tool provider implements '{tool_name}'.",
            )

        # Permission is room policy: an external tool needs an explicit grant.
        # First-party persona tools are the persona's own self-model and are
        # always allowed once agent tools are enabled.
        if (
            not provider.builtin
            and tool_permissions is not None
            and tool_name not in set(tool_permissions)
        ):
            return tool_error(
                TOOL_PERMISSION_DENIED,
                tool=tool_name,
                provider=provider.provider_id,
                message=(
                    f"This participant is not permitted to call '{tool_name}'. "
                    f"Permitted external tools: {', '.join(sorted(tool_permissions)) or 'none'}."
                ),
            )

        try:
            if not provider.is_available():
                reason = ""
                with contextlib.suppress(Exception):
                    reason = str(getattr(provider, "unavailable_reason", lambda: "")() or "")
                return tool_error(
                    "tool_provider_unhealthy",
                    tool=tool_name,
                    provider=provider.provider_id,
                    message=(
                        f"Tool provider '{provider.provider_id}' is not available."
                        + (f" {reason}" if reason else "")
                    ),
                )
            return await provider.execute(tool_name, arguments, context)
        except Exception as exc:
            return tool_error(
                "tool_execution_failed",
                tool=tool_name,
                provider=provider.provider_id,
                message=f"Tool execution failed: {exc}",
            )

    def _provider_for(self, tool_name: str) -> ToolProvider | None:
        # First provider that can route the name wins.
        for provider in self._providers:
            with contextlib.suppress(Exception):
                if provider.can_handle(tool_name):
                    return provider
        return None

    # -- health ------------------------------------------------------------
    async def _safe_health(self, provider: ToolProvider) -> Any:
        from persona_continuum.room.tool_providers.base import ProviderHealth

        try:
            return await provider.health()
        except Exception as exc:  # pragma: no cover - defensive
            return ProviderHealth(
                provider_id=provider.provider_id,
                display_name=getattr(provider, "display_name", ""),
                status="error",
                available=False,
                healthy=False,
                reason=str(exc),
            )

    async def health_report(self) -> dict[str, Any]:
        """Start providers and report real readiness (used by room preflight)."""

        providers: list[dict[str, Any]] = []
        total_tools = 0
        ready = True
        for provider in self._providers:
            health = await self._safe_health(provider)
            payload = health.as_dict()
            payload["builtin"] = bool(provider.builtin)
            providers.append(payload)
            total_tools += int(health.tool_count or 0)
            if not provider.builtin and not health.is_ready:
                ready = False
        return {
            "ready": ready,
            "total_tools": total_tools,
            "providers": providers,
            "summary": self._summarize(providers),
        }

    async def preflight(self) -> dict[str, Any]:
        """Non-blocking environment readiness used while a room starts."""

        providers: list[dict[str, Any]] = []
        ready = True
        for provider in self._providers:
            try:
                health = await provider.preflight()
            except Exception as exc:  # pragma: no cover - defensive
                from persona_continuum.room.tool_providers.base import ProviderHealth

                health = ProviderHealth(
                    provider_id=provider.provider_id,
                    status="error",
                    available=False,
                    healthy=False,
                    reason=str(exc),
                )
            payload = health.as_dict()
            payload["builtin"] = bool(provider.builtin)
            providers.append(payload)
            if not provider.builtin and not health.is_ready:
                ready = False
        return {
            "ready": ready,
            "total_tools": sum(int(item.get("tool_count") or 0) for item in providers),
            "providers": providers,
            "summary": self._summarize(providers),
        }

    @staticmethod
    def _summarize(providers: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for item in providers:
            name = item.get("display_name") or item.get("provider_id")
            status = item.get("status")
            if status == "ready":
                lines.append(f"{name}: {item.get('tool_count', 0)} tools ready")
            elif status == "starting":
                lines.append(f"{name}: starting on first use ({item.get('tool_count', 0)} tools)")
            else:
                reason = item.get("reason") or status
                lines.append(f"{name}: unavailable ({reason})")
        return lines

    # -- prompt ------------------------------------------------------------
    @staticmethod
    def render_tool_block(definitions: list[dict[str, Any]]) -> str:
        """Build the AVAILABLE TOOLS section injected into a room prompt.

        Only real availability ∩ permissions reaches this list, so the list is
        authoritative: a model must never be told to explain away missing
        tools here -- it simply cannot see them.
        """

        header = "AVAILABLE TOOLS FOR THIS TURN:"
        if not definitions:
            body = "- none"
        else:
            lines = []
            for definition in definitions:
                name = _tool_name(definition)
                function = definition.get("function") or {}
                description = str(function.get("description") or "").strip()
                lines.append(f"- {name}: {description}" if description else f"- {name}")
            body = "\n".join(lines)
        return f"{header}\n{body}"

    async def compose_tool_block(
        self,
        *,
        tool_permissions: list[str] | None = None,
        allow_agent_tools: bool = True,
    ) -> str:
        definitions = await self.list_available_tool_definitions(
            tool_permissions=tool_permissions,
            allow_agent_tools=allow_agent_tools,
        )
        return self.render_tool_block(definitions)

    # -- lifecycle ---------------------------------------------------------
    async def warmup(self) -> None:
        """Best-effort background start so the first tool call is not slow."""

        for provider in self._providers:
            if not provider.is_available():
                continue
            with contextlib.suppress(Exception):
                await provider.get_tool_definitions()

    async def shutdown(self) -> None:
        for provider in self._providers:
            with contextlib.suppress(Exception):
                await provider.shutdown()

    def shutdown_sync(self) -> None:
        for provider in self._providers:
            kill = getattr(provider, "shutdown_sync", None)
            if callable(kill):
                with contextlib.suppress(Exception):
                    kill()


__all__ = [
    "BUILTIN_TOOL_NAMES",
    "RoomToolBroker",
]
