from __future__ import annotations

import contextlib
import re
import tempfile
from pathlib import Path
from typing import Any

from persona_continuum.agent.adapter import AgentSession, safe_exec_cmd
from persona_continuum.agent.context_capability import ContextWindowMode
from persona_continuum.agent.models import (
    AgentSessionConfig,
    AgentTurn,
    ModelCapability,
    PermissionProfile,
    ResearchCapability,
    ResearchVerificationStatus,
    SelectionStrategy,
)
from persona_continuum.agent.protocols.plain_cli import PlainCliAdapter
from persona_continuum.agent.response_collector import sanitize_diagnostic

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
    match = re.search(r"agent executor error:\s*(.+)", blob, re.I)
    if match:
        return sanitize_diagnostic(match.group(1).strip(), limit=500)
    if "agent execution terminated due to error" in lowered:
        return "Agent execution terminated due to error."
    cleaned = sanitize_diagnostic((stderr_text or "").strip(), limit=500)
    return cleaned or None


class GeminiCliAdapter(PlainCliAdapter):
    # The Gemini CLI fixes the window per model; nothing is passed on the wire.
    context_window_mode = ContextWindowMode.FIXED

    _HEADLESS_NO_TOOL_POLICY = """[HEADLESS TOOL POLICY]
This is a non-interactive task and every required input is already included below.
Do not call GrepSearch, read_file, shell, web, or any other tool. Do not inspect the
workspace. Return the requested answer directly, following the expected output schema."""

    def __init__(self) -> None:
        super().__init__(
            adapter_id="gemini_cli",
            name="Gemini CLI",
            binary_candidates=[
                "gemini",
                "agy",
                "gemini-cli",
                "~/.local/bin/agy",
                "~/.local/bin/gemini",
                "~/.npm-global/bin/gemini",
                "~/.gemini/bin/gemini",
                "/opt/homebrew/bin/gemini",
                "/opt/homebrew/bin/agy",
                "/usr/local/bin/gemini",
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
        """Keep no-tool Persona work from triggering headless permission prompts."""

        if (
            config.permission_profile == PermissionProfile.CHAT_SAFE
            and not config.tools
            and not turn.tools
        ):
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
        return ["--log-file", log_path]

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
            context_window=2_097_152,
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

        binary = self._find_binary()
        if binary:
            code, output, _ = await safe_exec_cmd([binary, "models"], timeout=10.0)
            if code == 0:
                discovered = self._parse_models(output)
                if discovered:
                    return discovered
        return [m.model_copy() for m in self._OFFICIAL_MODELS]

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
            models.append(
                ModelCapability(
                    id=model_id,
                    display_name=display_name,
                    provider="google" if model_id.startswith("gemini-") else "agy",
                    supported_reasoning_efforts=[effort] if effort else [],
                    default_reasoning_effort=effort,
                    source="official_cli",
                    reasoning_selection=(
                        SelectionStrategy.STARTUP
                        if effort
                        else SelectionStrategy.UNSUPPORTED
                    ),
                    context_window=official.context_window if official else None,
                )
            )
        return models
