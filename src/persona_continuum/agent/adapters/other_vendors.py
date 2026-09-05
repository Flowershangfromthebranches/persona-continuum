from __future__ import annotations

import re

from persona_continuum.agent.adapter import resolve_binary, safe_exec_cmd
from persona_continuum.agent.models import (
    AgentProbeResult,
    AgentStatus,
    ModelCapability,
    ResearchCapability,
    ResearchVerificationStatus,
    SelectionStrategy,
)
from persona_continuum.agent.protocols.plain_cli import PlainCliAdapter


class QwenAdapter(PlainCliAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="qwen",
            name="Qwen CLI",
            binary_candidates=[
                "qwen",
                "~/.qwen/bin/qwen",
                "/usr/local/bin/qwen",
                "/opt/homebrew/bin/qwen",
            ],
            version_args=["--version"],
            exec_args=["run"],
            model_flag="--model",
            default_models=[
                ModelCapability(
                    id="qwen3.8-max",
                    display_name="Qwen 3.8 Max",
                    provider="alibaba",
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="high",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="qwen3.8-27b",
                    display_name="Qwen 3.8 27B",
                    provider="alibaba",
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="medium",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="qwen3.7-max",
                    display_name="Qwen 3.7 Max",
                    provider="alibaba",
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="high",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="qwen3.7-plus",
                    display_name="Qwen 3.7 Plus",
                    provider="alibaba",
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="medium",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="qwen3.7-flash",
                    display_name="Qwen 3.7 Flash",
                    provider="alibaba",
                    supported_reasoning_efforts=["none", "low", "medium"],
                    default_reasoning_effort="medium",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="qwen-2.5-coder-32b",
                    display_name="Qwen 2.5 Coder 32B",
                    provider="alibaba",
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="medium",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
            ],
        )


class KimiAdapter(PlainCliAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="kimi",
            name="Kimi CLI",
            binary_candidates=[
                "kimi",
                "~/.kimi/bin/kimi",
                "/usr/local/bin/kimi",
                "/opt/homebrew/bin/kimi",
            ],
            version_args=["--version"],
            exec_args=["chat"],
            model_flag="--model",
            default_models=[
                ModelCapability(
                    id="kimi-k3",
                    display_name="Kimi K3 (1M Context)",
                    provider="moonshot",
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="high",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="kimi-k2.7-code",
                    display_name="Kimi K2.7 Code",
                    provider="moonshot",
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="medium",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="kimi-k2.6",
                    display_name="Kimi K2.6",
                    provider="moonshot",
                    supported_reasoning_efforts=["none", "low", "medium"],
                    default_reasoning_effort="medium",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="kimi-k2.5",
                    display_name="Kimi K2.5",
                    provider="moonshot",
                    supported_reasoning_efforts=["none"],
                    default_reasoning_effort="none",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
            ],
        )


class CopilotAdapter(PlainCliAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="copilot",
            name="GitHub Copilot CLI",
            binary_candidates=[
                "gh",
                "copilot",
                "/usr/local/bin/copilot",
                "/opt/homebrew/bin/copilot",
            ],
            version_args=["copilot", "--version"],
            exec_args=["copilot", "suggest"],
            model_flag="--model",
            default_models=[
                ModelCapability(
                    id="gpt-5.4",
                    display_name="GPT-5.4 (Copilot)",
                    provider="github",
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="medium",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="claude-sonnet-5",
                    display_name="Claude Sonnet 5 (Copilot)",
                    provider="github",
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="high",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="copilot-default",
                    display_name="Copilot Default Engine",
                    provider="github",
                    supported_reasoning_efforts=["none"],
                    default_reasoning_effort="none",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
            ],
        )


class QoderAdapter(PlainCliAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="qoder",
            name="Qoder",
            binary_candidates=[
                "qodercli",
                "qoderclicn",
                "qoder",
                "~/.local/bin/qodercli",
                "~/.local/bin/qoderclicn",
                "~/.qoder/bin/qodercli/qodercli",
                "~/.qoder-cn/bin/qoderclicn/qoderclicn",
                "/opt/homebrew/bin/qodercli",
                "/usr/local/bin/qodercli",
            ],
            version_args=["--version"],
            exec_args=["-p"],
            model_flag="-m",
            reasoning_flag="--reasoning-effort",
            # Qoder CLI ships native WebSearch/WebFetch tools.  This is a
            # declaration, not proof: the shared research backend still runs
            # a behavioral probe before a room turn relies on live sources.
            research=ResearchCapability(
                mode="native_cli",
                search=True,
                fetch=True,
                can_discover_sources=True,
                can_read_sources=True,
                citations=True,
                live=True,
                source="qoder_cli:builtin_websearch_webfetch",
                verification_status=ResearchVerificationStatus.DECLARED,
                verification_method="adapter_declaration",
            ),
            default_models=self._fallback_models(),
        )

    async def probe(self) -> AgentProbeResult:
        result = await super().probe()
        # ``list_models`` may pin a different edition than the one the base
        # probe read its version from (for example the international build is
        # not logged in while the CN build is).  Re-read the version from the
        # pinned binary so the snapshot matches the runtime sessions use.
        pinned = self._resolved_binary
        if result.binary_path and pinned and pinned != result.binary_path:
            code, out, _ = await safe_exec_cmd(
                [pinned, *self.version_args], timeout=5.0
            )
            version_str = out.strip().splitlines()[0] if code == 0 and out else result.version
            result = result.model_copy(
                update={"binary_path": pinned, "version": version_str}
            )
        return result

    @classmethod
    def _fallback_models(cls) -> list[ModelCapability]:
        # Mirrors the verified Qoder CLI catalog (v1.1.32 listing) for the
        # case where no local edition is logged in to list dynamically.
        known = [
            ("Auto", "qoder"),
            ("Qwen3.8-Max", "alibaba"),
            ("Qwen3.8-Flash", "alibaba"),
            ("Qwen3.7-Max", "alibaba"),
            ("Qwen3.7-Plus", "alibaba"),
            ("Qwen3.7-Flash", "alibaba"),
            ("DeepSeek-V4-Pro", "deepseek"),
            ("DeepSeek-V4-Flash", "deepseek"),
            ("GLM-5.3", "zhipu"),
            ("GLM-5.3-Flash", "zhipu"),
            ("GLM-5.2", "zhipu"),
            ("Kimi-K2.7-Code", "moonshot"),
            ("MiniMax-M2.7", "minimax"),
        ]
        return [
            ModelCapability(
                id=m_id,
                display_name=m_id,
                provider=provider,
                supported_reasoning_efforts=["none", "low", "medium", "high"],
                default_reasoning_effort="medium",
                source="config",
                reasoning_selection=SelectionStrategy.STARTUP,
            )
            for m_id, provider in known
        ]

    def _candidate_binaries(self) -> list[str]:
        """Every installed Qoder edition, pinned binary first.

        The international and CN builds are separate binaries with separate
        logins; probing only the first one hides the models the user can
        actually run.
        """

        paths: list[str] = []
        if self._resolved_binary:
            paths.append(self._resolved_binary)
        for candidate in self.binary_candidates:
            resolved = resolve_binary([candidate])
            if resolved and resolved not in paths:
                paths.append(resolved)
        return paths

    async def list_models(self) -> list[ModelCapability]:
        merged: dict[str, ModelCapability] = {}
        listing_binaries: list[str] = []
        for binary in self._candidate_binaries():
            code, out, err = await safe_exec_cmd([binary, "--list-models"], timeout=6.0)
            output = out or err or ""
            if code != 0 or "not logged in" in output.lower():
                continue
            parsed = self._parse_models(output)
            if not parsed:
                continue
            listing_binaries.append(binary)
            for model in parsed:
                merged.setdefault(model.id.lower(), model)
        if merged:
            # Sessions must run on an edition that served the model list.
            self._resolved_binary = listing_binaries[0]
            return list(merged.values())
        return self._fallback_models()

    @staticmethod
    def _provider_for(model_id: str) -> str:
        lowered = model_id.lower()
        for prefix, provider in (
            ("qwen", "alibaba"),
            ("deepseek", "deepseek"),
            ("glm", "zhipu"),
            ("kimi", "moonshot"),
            ("minimax", "minimax"),
        ):
            if lowered.startswith(prefix):
                return provider
        return "qoder"

    def _parse_models(self, text: str) -> list[ModelCapability]:
        models: list[ModelCapability] = []
        seen: set[str] = set()
        for line in text.splitlines():
            line_s = line.strip().lstrip("*-• ")
            if not line_s or line_s.upper() == "MODEL":
                continue
            if line_s.lower().startswith(
                ("available", "note", "tip", "error", "warning", "not logged in")
            ):
                continue
            parts = line_s.split()
            if not parts:
                continue
            m_id = parts[0]
            key = m_id.lower()
            if key in seen:
                continue
            seen.add(key)
            models.append(
                ModelCapability(
                    id=m_id,
                    display_name=line_s,
                    provider=self._provider_for(m_id),
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="medium",
                    # The listing came from the official CLI and the adapter
                    # binds --reasoning-effort at session startup; classifying
                    # it as "dynamic" would hide the effort ladder from the UI.
                    source="official_cli",
                    reasoning_selection=SelectionStrategy.STARTUP,
                )
            )
        return models


class CodeBuddyAdapter(PlainCliAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="codebuddy",
            name="CodeBuddy CLI",
            binary_candidates=[
                "codebuddy",
                "codebuddy-code",
                "cbc",
                "~/.npm-global/bin/codebuddy",
                "~/.npm-global/bin/codebuddy-code",
                "~/.npm-global/bin/cbc",
                "~/.local/bin/codebuddy",
                "/usr/local/bin/codebuddy",
                "/opt/homebrew/bin/codebuddy",
            ],
            version_args=["--version"],
            exec_args=["-p"],
            model_flag="--model",
            reasoning_flag="--effort",
            default_models=self._fallback_models(),
        )

    async def probe(self) -> AgentProbeResult:
        result = await super().probe()
        if result.status == AgentStatus.READY and result.binary_path:
            code, out, err = await safe_exec_cmd([result.binary_path, "status"], timeout=4.0)
            combined = f"{out}\n{err}".lower()
            if "authentication required" in combined or "use /login" in combined:
                result.status = AgentStatus.AUTH_REQUIRED
                result.auth_status = "auth_required"
                result.status_detail = (
                    "Authentication required. Please use /login command to sign in to your account"
                )
        return result

    @classmethod
    def _fallback_models(cls) -> list[ModelCapability]:
        known = [
            ("hy3", "Hunyuan 3", "tencent", ["none", "low", "medium", "high"]),
            ("hy3-x", "Hunyuan 3-X Extended", "tencent", ["none", "low", "medium", "high"]),
            ("glm-5.3", "GLM-5.3 Frontier", "zhipu", ["none", "low", "medium", "high", "max"]),
            ("glm-5.2", "GLM-5.2 Long-Horizon", "zhipu", ["none", "low", "medium", "high"]),
            ("glm-5.1", "GLM-5.1 Autonomous", "zhipu", ["none", "low", "medium", "high"]),
            ("glm-5v-turbo", "GLM-5V Turbo Vision", "zhipu", ["none", "low", "medium"]),
            (
                "minimax-m3",
                "MiniMax M3 Native Multimodal",
                "minimax",
                ["none", "low", "medium", "high"],
            ),
            ("minimax-m2.7", "MiniMax M2.7 SE Agent", "minimax", ["none", "low", "medium", "high"]),
            ("kimi-k3-1", "Kimi K3-1 Long-Horizon", "moonshot", ["none", "low", "medium", "high"]),
            ("kimi-k2.7", "Kimi K2.7 Coding", "moonshot", ["none", "low", "medium", "high"]),
            ("kimi-k2.6", "Kimi K2.6 Vision", "moonshot", ["none", "low", "medium"]),
            (
                "deepseek-v4-pro",
                "DeepSeek V4 Pro Reasoning",
                "deepseek",
                ["none", "low", "medium", "high", "max"],
            ),
            (
                "deepseek-v4-flash",
                "DeepSeek V4 Flash",
                "deepseek",
                ["none", "low", "medium", "high"],
            ),
        ]
        return [
            ModelCapability(
                id=m_id,
                display_name=f"{m_id} ({desc})",
                provider=prov,
                supported_reasoning_efforts=efforts,
                default_reasoning_effort="high" if "max" in efforts else "medium",
                source="config",
                reasoning_selection=SelectionStrategy.STARTUP,
            )
            for m_id, desc, prov, efforts in known
        ]

    async def list_models(self) -> list[ModelCapability]:
        binary = self._find_binary()
        if binary:
            code, out, _ = await safe_exec_cmd([binary, "--help"], timeout=6.0)
            if code == 0 and out:
                parsed = self._parse_models_from_help(out)
                if parsed:
                    return parsed
        return self._fallback_models()

    def _parse_models_from_help(self, text: str) -> list[ModelCapability]:
        match = re.search(r"Currently supported:\s*\(([^)]+)\)", text, re.IGNORECASE)
        if not match:
            return []
        model_names = [m.strip() for m in match.group(1).split(",") if m.strip()]
        models: list[ModelCapability] = []
        for name in model_names:
            if name.startswith("hy3"):
                provider = "tencent"
            elif name.startswith("glm"):
                provider = "zhipu"
            elif name.startswith("minimax"):
                provider = "minimax"
            elif name.startswith("kimi"):
                provider = "moonshot"
            elif name.startswith("deepseek"):
                provider = "deepseek"
            else:
                provider = "codebuddy"

            has_reasoning = any(k in name for k in ("pro", "k3", "glm-5", "hy3-x"))
            efforts = (
                ["none", "low", "medium", "high", "xhigh", "max"]
                if has_reasoning
                else ["none", "low", "medium", "high"]
            )
            models.append(
                ModelCapability(
                    id=name,
                    display_name=name,
                    provider=provider,
                    supported_reasoning_efforts=efforts,
                    default_reasoning_effort="high" if has_reasoning else "medium",
                    # Parsed from the official CLI --help listing; the adapter
                    # binds --effort at session startup, so this is native
                    # startup-selection evidence, not an unknown guess.
                    source="official_cli",
                    reasoning_selection=SelectionStrategy.STARTUP,
                )
            )
        return models


class WorkBuddyAdapter(CodeBuddyAdapter):
    def __init__(self) -> None:
        PlainCliAdapter.__init__(
            self,
            adapter_id="workbuddy",
            name="WorkBuddy",
            binary_candidates=[
                "workbuddy",
                "codebuddy",
                "~/.npm-global/bin/codebuddy",
                "~/.local/bin/workbuddy",
                "/usr/local/bin/workbuddy",
                "/opt/homebrew/bin/workbuddy",
            ],
            version_args=["--version"],
            exec_args=["-p"],
            model_flag="--model",
            reasoning_flag="--effort",
            default_models=CodeBuddyAdapter._fallback_models(),
        )

    async def list_models(self) -> list[ModelCapability]:
        binary = self._find_binary()
        if binary:
            code, out, _ = await safe_exec_cmd([binary, "--help"], timeout=6.0)
            if code == 0 and out:
                parsed = self._parse_models_from_help(out)
                if parsed:
                    return parsed
        return CodeBuddyAdapter._fallback_models()

    @staticmethod
    def _parse_models_from_help(text: str) -> list[ModelCapability]:
        match = re.search(r"Currently supported:\s*\(([^)]+)\)", text, re.IGNORECASE)
        if not match:
            return []
        model_names = [m.strip() for m in match.group(1).split(",") if m.strip()]
        return [
            ModelCapability(
                id=m,
                display_name=m,
                provider="workbuddy",
                supported_reasoning_efforts=["none", "low", "medium", "high"],
                default_reasoning_effort="medium",
                # Parsed from the official CLI --help listing and bound via
                # --effort at startup; "dynamic" would classify as unknown
                # and hide the effort ladder.
                source="official_cli",
                reasoning_selection=SelectionStrategy.STARTUP,
            )
            for m in model_names
        ]


class DeepSeekHarnessAdapter(PlainCliAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="deepseek_harness",
            name="DeepSeek Harness",
            binary_candidates=[
                "deepseek",
                "dsh",
                "~/.npm-global/bin/dsh",
                "~/.local/bin/dsh",
                "~/.deepseek/bin/deepseek",
                "/opt/homebrew/bin/dsh",
                "/usr/local/bin/deepseek",
            ],
            version_args=["--version"],
            exec_args=["chat"],
            model_flag="--model",
            default_models=[
                ModelCapability(
                    id="deepseek-v4-pro",
                    display_name="DeepSeek V4 Pro Reasoning",
                    provider="deepseek",
                    supported_reasoning_efforts=["low", "medium", "high", "xhigh", "max"],
                    default_reasoning_effort="high",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="deepseek-v4-flash",
                    display_name="DeepSeek V4 Flash",
                    provider="deepseek",
                    supported_reasoning_efforts=["none", "low", "medium", "high"],
                    default_reasoning_effort="medium",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="deepseek-reasoner",
                    display_name="DeepSeek R1 (Reasoner)",
                    provider="deepseek",
                    supported_reasoning_efforts=["low", "medium", "high", "xhigh"],
                    default_reasoning_effort="high",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="deepseek-chat",
                    display_name="DeepSeek V3",
                    provider="deepseek",
                    supported_reasoning_efforts=["none"],
                    default_reasoning_effort="none",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
                ModelCapability(
                    id="deepseek-coder",
                    display_name="DeepSeek Coder 33B",
                    provider="deepseek",
                    supported_reasoning_efforts=["none", "low", "medium"],
                    default_reasoning_effort="medium",
                    source="config",
                    reasoning_selection=SelectionStrategy.STARTUP,
                ),
            ],
        )
