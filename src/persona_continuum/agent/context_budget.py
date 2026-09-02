"""Context-window budgeting and lossless evidence batching."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from persona_continuum.agent.context_capability import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    PLANNING_CONTEXT_WINDOW_TOKENS,
    compute_preferred_working_context,
    compute_usable_budget,
    default_model_capability_registry,
)
from persona_continuum.agent.models import (
    AgentTurn,
    EffectiveModelCapabilities,
    ModelCapability,
    PromptEnvelope,
)
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.response_collector import ContextBudgetExceededError
from persona_continuum.numeric import safe_int

# Re-exported for compatibility.  This is a PLANNING fallback only: an unknown
# context window is never reported as "32K of measured capability".
CHARS_PER_TOKEN = 4

# ModelCapability.source values that count as measured evidence rather than a
# curated guess.
_PROBE_SOURCE_VALUES = frozenset({"dynamic", "protocol_model_list", "official_cli"})


class TokenEstimator:
    """Tokenizer facade with a conservative multilingual fallback.

    Runtime integrations may inject a model tokenizer later.  The fallback
    counts CJK characters separately so Chinese material is not budgeted at
    the historically unsafe ``chars / 4`` ratio.
    """

    def __init__(self, tokenizer: Callable[[str], int] | None = None) -> None:
        self.tokenizer = tokenizer

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        if self.tokenizer is not None:
            try:
                return max(0, int(self.tokenizer(text)))
            except (TypeError, ValueError, RuntimeError):
                pass
        cjk_count = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
        non_cjk_count = max(0, len(text) - cjk_count)
        return cjk_count + math.ceil(non_cjk_count / CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    context_window_tokens: int
    max_prompt_tokens: int
    system_schema_reserve_tokens: int
    expected_output_reserve_tokens: int
    reasoning_reserve_tokens: int
    evidence_token_budget: int
    estimated_prompt_tokens: int = 0
    estimated_prompt_chars: int = 0
    phase: str = "agent_turn"
    # Provenance of ``context_window_tokens``: an unverified budget means the
    # number came from the planning fallback policy, not from the runtime.
    context_window_source: str = "unknown"
    context_verified: bool = False
    usable_context_budget: int | None = None
    preferred_working_context: int | None = None

    @property
    def within_budget(self) -> bool:
        return self.estimated_prompt_tokens <= self.max_prompt_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_window_tokens": self.context_window_tokens,
            "max_prompt_tokens": self.max_prompt_tokens,
            "system_schema_reserve_tokens": self.system_schema_reserve_tokens,
            "expected_output_reserve_tokens": self.expected_output_reserve_tokens,
            "reasoning_reserve_tokens": self.reasoning_reserve_tokens,
            "evidence_token_budget": self.evidence_token_budget,
            "estimated_prompt_tokens": self.estimated_prompt_tokens,
            "estimated_prompt_chars": self.estimated_prompt_chars,
            "phase": self.phase,
            "context_window_source": self.context_window_source,
            "context_verified": self.context_verified,
            "usable_context_budget": self.usable_context_budget,
            "preferred_working_context": self.preferred_working_context,
        }


class AgentContextBudgetManager:
    """Calculate a bounded input budget before any Agent request is built."""

    OUTPUT_RESERVE_TOKENS = {
        "classification": 4_096,
        "material_classification": 6_000,
        "semantic_relation": 6_000,
        "evidence_fusion": 6_000,
        "dimension_extraction": 12_000,
        "public_research": 8_000,
        "world_builder": 8_000,
        "actor_decision": 4_000,
        "room": 4_000,
        # Narrative Shooting Agent / prompt compilation (structured JSON).
        "shooting_agent": 8_000,
        "prompt_compilation": 8_000,
        # Video production guide compilation (batched asset/clip refinement).
        "guide_compilation": 8_000,
    }
    REASONING_RESERVE_TOKENS = {
        "classification": 2_000,
        "material_classification": 3_000,
        "semantic_relation": 4_000,
        "evidence_fusion": 4_000,
        "dimension_extraction": 8_000,
        "public_research": 6_000,
        "world_builder": 4_000,
        "actor_decision": 4_000,
        "room": 4_000,
        "shooting_agent": 4_000,
        "prompt_compilation": 4_000,
        "guide_compilation": 4_000,
    }

    def __init__(
        self,
        *,
        default_context_window_tokens: int | None = None,
        planning_context_window_tokens: int | None = None,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        # ``default_context_window_tokens`` is a PLANNING fallback.  It is only
        # consulted when the execution chain reported no context capability at
        # all, and the resulting budget is then flagged unverified.
        planning = (
            planning_context_window_tokens
            if planning_context_window_tokens is not None
            else default_context_window_tokens
        )
        self.planning_context_window_tokens = max(
            1,
            safe_int(planning, default=PLANNING_CONTEXT_WINDOW_TOKENS, minimum=1)
            or PLANNING_CONTEXT_WINDOW_TOKENS,
        )
        self.default_context_window_tokens = self.planning_context_window_tokens
        self.token_estimator = token_estimator or TokenEstimator()

    def estimate_tokens(self, value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
            except (TypeError, ValueError):
                text = str(value)
        return self.token_estimator.estimate(text)

    def context_capability(
        self, model: ModelCapability | EffectiveModelCapabilities | dict[str, Any] | None
    ) -> tuple[int | None, str, bool]:
        """Return ``(effective_context_window, source, verified)``.

        The manager never parses ``models[]``, guesses the selected model, or
        queries a provider: it consumes an already-resolved capability.  A
        ``None`` window means Unknown and is the caller's signal to fall back
        to ``planning_context_window_tokens``.
        """

        if isinstance(model, EffectiveModelCapabilities):
            source = str(model.context_capability_source or "unknown")
            verified = bool(model.context_verified)
            window = safe_int(model.effective_context_window, default=None, minimum=1)
            if window is None:
                window = safe_int(model.native_context_window, default=None, minimum=1)
                if window is not None:
                    source = source if source != "unknown" else "provider_metadata"
            return window, source, verified and window is not None
        if isinstance(model, ModelCapability):
            window = safe_int(model.context_window, default=None, minimum=1)
            if window is None:
                window = default_model_capability_registry().native_context_window(model.id)
                if window is not None:
                    return window, "model_registry", True
                return None, "unknown", False
            source = "adapter_dynamic_probe"
            if str(model.source or "").casefold() not in _PROBE_SOURCE_VALUES:
                source = "provider_metadata"
            return window, source, True
        if isinstance(model, dict):
            return self._dict_capability(model)
        return None, "unknown", False

    def _dict_capability(self, model: dict[str, Any]) -> tuple[int | None, str, bool]:
        """Minimal adapter for dict-shaped snapshots (tests/legacy callers)."""

        raw: Any = None
        source: Any = None
        selected = model.get("selected_model") or model.get("model_capability")
        if not isinstance(selected, dict):
            selected_id = model.get("effective_model") or model.get("model_id")
            selected = next(
                (
                    item
                    for item in (model.get("models") or [])
                    if isinstance(item, dict)
                    and str(item.get("id") or item.get("model_id")) == str(selected_id)
                ),
                None,
            )
        if isinstance(selected, dict) and selected.get("context_window") is not None:
            raw = selected.get("context_window")
            source = selected.get("context_window_source") or "provider_metadata"
        if raw is None and model.get("effective_context_window") is not None:
            raw = model.get("effective_context_window")
            source = (
                model.get("context_capability_source")
                or model.get("context_window_source")
                or "provider_metadata"
            )
        if raw is None:
            raw = model.get("context_window")
            source = (
                model.get("context_capability_source")
                or model.get("context_window_source")
                or ("adapter_declared" if raw is not None else None)
            )
        window = safe_int(raw, default=None, minimum=1)
        if window is not None:
            return window, str(source or "provider_metadata"), True
        model_id = model.get("effective_model") or model.get("model_id") or model.get("id")
        registry_window = default_model_capability_registry().native_context_window(model_id)
        if registry_window is not None:
            return registry_window, "model_registry", True
        return None, "unknown", False

    def context_window_resolution(
        self, model: ModelCapability | EffectiveModelCapabilities | dict[str, Any] | None
    ) -> tuple[int | None, str, bool]:
        """Backwards-compatible alias for :meth:`context_capability`."""

        return self.context_capability(model)

    def context_window(
        self, model: ModelCapability | EffectiveModelCapabilities | dict[str, Any] | None
    ) -> int | None:
        """Effective context window, or ``None`` when Unknown."""

        return self.context_capability(model)[0]

    def planning_window(
        self, model: ModelCapability | EffectiveModelCapabilities | dict[str, Any] | None
    ) -> tuple[int, str, bool]:
        """Always returns a usable number plus its provenance.

        An Unknown capability resolves to the planning fallback and is flagged
        ``verified=False`` / ``source="fallback_policy"`` so no caller can
        present it as a measured context window.
        """

        window, source, verified = self.context_capability(model)
        if window is not None:
            return int(window), source, bool(verified)
        return int(self.planning_context_window_tokens), "fallback_policy", False

    @staticmethod
    def _phase_key(phase: str) -> str:
        normalized = str(phase or "agent_turn").lower()
        aliases = {
            "relations": "semantic_relation",
            "relation": "semantic_relation",
            "fusion": "evidence_fusion",
            "research": "public_research",
            "dimension": "dimension_extraction",
        }
        for alias, key in aliases.items():
            if alias in normalized:
                return key
        return next(
            (
                candidate
                for candidate in sorted(
                    AgentContextBudgetManager.OUTPUT_RESERVE_TOKENS,
                    key=len,
                    reverse=True,
                )
                if candidate in normalized
            ),
            "agent_turn",
        )

    def budget_for(
        self,
        *,
        model: ModelCapability | EffectiveModelCapabilities | dict[str, Any] | None = None,
        phase: str = "agent_turn",
        expected_output: Any = None,
        prompt_chars: int = 0,
    ) -> ContextBudget:
        normalized = str(phase or "agent_turn").lower()
        key = self._phase_key(normalized)
        effective, source, verified = self.context_capability(model)
        window, planning_source, planning_verified = self.planning_window(model)
        output_reserve = self.OUTPUT_RESERVE_TOKENS.get(key, 4_096)
        reasoning_reserve = self.REASONING_RESERVE_TOKENS.get(key, 2_000)
        schema_reserve = self.estimate_tokens(expected_output)
        # Keep a separate fixed reserve for system/schema scaffolding.  Actual
        # prompt estimation is performed in ``plan`` below.
        system_schema = schema_reserve
        if effective is None:
            max_prompt = max(0, window - output_reserve - reasoning_reserve)
            usable: int | None = None
            preferred: int | None = None
        else:
            usable = compute_usable_budget(
                effective,
                output_reserve=output_reserve,
                reasoning_reserve=reasoning_reserve,
            )
            max_prompt = max(0, usable)
            preferred = compute_preferred_working_context(effective, usable - system_schema)
        evidence_budget = max(0, max_prompt - system_schema)
        return ContextBudget(
            context_window_tokens=window,
            max_prompt_tokens=max_prompt,
            system_schema_reserve_tokens=system_schema,
            expected_output_reserve_tokens=output_reserve,
            reasoning_reserve_tokens=reasoning_reserve,
            evidence_token_budget=evidence_budget,
            phase=normalized,
            context_window_source=source if effective is not None else planning_source,
            context_verified=verified if effective is not None else planning_verified,
            usable_context_budget=usable,
            preferred_working_context=preferred,
        )

    def plan(
        self,
        turn: AgentTurn | PromptEnvelope,
        *,
        model: ModelCapability | EffectiveModelCapabilities | dict[str, Any] | None = None,
        phase: str = "agent_turn",
        enforce: bool = True,
    ) -> ContextBudget:
        envelope = turn if isinstance(turn, PromptEnvelope) else AgentPromptRenderer.envelope(turn)
        base = self.budget_for(model=model, phase=phase, expected_output=envelope.expected_output)
        rendered = AgentPromptRenderer.render_for_single_prompt(envelope)
        estimate = self.estimate_tokens(rendered)
        system_and_context_tokens = self.estimate_tokens(
            envelope.system_prompt
        ) + self.estimate_tokens(envelope.messages)
        system_schema_reserve = base.system_schema_reserve_tokens + system_and_context_tokens
        evidence_budget = max(0, base.max_prompt_tokens - system_schema_reserve)
        budget = ContextBudget(
            context_window_tokens=base.context_window_tokens,
            max_prompt_tokens=base.max_prompt_tokens,
            system_schema_reserve_tokens=system_schema_reserve,
            expected_output_reserve_tokens=base.expected_output_reserve_tokens,
            reasoning_reserve_tokens=base.reasoning_reserve_tokens,
            evidence_token_budget=evidence_budget,
            estimated_prompt_tokens=estimate,
            estimated_prompt_chars=len(rendered),
            phase=str(phase or "agent_turn"),
            context_window_source=base.context_window_source,
            context_verified=base.context_verified,
            usable_context_budget=base.usable_context_budget,
            preferred_working_context=base.preferred_working_context,
        )
        if enforce and not budget.within_budget:
            raise ContextBudgetExceededError(
                "Agent prompt exceeds the configured context budget",
                phase=phase,
                diagnostics={
                    "context_window_tokens": budget.context_window_tokens,
                    "max_prompt_tokens": budget.max_prompt_tokens,
                    "estimated_prompt_tokens": budget.estimated_prompt_tokens,
                    "estimated_prompt_chars": budget.estimated_prompt_chars,
                },
            )
        return budget

    def iter_batches(
        self,
        items: Sequence[Any] | Iterable[Any],
        *,
        item_text: Callable[[Any], str],
        max_items: int,
        phase: str,
        model: ModelCapability | EffectiveModelCapabilities | dict[str, Any] | None = None,
        base_text: str = "",
        system_prompt: str = "",
        expected_output: Any = None,
        preferred_working_context: int | None = None,
    ) -> Iterable[list[Any]]:
        """Yield all items in budget-fitting batches; never truncate an item.

        ``preferred_working_context`` is the best single-pass workload.  When
        supplied it caps the per-batch budget below the hard capacity so a 1M
        model is not forced to swallow ~950K tokens in one turn.  An Unknown
        context window must never collapse the batch size back to a 32K-shaped
        plan: the cap is only ever applied when it is larger than the floor.
        """

        limit = max(1, safe_int(max_items, default=1, minimum=1) or 1)
        base_tokens = self.estimate_tokens(base_text) + self.estimate_tokens(system_prompt)
        budget = self.budget_for(model=model, phase=phase, expected_output=expected_output)
        raw_available = budget.evidence_token_budget - base_tokens
        preferred_cap = safe_int(preferred_working_context, default=None, minimum=1)
        if preferred_cap is None and budget.preferred_working_context is not None:
            preferred_cap = int(budget.preferred_working_context) - base_tokens
        else:
            preferred_cap = (
                int(preferred_cap) - base_tokens if preferred_cap is not None else None
            )
        if preferred_cap is not None and preferred_cap > 0:
            raw_available = min(raw_available, preferred_cap)
        if raw_available <= 0:
            raise ContextBudgetExceededError(
                "The fixed Agent prompt context exceeds the evidence budget",
                phase=phase,
                diagnostics={
                    "context_window_tokens": budget.context_window_tokens,
                    "evidence_token_budget": budget.evidence_token_budget,
                    "base_estimated_tokens": base_tokens,
                    "expected_output_estimated_tokens": self.estimate_tokens(expected_output),
                },
            )
        # Reserve a bounded safety headroom for envelope section headers,
        # newlines and schema serialization.
        scaffolding_headroom = min(256, max(0, raw_available // 10))
        available = max(1, raw_available - scaffolding_headroom)
        current: list[Any] = []
        current_tokens = 0
        for item in items:
            item_tokens = self.estimate_tokens(item_text(item))
            if item_tokens > available:
                if current:
                    yield current
                    current = []
                    current_tokens = 0
                raise ContextBudgetExceededError(
                    "A single evidence item exceeds the context budget",
                    phase=phase,
                    diagnostics={
                        "context_window_tokens": budget.context_window_tokens,
                        "evidence_token_budget": available,
                        "item_estimated_tokens": item_tokens,
                    },
                )
            if current and (len(current) >= limit or current_tokens + item_tokens > available):
                yield current
                current = []
                current_tokens = 0
            current.append(item)
            current_tokens += item_tokens
        if current:
            yield current

    def hierarchical_reduce(
        self,
        items: Sequence[Any] | Iterable[Any],
        *,
        item_text: Callable[[Any], str],
        reducer: Callable[[list[Any]], Any],
        max_items: int,
        phase: str,
        model: ModelCapability | EffectiveModelCapabilities | dict[str, Any] | None = None,
        base_text: str = "",
        system_prompt: str = "",
        expected_output: Any = None,
        max_rounds: int = 8,
    ) -> list[Any]:
        """Reduce complete batches without silently dropping an item.

        ``reducer`` owns provenance for its summary.  A round is rejected when
        it produces no reduction, which prevents a caller from accidentally
        looping forever or treating a truncated tail as a successful summary.
        """

        current = list(items)
        rounds = 0
        while len(current) > max(1, int(max_items)):
            rounds += 1
            if rounds > max(1, int(max_rounds)):
                raise ContextBudgetExceededError(
                    "Hierarchical context reduction exceeded its round limit",
                    phase=phase,
                    diagnostics={"item_count": len(current), "max_rounds": max_rounds},
                )
            reduced: list[Any] = []
            batches = self.iter_batches(
                current,
                item_text=item_text,
                max_items=max_items,
                phase=phase,
                model=model,
                base_text=base_text,
                system_prompt=system_prompt,
                expected_output=expected_output,
            )
            for batch in batches:
                value = reducer(batch)
                if value is None:
                    raise ContextBudgetExceededError(
                        "Hierarchical reduction returned no provenance-preserving value",
                        phase=phase,
                        diagnostics={"batch_size": len(batch)},
                    )
                if isinstance(value, list):
                    reduced.extend(value)
                else:
                    reduced.append(value)
            if len(reduced) >= len(current):
                raise ContextBudgetExceededError(
                    "Hierarchical reduction did not reduce the context",
                    phase=phase,
                    diagnostics={"item_count": len(current), "reduced_count": len(reduced)},
                )
            current = reduced
        return current


__all__ = [
    "AgentContextBudgetManager",
    "ContextBudget",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "PLANNING_CONTEXT_WINDOW_TOKENS",
    "TokenEstimator",
]
