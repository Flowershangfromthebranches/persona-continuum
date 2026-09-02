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
from persona_continuum.agent.response_collector import (
    RuntimeUnavailableError,
    agent_error_event,
)
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.auth.credentials import CredentialManager, build_runtime_environment


class StreamingJsonCliAdapter(AgentAdapter):
    def __init__(
        self,
        adapter_id: str,
        name: str,
        binary_candidates: list[str],
        version_args: list[str] | None = None,
        exec_args: list[str] | None = None,
        default_models: list[ModelCapability] | None = None,
        model_flag: str = "--model",
        reasoning_flag: str = "--reasoning-effort",
    ) -> None:
        self.adapter_id = adapter_id
        self.name = name
        self.binary_candidates = binary_candidates
        self.version_args = version_args or ["--version"]
        self.exec_args = exec_args or ["--json"]
        self._default_models = default_models or []
        self.model_flag = model_flag
        self.reasoning_flag = reasoning_flag
        self._resolved_binary: str | None = None
        self.credential_manager: CredentialManager | None = None
        self.prompt_mode = PromptMode.INLINE_SYSTEM
        self.structured_output_mode = StructuredOutputMode.PROMPT_ONLY
        self.output_streaming_mode = OutputStreamingMode.PROTOCOL_STREAM

    async def probe(self) -> AgentProbeResult:
        binary = self._find_binary()
        if not binary:
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.DISABLED,
                binary_path=None,
                version=None,
                protocols=["streaming_json_cli"],
                capabilities=AgentCapabilityFlags(
                    streaming=True,
                    persistent_session=False,
                    model_selection=SelectionStrategy.STARTUP,
                    reasoning_selection=SelectionStrategy.UNSUPPORTED,
                    structured_output_mode=self.structured_output_mode,
                ),
                models=[],
                status_detail="CLI binary not found on system",
            )

        code, out, err = await safe_exec_cmd([binary, *self.version_args], timeout=5.0)
        version_str = out.strip().split("\n")[0] if code == 0 and out else None
        # Honesty gate: CLI detected but unverified protocol defaults to DETECTED
        status = AgentStatus.DETECTED if code == 0 else AgentStatus.BROKEN
        models = await self.list_models() if code == 0 else []

        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=status,
            binary_path=binary,
            version=version_str or "detected",
            auth_status="unverified",
            protocols=["streaming_json_cli"],
            capabilities=AgentCapabilityFlags(
                streaming=True,
                persistent_session=False,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_selection=SelectionStrategy.UNSUPPORTED,
                structured_output_mode=self.structured_output_mode,
            ),
            models=models,
            status_detail="CLI binary detected; auth and programmable streaming unverified",
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

        session = AgentSession(
            config=config,
            is_active=True,
            session_data={
                "binary": binary,
                "active_proc": None,
                "protocol": "streaming_json_cli",
                "effective_model": config.model_id if config.model_id else None,
                "effective_reasoning": config.reasoning_effort if config.reasoning_effort else None,
                "model_selection_applied": not bool(config.model_id) or bool(self.model_flag),
                "reasoning_selection_applied": not bool(config.reasoning_effort)
                or bool(self.reasoning_flag),
            },
        )
        session._cancel_event = asyncio.Event()
        return session

    async def bind_runtime(self, session: AgentSession) -> RuntimeBindingSnapshot:
        return build_runtime_binding_snapshot(
            self,
            session,
            protocol="streaming_json_cli",
            model_verified=bool(not session.config.model_id or self.model_flag),
            reasoning_verified=bool(
                not session.config.reasoning_effort or self.reasoning_flag
            ),
        )

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        binary: str = session.session_data.get("binary") or self._find_binary() or ""
        if not binary:
            yield agent_error_event(
                RuntimeUnavailableError(
                    f"Cannot start session: binary for {self.name} not found",
                    phase="session_prompt",
                ),
                protocol="streaming_json_cli",
            )
            return

        cmd = [binary, *self.exec_args]
        if session.config.model_id:
            cmd.extend([self.model_flag, session.config.model_id])
        if session.config.reasoning_effort:
            cmd.extend([self.reasoning_flag, session.config.reasoning_effort])

        prompt = AgentPromptRenderer.render_for_single_prompt(turn)

        env = build_runtime_environment(
            credential_manager=self.credential_manager,
            credential_id=session.config.auth_profile_id,
            auth_env_var=session.config.auth_env_var,
        )

        transport: SubprocessAgentTransport | None = None
        try:
            transport = await SubprocessAgentTransport.spawn(
                session,
                cmd,
                stdin=True,
                env=env,
                cwd=session.config.working_dir,
            )

            await transport.write(prompt.encode("utf-8"))
            transport.close_stdin()

            full_content: list[str] = []
            while True:
                if session._cancel_event and session._cancel_event.is_set():
                    await transport.kill()
                    yield AgentEvent(
                        type=AgentEventType.DONE,
                        content="".join(full_content),
                        metadata={"cancelled": True},
                    )
                    return

                line_bytes = await transport.readline()
                if not line_bytes:
                    break

                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                    if isinstance(data, dict):
                        event_type = str(data.get("type") or data.get("event") or "unknown")
                        session.touch_activity(
                            "protocol_frame",
                            byte_count=len(line_bytes),
                            metadata={"frame_type": event_type, "event_type": event_type},
                        )
                        chunk = (
                            data.get("delta")
                            or data.get("text")
                            or data.get("content")
                            or (data.get("choices", [{}])[0].get("delta", {}).get("content"))
                        )
                        if chunk:
                            session.touch_activity("chunk", metadata={"event_type": event_type})
                            yield AgentEvent(
                                type=AgentEventType.CHUNK,
                                content=str(chunk),
                                metadata={"activity_recorded": True},
                            )
                            full_content.append(str(chunk))
                        elif data.get("type") == "thinking":
                            session.touch_activity("thinking", metadata={"event_type": event_type})
                            yield AgentEvent(
                                type=AgentEventType.THINKING,
                                thinking=str(data.get("content", "")),
                                metadata={"activity_recorded": True},
                            )
                        elif data.get("type") == "tool_call":
                            session.touch_activity(
                                "tool_call",
                                metadata={"event_type": event_type, "tool_name": data.get("name")},
                            )
                            yield AgentEvent(
                                type=AgentEventType.TOOL_CALL,
                                tool_call_id=data.get("id"),
                                tool_name=data.get("name"),
                                tool_arguments=data.get("arguments"),
                                metadata={"activity_recorded": True},
                            )
                        continue
                except (json.JSONDecodeError, TypeError, IndexError, AttributeError):
                    pass

                yield AgentEvent(
                    type=AgentEventType.CHUNK,
                    content=line_str + "\n",
                    metadata={"activity_recorded": True},
                )
                full_content.append(line_str + "\n")
                session.touch_activity("chunk", metadata={"event_type": "text_line"})

            returncode = await transport.wait()
            diagnostics = {
                "returncode": returncode,
                "stderr_tail": transport.stderr_tail,
                "output_streaming_mode": self.output_streaming_mode.value,
            }
            if returncode and not full_content:
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error="AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                    metadata={
                        "failure_code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                        **diagnostics,
                    },
                )
                return
            yield AgentEvent(
                type=AgentEventType.DONE,
                content="".join(full_content),
                metadata=diagnostics,
            )

        except Exception as exc:
            yield agent_error_event(exc, protocol="streaming_json_cli")
        finally:
            if transport is not None:
                await transport.close(force=transport.process_alive)

    async def cancel(self, session: AgentSession) -> None:
        session.request_cancel()
        proc: asyncio.subprocess.Process | None = session.session_data.get("active_proc")
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()
        await self.cancel(session)

    def _find_binary(self) -> str | None:
        if self._resolved_binary:
            return self._resolved_binary
        self._resolved_binary = resolve_binary(self.binary_candidates)
        return self._resolved_binary
