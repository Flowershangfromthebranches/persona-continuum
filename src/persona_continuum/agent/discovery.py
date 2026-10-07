from __future__ import annotations

import asyncio
from typing import Any

from persona_continuum.agent.manifest_adapter import ManifestAgentAdapter
from persona_continuum.agent.models import (
    AgentProbeResult,
    AgentStatus,
    ResearchCapability,
    ResearchVerificationStatus,
)
from persona_continuum.agent.protocols.openai_compatible import OpenAICompatibleAPIAdapter
from persona_continuum.agent.registry import AgentRegistry
from persona_continuum.application.research_capability_cache import ResearchCapabilityCache
from persona_continuum.auth.profiles import AuthProfileService

PROBE_TIMEOUT_SECONDS = 12.0
SLOW_PROBE_TIMEOUT_SECONDS = {"gemini_cli": 25.0}
# Each probe is an independent CLI subprocess; 6 concurrent probes roughly
# halves the cold-scan window that gates first-use of the runtime selectors
# without meaningfully increasing process pressure.
PROBE_CONCURRENCY = 6
_STATUS_ORDER = {
    AgentStatus.READY: 0,
    AgentStatus.AUTH_REQUIRED: 1,
    AgentStatus.DETECTED: 2,
    AgentStatus.DETECTED_UNCONTROLLABLE: 3,
    AgentStatus.UNSUPPORTED_VERSION: 4,
    AgentStatus.BROKEN: 5,
    AgentStatus.DISABLED: 6,
}
# A live CLI turn can make `agy --version` / `agy models` time out or exit
# non-zero.  Those results are not proof the runtime disappeared.
_TRANSIENT_PROBE_STATUSES = {AgentStatus.DETECTED, AgentStatus.BROKEN}


class AgentDiscoveryService:
    def __init__(
        self,
        registry: AgentRegistry,
        auth_service: AuthProfileService | None = None,
        research_capability_cache: ResearchCapabilityCache | None = None,
    ) -> None:
        self.registry = registry
        self.auth_service = auth_service
        self.research_capability_cache = research_capability_cache
        self._cached_probes: dict[str, AgentProbeResult] = {}
        self._scan_lock = asyncio.Lock()
        self._probe_sema = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def scan(self, force_refresh: bool = True) -> list[AgentProbeResult]:
        if not force_refresh and self._cached_probes and not self._scan_lock.locked():
            return self.get_cached_probes()
        async with self._scan_lock:
            if not force_refresh and self._cached_probes:
                return self.get_cached_probes()
            return await self._scan_uncached(invalidate_capabilities=force_refresh)

    async def _scan_uncached(
        self, *, invalidate_capabilities: bool = False
    ) -> list[AgentProbeResult]:
        self.registry.load_manifests_from_dir()
        self.registry.load_plugin_entrypoints()

        if self.auth_service:
            profiles = self.auth_service.list_profiles()
            for prof in profiles:
                adapter_id = f"api_{prof.id}"
                api_adapter = OpenAICompatibleAPIAdapter(
                    adapter_id=adapter_id,
                    name=f"API: {prof.name}",
                    base_url=prof.base_url,
                    auth_env_var=prof.auth_env_var,
                    default_model=prof.default_model or "default",
                    custom_headers=prof.headers,
                    model_capabilities=dict(prof.metadata.get("model_capabilities") or {}),
                    provider_type=prof.provider_type,
                    credential_manager=self.auth_service.credential_manager,
                    credential_id=prof.id,
                )
                self.registry.register_adapter(api_adapter)

        adapters = self.registry.list_adapters()
        active_ids = {adapter.adapter_id for adapter in adapters}
        self._cached_probes = {
            adapter_id: probe
            for adapter_id, probe in self._cached_probes.items()
            if adapter_id in active_ids
        }
        if invalidate_capabilities:
            from persona_continuum.performance.capability_cache import (
                default_model_capability_cache,
            )

            cache = default_model_capability_cache()
            for adapter in adapters:
                invalidate = getattr(adapter, "invalidate_model_cache", None)
                if callable(invalidate):
                    invalidate()
            await asyncio.gather(*(cache.invalidate(adapter) for adapter in adapters))
        results = await asyncio.gather(
            *(self._probe_one(a.adapter_id) for a in adapters),
            return_exceptions=True,
        )

        probes: list[AgentProbeResult] = []
        for r in results:
            if isinstance(r, AgentProbeResult):
                probes.append(r)
                self._cached_probes[r.id] = r

        # agy's remote catalog can stall while other CLIs are being probed.
        # Retry once after that contention ends, before caching an empty UI.
        for index, probe in enumerate(probes):
            retry_adapter = self.registry.get_adapter(probe.id)
            if (
                retry_adapter is not None
                and getattr(retry_adapter, "retry_catalog_after_scan", False)
                and not probe.models
                and probe.model_discovery_error
            ):
                retried = await self._probe_one(probe.id)
                if retried is not None:
                    probes[index] = retried
        probes.sort(key=lambda p: (_STATUS_ORDER.get(p.status, 99), p.name))
        return probes

    async def probe_adapter(self, adapter_id: str) -> AgentProbeResult | None:
        adapter = self.registry.get_adapter(adapter_id)
        if not adapter:
            return None
        return await self._probe_one(adapter_id)

    async def get_ready_agents(self) -> list[AgentProbeResult]:
        probes = await self.scan(force_refresh=False)
        return [p for p in probes if p.status == AgentStatus.READY]

    def get_cached_probes(self) -> list[AgentProbeResult]:
        return list(self._cached_probes.values())

    def _retain_ready_probe(
        self, adapter_id: str, incoming: AgentProbeResult
    ) -> AgentProbeResult:
        """Keep a prior READY result across a busy-CLI timeout or version blip."""

        previous = self._cached_probes.get(adapter_id)
        if (
            previous is not None
            and previous.status == AgentStatus.READY
            and incoming.status in _TRANSIENT_PROBE_STATUSES
            and (not incoming.binary_path or incoming.binary_path == previous.binary_path)
            and (not incoming.version or incoming.version == previous.version)
        ):
            return previous.model_copy(
                update={"status_detail": incoming.status_detail or previous.status_detail}
            )
        return incoming

    async def _probe_one(self, adapter_id: str) -> AgentProbeResult | None:
        adapter = self.registry.get_adapter(adapter_id)
        if not adapter:
            return None

        # 1. Determine definition_source: "builtin" | "manifest" | "plugin" | "dynamic_api"
        definition_source = "builtin"
        if adapter_id.startswith("api_") or isinstance(adapter, OpenAICompatibleAPIAdapter):
            definition_source = "dynamic_api"
        elif isinstance(adapter, ManifestAgentAdapter) or hasattr(adapter, "source_path"):
            definition_source = "manifest"
        elif getattr(adapter, "_is_plugin", False) or "plugin" in type(adapter).__name__.lower():
            definition_source = "plugin"

        # 2. Determine runtime_source: "local_cli" | "api" | "test"
        runtime_source = "local_cli"
        if definition_source == "dynamic_api" or isinstance(adapter, OpenAICompatibleAPIAdapter):
            runtime_source = "api"
        elif (
            adapter_id == "fake_agent"
            or "fake" in adapter_id.lower()
            or "test" in adapter_id.lower()
        ):
            runtime_source = "test"
        elif isinstance(adapter, ManifestAgentAdapter):
            proto = getattr(adapter, "protocol", "plain_cli")
            runtime_source = "api" if proto in {"openai_compatible", "api"} else "local_cli"
        else:
            runtime_source = "local_cli"

        try:
            async with self._probe_sema:
                probe_res = await asyncio.wait_for(
                    adapter.probe(),
                    timeout=getattr(
                        adapter, "probe_timeout_seconds",
                        SLOW_PROBE_TIMEOUT_SECONDS.get(adapter_id, PROBE_TIMEOUT_SECONDS),
                    ),
                )
            if probe_res:
                probe_res.runtime_source = runtime_source
                probe_res.definition_source = definition_source
                probe_res.research = self._normalise_research_capability(probe_res)
                self._apply_context_capability_declaration(adapter, probe_res)
                probe_res = self._retain_ready_probe(adapter_id, probe_res)
            self._cached_probes[adapter_id] = probe_res
            return probe_res
        except TimeoutError:
            timed_out = AgentProbeResult(
                id=adapter_id,
                name=adapter.name,
                status=AgentStatus.DETECTED,
                runtime_source=runtime_source,
                definition_source=definition_source,
                protocols=[],
                models=[],
                status_detail=(
                    "Probe timed out; runtime may still be usable after a targeted rescan"
                ),
            )
            retained = self._retain_ready_probe(adapter_id, timed_out)
            self._cached_probes[adapter_id] = retained
            return retained
        except Exception as exc:
            err_probe = AgentProbeResult(
                id=adapter_id,
                name=adapter.name,
                status=AgentStatus.BROKEN,
                runtime_source=runtime_source,
                definition_source=definition_source,
                protocols=[],
                models=[],
                status_detail=f"Probe error: {exc}",
            )
            retained = self._retain_ready_probe(adapter_id, err_probe)
            self._cached_probes[adapter_id] = retained
            return retained

    @staticmethod
    def _apply_context_capability_declaration(
        adapter: Any, probe: AgentProbeResult
    ) -> None:
        """Copy the adapter's declared context-window handling onto the probe.

        Context handling is an Adapter capability, not a property of one
        vendor, so it is declared once per adapter and read generically here.
        No business code branches on an adapter id.
        """

        declared_mode = getattr(adapter, "context_window_mode", None)
        if declared_mode is not None:
            probe.context_window_mode = str(declared_mode)
            probe.capabilities.context_window_mode = str(declared_mode)
        native_only = getattr(adapter, "upstream_context_is_native_only", None)
        if native_only is not None:
            probe.capabilities.upstream_context_is_native_only = bool(native_only)
        declared_limit = getattr(adapter, "adapter_context_limit", None)
        if declared_limit is not None:
            from persona_continuum.numeric import safe_int

            limit = safe_int(declared_limit, default=None, minimum=1)
            if limit is not None:
                probe.adapter_context_limit = limit
                probe.capabilities.adapter_context_limit = limit
        # Session lifecycle and concurrency are adapter declarations too.  They
        # describe what the adapter can host, never what the current workload
        # does with model context, so they travel separately from
        # ``persistent_session``.
        session_mode = getattr(adapter, "adapter_session_mode", None)
        if session_mode is not None:
            probe.capabilities.adapter_session_mode = str(session_mode)
        parallel_turns = getattr(adapter, "parallel_turns_same_session", None)
        if parallel_turns is not None:
            probe.capabilities.parallel_turns_same_session = bool(parallel_turns)
        parallel_independent = getattr(adapter, "parallel_independent_sessions", None)
        if parallel_independent is not None:
            probe.capabilities.parallel_independent_sessions = bool(parallel_independent)
        max_independent = getattr(adapter, "max_parallel_independent_sessions", None)
        if max_independent is not None:
            from persona_continuum.numeric import safe_int

            parsed = safe_int(max_independent, default=None, minimum=1)
            if parsed is not None:
                probe.capabilities.max_parallel_independent_sessions = parsed

    def _normalise_research_capability(self, probe: AgentProbeResult) -> ResearchCapability:
        """Keep unreported local-CLI research capability explicitly unknown.

        Older adapters and persisted probe snapshots may still return the
        legacy ``mode=none`` shape.  A READY local CLI is eligible for a
        behavioral probe; only a missing/broken binary is unavailable.
        API runtimes intentionally remain strict and are not promoted here.
        """

        research = probe.research
        if (
            probe.runtime_source == "local_cli"
            and probe.status == AgentStatus.READY
            and self.research_capability_cache is not None
        ):
            cached = self.research_capability_cache.latest_for_runtime(
                agent_id=probe.id,
                agent_version=probe.version,
                runtime_source=probe.runtime_source,
            )
            if cached is not None:
                probe.research_model_id = cached.model_id
                return cached.capability
        if probe.runtime_source != "local_cli":
            return research
        if (
            probe.status == AgentStatus.READY
            and research.verification_status == ResearchVerificationStatus.UNKNOWN
            and research.mode == "none"
        ):
            return research.model_copy(
                update={
                    "mode": "agentic_cli",
                    "verification_method": research.verification_method or "metadata_absent",
                }
            )
        if (
            probe.status == AgentStatus.DISABLED
            and not probe.binary_path
            and research.verification_status == ResearchVerificationStatus.UNKNOWN
        ):
            return research.model_copy(
                update={
                    "verification_status": ResearchVerificationStatus.UNAVAILABLE,
                    "verification_method": research.verification_method or "binary_probe",
                    "verification_error": research.verification_error or "cli_not_found",
                }
            )
        return research
