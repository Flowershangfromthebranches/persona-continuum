"""Deterministic, LLM-free chat style profiling (Large Conversation Pipeline V2).

ChatStyleProfiler reads the full target-persona chat corpus and produces one
global Expression DNA statistics record.  It never calls a model, never maps
an emoji name to an emotion, and never creates one EvidenceUnit per
statistic.  The persisted profile is readable by the Evidence Index and the
expression_dna compiler dimension as derived, clearly-labelled statistical
evidence.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.application.material_chat import (
    ROLE_EXPORTER,
    fold_conversation_turns,
    parse_turn_time,
)

PROFILE_CONTRACT = "expression-profile-v2"

_LT = chr(60)
_GT = chr(62)
# Platform sticker markers: <emoji name="流泪" count="4"/>
PLATFORM_EMOJI = re.compile(_LT + r'emoji\s+name="([^"]+)"(?:\s+count="(\d+)")?\s*/' + _GT)
VOICE_TAG = re.compile(
    _LT + "voice[^" + _GT + r"]*>(.*?)" + _LT + "/voice" + _GT + "|" + _LT + r"voice\b[^>]*" + _GT,
    re.DOTALL | re.IGNORECASE,
)
UNICODE_EMOJI = re.compile(
    "["
    + "".join(
        chr(range_start) + "-" + chr(range_end)
        for range_start, range_end in (
            (0x1F000, 0x1FAFF),
            (0x2600, 0x27BF),
            (0x1F1E6, 0x1F1FF),
            (0x2B00, 0x2BFF),
            (0xFE00, 0xFE0F),
        )
    )
    + "]"
)
_PHRASE_CHARS = re.compile("[" + chr(0x4E00) + "-" + chr(0x9FFF) + "A-Za-z0-9]+")
_CJK = re.compile("[" + chr(0x4E00) + "-" + chr(0x9FFF) + "]")

NIGHT_HOURS = frozenset({22, 23, 0, 1, 2, 3, 4, 5})
_TOP_N = 25


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    """Compact bounded summary; never a per-item map that could explode."""

    items = sorted(float(value) for value in values)
    if not items:
        return {"count": 0}

    def quantile(fraction: float) -> float:
        index = min(len(items) - 1, max(0, int(round(fraction * (len(items) - 1)))))
        return items[index]

    mean = sum(items) / len(items)
    variance = sum((value - mean) ** 2 for value in items) / len(items)
    return {
        "count": len(items),
        "min": round(items[0], 2),
        "max": round(items[-1], 2),
        "mean": round(mean, 2),
        "stdev": round(math.sqrt(variance), 2),
        "p50": round(quantile(0.50), 2),
        "p90": round(quantile(0.90), 2),
        "p99": round(quantile(0.99), 2),
    }


def _top(counter: Counter[str] | Counter[int], limit: int = _TOP_N) -> list[dict[str, Any]]:
    total = sum(counter.values()) or 1
    return [
        {"value": str(value), "count": int(count), "ratio": round(count / total, 4)}
        for value, count in counter.most_common(limit)
    ]


def _ngrams(text: str, size: int) -> Iterator[str]:
    for word in _PHRASE_CHARS.findall(text or ""):
        if _CJK.search(word):
            for index in range(max(0, len(word) - size + 1)):
                yield word[index : index + size]
        elif size == 1:
            yield word.lower()


class ChatStyleProfile(BaseModel):
    """One persisted global statistics record (kind = expression_profile)."""

    persona_id: str
    kind: str = "expression_profile"
    contract: str = PROFILE_CONTRACT
    method: str = "statistical_deterministic"
    corpus_size: int = 0
    turn_corpus_size: int = 0
    time_range: list[str | None] = Field(default_factory=list)
    statistics: dict[str, Any] = Field(default_factory=dict)
    representative_evidence_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ChatStyleProfiler:
    """Aggregate Expression DNA statistics over target-persona chat messages.

    A unit counts toward the profile when its declared speaker role is the
    target persona; a chat source whose roles were never declared (unknown)
    still contributes, matching the conservative P0-A behavior.  Exporter /
    context-only messages are excluded from target statistics but are never
    removed from the corpus.
    """

    def profile(self, persona_id: str, units: Iterable[Any]) -> ChatStyleProfile | None:
        target = [item for item in units if self._counts_for_target(item)]
        if not target:
            return None
        turns = fold_conversation_turns(target)
        message_lengths: list[float] = []
        voice_lengths: list[float] = []
        voice_count = 0
        platform_emoji: Counter[str] = Counter()
        platform_repeats: Counter[int] = Counter()
        unicode_emoji: Counter[str] = Counter()
        punctuation: Counter[str] = Counter()
        bigrams: Counter[str] = Counter()
        trigrams: Counter[str] = Counter()
        line_heads: Counter[str] = Counter()
        line_tails: Counter[str] = Counter()
        hour_counts: Counter[int] = Counter()
        weekday_counts: Counter[int] = Counter()
        parsed_times: list[datetime] = []
        question = 0
        exclamation = 0
        ellipsis = 0
        night = 0
        by_id = {str(item.id): item for item in target}

        for item in target:
            text = str(item.text or "")
            voices = [str(match.group(1) or "").strip() for match in VOICE_TAG.finditer(text)]
            if voices or (_LT + "voice" in text.lower()):
                voice_count += 1
            for voice_text in voices:
                if voice_text:
                    voice_lengths.append(float(len(voice_text)))
            for match in PLATFORM_EMOJI.finditer(text):
                platform_emoji[match.group(1)] += int(match.group(2) or 1)
                platform_repeats[int(match.group(2) or 1)] += 1
            visible = VOICE_TAG.sub("", text)
            for char in UNICODE_EMOJI.findall(visible):
                unicode_emoji[char] += 1
            plain = PLATFORM_EMOJI.sub("", visible)
            message_lengths.append(float(len(plain.strip())))
            punctuation.update(
                char
                for char in plain
                if not char.isalnum() and not char.isspace() and ord(char) > 31
            )
            question += plain.count(chr(0xFF1F)) + plain.count("?")
            exclamation += plain.count(chr(0xFF01)) + plain.count("!")
            ellipsis += plain.count(chr(0x2026)) + plain.count("...")
            for line in plain.splitlines():
                line = line.strip()
                if not line:
                    continue
                head_chars = _PHRASE_CHARS.findall(line[:8])
                tail_chars = _PHRASE_CHARS.findall(line[-8:])
                if head_chars:
                    line_heads["".join(head_chars)[:4]] += 1
                if tail_chars:
                    line_tails["".join(tail_chars)[-4:]] += 1
            bigrams.update(_ngrams(plain, 2))
            trigrams.update(_ngrams(plain, 3))
            stamp = parse_turn_time(
                getattr(item, "timestamp", None) or getattr(item, "event_time", None)
            )
            if stamp is not None:
                parsed_times.append(stamp)
                hour_counts[stamp.hour] += 1
                weekday_counts[stamp.weekday()] += 1
                if stamp.hour in NIGHT_HOURS:
                    night += 1

        burst_sizes = [float(len(turn.evidence_unit_ids)) for turn in turns]
        total_messages = len(target)
        stamped = len(parsed_times)
        ordered_times = sorted(parsed_times)
        mean_len = sum(message_lengths) / max(1, total_messages)
        representatives: list[str] = []

        def remember(evidence_id: str) -> None:
            if evidence_id in by_id and evidence_id not in representatives:
                representatives.append(evidence_id)

        remember(str(max(target, key=lambda item: len(str(item.text or ""))).id))
        for item in target:
            if PLATFORM_EMOJI.search(str(item.text or "")):
                remember(str(item.id))
                break
        for item in target:
            if VOICE_TAG.search(str(item.text or "")):
                remember(str(item.id))
                break
        near_mean = sorted(target, key=lambda value: abs(len(str(value.text or "")) - mean_len))[:3]
        for item in near_mean:
            remember(str(item.id))

        tic_counter: Counter[str] = Counter()
        for phrase, count in bigrams.items():
            if count >= 3:
                tic_counter[phrase] += count
        for phrase, count in trigrams.items():
            if count >= 3:
                tic_counter[phrase] += count
        return ChatStyleProfile(
            persona_id=persona_id,
            corpus_size=total_messages,
            turn_corpus_size=len(turns),
            time_range=[
                ordered_times[0].isoformat() if ordered_times else None,
                ordered_times[-1].isoformat() if ordered_times else None,
            ],
            representative_evidence_ids=representatives[:8],
            statistics={
                "total_target_messages": total_messages,
                "total_target_turns": len(turns),
                "average_message_length": round(mean_len, 2),
                "message_length_distribution": _distribution(message_lengths),
                "average_messages_per_turn": round(sum(burst_sizes) / max(1, len(burst_sizes)), 3),
                "messages_per_turn_distribution": _distribution(burst_sizes),
                "voice_message_count": voice_count,
                "voice_message_ratio": round(voice_count / max(1, total_messages), 4),
                "voice_transcription_length_distribution": _distribution(voice_lengths),
                "platform_emoji": _top(platform_emoji),
                "platform_emoji_repeat_distribution": sorted(
                    ({"count": key, "uses": value} for key, value in platform_repeats.items()),
                    key=lambda row: int(row["count"]),
                )[:25],
                "unicode_emoji": _top(unicode_emoji),
                "punctuation_frequency": _top(punctuation, 20),
                "question_marks": question,
                "exclamation_marks": exclamation,
                "ellipsis_uses": ellipsis,
                "frequent_phrases_2gram": _top(bigrams, 40),
                "frequent_phrases_3gram": _top(trigrams, 40),
                "frequent_line_openings": _top(line_heads),
                "frequent_line_endings": _top(line_tails),
                "verbal_tic_candidates": _top(tic_counter, 20),
                "hour_distribution": {
                    str(hour): int(hour_counts[hour]) for hour in sorted(hour_counts)
                },
                "weekday_distribution": {
                    str(day): int(weekday_counts[day]) for day in sorted(weekday_counts)
                },
                "night_chat_ratio": round(night / stamped, 4) if stamped else None,
                "interpretation_note": (
                    "统计值为确定性聚合；emoji 名称不得被直接解释为情绪，"
                    "口头禅候选仅为频率信号，不是人格断言。"
                ),
            },
        )

    @staticmethod
    def _counts_for_target(unit: Any) -> bool:
        source_kind = str(getattr(unit, "source_kind", "") or "")
        if source_kind not in {"chat", "chat_import", "guided_interview"}:
            return False
        role = str(getattr(unit, "speaker_role", None) or "")
        status = str((getattr(unit, "metadata", None) or {}).get("semantic_status") or "")
        # Only an explicit exporter/context-only declaration removes a message
        # from target statistics.  Undeclared roles stay conservative (P0-A):
        # the messages still belong to the material's own voice statistics
        # instead of vanishing from the Expression DNA.
        return not (role == ROLE_EXPORTER or status == "context_only")


__all__ = ["ChatStyleProfile", "ChatStyleProfiler", "PROFILE_CONTRACT"]
