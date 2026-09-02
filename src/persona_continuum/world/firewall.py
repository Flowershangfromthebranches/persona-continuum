from __future__ import annotations

import re
from typing import Any

from persona_continuum.world.models import WorldSeed


class TemporalKnowledgeFirewall:
    """Enforces temporal knowledge isolation for Parallel World simulations.

    Actors and personas can only know:
      1. Real-world historical facts up to `current_world_time`
      2. Simulated events that have already occurred in the current parallel branch
    Actors CANNOT know future real-world events, products, or technologies.
    """

    # Generic anachronism classes for real-world-baseline simulations. Tokens
    # describe technology *categories*, never specific companies or products,
    # so the firewall stays domain-agnostic.
    ANACHRONISTIC_ITEMS: dict[int, list[str]] = {
        2022: [
            "conversational ai assistant",
            "text-to-image model",
            "large language model",
        ],
        2023: ["multimodal ai assistant", "mixed-reality headset", "open weight llm"],
        2024: ["text-to-video model", "reasoning llm", "open-source frontier model"],
        2025: ["agentic ai framework", "frontier reasoning model"],
    }

    def filter_memories(
        self, memories: list[dict[str, Any]], current_world_time: str
    ) -> list[dict[str, Any]]:
        """Filters memories so only memories occurring on or before
        current_world_time are retained.
        """
        current_dt_str = self._normalize_date(current_world_time)
        filtered: list[dict[str, Any]] = []

        for mem in memories:
            occurred_at = mem.get("occurred_at") or mem.get("event_time") or mem.get("written_at")
            if not occurred_at:
                filtered.append(mem)
                continue
            norm_occurred = self._normalize_date(str(occurred_at))
            if norm_occurred <= current_dt_str:
                filtered.append(mem)

        return filtered

    def build_firewall_prompt_instruction(
        self, current_world_time: str, seed: WorldSeed | None = None
    ) -> str:
        """Constructs temporal constraint instruction to inject into persona/actor prompts."""
        year = self._extract_year(current_world_time)
        anachronisms: list[str] = []
        for y, items in self.ANACHRONISTIC_ITEMS.items():
            if y > year:
                anachronisms.extend(items)

        examples_str = f" (e.g., {', '.join(anachronisms[:6])})" if anachronisms else ""

        return (
            f"### TEMPORAL KNOWLEDGE FIREWALL\n"
            f"- Current World Date: {current_world_time}\n"
            f"- You exist strictly within this time period.\n"
            f"- You have ZERO knowledge of real-world events, products, technologies, companies, "
            f"or developments that occurred after {current_world_time}{examples_str}.\n"
            f"- You must base all thoughts, decisions, and speech ONLY on baseline facts prior to "
            f"{current_world_time} and the events that have occurred within this parallel branch."
        )

    def check_knowledge_violation(self, text: str, current_world_time: str) -> list[str]:
        """Detects if an output text accidentally leaked future real-world knowledge."""
        year = self._extract_year(current_world_time)
        violations: list[str] = []
        text_lower = text.lower()

        for y, items in self.ANACHRONISTIC_ITEMS.items():
            if y > year:
                for item in items:
                    pattern = rf"\b{re.escape(item)}\b"
                    if re.search(pattern, text_lower):
                        violations.append(
                            f"Future knowledge leak: '{item}' (first appeared ~{y}) "
                            f"referenced at world time {current_world_time}"
                        )
        return violations

    def _normalize_date(self, dt_str: str) -> str:
        s = dt_str.strip()
        if len(s) == 4 and s.isdigit():
            return f"{s}-01-01"
        return s

    def _extract_year(self, dt_str: str) -> int:
        match = re.search(r"\b(19\d{2}|20\d{2})\b", dt_str)
        if match:
            return int(match.group(1))
        return 2011
