"""RoomCaseState: the durable, accumulating facts of one consultation case.

Three things must never overwrite each other:

    Room topic            stable, long-lived description of what the room is about
    Current user message  only the *new* input of this turn
    RoomCaseState         everything the case has accumulated so far

A follow-up like "计划持有一年，最大可承受 10 万元亏损" must MERGE into the
case state; it must never replace the topic and must never drop the birth
date / gender / original goal the user already supplied.  Losing those facts
is exactly what produced the "ask the same question forever" failure mode:
once the original goal fell out of the recent-transcript window, the experts
had nothing left to work from and started interrogating the user again.

The merge is deliberately *deterministic* (no model call) for the common
structural facts -- dates, genders, places, money, durations -- because a
model-driven extraction that silently fails is worse than a deterministic
one that is merely incomplete.

The structure is domain-neutral: no 术数 / Taibu concepts live here.  A case
about a fortune-telling consultation and a case about a hardware roadmap use
exactly the same fields.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# A single case may block the user at most once with a clarification request.
MAX_BLOCKING_CLARIFICATIONS = 1

# Keys under ``known_user_facts`` that must survive every merge.
STICKY_FACT_KEYS: frozenset[str] = frozenset(
    {
        "gender",
        "birth_datetime",
        "birth_date",
        "birth_time",
        "birth_place",
        "age",
        "name",
    }
)


class RoomCaseState(BaseModel):
    """Accumulated, merge-only description of what this case is about."""

    model_config = ConfigDict(extra="allow")

    intent: str = ""
    problem_definition: str = ""
    known_user_facts: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    resolved_questions: list[str] = Field(default_factory=list)
    open_indispensable_questions: list[str] = Field(default_factory=list)
    optional_details: list[str] = Field(default_factory=list)
    last_user_message: str = ""
    updated_at: str = ""

    # -- clarification budget ---------------------------------------------
    clarification_round_count: int = 0
    clarification_asked_questions: list[str] = Field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat()

    @property
    def clarification_budget_exhausted(self) -> bool:
        return self.clarification_round_count >= MAX_BLOCKING_CLARIFICATIONS

    def note_clarification(self, question: str) -> None:
        self.clarification_round_count += 1
        cleaned = str(question or "").strip()
        if cleaned and cleaned not in self.clarification_asked_questions:
            self.clarification_asked_questions.append(cleaned)

    def as_prompt_block(self) -> dict[str, Any]:
        """Compact, transport-cheap projection for prompt packing."""

        return {
            "intent": self.intent,
            "problem_definition": self.problem_definition,
            "known_user_facts": self.known_user_facts,
            "constraints": self.constraints,
            "assumptions": self.assumptions,
            "open_indispensable_questions": self.open_indispensable_questions,
            "optional_details": self.optional_details,
        }


# --- deterministic fact extraction ---------------------------------------
#
# These are intentionally narrow patterns.  A false positive here costs a
# wrong fact in the case state; a false negative only means the model still
# has to read the raw text.  Prefer the latter.

_CN_NUMERALS = "零〇一二三四五六七八九十两"

_GENDER_PATTERNS = (
    (re.compile(r"(男性|男\s*性|男生|男子|我是男)"), "male"),
    (re.compile(r"(女性|女\s*性|女生|女子|我是女)"), "female"),
)

_BIRTH_DATETIME_PATTERNS = (
    re.compile(
        r"(?P<y>(?:19|20)\d{2})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"
        r"(?:\s*(?P<h>\d{1,2})\s*[::时点]\s*(?P<min>\d{1,2})?\s*分?)?"
    ),
    re.compile(
        r"(?P<y>(?:19|20)\d{2})[-/.]\s*(?P<m>\d{1,2})[-/.]\s*(?P<d>\d{1,2})"
        r"(?:\s+(?P<h>\d{1,2})\s*:\s*(?P<min>\d{2}))?"
    ),
)

_BIRTH_TIME_ONLY = re.compile(r"(?P<h>\d{1,2})\s*[::时点]\s*(?P<min>\d{1,2})?\s*分?")

_BIRTH_PLACE_PATTERNS = (
    re.compile(r"(?:出生(?:于|在)?|籍贯[:：]?)\s*([一-鿿]{2,10}(?:市|省|县|区|镇|村)?)"),
    re.compile(r"(北京|上海|天津|重庆|广州|深圳|杭州|南京|成都|武汉|西安|苏州|沈阳|青岛)"),
)

_MONEY_PATTERNS = (
    re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>万元|万|亿元|亿|元|块)"),
)

_DURATION_PATTERNS = (
    re.compile(r"(?P<num>\d+|[" + _CN_NUMERALS + r"]+)\s*(?P<unit>年|个月|月|周|天|日)"),
)

_YEAR_MENTION = re.compile(r"(?P<y>(?:19|20)\d{2})\s*年")


def _dedupe(values: list[str], limit: int = 24) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered[-limit:]


def extract_facts(text: str) -> dict[str, Any]:
    """Deterministically pull structural facts out of one user message."""

    facts: dict[str, Any] = {}
    source = str(text or "")
    if not source.strip():
        return facts

    for pattern, value in _GENDER_PATTERNS:
        if pattern.search(source):
            facts["gender"] = value
            break

    for pattern in _BIRTH_DATETIME_PATTERNS:
        match = pattern.search(source)
        if match is None:
            continue
        year = int(match.group("y"))
        month = int(match.group("m"))
        day = int(match.group("d"))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        facts["birth_date"] = f"{year:04d}-{month:02d}-{day:02d}"
        hour = match.groupdict().get("h")
        if hour:
            minute = match.groupdict().get("min") or "00"
            facts["birth_time"] = f"{int(hour):02d}:{int(minute):02d}"
            facts["birth_datetime"] = f"{facts['birth_date']} {facts['birth_time']}"
        else:
            facts["birth_datetime"] = facts["birth_date"]
        break
    else:
        # No full date: a bare clock time still anchors the hour pillar.
        time_match = _BIRTH_TIME_ONLY.search(source)
        if time_match and not _YEAR_MENTION.search(source):
            hour = int(time_match.group("h"))
            minute = int(time_match.group("min") or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                facts["birth_time"] = f"{hour:02d}:{minute:02d}"

    for pattern in _BIRTH_PLACE_PATTERNS:
        match = pattern.search(source)
        if match:
            place = str(match.group(1)).strip()
            if place:
                facts["birth_place"] = place
                break

    amounts = []
    for match in _MONEY_PATTERNS[0].finditer(source):
        amounts.append(f"{match.group('num')}{match.group('unit')}")
    if amounts:
        facts["mentioned_amounts"] = _dedupe(amounts)

    durations = []
    for match in _DURATION_PATTERNS[0].finditer(source):
        durations.append(f"{match.group('num')}{match.group('unit')}")
    if durations:
        facts["mentioned_durations"] = _dedupe(durations)

    return facts


def _merge_facts(
    current: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Merge two fact dicts; sticky keys are never downgraded to empty."""

    merged = dict(current)
    for key, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        existing = merged.get(key)
        if key in STICKY_FACT_KEYS and existing not in (None, "", [], {}):
            # Already known and authoritative -- a later vague mention must
            # not overwrite a previously exact one.
            continue
        if isinstance(existing, list) and isinstance(value, list):
            merged[key] = _dedupe([*existing, *value])
        else:
            merged[key] = value
    return merged


def _looks_like_followup(text: str) -> bool:
    """Heuristic: short messages without a subject are usually additions."""

    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    return len(cleaned) <= 40


def merge_case_state(
    state: RoomCaseState | None,
    user_message: str,
    *,
    intent: str | None = None,
    problem_definition: str | None = None,
    constraints: list[str] | None = None,
    assumptions: list[str] | None = None,
    resolved: list[str] | None = None,
    optional_details: list[str] | None = None,
    open_questions: list[str] | None = None,
) -> RoomCaseState:
    """Merge one new user turn into the case state without losing anything.

    Never overwrites ``intent``/``problem_definition`` with the follow-up
    text: the first substantial statement of the problem is the case anchor,
    and later turns only *add* constraints, facts and refinements.
    """

    current = state or RoomCaseState()
    message = str(user_message or "").strip()

    if not current.last_user_message and message:
        # First real user turn: it defines the case.
        current.intent = message
        if not current.problem_definition:
            current.problem_definition = message
    elif message and not current.intent:
        current.intent = message

    current.known_user_facts = _merge_facts(
        current.known_user_facts, extract_facts(message)
    )
    if constraints:
        current.constraints = _dedupe([*current.constraints, *constraints])
    if assumptions:
        current.assumptions = _dedupe([*current.assumptions, *assumptions])
    if resolved:
        current.resolved_questions = _dedupe([*current.resolved_questions, *resolved])
    if optional_details:
        current.optional_details = _dedupe([*current.optional_details, *optional_details])
    if open_questions is not None:
        current.open_indispensable_questions = _dedupe(open_questions)
    if intent:
        current.intent = str(intent)
    if problem_definition:
        current.problem_definition = str(problem_definition)
    if message:
        current.last_user_message = message
    current.touch()
    return current


def merge_from_host_analysis(
    state: RoomCaseState,
    analysis: dict[str, Any],
    *,
    allow_clarification: bool = True,
) -> RoomCaseState:
    """Fold the host's structured analysis into the case state.

    The host may contribute a better problem definition, a fact patch and
    assumptions.  A clarification it actually asked is charged against the
    one-shot clarification budget -- unless ``allow_clarification`` is
    False (a gate-suppressed ask): then the budget is untouched, the
    asked-questions list gains nothing and no open question is recorded,
    because the run continues on defaults instead of this ask.
    """

    patch = analysis.get("known_facts_patch")
    if isinstance(patch, dict) and patch:
        state.known_user_facts = _merge_facts(state.known_user_facts, patch)
    definition = str(analysis.get("problem_definition") or "").strip()
    if definition:
        state.problem_definition = definition
    assumptions = analysis.get("assumptions")
    if isinstance(assumptions, list):
        state.assumptions = _dedupe([*state.assumptions, *assumptions])
    if allow_clarification:
        missing = analysis.get("missing_indispensable_fields")
        if isinstance(missing, list):
            state.open_indispensable_questions = _dedupe(
                [str(item) for item in missing if str(item).strip()]
            )
        question = str(analysis.get("clarification_question") or "").strip()
        if question and not _looks_like_optional(question):
            state.note_clarification(question)
    state.touch()
    return state


# --- output-vs-input guard ------------------------------------------------

#: Things the user is asking the experts to *produce*.  Asking for them back
#: as "required input" is the infinite-clarification loop.
OUTPUT_INTENT_MARKERS: tuple[str, ...] = (
    "什么时候",
    "何时",
    "哪天",
    "哪些日期",
    "什么日期",
    "吉日",
    "择日",
    "择时",
    "什么数字",
    "哪个数字",
    "什么号码",
    "什么方向",
    "哪个方位",
    "什么条件下",
    "何时停止",
    "什么时候买",
    "什么时候卖",
    "哪个更适合",
    "什么时候更适合",
    "是否可行",
    "可行吗",
    "运势",
    "财运",
    # Echo-loop signal: "I cannot deliver X to you" talks about producing
    # a deliverable while pinning it on missing input (§18).
    "无法给你",
    "无法给出",
)

#: Phrases that turn a question into a demand for missing *input*.
INPUT_DEMAND_MARKERS: tuple[str, ...] = (
    "请提供",
    "请补充",
    "请给出",
    "请告诉我",
    "请告知",
    "请先提供",
    "需要提供",
    "没有提供",
    "没有给",
    "无法给你",
    "无法给出",
    "缺少",
    "缺失",
    "无法计算",
    "无法判断",
    "不能开始",
    "暂不开始",
)


def is_output_intent_question(question: str) -> bool:
    """True when the question asks for something the expert must compute."""

    text = str(question or "")
    return any(marker in text for marker in OUTPUT_INTENT_MARKERS)


def _looks_like_optional(question: str) -> bool:
    text = str(question or "")
    return "可选项" in text or "可选" in text[:12]


def violates_output_input_boundary(question: str) -> bool:
    """True when a clarification demands back something that is an output.

    "你没有给购买日期，因此我无法给你购买日期" is the canonical violation:
    if the user asked *when* to buy, the date is the deliverable.
    """

    text = str(question or "")
    if not is_output_intent_question(text):
        return False
    return any(marker in text for marker in INPUT_DEMAND_MARKERS)


__all__ = [
    "MAX_BLOCKING_CLARIFICATIONS",
    "OUTPUT_INTENT_MARKERS",
    "STICKY_FACT_KEYS",
    "RoomCaseState",
    "extract_facts",
    "is_output_intent_question",
    "merge_case_state",
    "merge_from_host_analysis",
    "violates_output_input_boundary",
]
