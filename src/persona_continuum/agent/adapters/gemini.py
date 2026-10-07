from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from persona_continuum.agent.adapter import (
    AgentSession,
    build_runtime_binding_snapshot,
    safe_exec_cmd,
)
from persona_continuum.agent.context_capability import (
    ContextWindowMode,
    default_model_capability_registry,
)
from persona_continuum.agent.context_fields import (
    GEMINI_USAGE_SEMANTICS,
    ContextScope,
    apply_runtime_context_to_session,
    extract_runtime_context_facts,
)
from persona_continuum.agent.models import (
    AgentEvent,
    AgentEventType,
    AgentProbeResult,
    AgentSessionConfig,
    AgentTurn,
    ModelCapability,
    PermissionProfile,
    ReasoningCapability,
    ReasoningCapabilityMode,
    ResearchCapability,
    ResearchVerificationStatus,
    RuntimeBindingSnapshot,
    SelectionStrategy,
)
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.protocols.plain_cli import (
    INVALID_CLI_ARGUMENT_SUBTYPE,
    PlainCliAdapter,
    cli_argument_error_detail,
    is_invalid_cli_argument_error,
)
from persona_continuum.agent.response_collector import (
    ModelBindingUnverifiedError,
    ReasoningBindingUnverifiedError,
    sanitize_diagnostic,
)
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport

LOCATION_UNSUPPORTED_MESSAGE = (
    "Gemini API rejected the request: user location is not supported "
    "(FAILED_PRECONDITION). This can be transient — retry, or switch the "
    "studio runtime to a reachable API/CLI."
)


def classify_agy_cli_failure(stderr_text: str, log_text: str = "") -> str | None:
    """Map agy stderr/log noise to a short, secret-safe failure reason."""

    blob = f"{stderr_text or ''}\n{log_text or ''}"
    lowered = blob.casefold()
    if "user location is not supported" in lowered:
        return LOCATION_UNSUPPORTED_MESSAGE
    if "not currently available in your location" in lowered:
        return "Gemini account rejected: Antigravity is not currently available in your location"
    if "eligibility check failed" in lowered or (
        "oauth2/v2/userinfo" in lowered and "eof" in lowered
    ):
        return "Gemini eligibility check failed (transient Google OAuth/network error)"
    match = re.search(r"agent executor error:\s*(.+)", blob, re.I)
    if match:
        return sanitize_diagnostic(match.group(1).strip(), limit=500)
    if "agent execution terminated due to error" in lowered:
        return "Agent execution terminated due to error."
    cleaned = sanitize_diagnostic((stderr_text or "").strip(), limit=500)
    return cleaned or None


class GeminiCliAdapter(PlainCliAdapter):
    # Official Gemini windows are per-model.  agy stream-json can also report
    # usage at runtime; DISCOVERABLE lets those values propagate.
    context_window_mode = ContextWindowMode.DISCOVERABLE
    usage_context_semantics = GEMINI_USAGE_SEMANTICS
    context_scope = ContextScope.PER_REQUEST

    _HEADLESS_NO_TOOL_POLICY = """[HEADLESS TOOL POLICY]
This is a non-interactive task and every required input is already included below.
Do not call GrepSearch, read_file, shell, web, or any other tool. Do not inspect the
workspace. Return the requested answer directly, following the expected output schema."""

    def __init__(self) -> None:
        super().__init__(
            adapter_id="gemini_cli",
            name="Gemini CLI (agy)",
            binary_candidates=[
                "agy",
                "~/.local/bin/agy",
                "/opt/homebrew/bin/agy",
                "/usr/local/bin/agy",
            ],
            version_args=["--version"],
            exec_args=["-p"],
            model_flag="--model",
            reasoning_flag="--effort",
            research=ResearchCapability(
                mode="native_cli",
                search=True,
                fetch=True,
                can_discover_sources=True,
                can_read_sources=True,
                browser=True,
                citations=True,
                live=True,
                source="gemini_cli:google_web_search+web_fetch",
                verification_status=ResearchVerificationStatus.DECLARED,
                verification_method="adapter_declaration",
            ),
            default_models=[],
        )
        self._models_cache: tuple[str, float, list[ModelCapability]] | None = None
        self._model_discovery_error = ""
        self._refresh_models = False

    # agy fetches its catalog remotely; the outer probe also runs --version.
    model_discovery_timeout_seconds = 20.0
    probe_timeout_seconds = 27.0
    retry_catalog_after_scan = True

    def invalidate_model_cache(self) -> None:
        self._refresh_models = True

    async def probe(self) -> AgentProbeResult:
        result = await super().probe()
        if any(model.supported_reasoning_efforts for model in result.models):
            result.capabilities.reasoning_selection = (
                SelectionStrategy.STARTUP if self.reasoning_flag else SelectionStrategy.CONFIG
            )
        if result.model_discovery_error:
            result.status_detail = (
                "Model discovery failed; using the last verified CLI catalog"
                if result.models else "Model discovery failed; rescan to retry"
            )
        return result

    def build_permission_args(self, config: AgentSessionConfig) -> list[str]:
        """Enforce Persona Continuum's research profile in headless Gemini.

        ``AgentSessionConfig.tools`` is retained as an auditable description.
        The official Gemini CLI exposes ``--allowed-tools``; ``agy`` 1.1.21
        does not, so passing that unknown flag would fail before the turn.
        Keep the official-CLI allowlist deliberately narrow: research sessions
        must not gain shell, write, or replacement tools.
        """

        binary = self._find_binary()
        is_agy = bool(binary and Path(binary).name.casefold() == "agy")
        if config.permission_profile == PermissionProfile.RESEARCH_READ_ONLY:
            if is_agy:
                return ["--dangerously-skip-permissions"]
            return ["--allowed-tools", "google_web_search,web_fetch"]
        return super().build_permission_args(config)

    def prepare_prompt(
        self,
        config: AgentSessionConfig,
        turn: AgentTurn,
        prompt: str,
    ) -> str:
        """Keep no-tool Persona work from triggering headless permission prompts.

        ``config.tools`` is only an auditable description here: the plain-CLI
        protocol cannot round-trip broker tool calls, so a room participant
        with ``allow_agent_tools`` still gets the policy and must answer
        directly instead of reaching for the CLI's native tools (which
        headless mode auto-denies, producing an empty response).
        """

        if config.permission_profile == PermissionProfile.CHAT_SAFE and not turn.tools:
            return f"{self._HEADLESS_NO_TOOL_POLICY}\n\n{prompt}"
        return prompt

    _EFFORT_SUFFIX = re.compile(r"-(low|medium|high)$")

    def resolve_cli_model_and_reasoning(
        self, config: AgentSessionConfig
    ) -> tuple[str | None, str | None]:
        """Keep agy's suffixed model id in sync with ``--effort``.

        agy 1.1.22 bakes effort into ids such as ``gemini-3.7-flash-high``.
        Omitting ``--effort`` makes print mode exit 1 with empty stdout.
        Passing a mismatched pair is rewritten onto the matching suffix.
        """

        model_id = (config.model_id or "").strip() or None
        requested = (config.reasoning_effort or "").strip().casefold()
        reasoning = (config.reasoning_effort or "").strip() or None
        if model_id and (match := self._EFFORT_SUFFIX.search(model_id)):
            if requested in {"low", "medium", "high"}:
                model_id = self._EFFORT_SUFFIX.sub(f"-{requested}", model_id)
                reasoning = requested
            else:
                reasoning = match.group(1)
        if reasoning in {"", "none", "default"}:
            reasoning = None
        return model_id, reasoning

    def _is_agy_binary(self) -> bool:
        binary = self._find_binary()
        return bool(binary and Path(binary).name.casefold() == "agy")

    def _use_stream_json(self) -> bool:
        if getattr(self, "_force_argv", False):
            return False
        return self._is_agy_binary()

    @property
    def prompt_transport_mode(self) -> str:
        return "stdin" if self._use_stream_json() else "argv"

    def build_extra_cli_args(self, config: AgentSessionConfig) -> list[str]:
        """Capture agy's private log so headless failures are explainable.

        Print mode writes ``Agent execution terminated due to error.`` to
        stderr and hides ``FAILED_PRECONDITION`` in ``--log-file``.
        """

        if not self._is_agy_binary():
            return []
        with tempfile.NamedTemporaryFile(
            prefix="persona-agy-",
            suffix=".log",
            delete=False,
        ) as handle:
            log_path = handle.name
        extra = dict(config.extra or {})
        extra["cli_log_file"] = log_path
        config.extra = extra
        raw_timeout = extra.get("hard_timeout_seconds") or extra.get("turn_timeout_seconds")
        try:
            timeout = float(raw_timeout) if raw_timeout is not None else 1200.0
        except (TypeError, ValueError):
            timeout = 1200.0
        if not math.isfinite(timeout) or timeout <= 0:
            timeout = 1200.0
        # agy's own 5m default otherwise expires before the host deadline.
        return ["--log-file", log_path, "--print-timeout", f"{timeout:g}s"]

    def _read_and_clear_cli_log(self, session: AgentSession) -> str:
        raw = str((session.config.extra or {}).get("cli_log_file") or "")
        if not raw:
            return ""
        path = Path(raw)
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")[-12_000:]
            return ""
        except OSError:
            return ""
        finally:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            extra = dict(session.config.extra or {})
            extra.pop("cli_log_file", None)
            session.config.extra = extra

    def build_turn_cli_args(self, session: AgentSession, turn: AgentTurn) -> list[str]:
        """Never pass CLI flags for the structured-output contract.

        This adapter is PROMPT_ONLY: ``AgentPromptRenderer`` embeds the
        expected-output schema in the prompt and ``StructuredOutputEngine``
        parses/validates/repairs the plain-text reply.  agy requires
        ``--json-schema`` to be paired with ``--output-format json`` or
        ``stream-json`` and exits before the model call otherwise; simply
        adding ``--output-format json`` would also be wrong because the
        plain-CLI pipeline would then schema-validate agy's JSON envelope
        instead of the model text.  A future native-schema mode must add
        ``--output-format json`` *and* envelope unwrapping that feeds only
        the ``structured_output`` field to StructuredOutputEngine.
        """

        del session, turn
        return []

    def _stream_json_argv(self, session: AgentSession) -> list[str]:
        binary = session.session_data.get("binary") or self._find_binary() or "agy"
        cmd = [
            binary,
            "-p=",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
        ]
        model_id, reasoning = self.resolve_cli_model_and_reasoning(session.config)
        if self.model_flag and model_id:
            cmd.extend([self.model_flag, model_id])
        if self.reasoning_flag and reasoning and reasoning not in {"none", "default"}:
            cmd.extend([self.reasoning_flag, reasoning])
        cmd.extend(self.build_extra_cli_args(session.config))
        cmd.extend(self.build_permission_args(session.config))
        return cmd

    async def _ensure_stream_process(self, session: AgentSession) -> SubprocessAgentTransport:
        transport = session.session_data.get("agy_stream_transport")
        if isinstance(transport, SubprocessAgentTransport) and transport.process_alive:
            return transport
        from persona_continuum.auth.credentials import build_runtime_environment

        env = build_runtime_environment(
            credential_manager=self.credential_manager,
            credential_id=session.config.auth_profile_id,
            auth_env_var=session.config.auth_env_var,
        )
        transport = await SubprocessAgentTransport.spawn(
            session,
            self._stream_json_argv(session),
            stdin=True,
            env=env,
            cwd=session.config.working_dir,
        )
        session.session_data["agy_stream_transport"] = transport
        session.session_data["protocol"] = "agy_stream_json"
        return transport

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        if not self._use_stream_json():
            async for event in super().send(session, turn):
                yield event
            return
        try:
            async for event in self._send_stream_json(session, turn):
                yield event
        except Exception:
            await self._close_stream(session)
            raise

    async def _send_stream_json(
        self, session: AgentSession, turn: AgentTurn
    ) -> AsyncIterator[AgentEvent]:
        transport = await self._ensure_stream_process(session)
        prompt = self.prepare_prompt(
            session.config,
            turn,
            AgentPromptRenderer.render_for_single_prompt(turn),
        )
        media_block = self.attachment_prompt_block(turn)
        if media_block:
            prompt = f"{prompt}\n\n{media_block}"
        payload = (
            json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
        )
        await transport.write(payload.encode("utf-8"))

        full_content: list[str] = []
        while True:
            if session._cancel_event and session._cancel_event.is_set():
                await transport.kill()
                yield AgentEvent(
                    type=AgentEventType.DONE,
                    content="".join(full_content),
                    metadata={"cancelled": True, "protocol": "agy_stream_json"},
                )
                return
            line = await transport.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                yield AgentEvent(type=AgentEventType.CHUNK, content=text + "\n")
                full_content.append(text + "\n")
                continue
            if not isinstance(data, dict):
                continue
            facts = extract_runtime_context_facts(
                data, semantics=self.usage_context_semantics
            )
            apply_runtime_context_to_session(
                session.session_data,
                facts,
                semantics=self.usage_context_semantics,
            )
            event_type = str(data.get("type") or data.get("event") or "").casefold()
            chunk = (
               data.get("delta")
               or data.get("text")
                or (
                    data.get("step_update", {}).get("text_delta")
                    if isinstance(data.get("step_update"), dict)
                    else None
                )
               or (data.get("step_update") if isinstance(data.get("step_update"), str) else None)
            )
            if event_type in {"step_update", "chunk", "message"} and chunk:
               yield AgentEvent(
                   type=AgentEventType.CHUNK,
                   content=str(chunk),
                   metadata={"protocol": "agy_stream_json"},
               )
               full_content.append(str(chunk))
               continue
            if event_type == "result":
                result_obj = data.get("result") if isinstance(data.get("result"), dict) else data
                data = result_obj
                event_type = "result"
            if event_type in {"result", "final", "done"} or data.get("status") in {
               "SUCCESS",
                "ERROR",
                "success",
                "error",
            }:
                if str(data.get("status") or "").casefold() == "error" or data.get("is_error"):
                    raw_error = data.get("error") or data.get("message") or "CLI reported failure"
                    if isinstance(raw_error, dict):
                        raw_error = raw_error.get("message") or json.dumps(raw_error)
                    detail, diagnostics = self.classify_process_failure(
                        session, returncode=1, stderr_text=str(raw_error),
                        diagnostics={"protocol": "agy_stream_json"},
                    )
                    code = (
                        "AGENT_CLI_INVALID_ARGUMENT"
                        if diagnostics.get("failure_subtype") == INVALID_CLI_ARGUMENT_SUBTYPE
                        else "AGENT_REPORTED_ERROR"
                    )
                    yield AgentEvent(
                        type=AgentEventType.ERROR,
                        error=detail or "CLI reported failure",
                        metadata={
                            **diagnostics, "failure_code": code,
                            "failure": {"code": code, "message": detail, "retriable": False},
                        },
                    )
                    await self._close_stream(session)
                    return
                response = data.get("response") or data.get("result") or data.get("content") or ""
                if isinstance(response, dict):
                    response = response.get("text") or json.dumps(response, ensure_ascii=False)
                if response and not full_content:
                    yield AgentEvent(
                        type=AgentEventType.CHUNK,
                        content=str(response),
                        metadata={"protocol": "agy_stream_json"},
                    )
                    full_content.append(str(response))
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                yield AgentEvent(
                    type=AgentEventType.DONE,
                    content="".join(full_content),
                    metadata={
                        "protocol": "agy_stream_json",
                        "usage": usage,
                        "status": data.get("status"),
                    },
                )
                return
        returncode = await transport.wait()
        if returncode and not full_content:
            classified, diagnostics = self.classify_process_failure(
                session,
                returncode=returncode,
                stderr_text=transport.stderr_tail,
                diagnostics={
                    "returncode": returncode,
                    "stderr_tail": transport.stderr_tail,
                    "protocol": "agy_stream_json",
                },
            )
            if diagnostics.get("failure_subtype") == INVALID_CLI_ARGUMENT_SUBTYPE:
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error=f"AGENT_CLI_INVALID_ARGUMENT: {classified}",
                    metadata={
                        **diagnostics,
                        "failure_code": "AGENT_CLI_INVALID_ARGUMENT",
                        "failure": {
                            "code": "AGENT_CLI_INVALID_ARGUMENT",
                            "message": classified or "CLI rejected its arguments",
                            "retriable": False,
                        },
                    },
                )
                return
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error=classified or "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                metadata={
                    "failure_code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                    "failure": {
                        "code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                        "message": classified or "CLI exited without output",
                        "retriable": bool(diagnostics.get("retriable", True)),
                    },
                    **diagnostics,
                },
            )
            return
        yield AgentEvent(
            type=AgentEventType.DONE,
            content="".join(full_content),
            metadata={"protocol": "agy_stream_json"},
        )

    async def _close_stream(self, session: AgentSession) -> None:
        transport = session.session_data.pop("agy_stream_transport", None)
        if isinstance(transport, SubprocessAgentTransport):
            with contextlib.suppress(Exception):
                transport.close_stdin()
            await transport.close(force=transport.process_alive)

    async def close(self, session: AgentSession) -> None:
        await self._close_stream(session)
        await super().close(session)

    def classify_process_failure(
        self,
        session: AgentSession,
        *,
        returncode: int,
        stderr_text: str,
        diagnostics: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any]]:
        log_text = self._read_and_clear_cli_log(session)
        if not returncode:
            return None, diagnostics
        if is_invalid_cli_argument_error(stderr_text, log_text):
            # Deterministic invalid-argument exit: surface the CLI's own
            # message and never auto-retry the same argv.
            return (
                cli_argument_error_detail(stderr_text, log_text),
                {
                    **diagnostics,
                    "failure_subtype": INVALID_CLI_ARGUMENT_SUBTYPE,
                    "retriable": False,
                },
            )
        classified = classify_agy_cli_failure(stderr_text, log_text)
        if not classified:
            return None, diagnostics
        enriched = {
            **diagnostics,
            "stderr_tail": sanitize_diagnostic(classified, limit=2000),
            "cli_failure": classified,
        }
        return classified, enriched

    def cleanup_cli_artifacts(self, session: AgentSession) -> None:
        self._read_and_clear_cli_log(session)

    _OFFICIAL_MODELS: list[ModelCapability] = [
        ModelCapability(
            id="gemini-3.7-flash-high",
            display_name="Gemini 3.7 Flash (High Reasoning)",
            provider="google",
            supported_reasoning_efforts=["high", "medium", "low"],
            default_reasoning_effort="high",
            source="official_capability_table",
            reasoning_selection=SelectionStrategy.STARTUP,
            context_window=1_048_576,
        ),
        ModelCapability(
            id="gemini-3.7-flash",
            display_name="Gemini 3.7 Flash",
            provider="google",
            supported_reasoning_efforts=["high", "medium", "low"],
            default_reasoning_effort="medium",
            source="official_capability_table",
            reasoning_selection=SelectionStrategy.STARTUP,
            context_window=1_048_576,
        ),
        ModelCapability(
            id="gemini-2.5-pro",
            display_name="Gemini 2.5 Pro",
            provider="google",
            supported_reasoning_efforts=[],
            default_reasoning_effort=None,
            source="official_capability_table",
            reasoning_selection=SelectionStrategy.UNSUPPORTED,
            context_window=1_048_576,
        ),
        ModelCapability(
            id="gemini-2.5-flash",
            display_name="Gemini 2.5 Flash",
            provider="google",
            supported_reasoning_efforts=[],
            default_reasoning_effort=None,
            source="official_capability_table",
            reasoning_selection=SelectionStrategy.UNSUPPORTED,
            context_window=1_048_576,
        ),
    ]

    async def list_models(self) -> list[ModelCapability]:
        """Read the selectable model IDs reported by the installed CLI or official table."""

        from persona_continuum.performance.capability_cache import capability_cache_key

        binary = self._find_binary()
        if binary:
            identity = capability_cache_key(self)[1]
            cached = self._models_cache
            age = time.monotonic() - cached[1] if cached else float("inf")
            if cached and cached[0] == identity and age < 300 and not self._refresh_models:
                return [model.model_copy(deep=True) for model in cached[2]]
            self._refresh_models = False
            code, output, error = await safe_exec_cmd(
                [binary, "models"], timeout=self.model_discovery_timeout_seconds
            )
            if code == 0:
                discovered = self._parse_models(output)
                if discovered:
                    self._model_discovery_error = ""
                    self._models_cache = (identity, time.monotonic(), discovered)
                    return discovered
            self._model_discovery_error = sanitize_diagnostic(
                error or "CLI returned no usable model catalog", limit=250
            )
            # A failed refresh must not erase real evidence from this binary.
            if cached and cached[0] == identity and age < 6 * 60 * 60:
                return [model.model_copy(deep=True) for model in cached[2]]
        return []

    @staticmethod
    def _split_model_line(line: str) -> tuple[str, str] | None:
        parts = re.split(r"\t+|\s{2,}", line, maxsplit=1)
        model_id = parts[0].strip()
        if model_id and " " not in model_id:
            display = parts[1].strip() if len(parts) > 1 else model_id
            return model_id, display
        # agy 1.1.22 prints ``gemini-3.7-flash-highGemini 3.7 Flash (High)``
        # with no delimiter between id and display name.
        match = re.match(r"^([a-z0-9][a-z0-9._|-]*)([A-Z].+)$", line)
        if match:
            return match.group(1), match.group(2).strip()
        return None

    @classmethod
    def _parse_models(cls, output: str) -> list[ModelCapability]:
        models: list[ModelCapability] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line.casefold().startswith("fetching available models"):
                continue
            split = cls._split_model_line(line)
            if split is None:
                continue
            model_id, display_name = split
            match = re.search(r"-(low|medium|high)$", model_id)
            effort = match.group(1) if match else None
            official = next((item for item in cls._OFFICIAL_MODELS if item.id == model_id), None)
            registry_window = default_model_capability_registry().native_context_window(model_id)
            models.append(
                ModelCapability(
                    id=model_id,
                    display_name=display_name,
                    provider="google" if model_id.startswith("gemini-") else "agy",
                    supported_reasoning_efforts=[effort] if effort else [],
                    default_reasoning_effort=effort,
                    source="official_cli",
                    reasoning_selection=(
                        SelectionStrategy.STARTUP if effort else SelectionStrategy.UNSUPPORTED
                    ),
                    context_window=(
                        official.context_window if official else registry_window
                    ),
                )
            )
        families: dict[str, list[str]] = {}
        for model in models:
            if match := cls._EFFORT_SUFFIX.search(model.id):
                family = cls._EFFORT_SUFFIX.sub("", model.id)
                efforts = families.setdefault(family, [])
                if match.group(1) not in efforts:
                    efforts.append(match.group(1))
        for model in models:
            if cls._EFFORT_SUFFIX.search(model.id):
                efforts = families[cls._EFFORT_SUFFIX.sub("", model.id)]
                model.supported_reasoning_efforts = list(efforts)
                model.reasoning_capability = ReasoningCapability(
                    mode=ReasoningCapabilityMode.NATIVE_EFFORT,
                    supported_efforts=list(efforts),
                    default_effort=model.default_reasoning_effort,
                    binding_strategy="agy_model_suffix_and_effort_flag",
                    verified=True,
                    source="official_cli",
                )
        return models


class AgyStreamingAdapter(GeminiCliAdapter):
    """Persistent agy `--input-format stream-json` session (stdin NDJSON)."""

    adapter_id = "gemini_cli"


class GoogleGeminiCliAdapter(GeminiCliAdapter):
    """Bind Google thinkingLevel through isolated native model settings."""

    credential_env_var = "GEMINI_API_KEY"
    retry_catalog_after_scan = False
    # Google generation API's model-specific levels, not agy's suffix IDs.
    _THINKING_LEVELS = {
        "gemini-3-pro-preview": ["low", "high"],
        "gemini-3.1-pro-preview": ["low", "medium", "high"],
        "gemini-3.1-pro-preview-customtools": ["low", "medium", "high"],
        "gemini-3-flash-preview": ["minimal", "low", "medium", "high"],
        "gemini-3.5-flash": ["minimal", "low", "medium", "high"],
        "gemini-3.8-flash": ["low", "medium", "high"],
        "gemini-3.1-flash-lite": ["minimal", "low", "medium", "high"],
        "gemini-3.5-flash-lite": ["minimal", "low", "medium", "high"],
    }

    def __init__(self) -> None:
        super().__init__()
        self.adapter_id = "gemini_google"
        self.name = "Gemini CLI (Google)"
        self.binary_candidates = [
            "gemini", "gemini-cli", "~/.npm-global/bin/gemini",
            "~/.local/bin/gemini", "/opt/homebrew/bin/gemini", "/usr/local/bin/gemini",
        ]
        self.reasoning_flag = None
        self._thinking_config_supported = False

    def resolve_cli_model_and_reasoning(
        self, config: AgentSessionConfig
    ) -> tuple[str | None, str | None]:
        model = (config.model_id or "").strip() or None
        if model and self._EFFORT_SUFFIX.search(model):
            raise ModelBindingUnverifiedError(
                "This is an agy model ID; select the Gemini CLI (agy) runtime",
                phase="session_binding",
            )
        effort = (config.reasoning_effort or "").strip().casefold()
        if effort in {"", "none", "default"}:
            return model, None
        models = self._read_installed_catalog()
        capability = next((item for item in models if item.id == model), None)
        if not capability or effort not in capability.supported_reasoning_efforts:
            raise ReasoningBindingUnverifiedError(
                "Select a Google model and a thinking level reported by this CLI",
                phase="session_binding",
            )
        return model, effort

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        model, effort = self.resolve_cli_model_and_reasoning(config)
        session = await super().create_session(config)
        session.session_data.update(
            effective_model=model,
            effective_reasoning=effort,
            reasoning_selection_applied=True,
        )
        return session

    async def bind_runtime(self, session: AgentSession) -> RuntimeBindingSnapshot:
        self.resolve_cli_model_and_reasoning(session.config)
        return build_runtime_binding_snapshot(
            self, session, protocol="plain_cli",
            model_verified=True, reasoning_verified=True,
            verification_method="gemini_cli_custom_alias",
        )

    def build_model_cli_args(self, session: AgentSession, model_id: str | None) -> list[str]:
        _, effort = self.resolve_cli_model_and_reasoning(session.config)
        if model_id and effort:
            alias = f"{model_id}-pc-{session.config.session_id}"
            session.session_data["google_model_alias"] = alias
            return ["--model", alias]
        return super().build_model_cli_args(session, model_id)

    def build_cli_environment(self, session: AgentSession) -> dict[str, str]:
        env = super().build_cli_environment(session)
        model, effort = self.resolve_cli_model_and_reasoning(session.config)
        if not effort:
            return env
        self.build_model_cli_args(session, model)
        original_home = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home())
        original_dir = original_home / ".gemini"
        original = original_dir / "settings.json"
        settings: dict[str, Any] = {}
        if original.exists():
            # Preserve user configuration and comments without editing it.
            text = original.read_text(encoding="utf-8")
            text = re.sub(
                r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/',
                lambda match: match.group(0) if match.group(0).startswith('"') else "",
                text,
            )
            settings = json.loads(text)
        model_configs = settings.setdefault("modelConfigs", {})
        alias = session.session_data["google_model_alias"]
        model_configs.setdefault("customAliases", {})[alias] = {
            "extends": model,
            "modelConfig": {
                "model": model,
                "generateContentConfig": {"thinkingConfig": {"thinkingLevel": effort.upper()}},
            },
        }
        # Alias specificity keeps workspace/base-model overrides from replacing
        # this explicit selection; existing system policy remains authoritative.
        model_configs.setdefault("customOverrides", []).append({
            "match": {"model": alias},
            "modelConfig": {"generateContentConfig": {
                "thinkingConfig": {"thinkingLevel": effort.upper()}
            }},
        })
        self.cleanup_cli_artifacts(session)
        home = Path(tempfile.mkdtemp(prefix="persona-google-thinking-"))
        session.session_data["google_settings_home"] = str(home)
        gemini_dir = home / ".gemini"
        gemini_dir.mkdir(mode=0o700)
        if original_dir.is_dir():
            for entry in original_dir.iterdir():
                if entry.name != "settings.json":
                    (gemini_dir / entry.name).symlink_to(entry.resolve())
        with tempfile.NamedTemporaryFile(
            dir=gemini_dir, prefix="settings-", suffix=".json", mode="w",
            encoding="utf-8", delete=False,
        ) as handle:
            json.dump(settings, handle)
        Path(handle.name).replace(gemini_dir / "settings.json")
        env["GEMINI_CLI_HOME"] = str(home)
        return env

    def cleanup_cli_artifacts(self, session: AgentSession) -> None:
        raw = session.session_data.pop("google_settings_home", None)
        if raw:
            shutil.rmtree(raw, ignore_errors=True)

    async def close(self, session: AgentSession) -> None:
        self.cleanup_cli_artifacts(session)
        await super().close(session)

    async def list_models(self) -> list[ModelCapability]:
        return self._read_installed_catalog()

    def _read_installed_catalog(self) -> list[ModelCapability]:
        binary = self._find_binary()
        if not binary:
            return []
        # Read the installed package's catalog; `gemini models` is a prompt,
        # not a metadata command, and can accidentally start a model turn.
        root = Path(binary).resolve().parent
        package_root = next(
            (p for p in [root, *root.parents] if (p / "package.json").is_file()), None
        )
        if package_root is None:
            return []
        paths = [package_root / "dist/core/config/models.js"]
        paths.extend(sorted((package_root / "bundle").glob("chunk-*.js")))
        for path in paths:
            try:
                if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            ids = re.findall(
                r'(?:var|const|let)\s+(?:LATEST|BASE|DEFAULT|PREVIEW)_'
                r'[A-Z0-9_]*MODEL\s*=\s*[\"\'](gemini-[a-z0-9.\-]+)[\"\']', text
            )
            if ids:
                self._thinking_config_supported = all(
                    marker in text for marker in (
                        "GEMINI_CLI_HOME", "customAliases", "thinkingLevel"
                    )
                )
                return [
                    ModelCapability(
                        id=model_id, display_name=model_id, provider="google",
                        source="official_cli",
                        supported_reasoning_efforts=(
                            self._THINKING_LEVELS.get(model_id, [])
                            if self._thinking_config_supported else []
                        ),
                        default_reasoning_effort=(
                            "high" if self._thinking_config_supported
                            and model_id in self._THINKING_LEVELS else None
                        ),
                        reasoning_selection=SelectionStrategy.CONFIG,
                        reasoning_capability=ReasoningCapability(
                            mode=(ReasoningCapabilityMode.NATIVE_EFFORT
                                  if self._thinking_config_supported
                                  and model_id in self._THINKING_LEVELS
                                  else ReasoningCapabilityMode.UNSUPPORTED),
                            supported_efforts=(self._THINKING_LEVELS.get(model_id, [])
                                              if self._thinking_config_supported else []),
                            default_effort=("high" if self._thinking_config_supported
                                            and model_id in self._THINKING_LEVELS else None),
                            binding_strategy="gemini_model_config_thinking_level",
                            verified=(self._thinking_config_supported
                                      and model_id in self._THINKING_LEVELS),
                            source="installed_cli+google_thinking_table",
                        ),
                        context_window=default_model_capability_registry().native_context_window(
                            model_id
                        ),
                    )
                    for model_id in dict.fromkeys(ids) if "embedding" not in model_id
                ]
        return []
