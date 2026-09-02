"""Phase-aware idle and hard timeout policy for Agent turns."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from persona_continuum.agent.models import AgentSessionConfig, AgentTurn, OutputStreamingMode
from persona_continuum.numeric import safe_float, safe_timeout


@dataclass(frozen=True, slots=True)
class TimeoutBudget:
    first_response_timeout_seconds: float
    idle_timeout_seconds: float
    hard_timeout_seconds: float
    phase: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "first_response_timeout_seconds": self.first_response_timeout_seconds,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "hard_timeout_seconds": self.hard_timeout_seconds,
            "phase": self.phase,
        }


class AgentTimeoutPolicy:
    """Choose bounded timeouts from phase, prompt size, model and tool use."""

    PHASE_IDLE_SECONDS = {
        "classification": 90.0,
        "material_classification": 90.0,
        "semantic_relation": 120.0,
        "evidence_fusion": 120.0,
        "dimension_extraction": 150.0,
        "public_research": 180.0,
        "world_builder": 120.0,
        "actor_decision": 120.0,
        "room": 120.0,
        # Narrative Shooting Agent / prompt compilation.
        "shooting_agent": 120.0,
        "prompt_compilation": 120.0,
        "guide_compilation": 120.0,
    }
    PHASE_HARD_SECONDS = {
        "classification": 300.0,
        "material_classification": 300.0,
        "semantic_relation": 300.0,
        "evidence_fusion": 300.0,
        "dimension_extraction": 480.0,
        "public_research": 900.0,
        "world_builder": 480.0,
        "actor_decision": 600.0,
        "room": 900.0,
        "shooting_agent": 900.0,
        "prompt_compilation": 900.0,
        "guide_compilation": 900.0,
    }
    DEFAULT_IDLE_SECONDS = 120.0
    DEFAULT_HARD_SECONDS = 600.0
    DEFAULT_FIRST_RESPONSE_SECONDS = 180.0
    BUFFERED_FIRST_RESPONSE_SECONDS = 300.0
    BUFFERED_HARD_SECONDS = 900.0
    PHASE_FIRST_RESPONSE_SECONDS = {
        "material_classification": 420.0,
        "semantic_relation": 360.0,
        "evidence_fusion": 360.0,
        "dimension_extraction": 300.0,
    }

    def resolve(
        self,
        *,
        phase: str | None = None,
        model_id: str | None = None,
        reasoning_effort: str | None = None,
        prompt_chars: int = 0,
        tool_usage: bool = False,
        extra: dict[str, Any] | None = None,
        output_streaming_mode: OutputStreamingMode | str = OutputStreamingMode.UNKNOWN,
    ) -> TimeoutBudget:
        del model_id  # The hook is intentionally part of the contract for future model tables.
        normalized_phase = str(phase or "agent_turn").strip().lower()
        aliases = {
            "relations": "semantic_relation",
            "relation": "semantic_relation",
            "fusion": "evidence_fusion",
            "research": "public_research",
            "dimension": "dimension_extraction",
        }
        phase_key = next(
            (key for alias, key in aliases.items() if alias in normalized_phase),
            next(
                (
                    key
                    for key in sorted(self.PHASE_HARD_SECONDS, key=len, reverse=True)
                    if key in normalized_phase
                ),
                "agent_turn",
            ),
        )
        extras = dict(extra or {})
        # The executor owns the turn budget. Streaming transports need a
        # generous inactivity window even during a named sub-phase, while a
        # buffered CLI has no observable output until the process exits.
        idle_default = max(
            180.0, self.PHASE_IDLE_SECONDS.get(phase_key, self.DEFAULT_IDLE_SECONDS)
        )
        hard_default = max(
            900.0, self.PHASE_HARD_SECONDS.get(phase_key, self.DEFAULT_HARD_SECONDS)
        )

        # ``turn_timeout_seconds`` is a legacy explicit hard-timeout override.
        # It remains useful for controlled callers/tests, but it never makes
        # the adapter own the idle timer.
        hard_is_explicit = (
            extras.get("hard_timeout_seconds") is not None
            or extras.get("turn_timeout_seconds") is not None
        )
        if (
            extras.get("hard_timeout_seconds") is None
            and extras.get("turn_timeout_seconds") is not None
        ):
            hard_default = safe_timeout(
                extras.get("turn_timeout_seconds"), default=hard_default
            )
        idle = safe_timeout(extras.get("idle_timeout_seconds"), default=idle_default)
        hard = safe_timeout(extras.get("hard_timeout_seconds"), default=hard_default)

        prompt_size = max(0, int(safe_float(prompt_chars, default=0.0) or 0.0))
        if prompt_size > 20_000 and not hard_is_explicit:
            hard = min(3600.0, hard + 60.0 * math.ceil((prompt_size - 20_000) / 20_000))
        if (
            str(reasoning_effort or "").lower() in {"high", "xhigh", "max", "deep"}
            and not hard_is_explicit
        ):
            hard = min(3600.0, hard + 120.0)
        if tool_usage and not hard_is_explicit:
            hard = min(3600.0, hard + 120.0)
            idle = min(3600.0, idle + 30.0)
        if hard_is_explicit:
            idle = min(idle, hard)
        else:
            hard = max(idle, hard)
        try:
            stream_mode = (
                output_streaming_mode
                if isinstance(output_streaming_mode, OutputStreamingMode)
                else OutputStreamingMode(str(output_streaming_mode))
            )
        except ValueError:
            stream_mode = OutputStreamingMode.UNKNOWN
        if stream_mode == OutputStreamingMode.BUFFERED_FINAL:
            if not hard_is_explicit:
                hard = max(hard, self.BUFFERED_HARD_SECONDS)
            # Buffered processes cannot expose a first response before exit.
            # The executor's hard deadline remains the single bounded owner.
            first_response = hard
        else:
            reasoning_bonus = (
                120.0
                if str(reasoning_effort or "").lower() in {"high", "xhigh", "max", "deep"}
                else 0.0
            )
            first_response = max(
                idle,
                min(600.0, self.DEFAULT_FIRST_RESPONSE_SECONDS + reasoning_bonus),
            )
        first_response = min(hard, first_response)
        return TimeoutBudget(
            first_response_timeout_seconds=float(first_response),
            idle_timeout_seconds=float(idle),
            hard_timeout_seconds=float(hard),
            phase=normalized_phase,
        )


def timeout_budget_for_turn(
    config: AgentSessionConfig,
    turn: AgentTurn,
    *,
    output_streaming_mode: OutputStreamingMode | str = OutputStreamingMode.UNKNOWN,
) -> TimeoutBudget:
    """Resolve the same timeout budget for direct adapter callers."""

    phase = str(turn.metadata.get("phase") or turn.metadata.get("stage") or "agent_turn")
    return AgentTimeoutPolicy().resolve(
        phase=phase,
        model_id=config.model_id,
        reasoning_effort=config.reasoning_effort,
        prompt_chars=len(turn.user_message or "") + len(turn.system_prompt or ""),
        tool_usage=bool(turn.tools),
        extra=config.extra,
        output_streaming_mode=output_streaming_mode,
    )


__all__ = ["AgentTimeoutPolicy", "TimeoutBudget", "timeout_budget_for_turn"]
