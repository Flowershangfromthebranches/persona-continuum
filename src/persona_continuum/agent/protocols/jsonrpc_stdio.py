from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

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
    RuntimeBindingSnapshot,
    SelectionStrategy,
    StructuredOutputMode,
)
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.response_collector import AgentProcessExitError, agent_error_event
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.auth.credentials import CredentialManager, build_runtime_environment
from persona_continuum.numeric import safe_acp_stream_limit


class JsonRpcStdioAdapter(AgentAdapter):
    def __init__(
        self,
        adapter_id: str,
        name: str,
        binary_candidates: list[str],
        version_args: list[str] | None = None,
        server_args: list[str] | None = None,
        default_models: list[ModelCapability] | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.name = name
        self.binary_candidates = binary_candidates
        self.version_args = version_args or ["--version"]
        self.server_args = server_args or []
        self._default_models = default_models or []
        self._resolved_binary: str | None = None
        self.credential_manager: CredentialManager | None = None
        self.prompt_mode = PromptMode.PROTOCOL_SPECIFIC
        self.structured_output_mode = StructuredOutputMode.PROMPT_ONLY
        self.output_streaming_mode = OutputStreamingMode.PROTOCOL_STREAM
        self.protocols = ["jsonrpc_stdio"]

    async def probe(self) -> AgentProbeResult:
        binary = self._find_binary()
        if not binary:
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.DISABLED,
                binary_path=None,
                version=None,
                auth_status=None,
                protocols=["jsonrpc_stdio"],
                capabilities=AgentCapabilityFlags(
                    streaming=True,
                    persistent_session=True,
                    model_selection=SelectionStrategy.STARTUP,
                    reasoning_selection=SelectionStrategy.UNSUPPORTED,
                    structured_output_mode=self.structured_output_mode,
                ),
                models=[],
                status_detail="Binary not found in PATH or standard locations",
            )

        code, out, err = await safe_exec_cmd([binary, *self.version_args], timeout=5.0)
        version_str = out.strip().split("\n")[0] if code == 0 and out else None
        status = AgentStatus.DETECTED if code == 0 else AgentStatus.BROKEN
        models = await self.list_models() if code == 0 else []

        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=status,
            binary_path=binary,
            version=version_str or "detected",
            auth_status="unverified",
            protocols=["jsonrpc_stdio"],
            capabilities=AgentCapabilityFlags(
                streaming=True,
                persistent_session=True,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_selection=SelectionStrategy.UNSUPPORTED,
                structured_output_mode=self.structured_output_mode,
            ),
            models=models,
            status_detail=(
                "JSON-RPC binary detected; protocol/auth smoke is unverified"
                if status == AgentStatus.DETECTED
                else (err or "probe failed")
            ),
        )

    async def list_models(self) -> list[ModelCapability]:
        if self._default_models:
            return self._default_models
        return [
            ModelCapability(
                id="default",
                display_name=f"{self.name} Default",
                provider=self.adapter_id,
                supported_reasoning_efforts=[],
                default_reasoning_effort=None,
                selectable=False,
                source="unverified_config",
                reasoning_selection=SelectionStrategy.UNSUPPORTED,
            )
        ]

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        binary = self._find_binary()
        if not binary:
            raise RuntimeError(f"Cannot start session: binary for {self.name} not found")

        cmd = [binary, *self.server_args]
        env = build_runtime_environment(
            credential_manager=self.credential_manager,
            credential_id=config.auth_profile_id,
            auth_env_var=config.auth_env_var,
        )

        session = AgentSession(
            config=config,
            is_active=True,
            session_data={
                "msg_id": 1,
                "protocol": "jsonrpc_stdio",
                "effective_model": config.model_id,
                "effective_reasoning": config.reasoning_effort,
            },
        )
        session._cancel_event = asyncio.Event()
        transport = await SubprocessAgentTransport.spawn(
            session,
            cmd,
            stdin=True,
            stream_limit=safe_acp_stream_limit(config.extra.get("stream_limit_bytes")),
            env=env,
            cwd=config.working_dir,
        )
        proc = transport.process
        session.session_data["proc"] = proc
        session.session_data["transport"] = transport
        return session

    async def bind_runtime(self, session: AgentSession) -> RuntimeBindingSnapshot:
        return build_runtime_binding_snapshot(
            self,
            session,
            protocol="jsonrpc_stdio",
            model_verified=True,
            reasoning_verified=True,
            verification_method="jsonrpc_request_payload",
        )

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
        transport: SubprocessAgentTransport | None = session.session_data.get("transport")
        if not proc or proc.returncode is not None:
            yield agent_error_event(
                AgentProcessExitError(
                    "Agent process not running or has terminated",
                    phase="session_prompt",
                    diagnostics={"protocol": "jsonrpc_stdio"},
                ),
                protocol="jsonrpc_stdio",
            )
            return

        msg_id: int = session.session_data.get("msg_id", 1)
        session.session_data["msg_id"] = msg_id + 1

        req = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "turn/start",
            "params": {
                "turnId": turn.turn_id,
                "message": AgentPromptRenderer.render_for_single_prompt(turn),
                "messages": AgentPromptRenderer.render_for_native_roles(turn),
                "systemPrompt": turn.system_prompt,
                "model": session.config.model_id,
                "reasoningEffort": session.config.reasoning_effort,
            },
        }

        try:
            line = json.dumps(req, ensure_ascii=False) + "\n"
            if transport is not None:
                await transport.write(line.encode("utf-8"))
            elif proc.stdin:
                proc.stdin.write(line.encode("utf-8"))
                await proc.stdin.drain()
            else:
                raise AgentProcessExitError(
                    "Agent process stdin is unavailable",
                    phase="session_prompt",
                    diagnostics={"protocol": "jsonrpc_stdio"},
                )

            full_content: list[str] = []
            while True:
                if session._cancel_event and session._cancel_event.is_set():
                    yield AgentEvent(
                        type=AgentEventType.DONE,
                        content="".join(full_content),
                        metadata={"cancelled": True},
                    )
                    return

                if transport is not None:
                    line_bytes = await transport.readline()
                elif proc.stdout:
                    line_bytes = await proc.stdout.readline()
                else:
                    break
                if not line_bytes:
                    break

                text = line_bytes.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                try:
                    data = json.loads(text)
                except Exception:
                    yield AgentEvent(type=AgentEventType.CHUNK, content=text + "\n")
                    full_content.append(text + "\n")
                    continue

                if isinstance(data, dict):
                    method_name = str(data.get("method") or "")
                    session.touch_activity(
                        "protocol_frame",
                        byte_count=len(line_bytes),
                        metadata={
                            "frame_type": method_name
                            or ("response" if "id" in data else "unknown"),
                            "event_type": method_name or str(data.get("type") or "unknown"),
                            "method": method_name or None,
                        },
                    )

                if data.get("id") == msg_id and "result" in data:
                    res_text = str(data["result"].get("content", ""))
                    if res_text:
                        yield AgentEvent(type=AgentEventType.CHUNK, content=res_text)
                        full_content.append(res_text)
                    yield AgentEvent(type=AgentEventType.DONE, content="".join(full_content))
                    return
                elif data.get("id") == msg_id and "error" in data:
                    err_msg = str(data["error"].get("message", "JSON-RPC error"))
                    yield AgentEvent(type=AgentEventType.ERROR, error=err_msg)
                    return
                elif data.get("method") == "turn/delta":
                    params = data.get("params", {})
                    delta = str(params.get("delta", ""))
                    if delta:
                        yield AgentEvent(type=AgentEventType.CHUNK, content=delta)
                        full_content.append(delta)
                elif data.get("method") == "turn/thinking":
                    params = data.get("params", {})
                    thinking = str(params.get("thinking", ""))
                    if thinking:
                        yield AgentEvent(type=AgentEventType.THINKING, thinking=thinking)
                elif data.get("method") == "turn/done":
                    yield AgentEvent(type=AgentEventType.DONE, content="".join(full_content))
                    return

            yield AgentEvent(type=AgentEventType.DONE, content="".join(full_content))

        except Exception as exc:
            yield agent_error_event(exc, protocol="jsonrpc_stdio")

    async def cancel(self, session: AgentSession) -> None:
        session.request_cancel()
        proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
        transport: SubprocessAgentTransport | None = session.session_data.get("transport")
        if proc and proc.returncode is None and (transport is not None or proc.stdin):
            cancel_req = {
                "jsonrpc": "2.0",
                "method": "turn/interrupt",
                "params": {"sessionId": session.config.session_id},
            }
            try:
                line = json.dumps(cancel_req) + "\n"
                if transport is not None:
                    await transport.write(line.encode("utf-8"))
                elif proc.stdin:
                    proc.stdin.write(line.encode("utf-8"))
                    await proc.stdin.drain()
            except Exception:
                pass

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()
        transport: SubprocessAgentTransport | None = session.session_data.get("transport")
        if transport is not None:
            await transport.close(force=True)
            return
        proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()

    def _find_binary(self) -> str | None:
        if self._resolved_binary:
            return self._resolved_binary
        self._resolved_binary = resolve_binary(self.binary_candidates)
        return self._resolved_binary
