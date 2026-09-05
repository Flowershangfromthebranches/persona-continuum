from __future__ import annotations

import asyncio
import contextlib
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
from persona_continuum.agent.response_collector import (
    AgentPermissionBlockedError,
    RuntimeUnavailableError,
    agent_error_event,
    sanitize_command,
    sanitize_diagnostic,
)
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.auth.credentials import CredentialManager, build_runtime_environment


class PlainCliAdapter(AgentAdapter):
    def __init__(
        self,
        adapter_id: str,
        name: str,
        binary_candidates: list[str],
        version_args: list[str] | None = None,
        exec_args: list[str] | None = None,
        default_models: list[ModelCapability] | None = None,
        model_flag: str | None = None,
        reasoning_flag: str | None = None,
        research: ResearchCapability | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.name = name
        self.binary_candidates = binary_candidates
        self.version_args = version_args or ["--version"]
        self.exec_args = exec_args or []
        self._default_models = default_models or []
        self.model_flag = model_flag
        self.reasoning_flag = reasoning_flag
        self._research_capability = research or ResearchCapability(
            mode="agentic_cli",
            verification_status=ResearchVerificationStatus.UNKNOWN,
            source=f"{adapter_id}:capability_unreported",
        )
        self._resolved_binary: str | None = None
        self.credential_manager: CredentialManager | None = None
        self.prompt_mode = PromptMode.INLINE_SYSTEM
        self.structured_output_mode = StructuredOutputMode.PROMPT_ONLY
        self.output_streaming_mode = OutputStreamingMode.BUFFERED_FINAL

    def build_permission_args(self, config: AgentSessionConfig) -> list[str]:
        """Map an abstract session permission profile to CLI arguments.

        Most plain CLIs do not expose a portable permission flag.  Concrete
        adapters override this hook when they can enforce the profile at the
        process boundary; leaving it empty never broadens a session's tools.
        """

        del config
        return []

    def media_input_mode(self, kind: str) -> str:
        """How this CLI consumes one attachment kind.

        Plain CLIs cannot receive binary payloads: the model runtime reads
        the file from disk instead.  Concrete adapters override this hook
        when they verified a different carrier (native protocol items).
        """

        from persona_continuum.agent.models import MediaInputMode

        if str(kind or "").lower() in {"image", "video", "audio", "file"}:
            return MediaInputMode.LOCAL_PATH.value
        return MediaInputMode.UNSUPPORTED.value

    def attachment_prompt_block(self, turn: AgentTurn) -> str:
        """Wire-text reference block for this turn's attachments.

        Only short path references travel here -- never file bytes.  The CLI
        runtime opens the file itself, so the argv/stdin budget sees ~100
        bytes per file instead of megabytes of base64.
        """

        from persona_continuum.agent.media_transport import (
            describe_unconsumed_attachment,
            extract_attachment_text,
            local_path_reference,
            require_consumable_or_raise,
        )
        from persona_continuum.agent.models import AgentAttachment, MediaInputMode

        attachments = [
            AgentAttachment.model_validate(a) if isinstance(a, dict) else a
            for a in (turn.attachments or [])
            if isinstance(a, (dict, AgentAttachment))
        ]
        if not attachments:
            return ""
        uploads_root = self._cli_attachments_root()
        lines = ["用户本轮上传了以下附件："]
        for attachment in attachments:
            mode = self.media_input_mode(attachment.kind)
            if mode == MediaInputMode.LOCAL_PATH.value:
                lines.append(
                    f"- {attachment.kind} {attachment.filename}："
                    f"{local_path_reference(attachment)} "
                    "请读取该文件并将文件内容作为本轮用户输入的一部分，"
                    "结合房间上下文完成回答。"
                )
                continue
            if mode == MediaInputMode.EXTRACTED_CONTENT.value:
                extracted = (
                    attachment.extracted_text
                    or (
                        extract_attachment_text(attachment, uploads_root)
                        if uploads_root is not None
                        else None
                    )
                )
                if extracted:
                    lines.append(
                        f"- {attachment.kind} {attachment.filename} "
                        f"（{attachment.mime_type}）提取内容：\n{extracted}"
                    )
                    continue
            require_consumable_or_raise(
                attachment, mode, adapter_id=getattr(self, "adapter_id", "")
            )
            lines.append(describe_unconsumed_attachment(attachment, mode))
        return "\n".join(lines)

    def _cli_attachments_root(self) -> Path | None:
        from persona_continuum.config import Config

        override = getattr(self, "_session_uploads_root", None)
        if override:
            return Path(str(override))
        try:
            return Config().room_uploads_dir
        except Exception:
            return None

    def prepare_prompt(
        self,
        config: AgentSessionConfig,
        turn: AgentTurn,
        prompt: str,
    ) -> str:
        """Apply adapter-specific prompt policy without changing the envelope."""

        del config, turn
        return prompt

    def resolve_cli_model_and_reasoning(
        self, config: AgentSessionConfig
    ) -> tuple[str | None, str | None]:
        """Return the ``--model`` / reasoning values that should actually be passed.

        Adapters may rewrite these (for example baking effort into a model id)
        so the CLI is not given contradictory flags.
        """

        model_id = (config.model_id or "").strip() or None
        reasoning = (config.reasoning_effort or "").strip() or None
        return model_id, reasoning

    def build_extra_cli_args(self, config: AgentSessionConfig) -> list[str]:
        """Adapter-specific flags that must appear before the print-mode prompt."""

        del config
        return []

    def classify_process_failure(
        self,
        session: AgentSession,
        *,
        returncode: int,
        stderr_text: str,
        diagnostics: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any]]:
        """Return a user-facing CLI failure detail without leaking secrets."""

        del session, returncode
        stderr_lower = (stderr_text or "").lower()
        if "authentication required" in stderr_lower or "use /login" in stderr_lower:
            return (
                "CLI authentication required. Please log in using the CLI /login command.",
                diagnostics,
            )
        if "credit usage limit" in stderr_lower or "upgrade your subscription" in stderr_lower:
            return "Quota exceeded: CLI account credit usage limit reached.", diagnostics
        if stderr_text and stderr_text.strip():
            return sanitize_diagnostic(stderr_text.strip(), limit=200), diagnostics
        return None, diagnostics

    def cleanup_cli_artifacts(self, session: AgentSession) -> None:
        """Release adapter temp files even when the turn ends successfully."""

        del session

    @staticmethod
    def _headless_permission_denied(stderr_text: str) -> bool:
        normalized = str(stderr_text or "").casefold()
        permission_marker = any(
            marker in normalized
            for marker in (
                "permission",
                "auto-denied",
                "soft-denying tool confirmation",
            )
        )
        headless_marker = any(
            marker in normalized
            for marker in (
                "headless mode",
                "cannot prompt",
                "tool confirmation",
            )
        )
        return permission_marker and headless_marker

    async def probe(self) -> AgentProbeResult:
        binary = self._find_binary()
        if not binary:
            research = self._research_capability.model_copy(
                update={
                    "verification_status": ResearchVerificationStatus.UNAVAILABLE,
                    "verification_method": "binary_probe",
                    "verification_error": "cli_not_found",
                }
            )
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.DISABLED,
                binary_path=None,
                version=None,
                protocols=["plain_cli"],
                capabilities=AgentCapabilityFlags(
                    streaming=False,
                    persistent_session=False,
                    model_selection=SelectionStrategy.STARTUP,
                    reasoning_selection=SelectionStrategy.UNSUPPORTED,
                    web_search=research.discovers_sources,
                    web_fetch=research.reads_sources,
                    browser=research.browser,
                ),
                research=research,
                models=[],
                status_detail="Plain CLI binary not found on system",
            )

        code, out, err = await safe_exec_cmd([binary, *self.version_args], timeout=5.0)
        version_str = out.strip().split("\n")[0] if code == 0 and out else None
        status = AgentStatus.READY if code == 0 else AgentStatus.BROKEN
        models = await self.list_models() if code == 0 else []

        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=status,
            binary_path=binary,
            version=version_str or "detected",
            auth_status="ready" if code == 0 else "broken",
            protocols=["plain_cli"],
            capabilities=AgentCapabilityFlags(
                streaming=False,
                persistent_session=False,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_selection=SelectionStrategy.UNSUPPORTED,
                web_search=self._research_capability.discovers_sources,
                web_fetch=self._research_capability.reads_sources,
                browser=self._research_capability.browser,
            ),
            research=self._research_capability,
            models=models,
            status_detail=(
                "CLI binary verified and ready" if code == 0 else "CLI binary execution failed"
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

        session = AgentSession(
            config=config,
            is_active=True,
            session_data={
                "binary": binary,
                "active_proc": None,
                "protocol": "plain_cli",
                "effective_model": (
                    self.resolve_cli_model_and_reasoning(config)[0]
                    if self.model_flag
                    else None
                ),
                "effective_reasoning": (
                    self.resolve_cli_model_and_reasoning(config)[1]
                    if self.reasoning_flag
                    else None
                ),
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
            protocol="plain_cli",
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
                protocol="plain_cli",
            )
            return

        prompt_flags = [arg for arg in self.exec_args if arg in {"-p", "--print"}]
        command_args = [arg for arg in self.exec_args if arg not in {"-p", "--print"}]
        cmd = [binary, *command_args]
        model_id, reasoning = self.resolve_cli_model_and_reasoning(session.config)
        if self.model_flag and model_id:
            cmd.extend([self.model_flag, model_id])
        if (
            self.reasoning_flag
            and reasoning
            and reasoning not in {"none", "default"}
        ):
            cmd.extend([self.reasoning_flag, reasoning])
        cmd.extend(self.build_extra_cli_args(session.config))
        cmd.extend(self.build_permission_args(session.config))

        prompt = self.prepare_prompt(
            session.config,
            turn,
            AgentPromptRenderer.render_for_single_prompt(turn),
        )
        media_block = self.attachment_prompt_block(turn)
        if media_block:
            prompt = f"{prompt}\n\n{media_block}"
        pass_via_arg = bool(prompt_flags)
        if pass_via_arg:
            # Print-mode flags consume or govern the prompt on several CLIs.
            # Runtime selection flags must come first so they are not parsed
            # as the prompt value (for example by agy/Gemini CLI).
            cmd.extend(prompt_flags)
            cmd.append(prompt)

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
                stdin=not pass_via_arg,
                env=env,
                cwd=session.config.working_dir,
            )

            if not pass_via_arg:
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

                chunk = await transport.read(4096)
                if not chunk:
                    break

                text = chunk.decode("utf-8", errors="replace")
                if text:
                    full_content.append(text)

            returncode = await transport.wait()
            stderr_text = transport.stderr_tail
            diagnostics = {
                "command_shape": sanitize_command(cmd),
                "returncode": returncode,
                "stderr_tail": sanitize_diagnostic(stderr_text),
                "output_streaming_mode": self.output_streaming_mode.value,
            }
            classified, diagnostics = self.classify_process_failure(
                session,
                returncode=returncode,
                stderr_text=stderr_text,
                diagnostics=diagnostics,
            )
            if not full_content and self._headless_permission_denied(stderr_text):
                yield agent_error_event(
                    AgentPermissionBlockedError(
                        "Agent CLI requested a tool that could not be approved in headless mode",
                        phase=str(turn.metadata.get("phase") or "session_prompt"),
                        diagnostics=diagnostics,
                    ),
                    protocol="plain_cli",
                )
                return
            if returncode and not full_content:
                detail = classified or f"CLI exited with status {returncode}"
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error=f"AGENT_PROCESS_EXITED_WITHOUT_OUTPUT: {detail}",
                    metadata={
                        "failure_code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                        "failure": {
                            "code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                            "message": detail,
                            "retriable": True,
                        },
                        **diagnostics,
                    },
                )
                return
            if not full_content:
                detail = classified or "CLI returned no response"
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error=f"AGENT_PROCESS_EXITED_WITHOUT_OUTPUT: {detail}",
                    metadata={
                        "failure_code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                        "failure": {
                            "code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                            "message": detail,
                            "retriable": True,
                        },
                        **diagnostics,
                    },
                )
                return
            # Plain CLI output is deliberately buffered.  Do not advertise
            # STREAMING while yielding partial text that callers cannot treat
            # as a stable protocol frame.
            yield AgentEvent(
                type=AgentEventType.CHUNK,
                content="".join(full_content),
                metadata={"stream_mode": self.output_streaming_mode.value},
            )
            yield AgentEvent(
                type=AgentEventType.DONE,
                content="".join(full_content),
                metadata=diagnostics,
            )

        except Exception as exc:
            error_event = agent_error_event(exc, protocol="plain_cli")
            error_event.metadata["command_shape"] = sanitize_command(cmd)
            yield error_event
        finally:
            session.session_data["active_proc"] = None
            self.cleanup_cli_artifacts(session)
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
