"""Shared bounded subprocess transport for CLI Agent adapters."""

from __future__ import annotations

import asyncio
import contextlib

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.response_collector import sanitize_diagnostic
from persona_continuum.numeric import safe_acp_stream_limit

STDERR_TAIL_BYTES = 64 * 1024


def stream_limit_for_session(session: AgentSession, *, default: int = 16 * 1024 * 1024) -> int:
    extra = session.config.extra or {}
    value = extra.get("stream_limit_bytes")
    if value is None:
        value = extra.get("cli_stream_limit_bytes")
    if value is None:
        value = extra.get("acp_stream_limit_bytes")
    return safe_acp_stream_limit(value, default=default)


class SubprocessAgentTransport:
    """Bounded, observable process I/O owned by one Agent session."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        session: AgentSession,
        *,
        stream_limit: int,
        stderr_tail_bytes: int = STDERR_TAIL_BYTES,
    ) -> None:
        self.process = process
        self.session = session
        self.stream_limit = safe_acp_stream_limit(stream_limit)
        self._stderr_tail = bytearray()
        self._stderr_tail_bytes = max(1024, int(stderr_tail_bytes))
        self._stderr_task: asyncio.Task[None] | None = None
        if process.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(), name=f"agent-stderr-{session.config.session_id}"
            )
        session.session_data["active_proc"] = process
        session.session_data["active_transport"] = self
        session.session_data["stream_limit_bytes"] = self.stream_limit

    @classmethod
    async def spawn(
        cls,
        session: AgentSession,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        stdin: bool = True,
        stream_limit: int | None = None,
    ) -> SubprocessAgentTransport:
        limit = (
            stream_limit_for_session(session)
            if stream_limit is None
            else safe_acp_stream_limit(stream_limit)
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=limit,
            env=env,
            cwd=cwd,
        )
        from persona_continuum.performance.tracing import default_tracer

        default_tracer().incr_global("physical_process_spawn_count", 1)
        return cls(process, session, stream_limit=limit)

    async def _drain_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            self.session.touch_activity("stderr", byte_count=len(chunk))
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > self._stderr_tail_bytes:
                del self._stderr_tail[: len(self._stderr_tail) - self._stderr_tail_bytes]

    @property
    def stderr_tail(self) -> str:
        return sanitize_diagnostic(
            bytes(self._stderr_tail).decode("utf-8", errors="replace"), limit=4000
        )

    @property
    def process_alive(self) -> bool:
        return self.process.returncode is None

    async def read(self, size: int = 4096) -> bytes:
        stream = self.process.stdout
        if stream is None:
            return b""
        data = await stream.read(max(1, int(size)))
        if data:
            self.session.touch_activity("stdout", byte_count=len(data))
        return data

    async def readline(self) -> bytes:
        stream = self.process.stdout
        if stream is None:
            return b""
        data = await stream.readline()
        if data:
            self.session.touch_activity("stdout", byte_count=len(data))
        return data

    async def write(self, data: bytes) -> None:
        if self.process.stdin is None:
            return
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    def close_stdin(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()

    async def wait(self) -> int:
        result = await self.process.wait()
        if self._stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        return result

    async def terminate(self) -> None:
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()

    async def kill(self) -> None:
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()

    async def close(self, *, force: bool = False) -> None:
        if force and self.process.returncode is None:
            await self.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1.0)
            except Exception:
                await self.kill()
                with contextlib.suppress(Exception):
                    await self.process.wait()
        elif self.process.returncode is None:
            with contextlib.suppress(Exception):
                await self.process.wait()
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        self.session.session_data["stderr_tail"] = self.stderr_tail
        self.session.session_data["active_proc"] = None
        self.session.session_data["active_transport"] = None


__all__ = ["STDERR_TAIL_BYTES", "SubprocessAgentTransport", "stream_limit_for_session"]
