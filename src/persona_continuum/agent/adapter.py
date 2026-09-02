from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from persona_continuum.agent.activity import AgentActivityTracker
from persona_continuum.agent.models import (
    AgentCapabilityFlags,
    AgentEvent,
    AgentProbeResult,
    AgentSessionConfig,
    AgentTurn,
    ModelCapability,
    OutputStreamingMode,
    RuntimeBindingSnapshot,
)


class AgentSession(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    config: AgentSessionConfig
    is_active: bool = True
    session_data: dict[str, Any] = Field(default_factory=dict)
    activity_tracker: AgentActivityTracker = Field(default_factory=AgentActivityTracker)
    _cancel_event: asyncio.Event | None = None

    def begin_turn(self) -> None:
        self.activity_tracker.begin_turn()
        self.session_data["agent_turn_started_at"] = self.activity_tracker.turn_started_at
        self.session_data["last_agent_activity_at"] = self.activity_tracker.last_transport_activity

    def touch_activity(
        self,
        kind: str,
        byte_count: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.activity_tracker.touch(kind, byte_count=byte_count, metadata=metadata)
        self.session_data["last_agent_activity_at"] = self.activity_tracker.last_transport_activity
        self.session_data["last_agent_activity_monotonic"] = (
            self.activity_tracker.last_transport_monotonic
        )
        self.session_data["activity_kind"] = self.activity_tracker.activity_kind

    def mark_closed(self) -> None:
        self.is_active = False

    def request_cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()


def build_runtime_binding_snapshot(
    adapter: Any,
    session: AgentSession,
    *,
    protocol: str | None = None,
    model_verified: bool | None = None,
    reasoning_verified: bool | None = None,
    verification_method: str = "adapter_session_config",
) -> RuntimeBindingSnapshot:
    """Build the common binding record for adapters with explicit CLI/API flags."""

    config = session.config
    requested_model = str(config.model_id).strip() if config.model_id else None
    requested_reasoning = str(config.reasoning_effort).strip() if config.reasoning_effort else None
    if requested_model and requested_model.lower() in {"default", "auto"}:
        requested_model = None
    if requested_reasoning and requested_reasoning.lower() in {"default", "none", "auto"}:
        requested_reasoning = None
    effective_model = session.session_data.get("effective_model", requested_model)
    effective_reasoning = session.session_data.get(
        "effective_reasoning", requested_reasoning
    )
    model_ok = (
        bool(model_verified)
        if model_verified is not None
        else bool(not requested_model or effective_model == requested_model)
    )
    reasoning_ok = (
        bool(reasoning_verified)
        if reasoning_verified is not None
        else bool(not requested_reasoning or effective_reasoning == requested_reasoning)
    )
    return RuntimeBindingSnapshot(
        agent_id=str(getattr(adapter, "adapter_id", "unknown")),
        protocol=str(
            protocol
            or session.session_data.get("protocol")
            or session.session_data.get("mode")
            or "unknown"
        ),
        requested_model=requested_model,
        effective_model=str(effective_model) if effective_model else None,
        requested_reasoning=requested_reasoning,
        effective_reasoning=str(effective_reasoning) if effective_reasoning else None,
        model_verified=model_ok,
        reasoning_verified=reasoning_ok,
        binding_status="verified" if model_ok and reasoning_ok else "unverified",
        verification_method=verification_method,
        # Context window is only reported when the runtime actually told us.
        # Leaving it None keeps "Unknown == Unknown" instead of inventing 32K.
        context_window=session.session_data.get("effective_context_window"),
        context_window_source=session.session_data.get("effective_context_window_source"),
        context_window_mode=session.session_data.get("context_window_mode"),
    )


@runtime_checkable
class AgentAdapter(Protocol):
    adapter_id: str
    name: str
    output_streaming_mode: OutputStreamingMode | str

    async def probe(self) -> AgentProbeResult:
        """Probe binary existence, version, auth status, protocols, and capabilities."""
        ...

    async def capabilities(self) -> AgentCapabilityFlags:
        """Return the adapter's declared capability snapshot.

        ``probe`` remains the source of fresh discovery data.  This small
        accessor makes capabilities an explicit part of the runtime contract
        without forcing callers that already hold a probe result to probe a
        second time.
        """
        result = await self.probe()
        return result.capabilities

    async def list_models(self) -> list[ModelCapability]:
        """Dynamically list available models and their supported reasoning efforts."""
        ...

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        """Create a dedicated, isolated native agent session for a participant."""
        ...

    def build_permission_args(self, config: AgentSessionConfig) -> list[str]:
        """Map the session's explicit permission profile to CLI arguments.

        Adapters that expose native permission flags override this hook.  The
        default keeps third-party/test adapters compatible while making the
        permission mapping an explicit part of the adapter contract.
        """
        return []

    def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        """Stream events (chunks, tool calls, thinking, completion) for a turn."""
        ...

    async def cancel(self, session: AgentSession) -> None:
        """Cancel ongoing generation for the session."""
        ...

    async def close(self, session: AgentSession) -> None:
        """Clean up process or network resources for the session."""
        ...


def get_extended_search_paths() -> list[str]:
    """Return standard user and system binary directories for comprehensive CLI discovery."""
    paths: list[str] = []
    env_path = os.environ.get("PATH", "")
    if env_path:
        paths.extend(env_path.split(os.pathsep))

    home = os.path.expanduser("~")
    standard_dirs = [
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
        "/usr/bin",
        "/bin",
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".cargo", "bin"),
        os.path.join(home, ".npm-global", "bin"),
        os.path.join(home, ".bun", "bin"),
        os.path.join(home, ".grok", "bin"),
        os.path.join(home, ".gemini", "bin"),
        os.path.join(home, ".codex", "bin"),
        os.path.join(home, ".claude", "bin"),
        os.path.join(home, ".qoder", "bin"),
    ]
    for d in standard_dirs:
        if d not in paths and os.path.isdir(d):
            paths.append(d)

    return paths


def resolve_binary(candidates: list[str]) -> str | None:
    """Find the first matching executable binary path from candidate names or paths."""
    search_paths = get_extended_search_paths()
    search_path_str = os.pathsep.join(search_paths)

    for cand in candidates:
        if not cand:
            continue
        expanded = os.path.expanduser(cand)
        if os.path.isabs(expanded) and os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        which = shutil.which(cand, path=search_path_str)
        if which and os.path.isfile(which) and os.access(which, os.X_OK):
            return which
        base_name = os.path.basename(cand)
        which_base = shutil.which(base_name, path=search_path_str)
        if which_base and os.path.isfile(which_base) and os.access(which_base, os.X_OK):
            return which_base

    return None


async def safe_exec_cmd(
    argv: list[str],
    *,
    timeout: float = 8.0,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Execute a subprocess safely with argv (never shell=True) and strict timeout."""
    exec_env = dict(os.environ) if env is None else dict(env)
    current_path = exec_env.get("PATH", "")
    extended_paths = get_extended_search_paths()
    exec_env["PATH"] = os.pathsep.join(
        p for p in extended_paths if p in current_path.split(os.pathsep) or os.path.isdir(p)
    )

    proc: asyncio.subprocess.Process | None = None

    async def reap_process() -> None:
        if proc is None:
            return
        process = proc

        async def reap() -> None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            with contextlib.suppress(Exception):
                await process.communicate()

        cleanup = asyncio.create_task(reap())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=exec_env,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )
    except asyncio.CancelledError:
        await reap_process()
        raise
    except TimeoutError:
        await reap_process()
        return (-1, "", f"Command timed out after {timeout}s: {' '.join(argv)}")
    except Exception as exc:
        await reap_process()
        return (-1, "", f"Execution error: {exc}")
