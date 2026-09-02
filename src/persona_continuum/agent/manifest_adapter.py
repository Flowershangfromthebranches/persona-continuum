from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
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
from persona_continuum.agent.response_collector import agent_error_event
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.auth.credentials import CredentialManager, build_runtime_environment


class ManifestAgentAdapter(AgentAdapter):
    """Dynamically loaded adapter constructed from a TOML or JSON manifest file."""

    def __init__(self, manifest_data: dict[str, Any], source_path: Path | None = None) -> None:
        self.adapter_id: str = str(manifest_data.get("id", "custom_agent"))
        self.name: str = str(manifest_data.get("name", self.adapter_id))
        self.source_path = source_path
        self.manifest = manifest_data

        self.binary_candidates: list[str] = list(
            manifest_data.get("binary_candidates") or [manifest_data.get("binary", self.adapter_id)]
        )
        self.version_command: list[str] = list(
            manifest_data.get("version_command") or ["--version"]
        )
        self.auth_command: list[str] | None = manifest_data.get("auth_command")
        self.models_command: list[str] | None = manifest_data.get("models_command")
        self.invocation_command: list[str] = list(manifest_data.get("invocation_command") or [])
        self.protocol: str = str(manifest_data.get("protocol", "plain_cli"))
        self.model_flag: str = str(manifest_data.get("model_flag", "--model"))
        self.reasoning_flag: str = str(manifest_data.get("reasoning_flag", "--effort"))
        self.env_vars: dict[str, str] = dict(manifest_data.get("env_vars", {}))
        raw_prompt_mode = str(manifest_data.get("prompt_mode", PromptMode.INLINE_SYSTEM))
        try:
            self.prompt_mode = PromptMode(raw_prompt_mode)
        except ValueError:
            self.prompt_mode = PromptMode.PROTOCOL_SPECIFIC
        raw_output_mode = str(
            manifest_data.get("structured_output_mode", StructuredOutputMode.UNKNOWN)
        )
        try:
            self.structured_output_mode = StructuredOutputMode(raw_output_mode)
        except ValueError:
            self.structured_output_mode = StructuredOutputMode.UNKNOWN
        self.output_streaming_mode = (
            OutputStreamingMode.PROTOCOL_STREAM
            if self.protocol in {"streaming_json_cli", "json_stream", "jsonrpc_stdio", "acp"}
            else OutputStreamingMode.BUFFERED_FINAL
        )
        self.protocols = [self.protocol]

        raw_models = manifest_data.get("models", [])
        self._default_models: list[ModelCapability] = []
        for m in raw_models:
            if isinstance(m, dict):
                self._default_models.append(
                    ModelCapability(
                        id=str(m.get("id", "default")),
                        display_name=str(m.get("name", m.get("id", "Default"))),
                        provider=str(m.get("provider", self.adapter_id)),
                        supported_reasoning_efforts=list(m.get("supported_reasoning_efforts", [])),
                        default_reasoning_effort=m.get("default_reasoning_effort"),
                        selectable=bool(m.get("selectable", True)),
                        source="manifest",
                        reasoning_selection=(
                            SelectionStrategy.CONFIG
                            if m.get("supported_reasoning_efforts")
                            else SelectionStrategy.UNSUPPORTED
                        ),
                    )
                )
            elif isinstance(m, str):
                self._default_models.append(
                    ModelCapability(
                        id=m,
                        display_name=m,
                        provider=self.adapter_id,
                        supported_reasoning_efforts=[],
                        default_reasoning_effort=None,
                        selectable=False,
                        source="unverified_config",
                        reasoning_selection=SelectionStrategy.UNSUPPORTED,
                    )
                )

        if not self._default_models:
            self._default_models.append(
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
            )

        self._resolved_binary: str | None = None
        self.credential_manager: CredentialManager | None = None

    async def probe(self) -> AgentProbeResult:
        binary = self._find_binary()
        if not binary:
            research = ResearchCapability(
                mode="agentic_cli",
                verification_status=ResearchVerificationStatus.UNAVAILABLE,
                source=f"{self.adapter_id}:binary_not_found",
                verification_method="binary_probe",
                verification_error="cli_not_found",
            )
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.DISABLED,
                binary_path=None,
                version=None,
                protocols=[self.protocol],
                capabilities=AgentCapabilityFlags(
                    streaming=True,
                    persistent_session=False,
                    model_selection=SelectionStrategy.STARTUP,
                    reasoning_selection=(
                        SelectionStrategy.CONFIG
                        if any(model.supported_reasoning_efforts for model in self._default_models)
                        else SelectionStrategy.UNSUPPORTED
                    ),
                    structured_output_mode=self.structured_output_mode,
                ),
                research=research,
                models=[],
                status_detail="Manifest binary not found in system PATH",
            )

        code, out, err = await safe_exec_cmd([binary, *self.version_command], timeout=5.0)
        version_str = out.strip().split("\n")[0] if code == 0 and out else None

        auth_ok = True
        if self.auth_command:
            auth_code, _, _ = await safe_exec_cmd([binary, *self.auth_command], timeout=5.0)
            auth_ok = auth_code == 0

        status = (
            AgentStatus.DETECTED
            if (code == 0 and auth_ok)
            else (AgentStatus.AUTH_REQUIRED if not auth_ok else AgentStatus.BROKEN)
        )
        models = await self.list_models() if code == 0 else []

        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=status,
            binary_path=binary,
            version=version_str or "detected",
            auth_status="unverified" if auth_ok else "auth_required",
            protocols=[self.protocol],
            capabilities=AgentCapabilityFlags(
                streaming=True,
                persistent_session=False,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_selection=(
                    SelectionStrategy.CONFIG
                    if any(model.supported_reasoning_efforts for model in models)
                    else SelectionStrategy.UNSUPPORTED
                ),
                structured_output_mode=self.structured_output_mode,
            ),
            research=ResearchCapability(
                mode="agentic_cli",
                verification_status=ResearchVerificationStatus.UNKNOWN,
                source=f"{self.adapter_id}:capability_unreported",
                verification_method="metadata_absent",
            ),
            models=models,
            status_detail=(
                "Manifest binary detected; protocol smoke is unverified"
                if status == AgentStatus.DETECTED
                else (err or "probe check failed")
            ),
        )

    async def list_models(self) -> list[ModelCapability]:
        binary = self._find_binary()
        if binary and self.models_command:
            code, out, _ = await safe_exec_cmd([binary, *self.models_command], timeout=5.0)
            if code == 0 and out:
                parsed = self._parse_models(out)
                if parsed:
                    return parsed
        return self._default_models

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
                "protocol": self.protocol,
                "effective_model": config.model_id,
                "effective_reasoning": config.reasoning_effort,
            },
        )
        session._cancel_event = asyncio.Event()
        return session

    async def bind_runtime(self, session: AgentSession) -> RuntimeBindingSnapshot:
        return build_runtime_binding_snapshot(
            self,
            session,
            protocol=self.protocol,
            model_verified=True,
            reasoning_verified=True,
            verification_method="manifest_invocation",
        )

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        binary = session.session_data.get("binary") or self._find_binary() or ""
        cmd = [binary, *self.invocation_command]

        if session.config.model_id:
            cmd.extend([self.model_flag, session.config.model_id])
        if session.config.reasoning_effort:
            cmd.extend([self.reasoning_flag, session.config.reasoning_effort])

        prompt = AgentPromptRenderer.render_for_single_prompt(turn)

        env = build_runtime_environment(
            credential_manager=self.credential_manager,
            credential_id=session.config.auth_profile_id,
            auth_env_var=session.config.auth_env_var,
            overrides=self.env_vars,
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
                    with contextlib.suppress(Exception):
                        await transport.kill()
                    yield AgentEvent(type=AgentEventType.DONE, content="".join(full_content))
                    return

                line_b = await transport.readline()
                if not line_b:
                    break

                line_str = line_b.decode("utf-8", errors="replace")
                if not line_str:
                    continue

                if self.protocol in {"streaming_json_cli", "json_stream"}:
                    try:
                        data = json.loads(line_str.strip())
                        if isinstance(data, dict):
                            event_type = str(data.get("type") or data.get("event") or "unknown")
                            session.touch_activity(
                                "protocol_frame",
                                byte_count=len(line_b),
                                metadata={"frame_type": event_type, "event_type": event_type},
                            )
                        chunk = data.get("text") or data.get("content") or data.get("delta")
                        if chunk:
                            yield AgentEvent(type=AgentEventType.CHUNK, content=str(chunk))
                            full_content.append(str(chunk))
                            continue
                    except Exception:
                        pass

                yield AgentEvent(type=AgentEventType.CHUNK, content=line_str)
                full_content.append(line_str)

            returncode = await transport.wait()
            yield AgentEvent(
                type=AgentEventType.DONE,
                content="".join(full_content),
                metadata={
                    "returncode": returncode,
                    "stderr_tail": transport.stderr_tail,
                    "output_streaming_mode": self.output_streaming_mode.value,
                },
            )

        except Exception as exc:
            yield agent_error_event(exc, protocol=self.protocol)
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

    def _parse_models(self, text: str) -> list[ModelCapability]:
        models: list[ModelCapability] = []
        for line in text.splitlines():
            line = line.strip().lstrip("*-• ")
            if not line:
                continue
            parts = line.split()
            if parts:
                m_id = parts[0]
                models.append(
                    ModelCapability(
                        id=m_id,
                        display_name=m_id,
                        provider=self.adapter_id,
                        supported_reasoning_efforts=[],
                        default_reasoning_effort=None,
                        source="official_cli",
                        reasoning_selection=SelectionStrategy.UNSUPPORTED,
                    )
                )
        return models
