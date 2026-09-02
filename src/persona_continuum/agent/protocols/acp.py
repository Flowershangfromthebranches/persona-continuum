from __future__ import annotations

import asyncio
import contextlib
import json
import os
from asyncio.exceptions import LimitOverrunError
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from persona_continuum.agent.adapter import (
    AgentAdapter,
    AgentSession,
    build_runtime_binding_snapshot,
    resolve_binary,
    safe_exec_cmd,
)
from persona_continuum.agent.context_capability import ContextWindowMode
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
from persona_continuum.agent.response_collector import (
    ACPFrameTooLargeError,
    AgentHardTimeoutError,
    AgentIdleTimeoutError,
    AgentProcessExitError,
    AgentProtocolError,
    AgentRuntimeError,
    AgentTransportError,
    ModelBindingUnverifiedError,
    ReasoningBindingUnverifiedError,
    sanitize_diagnostic,
)
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.auth.credentials import CredentialManager, build_runtime_environment
from persona_continuum.numeric import safe_acp_stream_limit, safe_int

ACP_STREAM_MIN_BYTES = 1 * 1024 * 1024
ACP_STREAM_LIMIT_BYTES = 16 * 1024 * 1024
ACP_STREAM_HARD_MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ACPFrame:
    payload: dict[str, Any]
    frame_bytes: int
    frame_type: str
    event_type: str
    tool_name: str | None = None


class ACPJsonLineReader:
    """Read complete, bounded ACP JSONL frames from one subprocess stream."""

    def __init__(self, stream: Any, *, limit_bytes: int) -> None:
        self.stream = stream
        self.limit_bytes = safe_acp_stream_limit(limit_bytes)
        self.corrupted = False
        self.invalid_frame_count = 0

    async def read_frame(
        self, *, timeout: float | None = None, phase: str = "session_update"
    ) -> ACPFrame:
        if self.corrupted:
            raise ACPProtocolError(
                "ACP stream is already corrupted",
                phase=phase,
                diagnostics={"protocol": "acp", "frame_type": "unknown"},
            )
        try:
            while True:
                line = (
                    await asyncio.wait_for(self.stream.readline(), timeout=timeout)
                    if timeout is not None
                    else await self.stream.readline()
                )
                if not line:
                    break
                if not isinstance(line, bytes):
                    line = bytes(line)
                if line.strip():
                    break
        except LimitOverrunError as exc:
            self.corrupted = True
            consumed = getattr(exc, "consumed", None)
            raise ACPFrameTooLargeError(
                configured_limit_bytes=self.limit_bytes,
                observed_frame_bytes=(
                    safe_int(consumed, default=None, minimum=0)
                    if consumed is not None
                    else None
                ),
                phase=phase,
            ) from None
        except ValueError as exc:
            # asyncio.StreamReader.readline() converts a LimitOverrunError
            # into this message after clearing its buffer.  The stream is
            # already framing-corrupted at this point, so never expose that
            # implementation detail or attempt to read the next frame.
            if "Separator is not found" not in str(exc) or "limit" not in str(exc):
                raise
            self.corrupted = True
            raise ACPFrameTooLargeError(
                configured_limit_bytes=self.limit_bytes,
                phase=phase,
            ) from None
        if not line:
            raise ACPProcessExitError(
                "ACP process exited before a complete JSON frame was received",
                phase=phase,
                diagnostics={"protocol": "acp", "frame_type": "unknown"},
            )
        if len(line) > self.limit_bytes:
            self.corrupted = True
            raise ACPFrameTooLargeError(
                configured_limit_bytes=self.limit_bytes,
                observed_frame_bytes=len(line),
                phase=phase,
            ) from None
        stripped = line.strip()
        try:
            data = json.loads(stripped.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.invalid_frame_count += 1
            raise ACPProtocolError(
                "ACP returned an invalid JSON frame",
                phase=phase,
                diagnostics={
                    "protocol": "acp",
                    "frame_type": "unknown",
                    "frame_bytes": len(line),
                    "invalid_frame_count": self.invalid_frame_count,
                },
            ) from exc
        if not isinstance(data, dict):
            raise ACPProtocolError(
                "ACP JSON frame must be an object",
                phase=phase,
                diagnostics={
                    "protocol": "acp",
                    "frame_type": "unknown",
                    "frame_bytes": len(line),
                },
            )
        return self._describe(data, len(line))

    @staticmethod
    def _describe(payload: dict[str, Any], frame_bytes: int) -> ACPFrame:
        method = str(payload.get("method") or "")
        frame_type = method or ("response" if "id" in payload else "unknown")
        event_type = "unknown"
        tool_name: str | None = None
        if method == "session/update":
            params = payload.get("params")
            update = params.get("update") if isinstance(params, dict) else None
            if isinstance(update, dict):
                event_type = str(update.get("sessionUpdate") or "unknown")
                if event_type in {"tool_call", "tool_call_update"}:
                    raw_name = update.get("title") or update.get("tool") or update.get("name")
                    if raw_name:
                        tool_name = sanitize_diagnostic(raw_name, limit=120)
            else:
                event_type = (
                    str(params.get("type") or "unknown")
                    if isinstance(params, dict)
                    else "unknown"
                )
        elif "error" in payload:
            event_type = "error"
        return ACPFrame(
            payload=payload,
            frame_bytes=frame_bytes,
            frame_type=frame_type,
            event_type=event_type,
            tool_name=tool_name,
        )


class AuthRequiredError(RuntimeError):
    """Raised when an ACP server requires authentication and credentials are missing."""


class InteractiveAuthRequiredError(AuthRequiredError):
    """Raised when ACP requires a terminal login that this headless client cannot perform."""


class ACPProtocolError(AgentProtocolError):
    """Raised when an ACP server violates or rejects the negotiated protocol flow."""


class ACPProcessExitError(AgentProcessExitError):
    """Raised when an ACP process closes before completing its frame."""


class ACPAdapter(AgentAdapter):
    # An ACP runtime reports the model/window it negotiated for a session.
    context_window_mode = ContextWindowMode.DISCOVERABLE

    def __init__(
        self,
        adapter_id: str,
        name: str,
        binary_candidates: list[str],
        version_args: list[str] | None = None,
        acp_args: list[str] | None = None,
        default_models: list[ModelCapability] | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.name = name
        self.binary_candidates = binary_candidates
        self.version_args = version_args or ["--version"]
        self.acp_args = acp_args or ["acp"]
        self._default_models = default_models or []
        self._resolved_binary: str | None = None
        self.credential_manager: CredentialManager | None = None
        self.prompt_mode = PromptMode.INLINE_SYSTEM
        self.structured_output_mode = StructuredOutputMode.UNKNOWN
        self.output_streaming_mode = OutputStreamingMode.PROTOCOL_STREAM
        self.protocols = ["acp"]
        self.require_verified_binding = False

    async def probe(self) -> AgentProbeResult:
        binary = self._find_binary()
        if not binary:
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.DISABLED,
                binary_path=None,
                version=None,
                protocols=["acp"],
                capabilities=AgentCapabilityFlags(
                    streaming=True,
                    persistent_session=True,
                    model_selection=SelectionStrategy.STARTUP,
                    reasoning_selection=(
                        SelectionStrategy.STARTUP
                        if any(model.supported_reasoning_efforts for model in self._default_models)
                        else SelectionStrategy.UNSUPPORTED
                    ),
                    structured_output_mode=self.structured_output_mode,
                ),
                models=[],
                status_detail="ACP binary not found on system",
            )

        code, out, err = await safe_exec_cmd([binary, *self.version_args], timeout=5.0)
        version_str = out.strip().split("\n")[0] if code == 0 and out else None
        status = AgentStatus.BROKEN if code != 0 else AgentStatus.DETECTED
        auth_status = "unverified"
        detail = err or "ACP protocol smoke test failed"
        if code == 0:
            probe_config = AgentSessionConfig(
                room_id="__probe__",
                participant_id="__probe__",
                persona_id="__probe__",
                working_dir=os.getcwd(),
                allow_mcp=False,
            )
            try:
                session = await self.create_session(probe_config)
                await self.close(session)
                status = AgentStatus.READY
                auth_status = "configured"
                detail = ""
            except InteractiveAuthRequiredError as exc:
                status = AgentStatus.AUTH_REQUIRED
                auth_status = "interactive_auth_required"
                detail = str(exc)
            except AuthRequiredError as exc:
                status = AgentStatus.AUTH_REQUIRED
                auth_status = "auth_required"
                detail = str(exc)
            except Exception as exc:
                status = AgentStatus.BROKEN
                auth_status = "protocol_error"
                detail = str(exc)

        models = await self.list_models()

        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=status,
            binary_path=binary,
            version=version_str or "detected",
            auth_status=auth_status,
            protocols=["acp"],
            capabilities=AgentCapabilityFlags(
                streaming=True,
                persistent_session=True,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_selection=(
                    SelectionStrategy.STARTUP
                    if any(model.supported_reasoning_efforts for model in models)
                    else SelectionStrategy.UNSUPPORTED
                ),
                structured_output_mode=self.structured_output_mode,
            ),
            models=models,
            status_detail=None if status == AgentStatus.READY else detail,
        )

    async def list_models(self) -> list[ModelCapability]:
        if self._default_models:
            return self._default_models
        binary = self._find_binary()
        if binary:
            code, out, _ = await safe_exec_cmd([binary, "models"], timeout=5.0)
            if code == 0 and out:
                parsed = self._parse_models(out)
                if parsed:
                    return parsed
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

    def build_client_capabilities(self, config: AgentSessionConfig) -> dict[str, Any]:
        """Advertise only ACP client callbacks implemented by Persona Continuum."""
        return {}

    def build_initialize_params(self, config: AgentSessionConfig) -> dict[str, Any]:
        return {
            "protocolVersion": 1,
            "clientCapabilities": self.build_client_capabilities(config),
        }

    def build_process_env(self, config: AgentSessionConfig) -> dict[str, str]:
        return build_runtime_environment(
            credential_manager=self.credential_manager,
            credential_id=config.auth_profile_id,
            auth_env_var=config.auth_env_var,
        )

    def build_session_new_params(self, config: AgentSessionConfig) -> dict[str, Any]:
        """Return only protocol-safe session/new parameters.

        ACP implementations differ in how model and reasoning are selected;
        concrete adapters opt into those fields explicitly.  This keeps the
        base transport from claiming a selection it did not negotiate.
        """

        return {
            "cwd": config.working_dir or os.getcwd(),
            "mcpServers": config.mcp_servers if config.allow_mcp else [],
        }

    def _runtime_binding_from_session_result(
        self,
        config: AgentSessionConfig,
        session_result: Any,
    ) -> RuntimeBindingSnapshot:
        requested_model = self._requested_value(config.model_id)
        requested_reasoning = self._requested_value(config.reasoning_effort)
        result = session_result if isinstance(session_result, dict) else {}
        nested = result.get("session") if isinstance(result.get("session"), dict) else {}
        model = self._first_value(
            result,
            nested,
            "effectiveModel",
            "effective_model",
            "model",
            "modelId",
            "model_id",
        )
        reasoning = self._first_value(
            result,
            nested,
            "effectiveReasoning",
            "effective_reasoning",
            "reasoningEffort",
            "reasoning_effort",
        )
        model_text = str(model).strip() if model is not None else None
        reasoning_text = str(reasoning).strip() if reasoning is not None else None
        model_verified = bool(not requested_model or model_text == requested_model)
        reasoning_verified = bool(not requested_reasoning or reasoning_text == requested_reasoning)
        snapshot = RuntimeBindingSnapshot(
            agent_id=self.adapter_id,
            protocol="acp",
            requested_model=requested_model,
            effective_model=model_text,
            requested_reasoning=requested_reasoning,
            effective_reasoning=reasoning_text,
            model_verified=model_verified,
            reasoning_verified=reasoning_verified,
            binding_status="verified" if model_verified and reasoning_verified else "unverified",
            verification_method="acp_session_new_response",
            diagnostics={
                "session_new_result_keys": sorted(str(key) for key in result),
            },
        )
        if self.require_verified_binding and requested_model and not model_verified:
            raise ModelBindingUnverifiedError(
                "Selected model was not verified by ACP session/new",
                phase="session_binding",
                diagnostics={
                    "protocol": "acp",
                    "requested_model": requested_model,
                    "effective_model": model_text,
                    **snapshot.diagnostics,
                },
            )
        if self.require_verified_binding and requested_reasoning and not reasoning_verified:
            raise ReasoningBindingUnverifiedError(
                "Selected reasoning effort was not verified by ACP session/new",
                phase="session_binding",
                diagnostics={
                    "protocol": "acp",
                    "requested_reasoning": requested_reasoning,
                    "effective_reasoning": reasoning_text,
                    **snapshot.diagnostics,
                },
            )
        return snapshot

    @staticmethod
    def _requested_value(value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text if text and text.lower() not in {"default", "auto", "none"} else None

    @staticmethod
    def _first_value(primary: Any, secondary: Any, *keys: str) -> Any:
        for value in (primary, secondary):
            if isinstance(value, dict):
                for key in keys:
                    if value.get(key) is not None:
                        return value[key]
        return None

    @staticmethod
    def stream_limit_bytes(config: AgentSessionConfig) -> int:
        """Resolve a finite ACP stream limit from session config."""

        return safe_acp_stream_limit(
            config.extra.get("acp_stream_limit_bytes"), default=ACP_STREAM_LIMIT_BYTES
        )

    @staticmethod
    def _auth_method_id(auth_method: dict[str, Any]) -> str:
        raw_id = auth_method.get("id") or auth_method.get("methodId")
        return str(raw_id) if raw_id else ""

    def select_auth_method(
        self,
        auth_methods: list[dict[str, Any]],
        config: AgentSessionConfig,
    ) -> dict[str, Any] | None:
        if not auth_methods:
            return None

        terminal_method: dict[str, Any] | None = None
        for method in auth_methods:
            method_id = self._auth_method_id(method)
            method_type = str(method.get("type") or "").lower()
            normalized_id = method_id.lower()
            if method_type == "terminal" or "terminal" in normalized_id:
                terminal_method = method
                continue
            if method_type in {"agent", "default", "cached_token", "cached"}:
                return method
            if normalized_id in {"agent", "default", "agent-default", "cached_token"}:
                return method
            if (
                method_type in {"api_key", "api-key", "key"}
                and self.credential_manager is not None
                and self.credential_manager.has(config.auth_profile_id)
            ):
                return method

        if terminal_method is not None:
            raise InteractiveAuthRequiredError(
                f"Interactive terminal authentication is required for {self.name}"
            )
        raise AuthRequiredError(f"Authentication required for {self.name}")

    def build_authenticate_params(
        self,
        auth_method: dict[str, Any],
        config: AgentSessionConfig,
    ) -> dict[str, Any]:
        del config
        method_id = self._auth_method_id(auth_method)
        if not method_id:
            raise ACPProtocolError("ACP auth method is missing an id")
        return {"methodId": method_id}

    def supports_persistent_conversation(self, session: AgentSession) -> bool:
        # An established ACP session id means the agent process already owns
        # this conversation's server-side state.
        return bool(session.session_data.get("acp_session_id"))

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        binary = self._find_binary()
        if not binary:
            raise RuntimeError(f"Cannot start ACP session: binary for {self.name} not found")

        # Startup flags must only use acp_args, no unverified flags
        cmd = [binary, *self.acp_args]

        stream_limit = self.stream_limit_bytes(config)

        session = AgentSession(
            config=config,
            is_active=True,
            session_data={"acp_stream_limit_bytes": stream_limit, "protocol": "acp"},
        )
        session._cancel_event = asyncio.Event()
        transport = await SubprocessAgentTransport.spawn(
            session,
            cmd,
            stdin=True,
            stream_limit=stream_limit,
            env=self.build_process_env(config),
            cwd=config.working_dir,
        )
        proc = transport.process
        session.session_data["proc"] = proc
        session.session_data["acp_transport"] = transport
        try:
            acp_session_id, next_id, reader, binding = (
                await self._initialize_auth_and_create_session(
                    proc, config, stream_limit=stream_limit, stream=transport
                )
            )
        except BaseException:
            await transport.close(force=True)
            raise

        session.session_data.update(
            {
                "msg_id": next_id + 1,
                "acp_session_id": acp_session_id,
                "acp_reader": reader,
                "runtime_binding": binding.model_dump(mode="json"),
            }
        )
        return session

    async def _initialize_auth_and_create_session(
        self,
        proc: asyncio.subprocess.Process,
        config: AgentSessionConfig,
        *,
        stream_limit: int,
        stream: Any | None = None,
    ) -> tuple[str, int, ACPJsonLineReader, RuntimeBindingSnapshot]:
        frame_stream = stream or proc.stdout
        if frame_stream is None:
            raise ACPProtocolError("ACP process stdout is unavailable", phase="initialize")
        reader = ACPJsonLineReader(frame_stream, limit_bytes=stream_limit)
        request_id = 1
        await self._write_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": self.build_initialize_params(config),
            },
            transport=stream,
        )
        initialize = await self._read_response(reader, request_id, phase="initialize")
        if "error" in initialize:
            raise ACPProtocolError(f"ACP initialize failed: {initialize['error']}")
        result = initialize.get("result")
        if not isinstance(result, dict):
            raise ACPProtocolError("ACP initialize result must be an object")
        raw_auth_methods = result.get("authMethods", [])
        if not isinstance(raw_auth_methods, list) or not all(
            isinstance(item, dict) for item in raw_auth_methods
        ):
            raise ACPProtocolError("ACP authMethods must be an array of objects")
        auth_methods = list(raw_auth_methods)
        auth_method = self.select_auth_method(auth_methods, config)

        if auth_method is not None:
            request_id += 1
            await self._write_request(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "authenticate",
                    "params": self.build_authenticate_params(auth_method, config),
                },
                transport=stream,
            )
            authenticate = await self._read_response(reader, request_id, phase="authenticate")
            if "error" in authenticate:
                raise ACPProtocolError(f"ACP authentication failed: {authenticate['error']}")
            if "result" not in authenticate:
                raise ACPProtocolError("ACP authenticate response is missing result")

        request_id += 1
        await self._write_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/new",
                "params": self.build_session_new_params(config),
            },
            transport=stream,
        )
        session_response = await self._read_response(reader, request_id, phase="session_new")
        if "error" in session_response:
            raise ACPProtocolError(f"ACP session/new failed: {session_response['error']}")
        session_result = session_response.get("result")
        if isinstance(session_result, dict):
            nested = session_result.get("session")
            nested_id = nested.get("id") if isinstance(nested, dict) else None
            session_id = session_result.get("sessionId") or nested_id or session_result.get("id")
        else:
            session_id = session_result
        if not isinstance(session_id, str) or not session_id:
            raise ACPProtocolError(
                f"ACP session/new failed to return a valid sessionId for {self.name}"
            )
        binding = self._runtime_binding_from_session_result(config, session_result)
        return session_id, request_id, reader, binding

    async def _write_request(
        self,
        proc: asyncio.subprocess.Process,
        request: dict[str, Any],
        *,
        transport: Any | None = None,
    ) -> None:
        if transport is not None and callable(getattr(transport, "write", None)):
            await transport.write((json.dumps(request) + "\n").encode("utf-8"))
            return
        if not proc.stdin:
            raise ACPProtocolError("ACP process stdin is unavailable")
        proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def _read_response(
        self,
        reader: ACPJsonLineReader,
        request_id: int,
        *,
        phase: str,
    ) -> dict[str, Any]:
        while True:
            try:
                frame = await reader.read_frame(timeout=15.0, phase=phase)
            except TimeoutError:
                raise AgentIdleTimeoutError(
                    "ACP protocol response exceeded the initialization idle timeout",
                    phase=phase,
                    diagnostics={"protocol": "acp", "idle_timeout_seconds": 15.0},
                ) from None
            data = frame.payload
            if isinstance(data, dict) and data.get("id") == request_id:
                return data

    @staticmethod
    async def _drain_stderr(
        stream: Any,
        *,
        session: AgentSession | None = None,
        max_bytes: int = 64 * 1024,
    ) -> bytes:
        """Drain ACP stderr without allowing diagnostics to grow unbounded."""

        tail = bytearray()
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            if session is not None:
                session.touch_activity("stderr", byte_count=len(chunk))
            tail.extend(chunk)
            if len(tail) > max_bytes:
                del tail[: len(tail) - max_bytes]
        return bytes(tail)

    async def bind_runtime(self, session: AgentSession) -> RuntimeBindingSnapshot:
        raw = session.session_data.get("runtime_binding")
        if isinstance(raw, RuntimeBindingSnapshot):
            return raw
        if isinstance(raw, dict):
            return RuntimeBindingSnapshot.model_validate(raw)
        return build_runtime_binding_snapshot(
            self,
            session,
            protocol="acp",
            model_verified=not bool(self._requested_value(session.config.model_id)),
            reasoning_verified=not bool(
                self._requested_value(session.config.reasoning_effort)
            ),
            verification_method="acp_session_missing_binding",
        )

    async def _terminate_process(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except Exception:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        try:
            async for event in self._send_acp(session, turn):
                yield event
        except ACPFrameTooLargeError as exc:
            await self._close_corrupted_session(session)
            yield self._error_event(exc)
        except (AgentIdleTimeoutError, AgentHardTimeoutError) as exc:
            await self.close(session)
            yield self._error_event(exc)
        except AgentRuntimeError as exc:
            await self.close(session)
            yield self._error_event(exc)
        except Exception as exc:
            await self.close(session)
            yield self._error_event(
                AgentTransportError(
                    "ACP transport failed",
                    phase="session_update",
                    diagnostics={
                        "protocol": "acp",
                        "exception_type": type(exc).__name__,
                    },
                )
            )

    async def _send_acp(
        self, session: AgentSession, turn: AgentTurn
    ) -> AsyncIterator[AgentEvent]:
        proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
        transport = session.session_data.get("acp_transport")
        if not proc or proc.returncode is not None:
            raise ACPProcessExitError(
                "ACP process not running or has exited",
                phase="session_prompt",
                diagnostics={"protocol": "acp"},
            )
        acp_session_id = session.session_data.get("acp_session_id")
        if not acp_session_id:
            raise ACPProtocolError(
                "ACP session/new must be performed before session/prompt",
                phase="session_prompt",
                diagnostics={"protocol": "acp"},
            )
        if transport is None and not proc.stdin:
            raise ACPProtocolError(
                "ACP process stdin is unavailable",
                phase="session_prompt",
                diagnostics={"protocol": "acp"},
            )
        reader = session.session_data.get("acp_reader")
        if not isinstance(reader, ACPJsonLineReader):
            transport = session.session_data.get("acp_transport")
            frame_stream = transport or proc.stdout
            if frame_stream is None:
                raise ACPProtocolError(
                    "ACP process stdout is unavailable",
                    phase="session_prompt",
                    diagnostics={"protocol": "acp"},
                )
            reader = ACPJsonLineReader(
                frame_stream,
                limit_bytes=safe_acp_stream_limit(
                    session.session_data.get("acp_stream_limit_bytes"),
                    default=self.stream_limit_bytes(session.config),
                ),
            )
            session.session_data["acp_reader"] = reader

        msg_id: int = session.session_data.get("msg_id", 10)
        session.session_data["msg_id"] = msg_id + 1
        await self._write_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": acp_session_id,
                    "prompt": [
                        {
                            "type": "text",
                            "text": AgentPromptRenderer.render_for_single_prompt(turn),
                        }
                    ],
                },
            },
            transport=transport,
        )

        full_content: list[str] = []
        while True:
            if session._cancel_event and session._cancel_event.is_set():
                cancel_req = {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": acp_session_id},
                }
                with contextlib.suppress(Exception):
                    await self._write_request(proc, cancel_req, transport=transport)
                yield AgentEvent(
                    type=AgentEventType.DONE,
                    content="".join(full_content),
                    metadata={"cancelled": True, "protocol": "acp"},
                )
                return

            # The Runtime Executor owns turn idle/first-response/hard budgets.
            # This adapter only reads the next complete protocol frame.
            frame = await reader.read_frame(phase="session_update")
            data = frame.payload
            frame_metadata = self._frame_metadata(frame)
            session.touch_activity(
                "acp_frame", byte_count=frame.frame_bytes, metadata=frame_metadata
            )
            frame_metadata["activity_recorded"] = True

            if "error" in data and data.get("id") != msg_id:
                raise ACPProtocolError(
                    "ACP returned a protocol error frame",
                    phase="session_update",
                    diagnostics=frame_metadata,
                )

            if data.get("id") == msg_id:
                if "result" in data:
                    result = data["result"]
                    stop_reason = result.get("stopReason") if isinstance(result, dict) else None
                    content = str(result.get("content", "")) if isinstance(result, dict) else ""
                    if content:
                        yield AgentEvent(
                            type=AgentEventType.CHUNK,
                            content=content,
                            metadata=frame_metadata,
                        )
                        full_content.append(content)
                    is_cancelled = (stop_reason in {"cancelled", "interrupted"}) or bool(
                        session._cancel_event and session._cancel_event.is_set()
                    )
                    yield AgentEvent(
                        type=AgentEventType.DONE,
                        content="".join(full_content),
                        metadata={
                            **frame_metadata,
                            "cancelled": is_cancelled,
                            "stop_reason": stop_reason,
                        },
                    )
                    return
                if "error" in data:
                    raise ACPProtocolError(
                        "ACP session/prompt returned a protocol error",
                        phase="session_prompt",
                        diagnostics={**frame_metadata, "failure_code": "AGENT_PROTOCOL_ERROR"},
                    )

            method = data.get("method")
            params = data.get("params", {})
            if method == "session/update":
                update = params.get("update") if isinstance(params, dict) else None
                if isinstance(update, dict):
                    session_update = str(update.get("sessionUpdate") or "unknown")
                    if session_update == "agent_message_chunk":
                        content_obj = update.get("content")
                        chunk = ""
                        if isinstance(content_obj, dict):
                            chunk = str(content_obj.get("text", ""))
                        elif isinstance(content_obj, str):
                            chunk = content_obj
                        elif "text" in update:
                            chunk = str(update["text"])
                        if chunk:
                            yield AgentEvent(
                                type=AgentEventType.CHUNK,
                                content=chunk,
                                metadata=frame_metadata,
                            )
                            full_content.append(chunk)
                    elif session_update == "agent_thought_chunk":
                        content_obj = update.get("content")
                        thought = ""
                        if isinstance(content_obj, dict):
                            thought = str(content_obj.get("text", ""))
                        elif isinstance(content_obj, str):
                            thought = content_obj
                        elif "thought" in update:
                            thought = str(update["thought"])
                        elif "delta" in update:
                            thought = str(update["delta"])
                        if thought:
                            yield AgentEvent(
                                type=AgentEventType.THINKING,
                                thinking=thought,
                                metadata=frame_metadata,
                            )
                    elif session_update in {"tool_call", "tool_call_update"}:
                        yield AgentEvent(
                            type=AgentEventType.TOOL_CALL,
                            tool_call_id=update.get("callId") or update.get("toolCallId"),
                            tool_name=update.get("title")
                            or update.get("tool")
                            or update.get("name"),
                            tool_arguments=update.get("arguments") or update.get("input"),
                            metadata=frame_metadata,
                        )
                else:
                    update_type = (
                        params.get("type", "chunk")
                        if isinstance(params, dict)
                        else "unknown"
                    )
                    if update_type == "chunk":
                        chunk = str(params.get("delta") or params.get("content") or "")
                        if chunk:
                            yield AgentEvent(
                                type=AgentEventType.CHUNK,
                                content=chunk,
                                metadata=frame_metadata,
                            )
                            full_content.append(chunk)
                    elif update_type == "thinking":
                        thinking = str(params.get("delta") or params.get("content") or "")
                        if thinking:
                            yield AgentEvent(
                                type=AgentEventType.THINKING,
                                thinking=thinking,
                                metadata=frame_metadata,
                            )
                    elif update_type == "tool_call":
                        yield AgentEvent(
                            type=AgentEventType.TOOL_CALL,
                            tool_call_id=params.get("callId"),
                            tool_name=params.get("tool"),
                            tool_arguments=params.get("arguments"),
                            metadata=frame_metadata,
                        )
            elif method == "session/done":
                yield AgentEvent(
                    type=AgentEventType.DONE,
                    content="".join(full_content),
                    metadata=frame_metadata,
                )
                return

    @staticmethod
    def _frame_metadata(frame: ACPFrame) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "protocol": "acp",
            "frame_bytes": frame.frame_bytes,
            "frame_type": frame.frame_type,
            "event_type": frame.event_type,
        }
        if frame.tool_name:
            metadata["tool_name"] = frame.tool_name
        return metadata

    @staticmethod
    def _error_event(exc: AgentRuntimeError) -> AgentEvent:
        failure = exc.as_failure()
        diagnostics = dict(exc.diagnostics)
        metadata: dict[str, Any] = {
            "protocol": str(diagnostics.get("protocol") or "acp"),
            "failure_code": str(diagnostics.get("code") or exc.code),
            "failure": failure,
            **{
                key: diagnostics[key]
                for key in (
                    "configured_limit_bytes",
                    "observed_frame_bytes",
                    "frame_bytes",
                    "frame_type",
                    "event_type",
                    "tool_name",
                    "timeout_seconds",
                    "idle_timeout_seconds",
                    "hard_timeout_seconds",
                    "last_activity_at",
                    "last_activity_age_seconds",
                )
                if key in diagnostics
            },
        }
        return AgentEvent(
            type=AgentEventType.ERROR,
            error=str(exc),
            metadata=metadata,
        )

    async def _close_corrupted_session(self, session: AgentSession) -> None:
        session.session_data["acp_transport_corrupted"] = True
        await self.close(session)

    async def cancel(self, session: AgentSession) -> None:
        session.request_cancel()
        proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
        transport = session.session_data.get("acp_transport")
        acp_session_id = session.session_data.get("acp_session_id")
        if proc and proc.returncode is None and acp_session_id and (
            transport is not None or proc.stdin
        ):
            cancel_req = {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": acp_session_id},
            }
            with contextlib.suppress(Exception):
                await self._write_request(proc, cancel_req, transport=transport)

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()
        transport = session.session_data.get("acp_transport")
        if isinstance(transport, SubprocessAgentTransport):
            await transport.close(force=True)
            session.session_data["proc"] = None
            return
        proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        stderr_task = session.session_data.get("stderr_task")
        if isinstance(stderr_task, asyncio.Task):
            if not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
            elif not stderr_task.cancelled():
                with contextlib.suppress(Exception):
                    tail = stderr_task.result()
                    session.session_data["stderr_tail"] = sanitize_diagnostic(
                        tail.decode("utf-8", errors="replace") if isinstance(tail, bytes) else tail,
                        limit=4000,
                    )
        session.session_data["proc"] = None

    def _find_binary(self) -> str | None:
        if self._resolved_binary:
            return self._resolved_binary
        self._resolved_binary = resolve_binary(self.binary_candidates)
        return self._resolved_binary

    def _parse_models(self, raw_text: str) -> list[ModelCapability]:
        models: list[ModelCapability] = []
        for line in raw_text.splitlines():
            line = line.strip().lstrip("*-• ")
            if (
                not line
                or line.lower().startswith("available")
                or line.lower().startswith("default")
            ):
                continue
            parts = line.split()
            if parts:
                model_id = parts[0]
                models.append(
                    ModelCapability(
                        id=model_id,
                        display_name=model_id,
                        provider=self.adapter_id,
                        supported_reasoning_efforts=[],
                        default_reasoning_effort=None,
                        source="official_cli",
                        reasoning_selection=SelectionStrategy.UNSUPPORTED,
                    )
                )
        return models
