from __future__ import annotations

from typing import Any

from persona_continuum.agent.adapter import safe_exec_cmd
from persona_continuum.agent.models import (
    AgentSessionConfig,
    ModelCapability,
    RuntimeBindingSnapshot,
    SelectionStrategy,
)
from persona_continuum.agent.protocols.acp import ACPAdapter


class OpenCodeAdapter(ACPAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="opencode",
            name="OpenCode",
            binary_candidates=[
                "opencode",
                "opencodex",
                "ocx",
                "~/.npm-global/bin/ocx",
                "~/.npm-global/bin/opencodex",
                "~/.local/bin/opencode",
                "~/.local/bin/ocx",
                "/opt/homebrew/bin/opencode",
                "/opt/homebrew/bin/ocx",
                "/usr/local/bin/opencode",
                "/usr/local/bin/ocx",
            ],
            version_args=["--version"],
            acp_args=["acp"],
            default_models=self._fallback_models(),
        )
        self.require_verified_binding = True

    def select_auth_method(
        self,
        auth_methods: list[dict[str, Any]],
        config: AgentSessionConfig,
    ) -> dict[str, Any] | None:
        by_id = {self._auth_method_id(method): method for method in auth_methods}
        if "opencode-login" in by_id:
            return by_id["opencode-login"]
        return super().select_auth_method(auth_methods, config)

    def build_session_new_params(self, config: AgentSessionConfig) -> dict[str, Any]:
        """Bind OpenCode's selected model and reasoning at session creation."""

        params = super().build_session_new_params(config)
        default_binding = str(config.model_id or "").lower() in {
            "",
            "default",
            "auto",
            "opencode-default",
        }
        if config.model_id and not default_binding:
            params["model"] = config.model_id
        if (
            config.reasoning_effort
            and not default_binding
            and str(config.reasoning_effort).lower()
            not in {
                "none",
                "default",
            }
        ):
            params["reasoningEffort"] = config.reasoning_effort
        return params

    def _runtime_binding_from_session_result(
        self,
        config: AgentSessionConfig,
        session_result: Any,
    ) -> RuntimeBindingSnapshot:
        """Treat the explicit pseudo-model as an honest ACP default request."""

        if str(config.model_id or "").lower() == "opencode-default":
            default_config = config.model_copy(
                update={"model_id": None, "reasoning_effort": None}
            )
            snapshot = super()._runtime_binding_from_session_result(
                default_config, session_result
            )
            snapshot.verification_method = "acp_session_new_default"
            snapshot.diagnostics["default_binding"] = True
            return snapshot
        return super()._runtime_binding_from_session_result(config, session_result)

    @classmethod
    def _fallback_models(cls) -> list[ModelCapability]:
        known = [
            ("grok-4.6", "Grok 4.6", "xai", ["low", "medium", "high", "xhigh"]),
            ("grok-4.5", "Grok 4.5", "xai", ["low", "medium", "high"]),
            (
                "deepseek-v4-flash-0731",
                "DeepSeek V4 Flash",
                "alibailian",
                ["none", "low", "medium", "high"],
            ),
            (
                "anthropic/claude-sonnet-5",
                "Claude Sonnet 5",
                "openrouter",
                ["none", "low", "medium", "high"],
            ),
            ("openai/gpt-5.6", "GPT-5.6", "openrouter", ["none", "low", "medium", "high"]),
            (
                "openai/gpt-5.6-sol",
                "GPT-5.6 Sol Reasoning",
                "openrouter",
                ["low", "medium", "high", "max"],
            ),
            (
                "openai/gpt-5.6-terra",
                "GPT-5.6 Terra",
                "openrouter",
                ["none", "low", "medium", "high"],
            ),
            ("openai/gpt-5.6-luna", "GPT-5.6 Luna", "openrouter", ["none", "low", "medium"]),
            (
                "opencode-default",
                "OpenCode Default Engine",
                "opencode",
                ["none", "low", "medium", "high"],
            ),
        ]
        models = [
            ModelCapability(
                id=m_id,
                display_name=desc,
                provider=prov,
                supported_reasoning_efforts=efforts,
                default_reasoning_effort="high"
                if "xhigh" in efforts or "max" in efforts
                else "medium",
                source="config",
                reasoning_selection=SelectionStrategy.STARTUP,
            )
            for m_id, desc, prov, efforts in known
        ]
        default = next(model for model in models if model.id == "opencode-default")
        default = default.model_copy(
            update={
                "supported_reasoning_efforts": [],
                "default_reasoning_effort": None,
                "reasoning_selection": SelectionStrategy.UNSUPPORTED,
            }
        )
        explicit = [
            model.model_copy(update={"selectable": False})
            for model in models
            if model.id != "opencode-default"
        ]
        return [default, *explicit]

    async def list_models(self) -> list[ModelCapability]:
        binary = self._find_binary()
        if binary:
            code, out, _ = await safe_exec_cmd([binary, "models"], timeout=6.0)
            if code == 0 and out:
                parsed = self._parse_models(out)
                if parsed and any(model.selectable for model in parsed):
                    return parsed
        return self._fallback_models()

    def _parse_models(self, text: str) -> list[ModelCapability]:
        lines = text.splitlines()
        models: list[ModelCapability] = []
        current_provider = "opencode"

        for line in lines:
            line_s = line.strip()
            if not line_s or line_s.startswith(("*", "Note:", "Usage:", "error:", "warning:")):
                continue

            # Provider section header, e.g. "xai:", "alibailian:", "openrouter:"
            if line_s.endswith(":") and not line.startswith(" "):
                current_provider = line_s[:-1].strip().lower()
                continue

            clean_line = line.strip().lstrip("*-• ")
            parts = clean_line.split()
            if parts:
                m_id = parts[0]
                if m_id.lower() in {"note:", "usage:", "error:", "warning:", "available"}:
                    continue

                provider = current_provider
                if "/" in m_id:
                    provider = m_id.split("/")[0]

                has_reasoning = any(
                    k in m_id.lower() for k in ("reasoning", "r1", "sol", "terra", "grok-4.6")
                )
                efforts = (
                    ["low", "medium", "high", "xhigh"]
                    if has_reasoning
                    else ["none", "low", "medium", "high"]
                )

                models.append(
                    ModelCapability(
                        id=m_id,
                        display_name=m_id,
                        provider=provider,
                        supported_reasoning_efforts=efforts,
                        default_reasoning_effort="high" if has_reasoning else "medium",
                        # OpenCode reports model IDs, while effort values are
                        # this adapter's conservative configuration. ACP
                        # session/new must still echo the selected effort
                        # because require_verified_binding is enabled.
                        source="config",
                        selectable=False,
                        reasoning_selection=SelectionStrategy.STARTUP,
                    )
                )

        return models
