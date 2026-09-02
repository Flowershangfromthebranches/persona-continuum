from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator
from typing import Any

from persona_continuum.agent.adapter import AgentSession, resolve_binary, safe_exec_cmd
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
    SelectionStrategy,
)
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.protocols.acp import (
    ACPAdapter,
    AuthRequiredError,
    InteractiveAuthRequiredError,
)
from persona_continuum.agent.response_collector import AgentRuntimeError, AgentTransportError
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.auth.credentials import build_runtime_environment


class GrokBuildAdapter(ACPAdapter):
    adapter_id = "grok"
    name = "Grok Build"
    # Grok exposes a fixed per-model window; no context parameter is sent.
    context_window_mode = ContextWindowMode.FIXED
    _OFFICIAL_CAPABILITIES: dict[str, tuple[list[str], str, int]] = {
        "grok-4.6": (["low", "medium", "high", "xhigh"], "high", 131_072),
        "grok-4.5": (["low", "medium", "high"], "high", 131_072),
    }

    def __init__(self) -> None:
        super().__init__(
            adapter_id=self.adapter_id,
            name=self.name,
            binary_candidates=[
                "~/.grok/bin/grok",
                "~/.local/bin/grok",
                "/opt/homebrew/bin/grok",
                "/usr/local/bin/grok",
                "~/.npm-global/bin/grok",
                "grok",
                "grok-build",
                "grokbuild",
            ],
            acp_args=["agent", "stdio"],
        )
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
                protocols=["grok_agent_stdio", "streaming_json_cli"],
                capabilities=AgentCapabilityFlags(
                    streaming=True,
                    persistent_session=True,
                    model_discovery=True,
                    model_selection=SelectionStrategy.STARTUP,
                    reasoning_discovery=True,
                    reasoning_selection=SelectionStrategy.STARTUP,
                    mcp=True,
                ),
                models=[],
                status_detail="Grok Build CLI binary not found on system",
            )

        code, out, err = await safe_exec_cmd([binary, "--version"], timeout=3.0)
        version_str = out.strip().split("\n")[0] if code == 0 and out else "detected"
        status = AgentStatus.BROKEN if code != 0 else AgentStatus.DETECTED
        auth_status = "unverified"
        detail: str | None = err or "Grok CLI check failed"
        models: list[ModelCapability] = []
        if code == 0:
            models = await self.list_models()
            try:
                session = await asyncio.wait_for(
                    self.create_session(
                        AgentSessionConfig(
                            room_id="__probe__",
                            participant_id="__probe__",
                            persona_id="__probe__",
                            working_dir=os.getcwd(),
                            allow_mcp=False,
                        )
                    ),
                    timeout=3.0,
                )
                await self.close(session)
                status = AgentStatus.READY
                auth_status = "configured"
                detail = None
            except InteractiveAuthRequiredError as exc:
                status = AgentStatus.AUTH_REQUIRED
                auth_status = "interactive_auth_required"
                detail = str(exc)
            except AuthRequiredError as exc:
                status = AgentStatus.AUTH_REQUIRED
                auth_status = "auth_required"
                detail = str(exc)
            except Exception as exc:
                if models:
                    status = AgentStatus.READY
                    auth_status = "configured"
                    detail = None
                else:
                    status = AgentStatus.DETECTED
                    auth_status = "protocol_error"
                    detail = str(exc)
        else:
            models = self._fallback_models()

        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=status,
            binary_path=binary,
            version=version_str,
            auth_status=auth_status,
            protocols=["grok_agent_stdio", "streaming_json_cli"],
            capabilities=AgentCapabilityFlags(
                streaming=True,
                persistent_session=True,
                model_discovery=True,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_discovery=True,
                reasoning_selection=SelectionStrategy.STARTUP,
                mcp=True,
            ),
            models=models,
            status_detail=detail,
        )

    async def list_models(self) -> list[ModelCapability]:
        binary = self._find_binary()
        if binary:
            code, out, _ = await safe_exec_cmd([binary, "models"], timeout=8.0)
            if code == 0 and out:
                parsed = self._parse_models(out)
                if parsed:
                    return parsed
        return self._fallback_models()

    def build_headless_argv(
        self, binary: str, config: AgentSessionConfig, prompt: str
    ) -> list[str]:
        if binary.endswith(".py"):
            cmd = [sys.executable, binary, "-p", prompt, "--output-format", "streaming-json"]
        else:
            cmd = [binary, "-p", prompt, "--output-format", "streaming-json"]
        if config.model_id:
            cmd.extend(["--model", config.model_id])
        if config.reasoning_effort and config.reasoning_effort not in {"none", "default"}:
            cmd.extend(["--effort", config.reasoning_effort])
        return cmd

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        session = await super().create_session(config)
        session.session_data.update(
            {
                "binary": self._find_binary(),
                "mode": "acp",
            }
        )
        return session

    def select_auth_method(
        self,
        auth_methods: list[dict[str, Any]],
        config: AgentSessionConfig,
    ) -> dict[str, Any] | None:
        by_id = {self._auth_method_id(method): method for method in auth_methods}
        manager = getattr(self, "credential_manager", None)
        configured_key = bool(manager and manager.has(config.auth_profile_id or "grok"))
        if configured_key and "xai.api_key" in by_id:
            return by_id["xai.api_key"]
        if "cached_token" in by_id:
            return by_id["cached_token"]
        if not auth_methods:
            return None
        raise AuthRequiredError(
            "Grok authentication requires XAI_API_KEY or an existing cached grok login"
        )

    def build_process_env(self, config: AgentSessionConfig) -> dict[str, str]:
        cred_id = config.auth_profile_id
        if self.credential_manager:
            if cred_id and not self.credential_manager.has(cred_id):
                cred_id = None
            if not cred_id and self.credential_manager.has("grok"):
                cred_id = "grok"
        else:
            cred_id = None
        return build_runtime_environment(
            credential_manager=self.credential_manager,
            credential_id=cred_id,
            auth_env_var=config.auth_env_var,
            target_name="XAI_API_KEY",
        )

    def build_authenticate_params(
        self,
        auth_method: dict[str, Any],
        config: AgentSessionConfig,
    ) -> dict[str, Any]:
        params = super().build_authenticate_params(auth_method, config)
        params["_meta"] = {"headless": True}
        return params

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
        prompt = AgentPromptRenderer.render_for_single_prompt(turn)
        mode = session.session_data.get("mode", "acp")

        if mode == "acp" and proc and proc.returncode is None and proc.stdin:
            try:
                async for event in self._send_acp(session, turn):
                    yield event
                return
            except AgentRuntimeError as exc:
                # A failed ACP stream is not safe to continue reading.  Grok has
                # an explicitly equivalent headless path, so close the broken
                # session and retry once with the same model/effort settings.
                await self._close_corrupted_session(session)
                session.session_data["acp_fallback_reason"] = exc.as_failure()
            except Exception as exc:
                await self._close_corrupted_session(session)
                session.session_data["acp_fallback_reason"] = {
                    "code": "AGENT_TRANSPORT_ERROR",
                    "message": "ACP transport failed",
                    "exception_type": type(exc).__name__,
                }

        # Fallback to headless execution
        binary = session.session_data.get("binary") or self._find_binary() or "grok"
        cmd = self.build_headless_argv(binary, session.config, prompt)
        env = build_runtime_environment(
            credential_manager=getattr(self, "credential_manager", None),
            credential_id=session.config.auth_profile_id,
            auth_env_var=session.config.auth_env_var,
            target_name="XAI_API_KEY",
        )

        transport: SubprocessAgentTransport | None = None
        try:
            transport = await SubprocessAgentTransport.spawn(
                session,
                cmd,
                stdin=False,
                cwd=session.config.working_dir,
                env=env,
            )
            chunks: list[str] = []
            while True:
                chunk = await transport.read(4096)
                if not chunk:
                    break
                chunks.append(chunk.decode("utf-8", errors="replace"))
            code = await transport.wait()
            out = "".join(chunks)
            metadata = {
                "protocol": "streaming_json_cli",
                "fallback": session.session_data.get("acp_fallback_reason"),
                "stderr_tail": transport.stderr_tail,
            }
            if code == 0:
                yield AgentEvent(
                    type=AgentEventType.CHUNK,
                    content=out,
                    metadata={**metadata, "replace_response": True},
                )
                yield AgentEvent(type=AgentEventType.DONE, content=out, metadata=metadata)
            else:
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error=transport.stderr_tail or "Grok execution failed",
                    metadata={
                        **metadata,
                        "failure_code": "AGENT_PROCESS_EXITED",
                        "returncode": code,
                    },
                )
        except Exception as exc:
            error = AgentTransportError(
                "Grok headless transport failed",
                phase="headless_execute",
                diagnostics={
                    "protocol": "streaming_json_cli",
                    "exception_type": type(exc).__name__,
                },
            )
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error=str(error),
                metadata={"protocol": "streaming_json_cli", "failure": error.as_failure()},
            )
        finally:
            if transport is not None:
                await transport.close(force=transport.process_alive)

    async def cancel(self, session: AgentSession) -> None:
        session.request_cancel()
        proc: asyncio.subprocess.Process | None = session.session_data.get("proc")
        transport = session.session_data.get("acp_transport") or session.session_data.get(
            "active_transport"
        )
        if proc and proc.returncode is None:
            with contextlib.suppress(Exception):
                if transport is not None and callable(getattr(transport, "kill", None)):
                    await transport.kill()
                else:
                    proc.terminate()

    async def close(self, session: AgentSession) -> None:
        await super().close(session)

    def _find_binary(self) -> str | None:
        if self._resolved_binary:
            return self._resolved_binary
        self._resolved_binary = resolve_binary(self.binary_candidates)
        return self._resolved_binary

    def _fallback_models(self) -> list[ModelCapability]:
        return [
            self._capability_from_official_table(model_id) for model_id in ("grok-4.6", "grok-4.5")
        ]

    def _capability_from_official_table(self, model_id: str) -> ModelCapability:
        entry = self._OFFICIAL_CAPABILITIES[model_id]
        efforts = entry[0]
        default_effort = entry[1]
        context_window = entry[2] if len(entry) > 2 else 131_072
        return ModelCapability(
            id=model_id,
            display_name=model_id.replace("grok-", "Grok "),
            provider="xai",
            supported_reasoning_efforts=list(efforts),
            default_reasoning_effort=default_effort,
            context_window=context_window,
            source="official_capability_table",
            verified_for_version=model_id,
            reasoning_selection=SelectionStrategy.STARTUP,
        )

    def _parse_models(self, text: str) -> list[ModelCapability]:
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(text)
            raw_models = (
                payload.get("data") or payload.get("models") or []
                if isinstance(payload, dict)
                else payload
            )
            if isinstance(raw_models, list):
                parsed_json = [
                    self._parse_machine_model(item)
                    for item in raw_models
                    if isinstance(item, (dict, str))
                ]
                if parsed_json:
                    return parsed_json

        skip_prefixes = (
            "default model:",
            "available models:",
            "you are logged in",
            "logged in as",
            "logged in with",
            "authentication",
            "model '",
            "tip:",
            "note:",
            "error:",
            "warning:",
        )
        models: list[ModelCapability] = []
        for line in text.splitlines():
            clean = line.strip().lower()
            if not clean or any(clean.startswith(p) for p in skip_prefixes):
                continue
            line = line.strip().lstrip("*-• ")
            parts = line.split()
            if parts:
                m_id = parts[0]
                if m_id.lower() in {
                    "you",
                    "available",
                    "default",
                    "logged",
                    "note",
                    "tip",
                    "warning",
                    "error",
                }:
                    continue
                if m_id in self._OFFICIAL_CAPABILITIES:
                    models.append(self._capability_from_official_table(m_id))
                else:
                    provider = "xai"
                    if m_id.startswith("ocx-alibailian-"):
                        provider = "alibailian"
                    elif m_id.startswith("ocx-openrouter-"):
                        provider = "openrouter"
                    elif m_id.startswith("ocx-wechat-"):
                        provider = "wechat"
                    elif m_id.startswith("ocx-gpt-"):
                        provider = "openai"
                    elif m_id.startswith("ocx-claude-") or m_id.startswith("ocx-anthropic-"):
                        provider = "anthropic"
                    elif m_id.startswith("ocx-"):
                        parts_id = m_id.split("-")
                        provider = parts_id[1] if len(parts_id) > 1 else "ocx"

                    models.append(
                        ModelCapability(
                            id=m_id,
                            display_name=m_id,
                            provider=provider,
                            supported_reasoning_efforts=["none", "low", "medium", "high"],
                            default_reasoning_effort="medium",
                            source="official_cli",
                            reasoning_selection=SelectionStrategy.STARTUP,
                        )
                    )
        return models

    def _parse_machine_model(self, raw_model: dict[str, Any] | str) -> ModelCapability:
        if isinstance(raw_model, str):
            model_id = raw_model
            if model_id in self._OFFICIAL_CAPABILITIES:
                return self._capability_from_official_table(model_id)
            return ModelCapability(
                id=model_id,
                display_name=model_id,
                provider="xai",
                supported_reasoning_efforts=[],
                default_reasoning_effort=None,
                source="dynamic",
                reasoning_selection=SelectionStrategy.UNSUPPORTED,
            )

        model_id = str(raw_model.get("id") or raw_model.get("model") or "")
        context_window = (
            raw_model.get("context_window")
            or raw_model.get("contextWindow")
            or raw_model.get("context_length")
        )
        if not context_window and model_id in self._OFFICIAL_CAPABILITIES:
            context_window = self._OFFICIAL_CAPABILITIES[model_id][2]

        raw_efforts = raw_model.get("supportedReasoningEfforts")
        if raw_efforts is None:
            raw_efforts = raw_model.get("supported_reasoning_efforts")
        efforts: list[str] = []
        if isinstance(raw_efforts, list):
            for raw_effort in raw_efforts:
                effort = (
                    raw_effort.get("reasoningEffort")
                    if isinstance(raw_effort, dict)
                    else raw_effort
                )
                if isinstance(effort, str) and effort not in efforts:
                    efforts.append(effort)
        if efforts:
            default_effort = raw_model.get("defaultReasoningEffort")
            if default_effort is None:
                default_effort = raw_model.get("default_reasoning_effort")
            return ModelCapability(
                id=model_id,
                display_name=str(raw_model.get("displayName") or model_id),
                provider="xai",
                supported_reasoning_efforts=efforts,
                default_reasoning_effort=(
                    str(default_effort) if default_effort is not None else None
                ),
                context_window=context_window,
                source="protocol_model_list",
                reasoning_selection=SelectionStrategy.STARTUP,
            )
        if model_id in self._OFFICIAL_CAPABILITIES:
            return self._capability_from_official_table(model_id)
        return ModelCapability(
            id=model_id,
            display_name=str(raw_model.get("displayName") or model_id),
            provider="xai",
            supported_reasoning_efforts=[],
            default_reasoning_effort=None,
            context_window=context_window,
            source="dynamic",
            reasoning_selection=SelectionStrategy.UNSUPPORTED,
        )
