"""Phase-scoped working-context policy and dynamic count caps.

The model/runtime context window is a capability.  How much of it one Persona
Continuum phase should fill is a *policy*, never a second model limit.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from persona_continuum.numeric import safe_int

PREFERRED_WORKING_FLOOR_TOKENS = 8_192
UNVERIFIED_WORKING_RATIO = 0.50
PERSISTENT_SESSION_MAX_RATIO = 0.60

DEFAULT_PHASE_WORKING_RATIOS: dict[str, float] = {
    "material_classification": 0.80,
    "classification": 0.80,
    "persona_compilation": 0.65,
    "dimension_extraction": 0.65,
    "evidence_fusion": 0.65,
    "research": 0.65,
    "public_research": 0.65,
    "audit": 0.70,
    "guide_compilation": 0.65,
    "shooting_agent": 0.65,
    "prompt_compilation": 0.65,
    "persistent_agent_session": 0.60,
    "room": 0.55,
    "unknown": UNVERIFIED_WORKING_RATIO,
}

_PHASE_ALIASES: dict[str, str] = {
    "relations": "semantic_relation",
    "relation": "semantic_relation",
    "fusion": "evidence_fusion",
    "research": "public_research",
    "dimension": "dimension_extraction",
    "compile": "persona_compilation",
    "compilation": "persona_compilation",
    "classify": "material_classification",
    "material": "material_classification",
}

BASELINE_CONTEXT_TOKENS = 65_536
BASELINE_UNITS_CAP = 1_200
BASELINE_EPISODES_CAP = 24
MIN_UNITS_CAP = 200
MIN_EPISODES_CAP = 8
ABSOLUTE_MAX_UNITS_CAP = 50_000
ABSOLUTE_MAX_EPISODES_CAP = 2_000


class PhaseContextPolicy(BaseModel):
    """Configurable per-phase working ratios.  Never displayed as model context."""

    model_config = ConfigDict(extra="ignore")

    ratios: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_PHASE_WORKING_RATIOS))
    unverified_ratio: float = UNVERIFIED_WORKING_RATIO
    persistent_session_max_ratio: float = PERSISTENT_SESSION_MAX_RATIO
    floor_tokens: int = PREFERRED_WORKING_FLOOR_TOKENS

    def ratio_for(
        self,
        phase: str | None,
        *,
        verified: bool = False,
        persistent_session: bool = False,
        fresh_stateless: bool = True,
    ) -> float:
        if not verified:
            return _clamp_ratio(self.unverified_ratio)
        key = _phase_key(phase)
        raw = self.ratios.get(key)
        if raw is None:
            raw = self.ratios.get("unknown", self.unverified_ratio)
        ratio = _clamp_ratio(raw)
        if persistent_session and not fresh_stateless:
            ratio = min(ratio, _clamp_ratio(self.persistent_session_max_ratio))
        if key == "material_classification" and verified and fresh_stateless:
            ratio = max(ratio, 0.75)
            ratio = min(ratio, 0.85)
        return ratio

    def working_target(
        self,
        effective_or_remaining: int | None,
        *,
        phase: str | None,
        usable_budget: int | None = None,
        verified: bool = False,
        persistent_session: bool = False,
        fresh_stateless: bool = True,
    ) -> int | None:
        if effective_or_remaining is None:
            return None
        window = max(1, safe_int(effective_or_remaining, default=1, minimum=1) or 1)
        ratio = self.ratio_for(
            phase,
            verified=verified,
            persistent_session=persistent_session,
            fresh_stateless=fresh_stateless,
        )
        preferred = max(int(self.floor_tokens), int(math.ceil(window * ratio)))
        if usable_budget is not None:
            preferred = min(preferred, max(0, int(usable_budget)))
        return max(0, preferred)


def _clamp_ratio(value: Any) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return UNVERIFIED_WORKING_RATIO
    if not math.isfinite(ratio):
        return UNVERIFIED_WORKING_RATIO
    return min(0.95, max(0.10, ratio))


def _phase_key(phase: str | None) -> str:
    normalized = str(phase or "unknown").strip().casefold()
    if not normalized:
        return "unknown"
    for alias, key in _PHASE_ALIASES.items():
        if alias in normalized:
            return key
    for candidate in sorted(DEFAULT_PHASE_WORKING_RATIOS, key=len, reverse=True):
        if candidate in normalized:
            return candidate
    return normalized


def default_phase_context_policy(overrides: dict[str, Any] | None = None) -> PhaseContextPolicy:
    raw = dict(overrides or {})
    ratios = dict(DEFAULT_PHASE_WORKING_RATIOS)
    extra_ratios: dict[str, Any]
    nested = raw.get("ratios")
    extra_ratios = nested if isinstance(nested, dict) else raw
    alias_targets = {
        "research": "public_research",
        "compilation": "persona_compilation",
        "classify": "material_classification",
        "material": "material_classification",
    }
    for key, value in extra_ratios.items():
        if key in {"unverified_ratio", "persistent_session_max_ratio", "floor_tokens", "ratios"}:
            continue
        try:
            ratio = _clamp_ratio(value)
        except (TypeError, ValueError):
            continue
        ratios[str(key)] = ratio
        alias = alias_targets.get(str(key))
        if alias:
            ratios[alias] = ratio
    return PhaseContextPolicy(
        ratios=ratios,
        unverified_ratio=_clamp_ratio(raw.get("unverified_ratio", UNVERIFIED_WORKING_RATIO)),
        persistent_session_max_ratio=_clamp_ratio(
            raw.get("persistent_session_max_ratio", PERSISTENT_SESSION_MAX_RATIO)
        ),
        floor_tokens=max(
            1,
            safe_int(raw.get("floor_tokens"), default=PREFERRED_WORKING_FLOOR_TOKENS, minimum=1)
            or PREFERRED_WORKING_FLOOR_TOKENS,
        ),
    )


def scale_count_cap(
    working_tokens: int | None,
    *,
    baseline_tokens: int = BASELINE_CONTEXT_TOKENS,
    baseline_cap: int,
    min_cap: int,
    max_cap: int,
    configured: int | None | str = "auto",
    token_budget_verified: bool = False,
) -> int | None:
    """Soft safety guard.  Token/transport budgets remain the primary limit.

    ``None`` / ``"auto"`` scale with the working budget.  An explicit integer
    is honored.  When the token budget is verified, the cap is allowed to
    rise far enough that the token budget is what actually splits windows.
    """

    if isinstance(configured, str) and configured.strip().casefold() not in {"", "auto", "none"}:
        explicit = safe_int(configured, default=None, minimum=1)
        if explicit is not None:
            return int(explicit)
    if isinstance(configured, int):
        return max(1, int(configured))
    if configured is None or (
        isinstance(configured, str) and configured.strip().casefold() in {"auto", "none", ""}
    ):
        if token_budget_verified:
            # Verified token accounting: do not let a count cap split windows
            # before the token/transport budget does.
            return None
        if working_tokens is None:
            return int(baseline_cap)
        scale = max(1.0, float(working_tokens) / float(max(1, baseline_tokens)))
        scaled = int(math.floor(baseline_cap * scale))
        return min(int(max_cap), max(int(min_cap), scaled))
    return int(baseline_cap)


def auto_units_cap(
    working_tokens: int | None,
    *,
    configured: int | None | str = "auto",
    token_budget_verified: bool = False,
) -> int | None:
    return scale_count_cap(
        working_tokens,
        baseline_cap=BASELINE_UNITS_CAP,
        min_cap=MIN_UNITS_CAP,
        max_cap=ABSOLUTE_MAX_UNITS_CAP,
        configured=configured,
        token_budget_verified=token_budget_verified,
    )


def auto_episodes_cap(
    working_tokens: int | None,
    *,
    configured: int | None | str = "auto",
    token_budget_verified: bool = False,
) -> int | None:
    return scale_count_cap(
        working_tokens,
        baseline_cap=BASELINE_EPISODES_CAP,
        min_cap=MIN_EPISODES_CAP,
        max_cap=ABSOLUTE_MAX_EPISODES_CAP,
        configured=configured,
        token_budget_verified=token_budget_verified,
    )
