from __future__ import annotations

import re
from typing import Any

from persona_continuum.application.memory_service import MemoryService
from persona_continuum.domain.memory import MemoryRecord

_TEMPORAL_PATTERNS = [
    re.compile(
        r"(以前|之前|当时|上次|曾经|记得|你说过|我们讨论过|"
        r"发生过什么|为什么后来|谁曾经|过去|历史|从前|早前)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(earlier|previously|before|back then|remember when|"
        r"what did you say|in the past|history|formerly|used to)",
        re.IGNORECASE,
    ),
    re.compile(r"(关于|about|regarding)\s*([^\s,，?？。！!]+)", re.IGNORECASE),
]


class RecallAnalysisResult:
    def __init__(
        self,
        triggered: bool,
        reasons: list[str],
        query: str,
        memories: list[MemoryRecord],
    ) -> None:
        self.triggered = triggered
        self.reasons = reasons
        self.query = query
        self.memories = memories


class RecallGate:
    def __init__(self, memory_service: MemoryService) -> None:
        self.memory_service = memory_service

    def plan_turn(
        self,
        persona_id: str,
        current_speaker_name: str,
        user_message: str,
        recent_transcript: list[dict[str, Any]] | None = None,
        branch_id: str = "main",
    ) -> RecallAnalysisResult:
        """Analyze a turn WITHOUT touching memory storage.

        Returns a result whose ``memories`` is empty; the caller performs ONE
        retrieval with ``result.query`` (when triggered) and feeds the same
        records to ``attach_memories`` and to ``prepare_turn`` so the Recall
        Gate, context builder, and prompt composer share one search.
        """

        triggered, reasons, query, _text = self._analyze(
            persona_id, current_speaker_name, user_message, recent_transcript
        )
        return RecallAnalysisResult(
            triggered=triggered,
            reasons=reasons,
            query=query,
            memories=[],
        )

    @staticmethod
    def attach_memories(
        plan: RecallAnalysisResult,
        memories: list[MemoryRecord],
        *,
        triggered: bool | None = None,
    ) -> RecallAnalysisResult:
        """Attach a single shared retrieval result to a gate plan."""

        effective_trigger = plan.triggered if triggered is None else triggered
        return RecallAnalysisResult(
            triggered=effective_trigger,
            reasons=list(plan.reasons),
            query=plan.query,
            memories=list(memories) if effective_trigger else [],
        )

    def _analyze(
        self,
        persona_id: str,
        current_speaker_name: str,
        user_message: str,
        recent_transcript: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, list[str], str, str]:
        text = user_message.strip()
        if not text and recent_transcript:
            last_entry = recent_transcript[-1] if recent_transcript else {}
            text = str(last_entry.get("content", ""))

        reasons: list[str] = []
        # 1. Pattern matching
        for pattern in _TEMPORAL_PATTERNS:
            match = pattern.search(text)
            if match:
                reasons.append(f"pattern_matched:{match.group(0)}")

        # 2. Question check
        is_question = any(
            q in text for q in ["?", "？", "什么", "吗", "what", "why", "how", "when", "who"]
        )
        if is_question:
            reasons.append("interrogative_pattern")

        # 3. Mention check (e.g. mentions persona or speaker)
        if current_speaker_name.lower() in text.lower() or persona_id.lower() in text.lower():
            reasons.append(f"speaker_mentioned:{current_speaker_name}")

        triggered = len(reasons) > 0
        query = self._extract_search_query(text, persona_id, current_speaker_name)
        return triggered, reasons, query, text

    def analyze_turn(
        self,
        persona_id: str,
        current_speaker_name: str,
        user_message: str,
        recent_transcript: list[dict[str, Any]] | None = None,
        branch_id: str = "main",
        limit: int = 6,
    ) -> RecallAnalysisResult:
        """Analyze message context for temporal and factual inquiry patterns."""
        triggered, reasons, query, _text = self._analyze(
            persona_id, current_speaker_name, user_message, recent_transcript
        )

        memories: list[MemoryRecord] = []
        if triggered:
            # Query memory service using FTS and keyword extraction
            memories = self.memory_service.search_memories(
                persona_id=persona_id,
                query=query,
                limit=limit,
                branch_id=branch_id,
                include_main_history=True,
                include_shared_pre_divergence=True,
            )

        return RecallAnalysisResult(
            triggered=triggered,
            reasons=reasons,
            query=query,
            memories=memories,
        )

    def _extract_search_query(self, text: str, persona_id: str, current_speaker_name: str) -> str:
        cleaned = text
        for token in [
            "以前",
            "之前",
            "当时",
            "上次",
            "曾经",
            "记得",
            "你说过",
            "关于",
            "说过什么",
            "earlier",
            "previously",
            "remember when",
            "what did you say about",
            "in the past",
        ]:
            cleaned = cleaned.replace(token, " ")

        cleaned = cleaned.strip("?？!！.,，。 ")
        return cleaned or text or persona_id
