from __future__ import annotations

import asyncio
import contextlib
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
from persona_continuum.agent.response_collector import agent_error_event
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.auth.credentials import CredentialManager, build_runtime_environment


class CommandCodeAdapter(AgentAdapter):
    adapter_id = "command_code"
    name = "Command Code"

    def __init__(self) -> None:
        self.binary_candidates = [
            "command-code",
            "cmd",
            "commandcode",
            "cmdc",
            "~/.npm-global/bin/command-code",
            "~/.npm-global/bin/cmd",
            "~/.local/bin/command-code",
            "~/.local/bin/cmd",
            "/opt/homebrew/bin/command-code",
            "/opt/homebrew/bin/cmd",
            "/usr/local/bin/command-code",
            "/usr/local/bin/cmd",
        ]
        self._resolved_binary: str | None = None
        self.credential_manager: CredentialManager | None = None
        self.prompt_mode = PromptMode.INLINE_SYSTEM
        self.structured_output_mode = StructuredOutputMode.PROMPT_ONLY
        self.output_streaming_mode = OutputStreamingMode.BUFFERED_FINAL
        self.protocols = ["plain_cli", "streaming_json_cli"]

    async def probe(self) -> AgentProbeResult:
        binary = self._find_binary()
        if not binary:
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.DISABLED,
                binary_path=None,
                version=None,
                protocols=["plain_cli", "streaming_json_cli"],
                capabilities=AgentCapabilityFlags(
                    streaming=False,
                    persistent_session=False,
                    model_discovery=True,
                    model_selection=SelectionStrategy.STARTUP,
                    reasoning_discovery=True,
                    reasoning_selection=SelectionStrategy.STARTUP,
                    mcp=True,
                    structured_output_mode=self.structured_output_mode,
                ),
                models=[],
                status_detail="Command Code CLI binary not found on system",
            )

        code, out, _ = await safe_exec_cmd([binary, "--version"], timeout=5.0)
        version_str = out.strip().split("\n")[0] if code == 0 and out else "detected"

        # Check authentication status via command-code status
        status_code, status_out, _ = await safe_exec_cmd([binary, "status"], timeout=6.0)
        is_auth = status_code == 0 and (
            "authentication verified" in status_out.lower()
            or "authenticated as" in status_out.lower()
        )

        status = AgentStatus.READY if is_auth else AgentStatus.AUTH_REQUIRED
        models = await self.list_models()

        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=status,
            binary_path=binary,
            version=version_str,
            auth_status="configured" if is_auth else "auth_required",
            protocols=["plain_cli", "streaming_json_cli"],
            capabilities=AgentCapabilityFlags(
                streaming=False,
                persistent_session=False,
                model_discovery=True,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_discovery=True,
                reasoning_selection=SelectionStrategy.STARTUP,
                mcp=True,
                structured_output_mode=self.structured_output_mode,
            ),
            models=models,
            status_detail=(
                "Command Code CLI authenticated and ready"
                if is_auth
                else "Command Code auth required (run `command-code login`)"
            ),
        )

    async def list_models(self) -> list[ModelCapability]:
        binary = self._find_binary()
        if binary:
            code, out, _ = await safe_exec_cmd([binary, "--list-models"], timeout=10.0)
            if code == 0 and out:
                parsed = self._parse_models(out)
                if parsed:
                    return parsed
        return self._fallback_models()

    def _parse_models(self, text: str) -> list[ModelCapability]:
        lines = text.splitlines()
        models: list[ModelCapability] = []
        current_section = ""

        skip_prefixes = (
            "available models",
            "pass the full id",
            "cmd --model",
            "command-code --model",
            "docs:",
            "note:",
            "tip:",
            "error:",
            "warning:",
        )

        for line in lines:
            line_s = line.strip()
            if not line_s or any(line_s.lower().startswith(p) for p in skip_prefixes):
                continue

            # Section headers like 'Open Source', 'Stealth', 'Anthropic', 'OpenAI',
            # 'Google', 'Sakana', 'Meta', 'xAI'
            if (
                not line.startswith(" ")
                and not line.startswith("\t")
                and "/" not in line_s
                and not line_s.startswith("claude-")
                and not line_s.startswith("gpt-")
            ):
                current_section = line_s
                continue

            parts = line_s.split(None, 1)
            if parts:
                model_id = parts[0]
                desc = parts[1] if len(parts) > 1 else model_id

                # Determine provider
                if "/" in model_id:
                    provider = model_id.split("/")[0]
                elif model_id.startswith("claude-"):
                    provider = "anthropic"
                elif model_id.startswith("gpt-"):
                    provider = "openai"
                elif current_section:
                    provider = current_section.lower().replace(" ", "_")
                else:
                    provider = "command_code"

                # Check reasoning capabilities
                has_reasoning = any(
                    k in desc.lower() or k in model_id.lower()
                    for k in (
                        "reasoning",
                        "thinking",
                        "r1",
                        "sol",
                        "terra",
                        "opus",
                        "sonnet",
                        "k3",
                        "glm-5",
                    )
                )
                # Command Code's --effort contract exposes low, medium and
                # high. "none" means omit the flag and use the model default.
                supported_efforts = ["none", "low", "medium", "high"]

                models.append(
                    ModelCapability(
                        id=model_id,
                        display_name=f"{model_id} ({desc})" if desc != model_id else model_id,
                        provider=provider,
                        supported_reasoning_efforts=supported_efforts,
                        default_reasoning_effort="medium" if has_reasoning else "none",
                        # The CLI exposes --effort and this adapter binds it
                        # directly in build_exec_argv.
                        source="official_cli",
                        reasoning_selection=SelectionStrategy.STARTUP,
                    )
                )

        return models

    def _fallback_models(self) -> list[ModelCapability]:
        known_models = [
            ("deepseek/deepseek-v4-flash", "fast hybrid-attention reasoning", "deepseek"),
            ("deepseek/deepseek-v4-pro", "hybrid-attention long-context reasoning", "deepseek"),
            (
                "moonshotai/kimi-k3",
                "long-horizon coding & knowledge work with 1M context",
                "moonshotai",
            ),
            ("moonshotai/kimi-k2.7-code", "improved long-horizon coding with vision", "moonshotai"),
            ("zai-org/glm-5.3", "frontier coding with emergent cyber capabilities", "zai-org"),
            (
                "zai-org/glm-5.2",
                "powerful coding with 1M context and long-horizon tasks",
                "zai-org",
            ),
            ("minimaxai/minimax-m3", "frontier coding, agents & native multimodality", "minimaxai"),
            ("qwen/qwen3.8-max", "autonomous long-horizon coding & professional work", "qwen"),
            ("qwen/qwen3.7-max", "frontier coding & long-horizon agent execution", "qwen"),
            ("claude-sonnet-5", "best combo of speed & intelligence", "anthropic"),
            ("claude-opus-5", "most intelligent Opus for agents and coding", "anthropic"),
            ("claude-haiku-4-5", "fastest & most compact", "anthropic"),
            ("gpt-5.6-sol", "frontier model for complex professional work", "openai"),
            ("gpt-5.6-terra", "balances intelligence and cost", "openai"),
            ("gpt-5.6-luna", "optimized for cost-sensitive workloads", "openai"),
            ("gpt-5.4", "frontier model for general complex work", "openai"),
            ("google/gemini-3.7-flash", "higher-quality coding & agentic workflows", "google"),
            ("google/gemini-3.5-flash", "Pro-level coding proficiency", "google"),
            ("xai/grok-4.6", "frontier performance on coding and knowledge work", "xai"),
            ("xai/grok-4.5", "smartest model for coding, agentic tasks", "xai"),
        ]
        return [
            ModelCapability(
                id=m_id,
                display_name=f"{m_id} ({desc})",
                provider=prov,
                supported_reasoning_efforts=["none", "low", "medium", "high"],
                default_reasoning_effort="medium",
                source="config",
                reasoning_selection=SelectionStrategy.STARTUP,
            )
            for m_id, desc, prov in known_models
        ]

    def build_exec_argv(
        self,
        binary: str,
        config: AgentSessionConfig,
        prompt: str,
    ) -> list[str]:
        cmd = [binary, "-p", prompt, "--yolo", "--output-format", "text"]
        if config.model_id:
            cmd.extend(["-m", config.model_id])
        if config.reasoning_effort and config.reasoning_effort not in {"none", "default"}:
            cmd.extend(["--effort", config.reasoning_effort])
        return cmd

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
                "protocol": "plain_cli",
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
            protocol="plain_cli",
            model_verified=True,
            reasoning_verified=True,
            verification_method="cli_invocation_flags",
        )

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        binary = session.session_data.get("binary") or self._find_binary() or "command-code"
        prompt = AgentPromptRenderer.render_for_single_prompt(turn)
        cmd = self.build_exec_argv(binary, session.config, prompt)

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

                chunk = await transport.read(4096)
                if not chunk:
                    break

                text = chunk.decode("utf-8", errors="replace")
                if text:
                    full_content.append(text)

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
            if full_content:
                yield AgentEvent(
                    type=AgentEventType.CHUNK,
                    content="".join(full_content),
                    metadata={"stream_mode": "buffered_final"},
                )
            else:
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
            yield agent_error_event(exc, protocol="plain_cli")
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
