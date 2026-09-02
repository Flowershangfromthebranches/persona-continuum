"""Room Tool Broker: availability ∩ permission, and distinct error codes.

These tests exist because the previous implementation advertised tools the
broker could not execute, and collapsed every failure into a single
``room_tool_permission_denied``.  Both behaviours made "the provider is
down" indistinguishable from a user refusing a permission request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from persona_continuum.room.tool_providers.base import (
    PROVIDER_READY,
    PROVIDER_UNAVAILABLE,
    ProviderHealth,
    ToolExecutionContext,
    ToolProvider,
    is_tool_error,
    openai_function_definition,
)
from persona_continuum.room.tool_providers.broker import RoomToolBroker
from persona_continuum.room.tool_providers.persona_builtin import (
    BUILTIN_TOOL_NAMES,
    PersonaBuiltinToolProvider,
)

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum

# A deliberately narrow grant: only a subset of the external provider's
# tools (§11).
ZIPING_PERMISSIONS = ["bazi", "bazi_dayun", "bazi_pillars_resolve"]

EXTERNAL_TOOL_NAMES = (
    "astrology",
    "bazi",
    "bazi_dayun",
    "bazi_pillars_resolve",
    "ziwei",
    "ziwei_horoscope",
    "ziwei_flying_star",
    "liuyao",
    "meihua",
    "tarot",
    "taiyi",
    "almanac",
    "qimen",
    "daliuren",
    "xiaoliuren",
)


class FakeExternalProvider(ToolProvider):
    """Stand-in for an external tool provider (e.g. a future MCP source).

    It owns no subprocess, so the broker's gating logic -- not the provider's
    availability on this machine -- is what these tests exercise.
    """

    provider_id = "fake_external"
    display_name = "Fake external provider"
    builtin = False

    def __init__(
        self,
        tools: tuple[str, ...] = EXTERNAL_TOOL_NAMES,
        *,
        available: bool = True,
        fail: bool = False,
    ) -> None:
        self._tools = tuple(tools)
        self._available = available
        self._fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.starts = 0

    # -- availability ------------------------------------------------------
    def is_available(self) -> bool:
        return self._available

    def unavailable_reason(self) -> str | None:
        return None if self._available else "simulated missing engine"

    async def list_tools(self) -> list[str]:
        self.starts += 1
        return list(self._tools) if self._available else []

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            openai_function_definition(name, f"fake {name}")
            for name in await self.list_tools()
        ]

    def can_handle(self, tool_name: str) -> bool:
        return tool_name in self._tools

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        if self._fail:
            raise RuntimeError("engine exploded")
        return {
            "ok": True,
            "status": "success",
            "tool": tool_name,
            "structuredContent": {"chart": tool_name, "persona_id": context.persona_id},
        }

    async def health(self) -> ProviderHealth:
        tools = await self.list_tools()
        if not self._available:
            return ProviderHealth(
                provider_id=self.provider_id,
                display_name=self.display_name,
                status=PROVIDER_UNAVAILABLE,
                available=False,
                healthy=False,
                reason="simulated missing engine",
            )
        return ProviderHealth(
            provider_id=self.provider_id,
            display_name=self.display_name,
            status=PROVIDER_READY,
            available=True,
            healthy=True,
            tool_count=len(tools),
            tools=list(tools),
        )

    async def preflight(self) -> ProviderHealth:
        """Environment check that must not count as a start."""

        if not self._available:
            return ProviderHealth(
                provider_id=self.provider_id,
                display_name=self.display_name,
                status=PROVIDER_UNAVAILABLE,
                available=False,
                healthy=False,
                reason="simulated missing engine",
            )
        return ProviderHealth(
            provider_id=self.provider_id,
            display_name=self.display_name,
            status="starting",
            available=True,
            healthy=False,
            tool_count=len(self._tools),
            tools=list(self._tools),
            detail="resolved; starts on first use",
        )


def _names(definitions: list[dict[str, Any]]) -> set[str]:
    return {definition["function"]["name"] for definition in definitions}


class _StubContinuum:
    """Discovery-only stand-in for the application container.

    The built-in provider only needs a continuum to be present to advertise
    its tools; these tests never execute a built-in tool, so no behaviour is
    stubbed out.
    """


def _broker(
    provider: FakeExternalProvider, *, with_builtins: bool = True
) -> RoomToolBroker:
    # Mirrors the real registry: first-party built-ins plus one external
    # engine.  `with_builtins=False` isolates the external gating path when a
    # test needs "no tools at all" to be reachable.
    providers: list[ToolProvider] = []
    if with_builtins:
        providers.append(
            PersonaBuiltinToolProvider(cast("PersonaContinuum", _StubContinuum()))
        )
    providers.append(provider)
    return RoomToolBroker(continuum=None, providers=providers)


# -- §7 availability ∩ permission ------------------------------------------


@pytest.mark.anyio
async def test_only_permitted_and_available_tools_are_advertised() -> None:
    broker = _broker(FakeExternalProvider())

    definitions = await broker.list_available_tool_definitions(
        tool_permissions=["bazi", "ziwei"], allow_agent_tools=True
    )

    names = _names(definitions)
    assert set(BUILTIN_TOOL_NAMES).issubset(names)
    assert {"bazi", "ziwei"}.issubset(names)
    # Permitted but not granted, and granted but not permitted, both stay out.
    assert "tarot" not in names
    assert "qimen" not in names


@pytest.mark.anyio
async def test_ziping_sees_only_its_own_three_tools() -> None:
    """§11: a restricted participant must not be handed every external tool."""

    broker = _broker(FakeExternalProvider())

    definitions = await broker.list_available_tool_definitions(
        tool_permissions=list(ZIPING_PERMISSIONS), allow_agent_tools=True
    )

    external = _names(definitions) - set(BUILTIN_TOOL_NAMES)
    assert external == set(ZIPING_PERMISSIONS)


@pytest.mark.anyio
async def test_empty_permission_list_grants_builtins_only() -> None:
    broker = _broker(FakeExternalProvider())

    definitions = await broker.list_available_tool_definitions(
        tool_permissions=[], allow_agent_tools=True
    )

    assert _names(definitions) == set(BUILTIN_TOOL_NAMES)


@pytest.mark.anyio
async def test_disabled_agent_tools_yield_no_tools_at_all() -> None:
    broker = _broker(FakeExternalProvider())

    definitions = await broker.list_available_tool_definitions(
        tool_permissions=list(EXTERNAL_TOOL_NAMES), allow_agent_tools=False
    )

    assert definitions == []


# -- §5 availability is not permission -------------------------------------


@pytest.mark.anyio
async def test_unavailable_provider_tools_never_reach_the_model() -> None:
    """A down provider must shrink the tool list, not fail the first call."""

    broker = _broker(FakeExternalProvider(available=False))

    definitions = await broker.list_available_tool_definitions(
        tool_permissions=["bazi", "ziwei"], allow_agent_tools=True
    )

    assert _names(definitions) == set(BUILTIN_TOOL_NAMES)


@pytest.mark.anyio
async def test_availability_report_explains_why_a_tool_is_missing() -> None:
    broker = _broker(FakeExternalProvider(available=False))

    report = await broker.tool_availability_report(tool_permissions=["bazi"])

    # Built-ins stay granted; only the external tool is missing.
    assert set(BUILTIN_TOOL_NAMES).issubset(set(report["granted_names"]))
    assert "bazi" not in report["granted_names"]
    missing = {item["name"]: item for item in report["missing"]}
    assert "bazi" in missing
    assert missing["bazi"]["permitted"] is True
    assert missing["bazi"]["available"] is False
    assert missing["bazi"]["reason"] == "simulated missing engine"


# -- §9 error semantics -----------------------------------------------------


@pytest.mark.anyio
async def test_permitted_tool_executes() -> None:
    provider = FakeExternalProvider()
    broker = _broker(provider)

    result = await broker.execute_tool(
        "ziping",
        "bazi",
        {"solar_datetime": "1990-05-20T14:30:00+08:00"},
        tool_permissions=list(ZIPING_PERMISSIONS),
        allow_agent_tools=True,
    )

    assert result["ok"] is True
    assert result["structuredContent"]["chart"] == "bazi"
    assert provider.calls[0][0] == "bazi"


@pytest.mark.anyio
async def test_unpermitted_tool_is_permission_denied_not_permanently_broken() -> None:
    """§12: a permitted tool executes; an unpermitted one gets its own code."""

    broker = _broker(FakeExternalProvider())

    result = await broker.execute_tool(
        "ziping",
        "tarot",
        {},
        tool_permissions=list(ZIPING_PERMISSIONS),
        allow_agent_tools=True,
    )

    assert is_tool_error(result)
    assert result["error"] == "tool_permission_denied"
    assert result["error"] != "room_tool_permission_denied"
    assert "tarot" in result["message"]


@pytest.mark.anyio
async def test_unavailable_provider_is_reported_as_unhealthy() -> None:
    broker = _broker(FakeExternalProvider(available=False))

    result = await broker.execute_tool(
        "ziping", "bazi", {}, tool_permissions=list(ZIPING_PERMISSIONS)
    )

    assert is_tool_error(result)
    assert result["error"] == "tool_provider_unhealthy"
    assert result["provider"] == "fake_external"


@pytest.mark.anyio
async def test_unknown_tool_is_not_available() -> None:
    broker = _broker(FakeExternalProvider())

    result = await broker.execute_tool("ziping", "not_a_tool", {}, tool_permissions=[])

    assert is_tool_error(result)
    assert result["error"] == "tool_not_available"


@pytest.mark.anyio
async def test_execution_failure_is_its_own_code() -> None:
    broker = _broker(FakeExternalProvider(fail=True))

    result = await broker.execute_tool(
        "ziping", "bazi", {}, tool_permissions=list(ZIPING_PERMISSIONS)
    )

    assert is_tool_error(result)
    assert result["error"] == "tool_execution_failed"
    assert "engine exploded" in result["message"]


@pytest.mark.anyio
async def test_disabled_agent_tools_deny_execution() -> None:
    provider = FakeExternalProvider()
    broker = _broker(provider)

    result = await broker.execute_tool(
        "ziping", "bazi", {}, tool_permissions=list(ZIPING_PERMISSIONS), allow_agent_tools=False
    )

    assert is_tool_error(result)
    assert result["error"] == "tool_permission_denied"
    assert provider.calls == []


def test_error_codes_are_all_distinct() -> None:
    """§9: six failure modes must never collapse into one."""

    codes = {
        "tool_not_available",
        "tool_permission_denied",
        "tool_provider_unhealthy",
        "tool_execution_failed",
        "tool_timeout",
        "invalid_tool_arguments",
    }
    assert len(codes) == 6


# -- §6 preflight / §10 prompt injection ------------------------------------


@pytest.mark.anyio
async def test_preflight_does_not_start_the_provider() -> None:
    """Room startup must not pay provider startup cost (§4, §6)."""

    provider = FakeExternalProvider()
    broker = _broker(provider)

    report = await broker.preflight()

    assert provider.starts == 0
    assert report["ready"] is False  # "starting", not "ready"
    statuses = {item["provider_id"]: item["status"] for item in report["providers"]}
    assert statuses["fake_external"] == "starting"
    assert any("starting" in line for line in report["summary"])


@pytest.mark.anyio
async def test_preflight_reports_unavailable_reason_for_a_missing_provider() -> None:
    broker = _broker(FakeExternalProvider(available=False))

    report = await broker.preflight()

    entry = next(item for item in report["providers"] if item["provider_id"] == "fake_external")
    assert entry["status"] == "unavailable"
    assert entry["reason"] == "simulated missing engine"
    assert entry["builtin"] is False


@pytest.mark.anyio
async def test_prompt_block_lists_only_callable_tools() -> None:
    """§10: the prompt must mirror the tool list exactly (§8)."""

    provider = FakeExternalProvider()
    broker = _broker(provider)

    definitions = await broker.list_available_tool_definitions(
        tool_permissions=list(ZIPING_PERMISSIONS)
    )
    block = await broker.compose_tool_block(tool_permissions=list(ZIPING_PERMISSIONS))

    assert block.startswith("AVAILABLE TOOLS FOR THIS TURN:")
    for name in _names(definitions):
        assert f"- {name}" in block
    assert "tarot" not in block
    assert "qimen" not in block


@pytest.mark.anyio
async def test_prompt_block_says_no_tools_when_none_are_callable() -> None:
    # Built-ins are always callable, so reaching the "none" branch requires
    # looking at the external provider in isolation.
    broker = _broker(FakeExternalProvider(available=False), with_builtins=False)

    block = await broker.compose_tool_block(tool_permissions=["bazi"])

    assert block == "AVAILABLE TOOLS FOR THIS TURN:\n- none"


@pytest.mark.anyio
async def test_health_report_counts_real_tools() -> None:
    broker = _broker(FakeExternalProvider())

    report = await broker.health_report()

    entry = next(item for item in report["providers"] if item["provider_id"] == "fake_external")
    assert entry["tool_count"] == len(EXTERNAL_TOOL_NAMES)
    assert report["ready"] is True
