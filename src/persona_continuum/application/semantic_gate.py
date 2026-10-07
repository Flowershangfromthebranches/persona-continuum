"""Deterministic chat-only semantic routing; raw/style/episode lanes stay intact."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from persona_continuum.application.material_chat import (
    CHAT_SOURCE_KINDS,
    ROLE_TARGET,
    ConversationTurn,
)

SEMANTIC_GATE_POLICY_VERSION = "semantic-gate-v1"
SEMANTIC_GATE_SELECTED = "semantic_gate_selected"
SEMANTIC_GATE_SKIPPED = "semantic_gate_skipped"

# P1-C: Persona creation tasks pick a task-level Semantic Gate strategy.
# "auto" resolves to the concrete mode at dispatch time:
#   * non-chat / small chat material -> "full" (default fidelity);
#   * large private chat -> "balanced" only when the local ShadowGate
#     quality acceptance report passes, otherwise "full" with the reason
#     recorded.  The resolution never widens the random reserve.
TASK_SEMANTIC_GATE_MODES = ("auto", "full", "balanced", "fast")
# A chat source with more target messages than this counts as "large".
LARGE_CHAT_TARGET_MESSAGE_THRESHOLD = 2000

_FEATURES = [
    "我觉得",
    "我是",
    "我其实",
    "我一直",
    "我从来",
    "喜欢",
    "不喜欢",
    "讨厌",
    "想要",
    "不想",
    "更喜欢",
    "最喜欢",
    "觉得应该",
    "不能接受",
    "原则",
    "价值",
    "重要",
    "在乎",
    "相信",
    "决定",
    "选择",
    "拒绝",
    "答应",
    "放弃",
    "准备",
    "计划",
    "以后",
    "一定",
    "不会",
    "喜欢你",
    "爱",
    "讨厌你",
    "分手",
    "联系",
    "朋友",
    "关系",
    "在一起",
    "离开",
    "道歉",
    "原谅",
    "失望",
    "信任",
    "爸",
    "妈",
    "父母",
    "家里",
    "亲戚",
    "家庭",
    "工作",
    "公司",
    "老板",
    "学校",
    "老师",
    "考试",
    "专业",
    "大学",
    "学习",
    "钱",
    "工资",
    "买",
    "花",
    "贵",
    "便宜",
    "存钱",
    "毕业",
    "入职",
    "辞职",
    "搬家",
    "恋爱",
    "旅行",
    "事故",
    "生气",
    "难过",
    "开心",
    "害怕",
    "焦虑",
    "烦",
    "委屈",
    "崩溃",
    "后悔",
    "干活",
    "看电影",
    "跑步",
    "运动",
    "我感觉",
    "我俩",
    "我们那",
    "手术",
    "医院",
    "回家",
    "过敏",
    "睡不着",
    "不舒服",
    "受不了",
    "担心",
    "难受",
    "压力",
    "烦躁",
    "羡慕",
    "嫉妒",
    "打算",
    "学姐",
    "学长",
    "室友",
    "同事",
    "同学",
    "问问她",
    "问问他",
]
_BYPASS = re.compile(
    r"我是|我其实|我一直|我从来|原则|不能接受|应该|价值|决定|拒绝|答应|承诺|保证|"
    r"毕业|入职|辞职|搬家|事故|结婚|离婚|分手|别联系|不要联系|喜欢你|爱你|"
    r"还喜欢|在一起|离开我|原谅|失望|信任|绝交|去世|出生|住院|生病|"
    r"我(?:一定|不会)|我.{0,16}(?:选择|放弃|准备|计划)|手术|别说了|不提前|不告诉|不理我"
)
_ROUTINE = re.compile(
    r"^(?:嗯|哦|噢|啊|好|好的|好吧|行|是|对|哈|呵|嘿|到|到了|睡了|晚安|早安|谢谢|收到|ok|[\W_])+$",
    re.I,
)
# P1-B: deterministic recall features derived from ShadowGate false-negative
# analysis over real classified chats.  They target two textually visible
# evidence shapes the frequency sketch misses: replies to a direct question
# and first-person stance statements.  Both are exact bypass rules — the
# random reserve ratio stays untouched.
_QUESTION_PRECEDENT = re.compile(
    r"[?？]|吗[。！～!\s]|怎么|为什么|要不要|能不能|是不是|好不好|多少|哪一|哪个"
)
_FIRST_PERSON_STANCE = re.compile(
    r"我[不没很最还就常超太]|我怕|我担心|我想|我喜欢|我讨厌|我觉得|我准备|我打算|我决定|我宁愿|我倾向"
)


@dataclass(frozen=True)
class GateDecision:
    selected: bool
    reason: str
    score: float = 0.0
    reserve: bool = False
    bypass: bool = False


class SemanticGate:
    """Bounded frequency sketch, replayable in source order even across resume.

    The first low-score turn of each month guarantees sparse periods survive;
    stable hash sampling reserves another 8% (balanced) or 5% (fast).
    """

    def __init__(self, mode: str = "full") -> None:
        if mode not in {"full", "balanced", "fast"}:
            raise ValueError(f"Unknown semantic gate mode: {mode}")
        self.mode = mode
        self.counts = [0] * 16384
        self.months: set[tuple[str, str, str]] = set()

    def decide(
        self, turn: ConversationTurn, previous: ConversationTurn | None = None
    ) -> GateDecision:
        if not turn.is_target or turn.source_kind not in CHAT_SOURCE_KINDS:
            return GateDecision(True, "not_chat_target")
        if turn.speaker_role != ROLE_TARGET:
            return GateDecision(True, "ambiguous_role")
        text = turn.text.strip().casefold()
        normalized = re.sub(r"\d+", "#", re.sub(r"<[^>]+>", "", text))
        tokens = set(re.findall(r"[a-z]{3,}|[\u4e00-\u9fff]{2}|[^\w\s]{2,}", normalized))
        slots = {
            int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % len(self.counts)
            for token in tokens
        }
        rarity = sum(self.counts[slot] < 2 for slot in slots) / max(1, len(slots))
        for slot in slots:
            self.counts[slot] = min(65535, self.counts[slot] + 1)
        if self.mode == "full":
            return GateDecision(True, "full")
        same_context = previous is not None and (
            previous.source_id == turn.source_id
            and previous.conversation_id == turn.conversation_id
        )
        if len(text) >= 240 or ("<voice" in text and len(normalized) >= 80) or _BYPASS.search(text):
            return GateDecision(True, "explicit_semantics_or_long_expression", bypass=True)
        if same_context and previous is not None and _BYPASS.search(previous.text):
            return GateDecision(True, "critical_context", bypass=True)
        if (
            same_context
            and previous is not None
            and len(normalized) <= 32
            and (
                any(feature in previous.text for feature in _FEATURES)
                or re.search(r"帮我|能不能|可以.*吗|要不要|你愿意|答应我", previous.text)
            )
        ):
            return GateDecision(True, "short_semantic_context", bypass=True)
        if (
            same_context
            and previous is not None
            and _QUESTION_PRECEDENT.search(previous.text)
            and not _ROUTINE.fullmatch(text)
        ):
            return GateDecision(True, "answer_to_direct_question", bypass=True)
        if _FIRST_PERSON_STANCE.search(text):
            return GateDecision(True, "first_person_stance", bypass=True)
        routine = bool(_ROUTINE.fullmatch(text)) or bool(
            re.fullmatch(r"(?:<emoji\b[^>]*>\s*)+", text)
        )
        score = float(sum(feature in text for feature in _FEATURES))
        rare_emoji = len(set(re.findall(r"[🌀-🫿]", text))) >= 2
        rare_emoji |= len(set(re.findall(r'<emoji\s+name="([^"]+)"', text))) >= 2
        if (rare_emoji or (not routine and len(slots) >= 2)) and rarity >= 0.8:
            return GateDecision(True, "rare_or_novel_expression", score, bypass=True)
        if score >= (1 if self.mode == "balanced" else 2):
            return GateDecision(True, "lexical_features", score)
        month = (turn.start_time or "undated")[:7]
        bucket = (turn.source_id, turn.conversation_id or "", month)
        first = bucket not in self.months
        self.months.add(bucket)
        # Use source location/content rather than random database identifiers.
        key = f"{month}|{turn.start_time}|{turn.speaker}|{text}"
        sample = int(hashlib.sha256(key.encode()).hexdigest()[:16], 16) % 100
        reserve = first or sample < (8 if self.mode == "balanced" else 5)
        return GateDecision(
            reserve, "temporal_reserve" if reserve else "low_semantic_score", score, reserve=reserve
        )


def resolve_task_semantic_gate_mode(
    task_mode: str,
    *,
    chat_target_messages: int = 0,
    shadow_gate_accepted: bool = False,
) -> tuple[str, str]:
    """Resolve a task-level Semantic Gate strategy to a concrete mode (P1-C).

    Returns ``(effective_mode, reason)``.  Explicit ``full``/``balanced``/
    ``fast`` pass through unchanged; ``auto`` (and any unknown value) applies
    the recommendation rules.  This is a pure function: the shadow acceptance
    flag comes from a local quality report, never from a remote call.
    """

    mode = str(task_mode or "auto").strip().lower()
    if mode in {"full", "balanced", "fast"}:
        return mode, "task_explicit"
    large_chat = int(chat_target_messages) >= LARGE_CHAT_TARGET_MESSAGE_THRESHOLD
    if large_chat and shadow_gate_accepted:
        return "balanced", "auto_large_chat_shadow_gate_accepted"
    if large_chat:
        return "full", "auto_large_chat_shadow_gate_not_accepted"
    return "full", "auto_small_or_non_chat_full_fidelity"
