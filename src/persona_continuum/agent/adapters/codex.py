from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from persona_continuum.agent.adapter import (
    AgentAdapter,
    AgentSession,
    build_runtime_binding_snapshot,
    resolve_binary,
    safe_exec_cmd,
)
from persona_continuum.agent.models import (
    AgentCapabilityFlags,
    AgentEvent,
    AgentEventType,
    AgentProbeResult,
    AgentSessionConfig,
    AgentStatus,
    AgentTurn,
    ModelCapability,
    OutputStreamingMode,
    PromptMode,
    ResearchCapability,
    ResearchVerificationStatus,
    RuntimeBindingSnapshot,
    SelectionStrategy,
    StructuredOutputMode,
)
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.response_collector import (
    AgentTransportError,
    ReasoningBindingUnverifiedError,
    agent_error_event,
    sanitize_diagnostic,
)
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.auth.credentials import build_runtime_environment
from persona_continuum.numeric import safe_int

DEFAULT_CODEX_STREAM_LIMIT_BYTES = 16 * 1024 * 1024
MIN_CODEX_STREAM_LIMIT_BYTES = 1 * 1024 * 1024
MAX_CODEX_STREAM_LIMIT_BYTES = 32 * 1024 * 1024


def _codex_stream_limit(extra: dict[str, Any] | None = None) -> int:
    configured = safe_int(
        dict(extra or {}).get("codex_stream_limit_bytes"),
        default=DEFAULT_CODEX_STREAM_LIMIT_BYTES,
    )
    return min(
        MAX_CODEX_STREAM_LIMIT_BYTES,
        max(MIN_CODEX_STREAM_LIMIT_BYTES, configured or DEFAULT_CODEX_STREAM_LIMIT_BYTES),
    )


def _codex_pool_idle_seconds(config: AgentSessionConfig) -> float | None:
    """Optional per-session idle budget override for pooled runtimes."""

    from persona_continuum.numeric import safe_timeout

    raw = (config.extra or {}).get("runtime_pool_idle_seconds")
    if raw is None:
        return None
    try:
        return safe_timeout(raw, default=900.0)
    except Exception:
        return None


async def _drain_stderr(
    stream: asyncio.StreamReader | None,
    state: dict[str, Any],
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return
        session = state.get("session")
        if isinstance(session, AgentSession):
            session.touch_activity("stderr", byte_count=len(chunk))
        combined = state.get("tail", "") + chunk.decode("utf-8", errors="replace")
        state["tail"] = sanitize_diagnostic(combined, limit=2000)


async def _reap_subprocess(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
) -> None:
    """Terminate and reap a child even when graceful shutdown stalls."""

    async def reap() -> None:
        if proc.returncode is not None:
            await proc.wait()
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    cleanup = asyncio.create_task(reap())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        # Discovery may be cancelled by its outer probe timeout. Finish
        # reaping before propagating cancellation so the event loop never
        # closes around a live subprocess transport.
        await cleanup
        raise


class CodexAdapter(AgentAdapter):
    adapter_id = "codex"
    name = "Codex"
    # Both supported carriers are large-prompt safe: app-server uses a JSON-RPC
    # stream and the `codex exec --json` fallback writes the prompt to stdin.
    # Declare the smaller of those two ceilings so the shared transport guard
    # does not misclassify Codex as an unknown/ARGV adapter.
    prompt_transport_mode = "stdin"

    def __init__(self) -> None:
        self.binary_candidates = [
            "~/.npm-global/bin/codex",
            "~/.local/bin/codex",
            "/usr/local/bin/codex",
            "/opt/homebrew/bin/codex",
            "codex",
        ]
        self._resolved_binary: str | None = None
        self._cached_version: str | None = None
        self.model_discovery_error: str | None = None
        self.prompt_mode = PromptMode.INLINE_SYSTEM
        self.structured_output_mode = StructuredOutputMode.PROMPT_ONLY
        self.output_streaming_mode = OutputStreamingMode.PROTOCOL_STREAM
        self.protocols = ["codex_app_server", "jsonrpc_stdio", "streaming_json_cli"]

    def cached_version_hint(self) -> str | None:
        """Last observed CLI version; lets caches detect binary upgrades."""

        return self._cached_version

    async def probe(self) -> AgentProbeResult:
        binary = self._find_binary()
        if not binary:
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.DISABLED,
                binary_path=None,
                version=None,
                protocols=["codex_app_server", "jsonrpc_stdio", "streaming_json_cli"],
                capabilities=AgentCapabilityFlags(
                    streaming=True,
                    persistent_session=True,
                    model_discovery=True,
                    model_selection=SelectionStrategy.STARTUP,
                    reasoning_discovery=True,
                    reasoning_selection=SelectionStrategy.STARTUP,
                    mcp=True,
                    structured_output_mode=self.structured_output_mode,
                ),
                research=ResearchCapability(
                    mode="none",
                    verification_status=ResearchVerificationStatus.UNAVAILABLE,
                    source="codex:not_installed",
                    verification_method="binary_probe",
                    verification_error="cli_not_found",
                ),
                models=[],
                status_detail="Codex CLI binary not found on system",
            )

        # Probe version and health
        code, out, _ = await safe_exec_cmd([binary, "--version"], timeout=5.0)
        version_str = out.strip().split("\n")[0] if code == 0 and out else "detected"
        self._cached_version = version_str

        # Probe auth via doctor or auth check
        doc_code, doc_out, _ = await safe_exec_cmd([binary, "doctor", "--summary"], timeout=8.0)
        is_auth = "auth is configured" in doc_out or "ChatGPT tokens" in doc_out or doc_code == 0
        _, help_out, help_err = await safe_exec_cmd([binary, "--help"], timeout=5.0)
        research = self._probe_research_capability(
            version_str,
            "\n".join(part for part in (help_out, help_err, doc_out) if part),
        )

        status = AgentStatus.READY if is_auth else AgentStatus.AUTH_REQUIRED
        # ``list_models`` spawns and reaps a full app-server; only pay that on
        # a capability-cache miss instead of on every probe.
        from persona_continuum.performance.capability_cache import (
            default_model_capability_cache,
        )
        from persona_continuum.performance.tracing import default_tracer

        cache = default_model_capability_cache()
        models = cache.peek(self)
        if models is None:
            default_tracer().incr_global("agent_probe_count", 1)
            models = await cache.get_models(self)
        discovery_error = self.model_discovery_error

        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=status,
            binary_path=binary,
            version=version_str,
            auth_status="configured" if is_auth else "auth_required",
            protocols=["codex_app_server", "jsonrpc_stdio", "streaming_json_cli"],
            capabilities=AgentCapabilityFlags(
                streaming=True,
                persistent_session=True,
                model_discovery=True,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_discovery=True,
                reasoning_selection=SelectionStrategy.STARTUP,
                mcp=True,
                structured_output_mode=self.structured_output_mode,
                web_search=research.discovers_sources,
                web_fetch=research.reads_sources,
                browser=research.browser,
            ),
            research=research,
            models=models,
            status_detail=(
                f"model_discovery_error: {discovery_error}"
                if is_auth and discovery_error
                else (None if is_auth else "Codex auth required (run `codex login`)")
            ),
            model_discovery_error=discovery_error,
        )

    @staticmethod
    def _probe_research_capability(version: str | None, text: str) -> ResearchCapability:
        """Return a hint without treating CLI help as behavioral proof.

        ``--help`` and ``doctor`` output can only establish a declared hint.
        Missing tokens remain ``unknown`` so the Persona Creation resolver can
        run a real behavioral probe against a READY local CLI.
        """
        lowered = text.casefold()
        explicit = os.environ.get("CODEX_NATIVE_WEB_SEARCH", "").casefold() in {
            "1",
            "true",
            "yes",
            "live",
        }
        search = explicit or any(
            token in lowered for token in ("web_search", "web search", "browser search")
        )
        fetch = explicit or any(
            token in lowered for token in ("web_fetch", "web fetch", "fetch url", "fetch webpage")
        )
        browser = "browser" in lowered
        if explicit or search or fetch or browser:
            cached = any(
                token in lowered for token in ("cached", "cache-only", "offline")
            ) and not any(token in lowered for token in ("live web", "real-time", "realtime"))
            return ResearchCapability(
                mode="native_cli" if search and fetch else "agentic_cli",
                search=search,
                fetch=fetch,
                can_discover_sources=search or browser,
                can_read_sources=fetch or browser,
                browser=browser,
                citations=True,
                live=not cached,
                source=(
                    f"codex:env_override:{version or 'unknown'}"
                    if explicit
                    else f"codex:help_hint:{version or 'unknown'}"
                ),
                verification_status=ResearchVerificationStatus.DECLARED,
                verification_method="env_override" if explicit else "help_hint",
            )
        return ResearchCapability(
            mode="agentic_cli",
            verification_status=ResearchVerificationStatus.UNKNOWN,
            verification_method="metadata_absent",
            source=f"codex:research_unknown:{version or 'unknown'}",
        )

    async def list_models(self) -> list[ModelCapability]:
        from persona_continuum.performance.capability_cache import (
            default_model_capability_cache,
        )
        from persona_continuum.performance.tracing import default_tracer

        default_tracer().incr_global("model_list_count", 1)
        self.model_discovery_error = None
        binary = self._find_binary()
        if not binary:
            return self._fallback_models()

        proc: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[None] | None = None
        stderr_state: dict[str, Any] = {}
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=DEFAULT_CODEX_STREAM_LIMIT_BYTES,
            )
            stderr_task = asyncio.create_task(_drain_stderr(proc.stderr, stderr_state))
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "persona-continuum", "version": "1.0"}},
            }
            await self._write_jsonrpc(proc, init_req)
            init_response = await self._read_jsonrpc_response(proc, 1)
            if "error" in init_response:
                raise RuntimeError(f"initialize failed: {init_response['error']}")
            if "result" not in init_response:
                raise RuntimeError("initialize response is missing result")

            await self._write_jsonrpc(
                proc,
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            )

            discovered: list[ModelCapability] = []
            cursor: str | None = None
            seen_cursors: set[str] = set()
            request_id = 2
            while True:
                params = {"cursor": cursor} if cursor is not None else {}
                await self._write_jsonrpc(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "model/list",
                        "params": params,
                    },
                )
                response = await self._read_jsonrpc_response(proc, request_id)
                if "error" in response:
                    raise RuntimeError(f"model/list failed: {response['error']}")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("model/list result must be an object")
                items = result.get("data")
                if not isinstance(items, list):
                    raise RuntimeError("model/list result.data must be an array")
                discovered.extend(self._parse_model_list_items(items))

                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    break
                if not isinstance(next_cursor, str) or not next_cursor:
                    raise RuntimeError("model/list nextCursor must be a non-empty string or null")
                if next_cursor in seen_cursors:
                    raise RuntimeError("model/list pagination cursor repeated")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
                request_id += 1

            discovered = discovered or [self._manual_default_model()]
            await default_model_capability_cache().store(self, discovered)
            return discovered
        except Exception as exc:
            self.model_discovery_error = str(exc)
            return [self._manual_default_model()]
        finally:
            if proc is not None:
                await _reap_subprocess(proc, timeout=1.0)
            if stderr_task is not None:
                with contextlib.suppress(Exception):
                    await stderr_task

    async def _write_jsonrpc(
        self, proc: asyncio.subprocess.Process, payload: dict[str, Any]
    ) -> None:
        if not proc.stdin:
            raise RuntimeError("Codex app-server stdin is unavailable")
        proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def _read_jsonrpc_response(
        self, proc: asyncio.subprocess.Process, request_id: int
    ) -> dict[str, Any]:
        if not proc.stdout:
            raise RuntimeError("Codex app-server stdout is unavailable")
        while True:
            line_b = await asyncio.wait_for(proc.stdout.readline(), timeout=15.0)
            if not line_b:
                raise RuntimeError(f"Codex app-server closed before response {request_id}")
            try:
                data = json.loads(line_b.decode("utf-8", errors="replace").strip())
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("id") == request_id:
                return data

    def _parse_model_list_items(self, items: list[Any]) -> list[ModelCapability]:
        parsed: list[ModelCapability] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise RuntimeError("model/list data entries must contain a string id")
            raw_efforts = item.get("supportedReasoningEfforts", [])
            if not isinstance(raw_efforts, list):
                raise RuntimeError("supportedReasoningEfforts must be an array")
            efforts: list[str] = []
            for raw_effort in raw_efforts:
                if isinstance(raw_effort, str):
                    effort = raw_effort
                elif isinstance(raw_effort, dict) and isinstance(
                    raw_effort.get("reasoningEffort"), str
                ):
                    effort = raw_effort["reasoningEffort"]
                else:
                    raise RuntimeError(
                        "supportedReasoningEfforts entries must contain reasoningEffort"
                    )
                if effort not in efforts:
                    efforts.append(effort)

            default_effort = item.get("defaultReasoningEffort")
            if default_effort is not None and not isinstance(default_effort, str):
                raise RuntimeError("defaultReasoningEffort must be a string or null")
            parsed.append(
                ModelCapability(
                    id=item["id"],
                    display_name=str(item.get("displayName") or item.get("name") or item["id"]),
                    provider="openai",
                    supported_reasoning_efforts=efforts,
                    default_reasoning_effort=default_effort,
                    source="protocol_model_list",
                    reasoning_selection=(
                        SelectionStrategy.STARTUP if efforts else SelectionStrategy.UNSUPPORTED
                    ),
                )
            )
        return parsed

    def _manual_default_model(self) -> ModelCapability:
        return ModelCapability(
            id="default",
            display_name="Agent Default",
            provider="openai",
            supported_reasoning_efforts=[],
            default_reasoning_effort=None,
            source="manual",
            reasoning_selection=SelectionStrategy.MANUAL,
        )

    async def _initialize_shared_app_server(
        self, transport: SubprocessAgentTransport
    ) -> Callable[[], Awaitable[None]]:
        """Build the one-time JSON-RPC handshake for a pooled app-server."""

        async def _initialize() -> None:
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "persona-continuum", "version": "1.0"}},
            }
            await self._write_jsonrpc_transport(transport, init_req)
            response = await self._read_jsonrpc_transport(transport, 1)
            if "error" in response:
                raise RuntimeError(f"initialize failed: {response['error']}")
            await self._write_jsonrpc_transport(
                transport, {"jsonrpc": "2.0", "method": "initialized", "params": {}}
            )

        return _initialize

    @staticmethod
    async def _write_jsonrpc_transport(
        transport: SubprocessAgentTransport, payload: dict[str, Any]
    ) -> None:
        await transport.write((json.dumps(payload) + "\n").encode("utf-8"))

    @staticmethod
    async def _read_jsonrpc_transport(
        transport: SubprocessAgentTransport, request_id: int
    ) -> dict[str, Any]:
        while True:
            line_b = await asyncio.wait_for(transport.readline(), timeout=15.0)
            if not line_b:
                raise RuntimeError(
                    f"Codex app-server closed before response {request_id}"
                )
            try:
                data = json.loads(line_b.decode("utf-8", errors="replace").strip())
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("id") == request_id:
                return data

    @staticmethod
    def _parse_thread_id(response: dict[str, Any]) -> str | None:
        result = response.get("result")
        if isinstance(result, dict):
            thread_obj = result.get("thread")
            if isinstance(thread_obj, dict) and "id" in thread_obj:
                return str(thread_obj["id"])
            if "id" in result:
                return str(result["id"])
        return None

    def supports_persistent_conversation(self, session: AgentSession) -> bool:
        """True only when this session is a live app-server thread.

        The ``codex exec`` fallback re-spawns per turn and carries no thread
        memory, so callers must keep sending the full transcript there.
        """

        return session.session_data.get("mode") == "app_server" and bool(
            session.session_data.get("transport") is not None
        )

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        binary = self._find_binary()
        if not binary:
            raise RuntimeError("Codex binary not found")

        # Start Codex app-server stdio process
        app_server_cmd = [binary, "app-server", "--listen", "stdio://"]
        if config.model_id and config.model_id != "default":
            app_server_cmd.extend(["-c", f"model={config.model_id}"])

        env = build_runtime_environment(
            credential_manager=getattr(self, "credential_manager", None),
            credential_id=config.auth_profile_id,
            auth_env_var=config.auth_env_var,
            target_name="OPENAI_API_KEY",
        )

        stream_limit = _codex_stream_limit(config.extra)
        mode = "cli_exec_fallback"
        session = AgentSession(
            config=config,
            is_active=True,
            session_data={
                "proc": None,
                "mode": mode,
                "binary": binary,
                "thread_id": f"thr_{config.participant_id}",
                "active_turn_id": None,
                "msg_id": 10,
                "protocol": "codex_exec_json",
                "effective_model": config.model_id,
                "effective_reasoning": None,
                "stream_limit_bytes": stream_limit,
            },
        )
        session._cancel_event = asyncio.Event()

        from persona_continuum.performance.runtime_pool import (
            AgentRuntimePool,
            PoolAcquiredRuntime,
            default_runtime_pool,
        )

        pool = default_runtime_pool()
        pool_enabled = bool(pool.enabled) and bool(
            (config.extra or {}).get("use_runtime_pool", True)
        )

        thread_id: str | None = None
        transport: SubprocessAgentTransport | None = None
        lease: PoolAcquiredRuntime | None = None
        owned_transport: SubprocessAgentTransport | None = None
        try:
            if pool_enabled:
                pool_key = AgentRuntimePool.make_key(
                    f"codex:{config.auth_profile_id or 'default'}",
                    [binary, "app-server", "--listen", "stdio://"],
                    env,
                )
                lease = await pool.acquire(
                    pool_key,
                    factory=lambda: SubprocessAgentTransport.spawn(
                        session,
                        app_server_cmd,
                        stdin=True,
                        env=env,
                        cwd=config.working_dir,
                        stream_limit=stream_limit,
                    ),
                    max_idle_seconds=_codex_pool_idle_seconds(config),
                )
                transport = lease.transport
                await lease.managed.ensure_initialized(
                    await self._initialize_shared_app_server(transport)
                )
                thread_req = {"jsonrpc": "2.0", "id": 2, "method": "thread/start", "params": {}}
                await self._write_jsonrpc_transport(transport, thread_req)
                thread_response = await self._read_jsonrpc_transport(transport, 2)
                if "error" in thread_response:
                    raise RuntimeError(f"thread/start failed: {thread_response['error']}")
                thread_id = self._parse_thread_id(thread_response)
                proc = transport.process
                mode = "app_server"
            else:
                owned_transport = await SubprocessAgentTransport.spawn(
                    session,
                    app_server_cmd,
                    stdin=True,
                    env=env,
                    cwd=config.working_dir,
                    stream_limit=stream_limit,
                )
                transport = owned_transport
                await self._write_jsonrpc_transport(
                    transport,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"clientInfo": {"name": "persona-continuum", "version": "1.0"}},
                    },
                )
                init_response = await self._read_jsonrpc_transport(transport, 1)
                if "error" in init_response:
                    raise RuntimeError(f"initialize failed: {init_response['error']}")
                await self._write_jsonrpc_transport(
                    transport, {"jsonrpc": "2.0", "method": "initialized", "params": {}}
                )
                thread_req = {"jsonrpc": "2.0", "id": 2, "method": "thread/start", "params": {}}
                await self._write_jsonrpc_transport(transport, thread_req)
                thread_response = await self._read_jsonrpc_transport(transport, 2)
                if "error" in thread_response:
                    raise RuntimeError(f"thread/start failed: {thread_response['error']}")
                thread_id = self._parse_thread_id(thread_response)
                proc = transport.process
                mode = "app_server"
        except Exception:
            if lease is not None:
                # A reused pooled process may already carry other live Codex
                # threads.  Killing it because one handshake timed out evicts
                # every affinity on that process (the cascade shows up later
                # as pooled_runtime_affinity_unavailable), so only discard a
                # process that this acquisition actually spawned.
                spawned_by_this_lease = lease.managed.execution_count <= 1
                with contextlib.suppress(Exception):
                    await lease.release(kill=spawned_by_this_lease)
                lease = None
                transport = None
            elif owned_transport is not None:
                with contextlib.suppress(Exception):
                    await owned_transport.close(force=True)
                owned_transport = None
                transport = None
            proc = None
            mode = "cli_exec_fallback"

        session.session_data.update(
            {
                "proc": proc,
                "mode": mode,
                "thread_id": thread_id or f"thr_{config.participant_id}",
                "protocol": "codex_app_server" if mode == "app_server" else "codex_exec_json",
                "effective_reasoning": (
                    config.reasoning_effort if mode == "app_server" else None
                ),
                "transport": transport,
                "pool_runtime": None,
                "pool_managed_runtime": lease.managed if lease is not None else None,
            }
        )
        if lease is not None:
            # The logical Codex thread retains only affinity.  Holding this
            # lease until session.close would cap persistent Persona/Room/World
            # sessions at the physical pool size.
            await pool.retain_logical(lease.managed)
            await lease.release()
        return session

    async def bind_runtime(self, session: AgentSession) -> RuntimeBindingSnapshot:
        mode = session.session_data.get("mode", "cli_exec_fallback")
        requested_reasoning = str(session.config.reasoning_effort or "").strip().casefold()
        has_explicit_reasoning = requested_reasoning not in {"", "none", "default"}
        if has_explicit_reasoning and mode != "app_server":
            # ``codex exec --json`` has no verified reasoning-effort binding in
            # this adapter.  Do not let an app-server startup failure silently
            # turn a requested reasoning run into a default-effort run.
            raise ReasoningBindingUnverifiedError(
                "Codex exec fallback cannot verify the selected reasoning effort",
                phase="session_binding",
                diagnostics={
                    "protocol": "codex_exec_json",
                    "requested_reasoning": session.config.reasoning_effort,
                    "runtime_mode": mode,
                },
            )
        return build_runtime_binding_snapshot(
            self,
            session,
            protocol=str(session.session_data.get("protocol") or "codex_exec_json"),
            model_verified=True,
            reasoning_verified=bool(
                not has_explicit_reasoning or mode == "app_server"
            ),
            verification_method="codex_app_server_or_exec_flags",
        )

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        mode = session.session_data.get("mode", "cli_exec_fallback")
        managed_runtime = session.session_data.get("pool_managed_runtime")
        if (
            mode == "app_server"
            and managed_runtime is not None
            and not session.session_data.get("pool_execution_active", False)
        ):
            # Recursive delegation keeps the protocol implementation in one
            # place while narrowing the physical lease to exactly one turn.
            from persona_continuum.performance.runtime_pool import default_runtime_pool

            lease = await default_runtime_pool().acquire_managed(managed_runtime)
            session.session_data["pool_execution_active"] = True
            session.session_data["pool_runtime"] = lease
            session.session_data["transport"] = lease.transport
            session.session_data["proc"] = lease.managed.process
            try:
                async for event in self.send(session, turn):
                    yield event
            finally:
                dead = not lease.managed.alive
                if dead:
                    lease.mark_unhealthy()
                await lease.release(kill=dead)
                session.session_data["pool_execution_active"] = False
                session.session_data["pool_runtime"] = None
            return
        proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
        app_transport: SubprocessAgentTransport | None = session.session_data.get("transport")
        thread_id = session.session_data.get("thread_id")

        if mode == "app_server" and proc and proc.returncode is None:
            # Use JSON-RPC protocol over app-server: turn/start
            msg_id = session.session_data.get("msg_id", 10)
            session.session_data["msg_id"] = msg_id + 1

            prompt_text = AgentPromptRenderer.render_for_single_prompt(turn)
            params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt_text}],
            }
            if session.config.model_id:
                params["model"] = session.config.model_id
            if session.config.reasoning_effort and session.config.reasoning_effort not in {
                "none",
                "default",
            }:
                params["effort"] = session.config.reasoning_effort
            if session.config.working_dir:
                params["cwd"] = session.config.working_dir

            req = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "turn/start",
                "params": params,
            }

            try:
                if app_transport is None:
                    raise RuntimeError("Codex app-server transport is unavailable")
                await app_transport.write((json.dumps(req) + "\n").encode("utf-8"))

                full_content: list[str] = []
                active_turn_id: str | None = None
                session.session_data["interrupt_sent"] = False
                while True:
                    if (
                        session._cancel_event
                        and session._cancel_event.is_set()
                        and not session.session_data.get("interrupt_sent", False)
                        and app_transport is not None
                        and active_turn_id
                    ):
                        session.session_data["interrupt_sent"] = True
                        interrupt_req = {
                            "jsonrpc": "2.0",
                            "id": session.session_data.get("msg_id", 99),
                            "method": "turn/interrupt",
                            "params": {
                                "threadId": thread_id,
                                "turnId": active_turn_id,
                            },
                        }
                        with contextlib.suppress(Exception):
                            encoded = (json.dumps(interrupt_req) + "\n").encode("utf-8")
                            await app_transport.write(encoded)

                    if app_transport is None:
                        break

                    line_b = await app_transport.readline()
                    if not line_b:
                        break

                    line_str = line_b.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue

                    try:
                        data = json.loads(line_str)
                    except Exception:
                        yield AgentEvent(type=AgentEventType.CHUNK, content=line_str + "\n")
                        full_content.append(line_str + "\n")
                        continue

                    method_name = str(data.get("method") or "")
                    session.touch_activity(
                        "protocol_frame",
                        byte_count=len(line_b),
                        metadata={
                            "frame_type": method_name
                            or ("response" if "id" in data else "unknown"),
                            "event_type": method_name or str(data.get("type") or "unknown"),
                            "method": method_name or None,
                        },
                    )

                    # turn/start response: parses active_turn_id, but DOES NOT terminate turn!
                    if data.get("id") == msg_id:
                        if "result" in data:
                            res = data["result"]
                            if isinstance(res, dict):
                                turn_obj = res.get("turn")
                                if isinstance(turn_obj, dict) and "id" in turn_obj:
                                    active_turn_id = str(turn_obj["id"])
                                    session.session_data["active_turn_id"] = active_turn_id
                                elif "id" in res:
                                    active_turn_id = str(res["id"])
                                    session.session_data["active_turn_id"] = active_turn_id
                            # Continue listening for streamed notifications
                            continue
                        elif "error" in data:
                            yield AgentEvent(type=AgentEventType.ERROR, error=str(data["error"]))
                            return

                    method = data.get("method")
                    params_data = data.get("params", {})
                    if method in {
                        "turn/delta",
                        "item/agentMessage/delta",
                        "turn/notification",
                        "item/notification",
                    }:
                        item_obj = params_data.get("item")
                        item_text = (
                            item_obj.get("text") or item_obj.get("content")
                            if isinstance(item_obj, dict)
                            else ""
                        )
                        delta = str(
                            params_data.get("delta") or params_data.get("text") or item_text
                        )
                        if delta:
                            yield AgentEvent(type=AgentEventType.CHUNK, content=delta)
                            full_content.append(delta)
                    elif method in {"item/completed", "item/completion", "item/done"}:
                        # App-server versions differ: some expose the final
                        # assistant message only as item/completed and emit no
                        # delta events.  Parse that terminal item as text.
                        item_obj = (
                            params_data.get("item") if isinstance(params_data, dict) else None
                        )
                        if not isinstance(item_obj, dict) and isinstance(data.get("item"), dict):
                            item_obj = data.get("item")
                        item_type = str((item_obj or {}).get("type") or "")
                        if item_type in {
                            "agent_message",
                            "agentMessage",
                            "assistant_message",
                            "assistantMessage",
                            "message",
                        }:
                            final_text = str(
                                (item_obj or {}).get("text")
                                or (item_obj or {}).get("content")
                                or ""
                            )
                            if final_text and not "".join(full_content).endswith(final_text):
                                yield AgentEvent(type=AgentEventType.CHUNK, content=final_text)
                                full_content.append(final_text)
                    elif method in {"turn/thinking", "item/reasoning/delta"}:
                        item_obj = params_data.get("item")
                        item_text = (
                            item_obj.get("text") or item_obj.get("thinking")
                            if isinstance(item_obj, dict)
                            else ""
                        )
                        thinking = str(
                            params_data.get("thinking") or params_data.get("delta") or item_text
                        )
                        if thinking:
                            yield AgentEvent(type=AgentEventType.THINKING, thinking=thinking)
                    elif method in {"turn/done", "turn/completed", "turn/completion"}:
                        turn_obj = (
                            params_data.get("turn")
                            if isinstance(params_data.get("turn"), dict)
                            else {}
                        )
                        turn_status = params_data.get("status") or turn_obj.get("status")
                        if turn_status == "interrupted":
                            yield AgentEvent(
                                type=AgentEventType.DONE,
                                content="".join(full_content),
                                metadata={"cancelled": True},
                            )
                            return
                        if turn_status in {"failed", "systemError"}:
                            # The app-server itself reported a failed turn
                            # (e.g. a transient upstream stream disconnect).
                            # Propagate it as a retriable transport error on
                            # the healthy pooled process; silently falling
                            # through to `codex exec` guarantees a misleading
                            # reasoning-binding failure whenever an explicit
                            # effort was requested.
                            server_error = turn_obj.get("error") or params_data.get("error") or {}
                            message = str(
                                (server_error or {}).get("message")
                                if isinstance(server_error, dict)
                                else server_error
                                or "codex app-server reported a failed turn"
                            )
                            yield agent_error_event(
                                AgentTransportError(
                                    message[:500] or "codex app-server reported a failed turn",
                                    phase="agent_turn",
                                    diagnostics={
                                        "protocol": "codex_app_server",
                                        "turn_status": str(turn_status),
                                        "thread_id": str(params_data.get("threadId") or ""),
                                    },
                                ),
                                protocol="codex_app_server",
                            )
                            return
                        elif full_content:
                            yield AgentEvent(
                                type=AgentEventType.DONE,
                                content="".join(full_content),
                                metadata={"protocol": "codex_app_server"},
                            )
                            return
                        break

                if full_content:
                    yield AgentEvent(
                        type=AgentEventType.DONE,
                        content="".join(full_content),
                        metadata={"protocol": "codex_app_server"},
                    )
                    return
                if app_transport is not None:
                    await app_transport.close(force=True)
                    session.session_data["proc"] = None
                    session.session_data["transport"] = None
                yield AgentEvent(
                    type=AgentEventType.RAW_LOG,
                    content=(
                        "app-server completed without assistant output; "
                        "trying exec fallback"
                    ),
                    metadata={
                        "protocol": "codex_app_server",
                        "fallback": "codex_exec_json",
                        "event": "agent_protocol_fallback_started",
                    },
                )
            except Exception:
                # App server failed; fall back to exec
                if app_transport is not None:
                    await app_transport.close(force=True)
                    session.session_data["proc"] = None
                    session.session_data["transport"] = None
                yield AgentEvent(
                    type=AgentEventType.RAW_LOG,
                    content="app-server transport failed; trying exec fallback",
                    metadata={
                        "protocol": "codex_app_server",
                        "fallback": "codex_exec_json",
                        "event": "agent_protocol_fallback_started",
                    },
                )

        # Fallback to `codex exec --json`
        requested_reasoning = str(session.config.reasoning_effort or "").strip().casefold()
        if requested_reasoning not in {"", "none", "default"}:
            yield agent_error_event(
                ReasoningBindingUnverifiedError(
                    "Codex exec fallback cannot verify the selected reasoning effort",
                    phase="session_update",
                    diagnostics={
                        "protocol": "codex_exec_json",
                        "requested_reasoning": session.config.reasoning_effort,
                        "fallback_from": mode,
                    },
                ),
                protocol="codex_exec_json",
            )
            return
        binary = session.session_data.get("binary") or self._find_binary() or "codex"
        cmd = [binary, "exec", "--json"]
        if session.config.model_id and session.config.model_id != "default":
            cmd.extend(["-c", f"model={session.config.model_id}"])

        env = build_runtime_environment(
            credential_manager=getattr(self, "credential_manager", None),
            credential_id=session.config.auth_profile_id,
            auth_env_var=session.config.auth_env_var,
            target_name="OPENAI_API_KEY",
        )

        prompt = AgentPromptRenderer.render_for_single_prompt(turn)
        exec_transport: SubprocessAgentTransport | None = None
        exec_proc: asyncio.subprocess.Process | None = None
        try:
            exec_transport = await SubprocessAgentTransport.spawn(
                session,
                cmd,
                stdin=True,
                env=env,
                cwd=session.config.working_dir,
                stream_limit=_codex_stream_limit(session.config.extra),
            )
            exec_proc = exec_transport.process
            session.session_data["active_exec_proc"] = exec_proc
            session.session_data["active_exec_transport"] = exec_transport

            await exec_transport.write(prompt.encode("utf-8"))
            exec_transport.close_stdin()

            full_content = []
            usage: dict[str, Any] = {}
            while True:
                if session._cancel_event and session._cancel_event.is_set():
                    await exec_transport.kill()
                    yield AgentEvent(type=AgentEventType.DONE, content="".join(full_content))
                    return

                line_b = await exec_transport.readline()
                if not line_b:
                    break

                line_str = line_b.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                    method_name = str(data.get("method") or "") if isinstance(data, dict) else ""
                    session.touch_activity(
                        "protocol_frame",
                        byte_count=len(line_b),
                        metadata={
                            "frame_type": method_name
                            or ("event" if isinstance(data, dict) else "unknown"),
                            "event_type": str(data.get("type") or method_name or "unknown")
                            if isinstance(data, dict)
                            else "unknown",
                            "method": method_name or None,
                        },
                    )
                    chunk = data.get("text") or data.get("content") or data.get("delta")
                    if not chunk and data.get("type") == "item.completed":
                        item = data.get("item")
                        if isinstance(item, dict) and item.get("type") == "agent_message":
                            chunk = item.get("text") or item.get("content")
                    if data.get("type") == "turn.completed" and isinstance(data.get("usage"), dict):
                        usage = dict(data["usage"])
                    if chunk:
                        yield AgentEvent(type=AgentEventType.CHUNK, content=str(chunk))
                        full_content.append(str(chunk))
                        continue
                    # Tool lifecycle and usage events are protocol metadata,
                    # not assistant text.  Do not feed their JSON envelopes
                    # into structured-output consumers.
                    if data.get("type") in {
                        "thread.started",
                        "turn.started",
                        "turn.completed",
                        "item.started",
                        "item.completed",
                    }:
                        continue
                except Exception:
                    pass

                yield AgentEvent(type=AgentEventType.CHUNK, content=line_str + "\n")
                full_content.append(line_str + "\n")

            await exec_transport.wait()
            if not full_content:
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error=(
                        "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT: "
                        "codex exec returned no assistant message"
                    ),
                    metadata={
                        "failure_code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                        "protocol": "codex_exec_json",
                        "returncode": exec_proc.returncode,
                        "stderr_tail": exec_transport.stderr_tail,
                    },
                )
                return
            yield AgentEvent(
                type=AgentEventType.DONE,
                content="".join(full_content),
                metadata={
                    "usage": usage,
                    "protocol": "codex_exec_json",
                    "event": "agent_protocol_fallback_completed",
                },
            )

        except Exception as exc:
            yield agent_error_event(exc, protocol="codex_exec_json")
        finally:
            if exec_transport is not None:
                await exec_transport.close(force=exec_transport.process_alive)
            session.session_data["active_exec_proc"] = None
            session.session_data["active_exec_transport"] = None

    async def cancel(self, session: AgentSession) -> None:
        session.request_cancel()
        proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
        transport: SubprocessAgentTransport | None = session.session_data.get("transport")
        thread_id = session.session_data.get("thread_id")
        active_turn_id = session.session_data.get("active_turn_id")

        if (
            proc
            and proc.returncode is None
            and transport is not None
            and thread_id
            and active_turn_id
            and not session.session_data.get("interrupt_sent", False)
        ):
            session.session_data["interrupt_sent"] = True
            msg_id = session.session_data.get("msg_id", 99) + 1
            session.session_data["msg_id"] = msg_id
            interrupt_req = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "turn/interrupt",
                "params": {
                    "threadId": thread_id,
                    "turnId": active_turn_id,
                },
            }
            try:
                await transport.write((json.dumps(interrupt_req) + "\n").encode("utf-8"))
            except Exception:
                # App server broken/closed: fallback terminate
                with contextlib.suppress(Exception):
                    proc.terminate()
        elif proc and proc.returncode is None and not active_turn_id:
            # No active turn to interrupt; do not terminate persistent app-server
            pass

        exec_proc: asyncio.subprocess.Process | None = session.session_data.get("active_exec_proc")
        if exec_proc and exec_proc.returncode is None:
            with contextlib.suppress(Exception):
                exec_proc.terminate()

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()
        managed_runtime = session.session_data.get("pool_managed_runtime")
        if managed_runtime is not None:
            from persona_continuum.performance.runtime_pool import default_runtime_pool

            with contextlib.suppress(Exception):
                await default_runtime_pool().release_logical(managed_runtime)
            session.session_data["pool_runtime"] = None
            session.session_data["pool_managed_runtime"] = None
            session.session_data["transport"] = None
            session.session_data["proc"] = None
            return
        transport = session.session_data.get("transport")
        if isinstance(transport, SubprocessAgentTransport):
            await transport.close(force=True)
        else:
            proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
            if proc is not None:
                await _reap_subprocess(proc, timeout=1.5)
        exec_transport = session.session_data.get("active_exec_transport")
        if isinstance(exec_transport, SubprocessAgentTransport):
            await exec_transport.close(force=True)
        else:
            exec_proc: asyncio.subprocess.Process | None = session.session_data.get(
                "active_exec_proc"
            )
            if exec_proc is not None:
                await _reap_subprocess(exec_proc, timeout=1.5)

    def _find_binary(self) -> str | None:
        if self._resolved_binary:
            return self._resolved_binary
        self._resolved_binary = resolve_binary(self.binary_candidates)
        return self._resolved_binary

    def _fallback_models(self) -> list[ModelCapability]:
        return [
            ModelCapability(
                id="gpt-5.6-sol",
                display_name="GPT-5.6 Sol",
                provider="openai",
                supported_reasoning_efforts=["low", "medium", "high", "xhigh", "max"],
                default_reasoning_effort="high",
                source="config",
            ),
            ModelCapability(
                id="o3",
                display_name="OpenAI o3",
                provider="openai",
                supported_reasoning_efforts=["low", "medium", "high"],
                default_reasoning_effort="medium",
                source="config",
            ),
            ModelCapability(
                id="o4-mini",
                display_name="OpenAI o4-mini",
                provider="openai",
                supported_reasoning_efforts=["low", "medium", "high"],
                default_reasoning_effort="medium",
                source="config",
            ),
        ]
