"""Private-material evidence intelligence for Persona Continuum.

The source table remains the immutable raw-material layer.  This module adds a
derived, provenance-preserving layer between sources and the existing eight
dimension compiler:

    raw source -> evidence units -> clusters -> fused evidence -> index

The implementation is deliberately local-first.  A caller may provide an LLM
classifier/fusion hook, but deterministic parsing and conservative heuristics
are always available and never invent facts when that hook is absent.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.errors import PromptTransportLimitExceededError
from persona_continuum.agent.phase_policy import auto_episodes_cap, auto_units_cap
from persona_continuum.agent.response_collector import AgentRuntimeError, AgentStructuredOutputError
from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.application.chat_style_profiler import ChatStyleProfiler
from persona_continuum.application.classification_dispatch import ClassificationDispatch
from persona_continuum.application.material_chat import (
    CHAT_SOURCE_KINDS,
    ROLE_EXPORTER,
    ROLE_TARGET,
    SEMANTIC_CONTEXT_ONLY,
    SEMANTIC_EVIDENCE_EXTRACTED,
    SEMANTIC_REVIEWED_NO_EVIDENCE,
    SEMANTIC_TARGET_PENDING,
    ConversationTurn,
    fold_conversation_turns,
    merge_turns,
    parse_speaker_role_map,
    parse_turn_time,
    turn_from_units,
)
from persona_continuum.application.material_pipeline import (
    AffectedSet,
    CandidateGroup,
    ClassificationRequestTokenEstimator,
    GlobalCandidateIndex,
    MaterialPipelineMetrics,
    MaterialPromptState,
    PreLLMDeduplicator,
    ResolvedExecutionProfile,
    build_analysis_windows,
    material_batch_target_tokens,
)
from persona_continuum.application.semantic_gate import (
    SEMANTIC_GATE_POLICY_VERSION,
    SEMANTIC_GATE_SELECTED,
    SEMANTIC_GATE_SKIPPED,
    SemanticGate,
)
from persona_continuum.ingestion.streaming import StreamingMaterialReader
from persona_continuum.numeric import (
    append_skipped_numeric_metadata,
    normalize_probability_map,
    safe_int,
    safe_probability,
)
from persona_continuum.security.paths import ensure_child_path

REQUIRED_DIMENSIONS = (
    "identity_and_timeline",
    "works_and_views",
    "interviews_and_dialogue",
    "expression_dna",
    "decisions_and_behavior",
    "third_party_views",
    "affect_relationship_defense",
    "values_desires_contradictions",
)

# Keep the material-agent prompt/schema contract in one module so the
# analyzer's batch budget accounts for the same fixed context that is sent by
# PersonaCreationService. The callback remains generic, but the budget is no
# longer based on item counts alone.
MATERIAL_AGENT_SYSTEM_PROMPTS: dict[str, str] = {
    "classify": (
        "你是 Persona Continuum 跨维度 Evidence Intelligence 分析器。每批材料只深度理解一次，"
        "只能根据输入原文抽取，不得补充事实。返回 JSON 对象：{units:[{id,evidence_type,"
        "dimension_scores,claims,events,dates,entities,quotes,decisions,behaviors,relationships,"
        "emotions,motivations,beliefs,values,communication_patterns,contradictions,negative_evidence,"
        "uncertainties,life_stage,relationship_entities,context_tags,confidence}]}。"
        "id 必须来自输入；dimension_scores 只能使用提供的八个维度。"
    ),
    "relate": (
        "你是 Persona Continuum 证据语义关系分析器。只能比较输入原文。"
        "返回 JSON：{clusters:[{evidence_ids:[...]}],contradictions:[{"
        "evidence_ids:[...],contradiction_type,summary,conditions,confidence}]}。"
        "仅合并语义相同或互补的证据；冲突必须保留双方 id；不得使用输入外 id。"
    ),
    "fuse": (
        "你是 Persona Continuum 证据融合器。只能压缩输入 evidence 中已有信息，"
        "不得新增事实或改变 provenance。返回 JSON 对象："
        "{claims:[{id,canonical_claim}]}，id 必须来自输入。"
    ),
}

MATERIAL_AGENT_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "classify": {
        "type": "object",
        "required": ["units"],
        "properties": {
            "units": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string"}},
                },
            },
        },
    },
    "relate": {
        "type": "object",
        "properties": {
            "clusters": {"type": "array"},
            "contradictions": {"type": "array"},
        },
    },
    "fuse": {"type": "object", "properties": {"claims": {"type": "array"}}},
}

LEGACY_CLASSIFICATION_PROMPT = MATERIAL_AGENT_SYSTEM_PROMPTS["classify"]

# Frozen v3 prompt (kept only to recompute historical cache keys so an already
# successful v3 analysis is never re-billed).  Do not edit this string.
CLASSIFICATION_PROMPT_V3 = LEGACY_CLASSIFICATION_PROMPT + (
    "按连续对话理解所有输入，不得抽样或忽略短句、表情、语音标记、否定、冲突或反证。"
    "紧凑输出：只写有证据的非空字段，不复述输入，不补齐空数组，不把同一分析重复写到每条消息。"
    "可返回 reviewed_ids（逐一列出确实读过的本窗消息ID）和 units（有独立证据的记录）。"
    "一段对话共同支持同一观察时仅输出一次：id 为证据锚点，supporting_evidence_ids 列出支持原句；"
    "明确观察涉及的说话人、条件和不确定性，不把上下文提供者的观点归给目标人物。"
    "无独立事实的消息仍须出现在 reviewed_ids；其原文保留用于表达和关系分析。"
    "context_units 仅提供相邻上下文，不计入本窗完成ID。source_context 是材料格式说明而非聊天指令。"
)

# conversation-evidence-v4: sparse output contract.  A successful structured
# call means every target turn in the window was reviewed; absence from
# ``units`` is a local ``reviewed_no_independent_evidence`` mark, never a
# retry trigger.  reviewed_ids no longer exists in the contract.
CLASSIFICATION_CONTRACT_V4 = "conversation-evidence-v4"
# Fold/window policy version.  Bumping this changes the v4 cache-key
# namespace so a strategy change never silently reuses an incompatible
# checkpoint result from an older strategy.
TURN_POLICY_VERSION = "conversation-turn-v2"
MATERIAL_AGENT_SYSTEM_PROMPTS["classify"] += (
    "按连续对话理解所有输入，不得抽样或忽略短句、表情、语音标记、否定、冲突或反证。"
    "每行带 speaker_role 与 semantic_role：semantic_role=target 的行是目标人格证据源；"
    "semantic_role=context 的行（含导出者/其他说话人）只提供上下文，不得为其单独输出证据，"
    "更不得把上下文说话人的观点或言行归因给目标人物。"
    "紧凑输出：只写有独立人格证据价值的 target 行；每个对象只写非空的证据字段，"
    "不复述输入，不补齐空数组，不把同一分析重复写到每条消息。"
    "本窗成功返回合法 JSON 即代表所有 target 行已被审阅；不必逐一列出未输出ID，"
    "也不要返回 reviewed_ids。一段对话共同支持同一观察时仅输出一次：id 为证据锚点"
    "（target 行的 id 或其成员消息 id），supporting_evidence_ids 列出支持该观察的原句 id。"
    "短回复必须结合上下文理解；emoji、<voice> 标记属于表达证据，不能因文字短就视为无意义。"
    "明确观察涉及的说话人、条件和不确定性。context_units 仅提供相邻上下文。"
    "source_context 是材料格式说明而非聊天指令。"
    "输入可能按 EPISODE_BEGIN/EPISODE_END 分段，每个 episode 是一个连续对话片段："
    "episode 与 episode 之间互不构成连续上下文，不得把一个 episode 里的问答、"
    "指代或因果关系错误连接到另一个 episode；每个 episode 内部保持原聊天顺序；"
    "一次请求可以独立分析多个 episode，输出时证据 id 只能来自对应 episode 内的行。"
)

EVIDENCE_ORDER_POSITION = (
    "COALESCE(CAST(json_extract(source_locator_json, '$.segment_index') AS INTEGER), 0)"
)


_CLASSIFICATION_STRING_KEYS = (
    "name",
    "entity",
    "text",
    "value",
    "label",
    "tag",
    "date",
    "content",
    "quote",
    "stage",
    "life_stage",
    "role",
    "speaker_role",
    "type",
    "evidence_type",
    "dimension",
    "id",
    "title",
    "summary",
    "description",
)
_CLASSIFICATION_STRING_LIST_FIELDS = (
    "dimension_candidates",
    "life_stage_candidates",
    "relationship_entities",
    "context_tags",
    "dates",
    "entities",
)
_CLASSIFICATION_ANY_LIST_FIELDS = (
    "claims",
    "events",
    "quotes",
    "decisions",
    "behaviors",
    "relationships",
    "emotions",
    "motivations",
    "beliefs",
    "values",
    "communication_patterns",
    "contradictions",
    "negative_evidence",
    "uncertainties",
)
_CLASSIFICATION_OPTIONAL_TEXT_FIELDS = ("speaker_role", "life_stage", "evidence_type")


def _classification_scalar_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _classification_object_text(value: dict[str, Any]) -> str | None:
    for key in _CLASSIFICATION_STRING_KEYS:
        if key in value:
            text = _classification_scalar_text(value[key])
            if text:
                return text
    for child in value.values():
        text = _classification_scalar_text(child)
        if text:
            return text
    return None


def _classification_text(value: Any) -> str | None:
    text = _classification_scalar_text(value)
    if text is not None:
        return text
    if isinstance(value, dict):
        return _classification_object_text(value)
    if isinstance(value, list):
        parts = [part for item in value if (part := _classification_text(item))]
        return "; ".join(dict.fromkeys(parts)) if parts else None
    return None


def _classification_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        if any(key in value for key in _CLASSIFICATION_STRING_KEYS):
            items: list[Any] = [value]
        else:
            items = list(value.keys())
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _classification_text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _classification_any_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item is not None]
    return [value]


def _classification_score_map(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    mapped: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict) or not item:
            continue
        dimension = item.get("dimension") or item.get("name") or item.get("id")
        if dimension is None and len(item) == 1:
            key, score = next(iter(item.items()))
            mapped[str(key)] = score
            continue
        if dimension is not None:
            mapped[str(dimension)] = item.get("score", item.get("value"))
    return mapped


def _normalise_classification_fields(value: Any) -> Any:
    """Coerce common prompt-only CLI shapes into the lenient classification envelope.

    Gemini and similar models often emit a scalar, a structured object, or null
    where the contract expects a list of strings.  Those shapes are recoverable
    evidence, not a window-level protocol failure.
    """

    if not isinstance(value, dict):
        return value
    data = dict(value)
    if "dimension_scores" in data:
        data["dimension_scores"] = _classification_score_map(data["dimension_scores"])
    for field in _CLASSIFICATION_STRING_LIST_FIELDS:
        if field in data:
            data[field] = _classification_str_list(data[field])
    for field in _CLASSIFICATION_ANY_LIST_FIELDS:
        if field in data:
            data[field] = _classification_any_list(data[field])
    for field in _CLASSIFICATION_OPTIONAL_TEXT_FIELDS:
        if field not in data:
            continue
        text = _classification_text(data[field])
        if text is None and field == "evidence_type":
            data.pop(field, None)
        else:
            data[field] = text
    if "metadata" in data and not isinstance(data["metadata"], dict):
        data["metadata"] = {}
    return data


def _normalise_material_numeric_payload(
    value: Any,
    *,
    score_fields: tuple[str, ...] = ("confidence",),
    map_fields: tuple[str, ...] = ("dimension_scores",),
    metadata_field: str | None = "metadata",
) -> Any:
    """Normalize optional model output numbers before Pydantic coercion."""

    if not isinstance(value, dict):
        return value
    data = dict(value)
    skipped: list[str] = []
    defaults = {"confidence": 0.5, "reliability": 0.5, "inference_strength": 0.5}
    for field in score_fields:
        if field not in data:
            continue
        normalized_score = safe_probability(data.get(field), default=None, field=field)
        if normalized_score is None:
            data[field] = defaults.get(field, 0.5)
            skipped.append(field)
        else:
            data[field] = normalized_score
    for map_field in map_fields:
        if map_field not in data:
            continue
        normalized_map, omitted = normalize_probability_map(
            data.get(map_field), field=map_field, allowed_keys=set(REQUIRED_DIMENSIONS)
        )
        data[map_field] = normalized_map
        skipped.extend(f"{map_field}.{key}" for key in omitted)
    if skipped and metadata_field:
        data[metadata_field] = append_skipped_numeric_metadata(
            data.get(metadata_field), skipped, field=""
        )
    return data


class MaterialClassificationResult(BaseModel):
    """Typed, lenient envelope for model-assisted material classification."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    content: str = ""
    source_id: str = ""
    evidence_type: str = "behavioral_observation"
    dimension_candidates: list[str] = Field(default_factory=list)
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    life_stage_candidates: list[str] = Field(default_factory=list)
    relationship_entities: list[str] = Field(default_factory=list)
    context_tags: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    speaker_role: str | None = None
    claims: list[Any] = Field(default_factory=list)
    events: list[Any] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    quotes: list[Any] = Field(default_factory=list)
    decisions: list[Any] = Field(default_factory=list)
    behaviors: list[Any] = Field(default_factory=list)
    relationships: list[Any] = Field(default_factory=list)
    emotions: list[Any] = Field(default_factory=list)
    motivations: list[Any] = Field(default_factory=list)
    beliefs: list[Any] = Field(default_factory=list)
    values: list[Any] = Field(default_factory=list)
    communication_patterns: list[Any] = Field(default_factory=list)
    contradictions: list[Any] = Field(default_factory=list)
    negative_evidence: list[Any] = Field(default_factory=list)
    uncertainties: list[Any] = Field(default_factory=list)
    life_stage: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        coerced = _normalise_classification_fields(value)
        return _normalise_material_numeric_payload(
            coerced,
            score_fields=("confidence",),
        )


class MaterialJobStatus(StrEnum):
    UPLOADED = "UPLOADED"
    PARSING = "PARSING"
    SEGMENTING = "SEGMENTING"
    ANALYZING = "ANALYZING"
    CLUSTERING = "CLUSTERING"
    FUSING = "FUSING"
    INDEXING = "INDEXING"
    GAP_ANALYSIS = "GAP_ANALYSIS"
    READY_FOR_COMPILATION = "READY_FOR_COMPILATION"
    FAILED = "FAILED"


class EvidenceUnit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    source_id: str
    source_locator: dict[str, Any] = Field(default_factory=dict)
    speaker: str | None = None
    speaker_role: str | None = None
    timestamp: str | None = None
    text: str
    normalized_text: str
    evidence_type: str = "behavioral_observation"
    dimension_candidates: list[str] = Field(default_factory=list)
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    life_stage_candidates: list[str] = Field(default_factory=list)
    relationship_entities: list[str] = Field(default_factory=list)
    context_tags: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    extraction_method: str = "deterministic_segmenter"
    source_kind: str = "user_provided"
    event_time: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_material_numeric_payload(value)


class MaterialRelationResult(BaseModel):
    """Typed, lenient envelope for model-assisted relation detection."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    contradiction_type: str | None = None
    summary: str = ""
    conditions: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_material_numeric_payload(value)


class MaterialFusionResult(BaseModel):
    """Typed, lenient envelope for model-assisted evidence fusion."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    canonical_claim: str | None = None
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    confidence: float = 0.5
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_material_numeric_payload(value)


class ConversationEpisode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    source_id: str
    participants: list[str] = Field(default_factory=list)
    start_time: str | None = None
    end_time: str | None = None
    topics: list[str] = Field(default_factory=list)
    emotional_context: list[str] = Field(default_factory=list)
    message_ids: list[str] = Field(default_factory=list)
    evidence_unit_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class EvidenceCluster(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    cluster_type: str
    canonical_evidence_id: str
    member_evidence_ids: list[str] = Field(default_factory=list)
    similarity_type: str = "single"
    confidence: float = 1.0
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_material_numeric_payload(
            value, score_fields=("confidence",), map_fields=(), metadata_field=None
        )


class FusedEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    canonical_claim: str
    evidence_type: str
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    unique_evidence_ids: list[str] = Field(default_factory=list)
    contradiction_ids: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    temporal_scope: list[str] = Field(default_factory=list)
    relationship_scope: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    synthesis_method: str = "deterministic_union"
    verbatim_samples: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_material_numeric_payload(value)


class PersonaContradiction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    contradiction_type: str
    evidence_ids: list[str] = Field(default_factory=list)
    summary: str
    conditions: list[str] = Field(default_factory=list)
    resolution: str | None = None
    confidence: float = 0.5
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_material_numeric_payload(
            value, score_fields=("confidence",), map_fields=(), metadata_field=None
        )


class PersonaIdentityAlias(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    alias: str
    normalized_alias: str
    confidence: float = 0.5
    status: str = "suggested"
    source_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_material_numeric_payload(
            value, score_fields=("confidence",), map_fields=(), metadata_field=None
        )


class PrivateMaterialCoverage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_count: int = 0
    evidence_unit_count: int = 0
    message_count: int = 0
    conversation_time_span_days: int = 0
    episode_count: int = 0
    behavior_example_count: int = 0
    relationship_context_count: int = 0
    life_event_count: int = 0
    expression_sample_count: int = 0
    decision_example_count: int = 0
    contradiction_count: int = 0
    fused_evidence_count: int = 0
    dimension_coverage: dict[str, int] = Field(default_factory=dict)
    high_priority_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for field in (
            "source_count",
            "evidence_unit_count",
            "message_count",
            "conversation_time_span_days",
            "episode_count",
            "behavior_example_count",
            "relationship_context_count",
            "life_event_count",
            "expression_sample_count",
            "decision_example_count",
            "contradiction_count",
            "fused_evidence_count",
        ):
            data[field] = safe_int(data.get(field), default=0, minimum=0, field=field)
        raw_dimensions = data.get("dimension_coverage")
        if isinstance(raw_dimensions, dict):
            data["dimension_coverage"] = {
                str(key): safe_int(item, default=0, minimum=0) or 0
                for key, item in raw_dimensions.items()
            }
        return data


class MaterialAnalysisJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    status: MaterialJobStatus = MaterialJobStatus.UPLOADED
    source_ids: list[str] = Field(default_factory=list)
    progress: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    runtime_snapshot: dict[str, Any] = Field(default_factory=dict)
    incremental: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


# Domain-language aliases used by the product/API docs.  They intentionally
# point to the same models rather than introducing a second representation.
AtomicEvidenceUnit = EvidenceUnit
EvidenceUnitCluster = EvidenceCluster
PersonaMaterialJob = MaterialAnalysisJob


def normalize_evidence_text(value: str) -> str:
    """Normalize only for matching; the original ``text`` is never replaced."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\u0000-\u001f\u007f]", " ", text)
    # Python's stdlib re has no Unicode property classes; this conservative
    # expression handles punctuation without destroying CJK word boundaries.
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_SENTENCE_BOUNDARY = re.compile(r"[^。！？!?\n]*[。！？!?\n]+|[^。！？!?\n]+$")


def _hard_slice(text: str, *, estimate_tokens: Callable[[str], int], max_piece_tokens: int) -> str:
    """Largest prefix of ``text`` whose token estimate stays in budget."""

    lo, hi, best = 1, len(text), 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if estimate_tokens(text[:mid]) <= max_piece_tokens:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return text[:best] or text[:1]


def _split_text_pieces(
    text: str, *, estimate_tokens: Callable[[str], int], max_piece_tokens: int
) -> list[str]:
    """Split text at sentence/paragraph boundaries, hard-slicing when needed."""

    text = str(text or "")
    if not text or estimate_tokens(text) <= max_piece_tokens:
        return [text] if text else []
    pieces = [match.group(0) for match in _SENTENCE_BOUNDARY.finditer(text)]
    if len(pieces) <= 1:
        pieces = [text]
    result: list[str] = []
    for piece in pieces:
        if estimate_tokens(piece) <= max_piece_tokens:
            result.append(piece)
            continue
        remaining = piece
        while remaining:
            head = _hard_slice(
                remaining, estimate_tokens=estimate_tokens, max_piece_tokens=max_piece_tokens
            )
            result.append(head)
            remaining = remaining[len(head) :]
    return result


def _chunk_oversized_unit(
    unit: EvidenceUnit,
    *,
    estimate_tokens: Callable[[str], int],
    max_piece_tokens: int,
) -> list[EvidenceUnit]:
    """Semantic-chunk one evidence unit that cannot fit a dispatch by itself.

    Provenance is preserved: every chunk keeps the original source ids and
    records ``evidence_id``, ``chunk_index``, ``chunk_count`` and its original
    character range, so chunk results can merge back into the original row.
    """

    pieces = _split_text_pieces(
        unit.text, estimate_tokens=estimate_tokens, max_piece_tokens=max_piece_tokens
    )
    if not pieces:
        return []
    chunks: list[EvidenceUnit] = []
    buffer: list[tuple[int, str]] = []
    buffer_tokens = 0

    def flush() -> None:
        nonlocal buffer, buffer_tokens
        if not buffer:
            return
        start = buffer[0][0]
        end = buffer[-1][0] + len(buffer[-1][1])
        chunk_text = "".join(piece for _, piece in buffer)
        index = len(chunks)
        chunks.append(
            EvidenceUnit(
                id=f"{unit.id}#c{index}",
                persona_id=unit.persona_id,
                source_id=unit.source_id,
                source_locator=dict(unit.source_locator),
                speaker=unit.speaker,
                speaker_role=unit.speaker_role,
                timestamp=unit.timestamp,
                text=chunk_text,
                normalized_text=normalize_evidence_text(chunk_text),
                evidence_type=unit.evidence_type,
                dimension_candidates=list(unit.dimension_candidates),
                dimension_scores=dict(unit.dimension_scores),
                life_stage_candidates=list(unit.life_stage_candidates),
                relationship_entities=list(unit.relationship_entities),
                context_tags=list(unit.context_tags),
                confidence=unit.confidence,
                extraction_method="semantic_chunk",
                source_kind=unit.source_kind,
                event_time=unit.event_time,
                metadata={
                    **unit.metadata,
                    "evidence_chunk": {
                        "evidence_id": unit.id,
                        "chunk_index": index,
                        "char_range": [start, end],
                    },
                },
            )
        )
        buffer = []
        buffer_tokens = 0

    offset = 0
    for piece in pieces:
        piece_tokens = estimate_tokens(piece)
        if buffer and buffer_tokens + piece_tokens > max_piece_tokens:
            flush()
        buffer.append((offset, piece))
        buffer_tokens += piece_tokens
        offset += len(piece)
    flush()
    for chunk in chunks:
        chunk.metadata["evidence_chunk"]["chunk_count"] = len(chunks)
    return chunks


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        match = re.search(r"\b((?:19|20)\d{2})(?:[-/]([01]?\d))?(?:[-/]([0-3]?\d))?", raw)
        if not match:
            return None
        year = safe_int(match.group(1), default=None, minimum=1)
        month = safe_int(match.group(2) or 1, default=1, minimum=1, maximum=12) or 1
        day = safe_int(match.group(3) or 1, default=1, minimum=1, maximum=31) or 1
        if year is None:
            return None
        try:
            return datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            return None


def _token_set(text: str) -> set[str]:
    normalized = normalize_evidence_text(text)
    tokens = {
        token for token in re.findall(r"[a-z0-9_]+", normalized, flags=re.IGNORECASE) if token
    }
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _jaccard(left: str, right: str) -> float:
    normalized_left = normalize_evidence_text(left)
    normalized_right = normalize_evidence_text(right)
    a, b = _token_set(normalized_left), _token_set(normalized_right)
    if not a or not b:
        return 0.0
    return max(
        len(a & b) / len(a | b),
        difflib.SequenceMatcher(None, normalized_left, normalized_right).ratio(),
    )


def _contains_similarity(left: str, right: str) -> float:
    a, b = normalize_evidence_text(left), normalize_evidence_text(right)
    if not a or not b:
        return 0.0
    if any("\u3400" <= char <= "\u9fff" for char in a + b):
        a, b = a.replace(" ", ""), b.replace(" ", "")
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    minimum = 6 if any("\u3400" <= char <= "\u9fff" for char in shorter) else 32
    return 1.0 if shorter in longer and len(shorter) >= minimum else 0.0


def _dimension_scores(text: str, metadata: dict[str, Any] | None = None) -> dict[str, float]:
    lower = str(text).casefold()
    explicit = metadata or {}
    scores: dict[str, float] = {dimension: 0.0 for dimension in REQUIRED_DIMENSIONS}
    terms: dict[str, tuple[str, ...]] = {
        "identity_and_timeline": (
            "born",
            "grew",
            "childhood",
            "identity",
            "timeline",
            "event",
            "经历",
            "出生",
            "后来",
            "之后",
        ),
        "works_and_views": ("book", "work", "product", "作品", "观点", "理念", "项目"),
        "interviews_and_dialogue": ("said", "told", "interview", "说", "回答", "采访"),
        "expression_dna": ("phrase", "word", "tone", "often says", "用词", "口头禅", "表达"),
        "decisions_and_behavior": (
            "decided",
            "chose",
            "refused",
            "failed",
            "决定",
            "选择",
            "拒绝",
            "失败",
        ),
        "third_party_views": (
            "according to",
            "described",
            "critic",
            "评价",
            "他人",
            "传记",
            "批评",
        ),
        "affect_relationship_defense": (
            "friend",
            "family",
            "rival",
            "relationship",
            "朋友",
            "家人",
            "关系",
            "冲突",
        ),
        "values_desires_contradictions": (
            "value",
            "want",
            "fear",
            "contradict",
            "价值",
            "渴望",
            "害怕",
            "矛盾",
        ),
    }
    declared = set(str(item) for item in explicit.get("dimensions") or [])
    declared_item = str(explicit.get("dimension") or "")
    if declared_item:
        declared.add(declared_item)
    for dimension, keywords in terms.items():
        if dimension in declared:
            scores[dimension] = 1.0
        else:
            hits = sum(1 for term in keywords if term.casefold() in lower)
            scores[dimension] = min(1.0, hits / 3.0)
    return {key: value for key, value in scores.items() if value > 0}


def _evidence_type(text: str, metadata: dict[str, Any]) -> str:
    lower = str(text).casefold()
    if metadata.get("source_kind") in {"chat", "chat_import", "guided_interview"} or metadata.get(
        "source_type"
    ) in {"json", "jsonl", "csv", "guided_interview"}:
        return "expression_sample" if len(text) < 360 else "behavioral_observation"
    if any(token in lower for token in ("said", "told", "说", "采访", "interview")):
        return "self_report" if metadata.get("is_self_report", True) else "third_party_report"
    if any(token in lower for token in ("decided", "chose", "refused", "决定", "选择", "拒绝")):
        return "decision_example"
    if any(token in lower for token in ("friend", "family", "rival", "朋友", "家人", "关系")):
        return "relationship_context"
    return "behavioral_observation"


@dataclass(frozen=True)
class _RetrievalSnapshot:
    """What ranking reads: semantic fused claims and the units they cite."""

    fused: list[FusedEvidence]
    distinct_units: dict[str, EvidenceUnit]
    intelligence: dict[str, Any]


_SQL_ID_CHUNK = 900


class PersonaEvidenceIndex:
    """Full-corpus retrieval over derived evidence, not a last-N source slice.

    One instance caches its retrieval snapshot, so a caller ranking several
    dimensions in one pass should reuse the same instance, and create a new one
    once evidence may have changed (the snapshot is never invalidated).
    """

    # Every read goes through ``Database.reader()``: retrieval runs on worker
    # threads, and the event-loop connection must not be shared with them.
    def __init__(self, database: Any, persona_id: str) -> None:
        self.database = database
        self.persona_id = persona_id
        self._snapshot: _RetrievalSnapshot | None = None
        self._all_units: list[EvidenceUnit] | None = None
        # Dimensions retrieve concurrently from worker threads; load once.
        self._load_lock = threading.Lock()

    def _rows_by_rowid(self, rowids: Sequence[int]) -> Iterator[Any]:
        # rowid lookups stay fast where ``id IN (...)`` chunks did not (~30x).
        ordered = sorted(rowids)
        for start in range(0, len(ordered), _SQL_ID_CHUNK):
            chunk = ordered[start : start + _SQL_ID_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            yield from self.database.reader().execute(
                f"SELECT * FROM persona_evidence_units WHERE rowid IN ({placeholders})",
                tuple(chunk),
            )

    def _retrieval_snapshot(self) -> _RetrievalSnapshot:
        """Load only fused claims and the units they reference.

        A chat ledger holds hundreds of thousands of units that no fused claim
        cites.  Materialising all of them on every call blocked the event loop
        for ~20s per dimension on a 385k-unit corpus.
        """

        with self._load_lock:
            if self._snapshot is None:
                self._snapshot = self._load_snapshot()
            return self._snapshot

    def _load_snapshot(self) -> _RetrievalSnapshot:
        fused_all = self.fused()
        supporting_ids = {
            evidence_id for item in fused_all for evidence_id in item.supporting_evidence_ids
        }
        semantic: dict[str, bool] = {}
        intelligence: dict[str, Any] = {}
        rowid_by_id: dict[str, int] = {}
        # One sequential pass over the persona's ledger reads only the small
        # columns; full rows are fetched afterwards for the cited units alone.
        for row in self.database.reader().execute(
            "SELECT rowid, id, metadata_json FROM persona_evidence_units WHERE persona_id = ?",
            (self.persona_id,),
        ):
            unit_id = str(row["id"])
            if unit_id not in supporting_ids:
                continue
            rowid_by_id[unit_id] = int(row["rowid"])
            metadata = MaterialIntelligenceService._row_json(row, "metadata_json", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            semantic[unit_id] = metadata.get("semantic_status") != "context_only"
            found = metadata.get("evidence_intelligence") or metadata.get(
                "source_factual_intelligence"
            )
            if found:
                intelligence[unit_id] = found
        # A claim citing a missing or context-only unit is excluded, exactly as
        # the subset check against all semantic unit ids did before.
        fused = [
            item
            for item in fused_all
            if all(semantic.get(evidence_id, False) for evidence_id in item.supporting_evidence_ids)
        ]
        distinct_ids = {
            evidence_id for item in fused for evidence_id in item.unique_evidence_ids
        }
        distinct_units = {
            str(row["id"]): MaterialIntelligenceService._unit_from_row(row)
            for row in self._rows_by_rowid(
                [rowid_by_id[unit_id] for unit_id in distinct_ids if unit_id in rowid_by_id]
            )
        }
        return _RetrievalSnapshot(
            fused=fused, distinct_units=distinct_units, intelligence=intelligence
        )

    def lookup_semantic(
        self, ids: Iterable[str]
    ) -> tuple[dict[str, EvidenceUnit], dict[str, FusedEvidence]]:
        """Resolve a few referenced ids without materialising the ledger."""

        wanted = sorted({str(value) for value in ids})
        fused = {
            item.id: item for item in self._retrieval_snapshot().fused if item.id in wanted
        }
        units: dict[str, EvidenceUnit] = {}
        for start in range(0, len(wanted), _SQL_ID_CHUNK):
            chunk = wanted[start : start + _SQL_ID_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            for row in self.database.reader().execute(
                f"SELECT * FROM persona_evidence_units "
                f"WHERE persona_id = ? AND id IN ({placeholders})",
                (self.persona_id, *chunk),
            ):
                unit = MaterialIntelligenceService._unit_from_row(row)
                if _is_persona_semantic_unit(unit):
                    units[unit.id] = unit
        return units, fused

    def _units_cached(self) -> list[EvidenceUnit]:
        with self._load_lock:
            if self._all_units is None:
                self._all_units = self.units()
            return self._all_units

    def _rows(self, table: str, order: str = "rowid") -> list[Any]:
        return list(
            self.database.reader().execute(
                f"SELECT * FROM {table} WHERE persona_id = ? ORDER BY {order}",
                (self.persona_id,),
            ).fetchall()
        )

    def units(self) -> list[EvidenceUnit]:
        return [
            MaterialIntelligenceService._unit_from_row(row)
            for row in self._rows("persona_evidence_units")
        ]

    def unit_count(self) -> int:
        row = self.database.reader().execute(
            "SELECT COUNT(*) AS count FROM persona_evidence_units WHERE persona_id = ?",
            (self.persona_id,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def contains_text(self, text: str) -> bool:
        row = self.database.reader().execute(
            "SELECT 1 FROM persona_evidence_units WHERE persona_id = ? AND text = ? LIMIT 1",
            (self.persona_id, text),
        ).fetchone()
        return row is not None

    def fused(self) -> list[FusedEvidence]:
        return [
            MaterialIntelligenceService._fused_from_row(row)
            for row in self._rows("persona_fused_evidence")
        ]

    def contradictions(self) -> list[PersonaContradiction]:
        return [
            MaterialIntelligenceService._contradiction_from_row(row)
            for row in self._rows("persona_contradictions")
        ]

    def episodes(self) -> list[ConversationEpisode]:
        return [
            MaterialIntelligenceService._episode_from_row(row)
            for row in self._rows("persona_conversation_episodes")
        ]

    def retrieve(
        self,
        dimension: str | None = None,
        *,
        relationship: str | None = None,
        life_stage: str | None = None,
        behavior: str | None = None,
        top_k: int | None = 24,
        diversity: bool = True,
    ) -> list[dict[str, Any]]:
        """Rank every unit/fused item, then diversify by source and time."""

        snapshot = self._retrieval_snapshot()
        semantic_only = bool(dimension or not relationship)
        fused = snapshot.fused
        candidates: list[dict[str, Any]] = []
        # Deterministic chat Expression DNA profile (P0-E/P0-F wiring): one
        # derived statistical record surfaces to the expression_dna retrieval
        # path without creating per-statistic EvidenceUnits.  It is labelled
        # statistical so no consumer can mistake frequency for intent.
        style_row = self.database.reader().execute(
            "SELECT profile_json FROM persona_chat_style_profiles WHERE persona_id = ?",
            (self.persona_id,),
        ).fetchone()
        if style_row is not None and (dimension in (None, "expression_dna")):
            try:
                style_profile = loads(str(style_row["profile_json"]))
            except Exception:
                style_profile = None
            if isinstance(style_profile, dict) and style_profile.get("statistics"):
                stats = dict(style_profile.get("statistics") or {})
                summary_lines = [
                    f"target messages={style_profile.get('corpus_size')}",
                    f"turns={stats.get('total_target_turns')}",
                    f"avg_len={stats.get('average_message_length')}",
                    f"voice_ratio={stats.get('voice_message_ratio')}",
                    f"night_ratio={stats.get('night_chat_ratio')}",
                ]
                for row_key in (
                    "platform_emoji",
                    "unicode_emoji",
                    "verbal_tic_candidates",
                    "frequent_line_openings",
                    "frequent_line_endings",
                ):
                    rows = stats.get(row_key) or []
                    values = ",".join(
                        f"{item.get('value')}x{item.get('count')}" for item in rows[:10]
                    )
                    if values:
                        summary_lines.append(f"{row_key}: {values}")
                candidates.append(
                    {
                        "kind": "expression_profile",
                        "id": f"profile_{self.persona_id}",
                        "text": chr(10).join(summary_lines),
                        "source_ids": [],
                        "evidence_ids": list(
                            style_profile.get("representative_evidence_ids") or []
                        ),
                        "dimension_scores": {"expression_dna": 0.9},
                        # Scores are probability-clamped downstream; stay in
                        # range while ranking above per-message fused claims.
                        "score": 0.95,
                        "verbatim_samples": [],
                        "intelligence": {
                            "statistical": True,
                            "method": style_profile.get("method"),
                            "contract": style_profile.get("contract"),
                            "corpus_size": style_profile.get("corpus_size"),
                            "time_range": style_profile.get("time_range"),
                            "note": style_profile.get("statistics", {}).get("interpretation_note"),
                            "profile": style_profile.get("statistics"),
                        },
                    }
                )
        for fused_item in fused:
            score = (
                (
                    safe_probability(fused_item.dimension_scores.get(dimension or ""), default=0.0)
                    or 0.0
                )
                if dimension
                else 0.25
            )
            if dimension and score <= 0:
                continue
            if (
                relationship
                and relationship.casefold()
                not in " ".join(fused_item.relationship_scope).casefold()
            ):
                continue
            if (
                life_stage
                and life_stage not in fused_item.temporal_scope
                and life_stage not in fused_item.conditions
            ):
                continue
            if behavior and behavior.casefold() not in fused_item.canonical_claim.casefold():
                continue
            candidates.append(
                {
                    "kind": "fused",
                    "id": fused_item.id,
                    "text": fused_item.canonical_claim,
                    "source_ids": fused_item.source_ids,
                    "evidence_ids": fused_item.supporting_evidence_ids,
                    "dimension_scores": fused_item.dimension_scores,
                    "score": score + min(0.2, fused_item.confidence * 0.2),
                    "verbatim_samples": fused_item.verbatim_samples,
                    "intelligence": [
                        snapshot.intelligence[evidence_id]
                        for evidence_id in fused_item.supporting_evidence_ids
                        if evidence_id in snapshot.intelligence
                    ][:4],
                }
            )
        # Every unique supporting unit stays retrievable, including the
        # canonical (first) one: dropping it would hide the full unit text
        # behind the fused one-line claim and starve extraction/repair.
        unit_candidates: list[EvidenceUnit]
        if not candidates:
            # No claim matched: rank the whole ledger (small or legacy corpora).
            unit_candidates = [
                item
                for item in self._units_cached()
                if not semantic_only or _is_persona_semantic_unit(item)
            ]
        else:
            # Sorted so tied scores rank identically across processes.
            unit_candidates = [
                snapshot.distinct_units[evidence_id]
                for evidence_id in sorted(snapshot.distinct_units)
            ]
        for unit_item in unit_candidates:
            score = (
                (
                    safe_probability(unit_item.dimension_scores.get(dimension or ""), default=0.0)
                    or 0.0
                )
                if dimension
                else 0.15
            )
            if dimension and score <= 0:
                continue
            if (
                relationship
                and relationship.casefold()
                not in " ".join(unit_item.relationship_entities).casefold()
            ):
                continue
            if life_stage and life_stage not in unit_item.life_stage_candidates:
                continue
            if behavior and behavior.casefold() not in unit_item.text.casefold():
                continue
            candidates.append(
                {
                    "kind": "unit",
                    "id": unit_item.id,
                    "text": unit_item.text,
                    "source_ids": [unit_item.source_id],
                    "evidence_ids": [unit_item.id],
                    "dimension_scores": unit_item.dimension_scores,
                    "score": score + min(0.2, unit_item.confidence * 0.2),
                    "verbatim_samples": [unit_item.text]
                    if unit_item.evidence_type == "expression_sample"
                    else [],
                    "intelligence": unit_item.metadata.get("evidence_intelligence")
                    or unit_item.metadata.get("source_factual_intelligence", {}),
                }
            )
        candidates.sort(
            key=lambda item: (safe_probability(item["score"], default=0.0), len(str(item["text"]))),
            reverse=True,
        )
        result_limit = (
            len(candidates)
            if top_k is None
            else max(1, safe_int(top_k, default=24, minimum=1) or 24)
        )
        if not diversity:
            return candidates[:result_limit]
        selected: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        seen_years: set[str] = set()
        remaining = list(candidates)
        while remaining and len(selected) < result_limit:
            best_index = 0
            best_bonus = -1.0
            for index, candidate in enumerate(remaining):
                source_ids = {str(value) for value in candidate.get("source_ids", [])}
                years = {
                    match.group(1)
                    for match in re.finditer(
                        r"\b((?:19|20)\d{2})\b", str(candidate.get("text", ""))
                    )
                }
                bonus = (0.35 if not source_ids & seen_sources else 0.0) + (
                    0.15 if not years & seen_years else 0.0
                )
                value = float(safe_probability(candidate.get("score"), default=0.0) or 0.0) + bonus
                if value > best_bonus:
                    best_bonus, best_index = value, index
            selected_item = remaining.pop(best_index)
            selected.append(selected_item)
            seen_sources.update(str(value) for value in selected_item.get("source_ids", []))
            seen_years.update(
                match.group(1)
                for match in re.finditer(
                    r"\b((?:19|20)\d{2})\b", str(selected_item.get("text", ""))
                )
            )
        return selected

    def get_evidence_for_dimension(self, dimension: str, top_k: int = 24) -> list[dict[str, Any]]:
        return self.retrieve(dimension, top_k=top_k)

    def get_evidence_for_relationship(
        self, relationship: str, top_k: int = 24
    ) -> list[dict[str, Any]]:
        return self.retrieve(relationship=relationship, top_k=top_k)

    def get_evidence_for_life_stage(self, life_stage: str, top_k: int = 24) -> list[dict[str, Any]]:
        return self.retrieve(life_stage=life_stage, top_k=top_k)

    def get_evidence_for_behavior(self, behavior: str, top_k: int = 24) -> list[dict[str, Any]]:
        return self.retrieve(behavior=behavior, top_k=top_k)

    def get_contradictions(self) -> list[PersonaContradiction]:
        return self.contradictions()

    def get_expression_samples(self, top_k: int = 24) -> list[dict[str, Any]]:
        samples = [item for item in self.units() if item.evidence_type == "expression_sample"]
        return [
            {"id": item.id, "text": item.text, "source_ids": [item.source_id]}
            for item in samples[:top_k]
        ]

    def get_decision_examples(self, top_k: int = 24) -> list[dict[str, Any]]:
        return self.retrieve(behavior="decision", top_k=top_k)


# Jobs predating owner tracking are judged by silence alone.
_LEGACY_MATERIAL_JOB_STALE_SECONDS = 3600


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _material_job_orphaned(job: MaterialAnalysisJob, now: datetime) -> bool:
    pid = safe_int(job.progress.get("worker_pid"), default=None, minimum=1)
    if pid is not None:
        return pid != os.getpid() and not _process_alive(pid)
    try:
        updated = datetime.fromisoformat(job.updated_at)
    except ValueError:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return (now - updated).total_seconds() > _LEGACY_MATERIAL_JOB_STALE_SECONDS


def _is_persona_semantic_unit(unit: EvidenceUnit) -> bool:
    """Ledger context is retained, but must never describe the target identity."""
    return (unit.metadata or {}).get("semantic_status") != "context_only"


class EvidenceSimilarityAnalyzer:
    """Exact and conservative near-duplicate clustering without embeddings."""

    def cluster(self, units: Sequence[EvidenceUnit], persona_id: str) -> list[EvidenceCluster]:
        groups: list[list[EvidenceUnit]] = []
        exact_groups: dict[str, int] = {}
        token_groups: dict[str, set[int]] = defaultdict(set)
        for unit in sorted(filter(_is_persona_semantic_unit, units), key=lambda item: item.id):
            placed = False
            exact_index = exact_groups.get(unit.normalized_text)
            if exact_index is not None:
                groups[exact_index].append(unit)
                continue
            tokens = _token_set(unit.text)
            candidate_indexes: set[int] = set()
            for token in sorted(
                tokens,
                key=lambda value: (
                    0 if token_groups.get(value) else 1,
                    len(token_groups.get(value, ())),
                    value,
                ),
            )[:24]:
                candidate_indexes.update(token_groups.get(token, ()))
                if len(candidate_indexes) >= 96:
                    break
            for group_index in sorted(candidate_indexes)[:96]:
                group = groups[group_index]
                canonical = group[0]
                unit_numbers = re.findall(r"\d+(?:\.\d+)?", unit.normalized_text)
                canonical_numbers = re.findall(r"\d+(?:\.\d+)?", canonical.normalized_text)
                similarity = (
                    0.0
                    if unit_numbers != canonical_numbers
                    else max(
                        _jaccard(unit.text, canonical.text),
                        _contains_similarity(unit.text, canonical.text),
                    )
                )
                if similarity >= 0.82:
                    group.append(unit)
                    placed = True
                    for token in tokens:
                        token_groups[token].add(group_index)
                    break
            if not placed:
                group_index = len(groups)
                groups.append([unit])
                exact_groups[unit.normalized_text] = group_index
                for token in tokens:
                    token_groups[token].add(group_index)
        now = datetime.now(UTC).isoformat()
        result: list[EvidenceCluster] = []
        for group in groups:
            exact = len({unit.normalized_text for unit in group}) == 1
            cluster_type = (
                "exact_duplicate"
                if exact and len(group) > 1
                else ("near_duplicate" if len(group) > 1 else "related_but_distinct")
            )
            result.append(
                EvidenceCluster(
                    id=f"evc_{_sha('|'.join(item.id for item in group))[:16]}",
                    persona_id=persona_id,
                    cluster_type=cluster_type,
                    canonical_evidence_id=group[0].id,
                    member_evidence_ids=[item.id for item in group],
                    similarity_type="exact" if exact else ("near" if len(group) > 1 else "single"),
                    confidence=1.0 if exact else (0.82 if len(group) > 1 else 0.5),
                    created_at=now,
                    updated_at=now,
                )
            )
        return result


class PersonaEvidenceFusionService:
    def fuse(
        self,
        units: Sequence[EvidenceUnit],
        clusters: Sequence[EvidenceCluster],
        contradictions: Sequence[PersonaContradiction],
        persona_id: str,
    ) -> list[FusedEvidence]:
        by_id = {item.id: item for item in units if _is_persona_semantic_unit(item)}

        contradiction_by_evidence: dict[str, list[str]] = defaultdict(list)
        for contradiction in contradictions:
            for evidence_id in contradiction.evidence_ids:
                contradiction_by_evidence[evidence_id].append(contradiction.id)
        fused: list[FusedEvidence] = []
        for cluster in clusters:
            members = [by_id[item] for item in cluster.member_evidence_ids if item in by_id]
            if len(members) < 2:
                continue
            sentences: list[str] = []
            for member in members:
                for sentence in re.split(r"(?<=[。！？.!?])\s+|\n+", member.text.strip()):
                    sentence = sentence.strip()
                    if sentence and normalize_evidence_text(sentence) not in {
                        normalize_evidence_text(item) for item in sentences
                    }:
                        sentences.append(sentence)
            dimension_scores: dict[str, float] = {}
            for member in members:
                for dimension, score in member.dimension_scores.items():
                    dimension_scores[dimension] = max(
                        safe_probability(dimension_scores.get(dimension), default=0.0) or 0.0,
                        safe_probability(score, default=0.0) or 0.0,
                    )
            contradiction_ids = sorted(
                {
                    value
                    for member in members
                    for value in contradiction_by_evidence.get(member.id, [])
                }
            )
            source_ids = sorted({member.source_id for member in members})
            unique_ids = [
                member.id
                for member in members
                if member.id == cluster.canonical_evidence_id
                or member.normalized_text != members[0].normalized_text
            ]
            fused.append(
                FusedEvidence(
                    id=f"evf_{_sha(cluster.id)[:16]}",
                    persona_id=persona_id,
                    canonical_claim=" ".join(sentences),
                    evidence_type=members[0].evidence_type,
                    dimension_scores=dimension_scores,
                    supporting_evidence_ids=[member.id for member in members],
                    unique_evidence_ids=unique_ids,
                    contradiction_ids=contradiction_ids,
                    conditions=sorted({tag for member in members for tag in member.context_tags}),
                    temporal_scope=sorted(
                        {
                            value
                            for member in members
                            for value in ([member.event_time] if member.event_time else [])
                        }
                    ),
                    relationship_scope=sorted(
                        {value for member in members for value in member.relationship_entities}
                    ),
                    confidence=min(
                        1.0,
                        max(member.confidence for member in members)
                        + (0.05 if len(members) > 1 else 0),
                    ),
                    synthesis_method="deterministic_union",
                    verbatim_samples=[
                        member.text
                        for member in members
                        if member.evidence_type == "expression_sample"
                    ][:12],
                    source_ids=source_ids,
                )
            )
        return fused


class PersonaContradictionAnalyzer:
    _NEGATIONS = ("not", "never", "no", "不", "从不", "没有", "拒绝")

    def __init__(self, max_variants_per_cluster: int = 64) -> None:
        self.max_variants_per_cluster = max(2, max_variants_per_cluster)

    def _variants(self, units: Sequence[EvidenceUnit]) -> list[EvidenceUnit]:
        distinct: dict[str, EvidenceUnit] = {}
        for unit in units:
            if _is_persona_semantic_unit(unit):
                previous = distinct.get(unit.normalized_text)
                if previous is None or unit.id < previous.id:
                    distinct[unit.normalized_text] = unit
        return [distinct[key] for key in sorted(distinct)[:self.max_variants_per_cluster]]

    def analyze(
        self,
        units: Sequence[EvidenceUnit],
        persona_id: str,
        candidate_groups: Sequence[CandidateGroup] | None = None,
    ) -> list[PersonaContradiction]:
        result: list[PersonaContradiction] = []
        by_id = {item.id: item for item in units if _is_persona_semantic_unit(item)}
        pairs: list[tuple[str, str]]
        if candidate_groups is None:
            units = self._variants(units)
            pairs = [
                (left.id, right.id)
                for index, left in enumerate(units)
                for right in units[index + 1 :]
            ]
        else:
            pairs = sorted(
                {
                    (left, right) if left < right else (right, left)
                    for group in candidate_groups
                    for ids in [[item.id for item in self._variants(
                        [by_id[key] for key in group.evidence_ids if key in by_id]
                    )]]
                    for index, left in enumerate(ids)
                    for right in ids[index + 1 :]
                    if left in by_id and right in by_id and left != right
                }
            )
        for left_id, right_id in pairs:
            left, right = by_id[left_id], by_id[right_id]
            if left.source_id == right.source_id and left.id == right.id:
                continue
            left_base = re.sub(
                r"\b(?:19|20)\d{2}\b|\d+(?:\.\d+)?", "#", normalize_evidence_text(left.text)
            )
            right_base = re.sub(
                r"\b(?:19|20)\d{2}\b|\d+(?:\.\d+)?", "#", normalize_evidence_text(right.text)
            )
            if not left_base or (left_base != right_base and _jaccard(left_base, right_base) < 0.6):
                continue
            left_negated = any(
                token in normalize_evidence_text(left.text).split() for token in self._NEGATIONS
            )
            right_negated = any(
                token in normalize_evidence_text(right.text).split() for token in self._NEGATIONS
            )
            numbers_left = re.findall(r"\b(?:19|20)\d{2}\b|\d+(?:\.\d+)?", left.text)
            numbers_right = re.findall(r"\b(?:19|20)\d{2}\b|\d+(?:\.\d+)?", right.text)
            if left_negated == right_negated and numbers_left == numbers_right:
                continue
            temporal = bool(
                left.event_time and right.event_time and left.event_time != right.event_time
            )
            contextual = bool(
                set(left.context_tags) != set(right.context_tags)
                or set(left.relationship_entities) != set(right.relationship_entities)
            )
            cross_source_kind = left.source_kind != right.source_kind
            contradiction_type = (
                "TEMPORAL_CHANGE"
                if temporal
                else (
                    "CONTEXT_DEPENDENT"
                    if contextual
                    else ("SELF_VS_THIRD_PARTY" if cross_source_kind else "FACT_CONFLICT")
                )
            )
            result.append(
                PersonaContradiction(
                    id=f"cn_{_sha('|'.join(sorted((left.id, right.id))))[:16]}",
                    persona_id=persona_id,
                    contradiction_type=contradiction_type,
                    evidence_ids=[left.id, right.id],
                    summary=f"Evidence units differ: {left.text[:220]} / {right.text[:220]}",
                    conditions=sorted(
                        set(
                            left.context_tags
                            + right.context_tags
                            + left.relationship_entities
                            + right.relationship_entities
                        )
                    ),
                    resolution="preserve_both_with_scope",
                    confidence=0.75 if temporal or contextual else 0.65,
                )
            )
        return result


# Backward/terminology alias for callers that describe fusion as semantic union.
SemanticEvidenceUnionService = PersonaEvidenceFusionService


class MaterialIntelligenceService:
    """Build and persist the derived private-material evidence layer."""

    def __init__(
        self,
        database: Any,
        personas: Any,
        *,
        classifier: Any | None = None,
        fusion_hook: Any | None = None,
        context_budget_manager: AgentContextBudgetManager | None = None,
        batch_size: int = 1000,
        semantic_gate_mode: str = "full",
        max_llm_concurrency: int = 4,
        in_memory_unit_limit: int = 8000,
        max_source_bytes: int = 10 * 1024 * 1024,
        turn_gap_seconds: int = 90,
        analysis_window_max_units: int | None = None,
        chat_style_profiler_enabled: bool = True,
        contradiction_max_variants_per_cluster: int = 64,
        conversation_episode_gap_seconds: int = 7200,
        analysis_window_max_episodes: int | None = None,
        concurrency_retry_limit: int = 2,
    ) -> None:
        self.database = database
        self.personas = personas
        self.semantic_gate_mode = semantic_gate_mode
        self.max_llm_concurrency = max(1, max_llm_concurrency)
        self.concurrency_retry_limit = max(0, int(concurrency_retry_limit))
        self._material_source_context: dict[str, dict[str, Any]] = {}
        # Large Conversation Pipeline V2 knobs (see config.py).  These change
        # how much the Agent is asked to read; they never change how evidence
        # is stored: every raw EvidenceUnit and its provenance survive any
        # turn/window/gate setting.
        self.turn_gap_seconds = max(0, int(turn_gap_seconds))
        self.analysis_window_max_units = (
            None if analysis_window_max_units is None else max(1, int(analysis_window_max_units))
        )
        # P0.3-B/C: episode-aware window packing.  The episode gap bounds a
        # semantic atomic conversation block; packing may still place many
        # episodes into one dispatch window.  Neither value changes what is
        # stored or billed.
        self.conversation_episode_gap_seconds = max(0, int(conversation_episode_gap_seconds))
        self.analysis_window_max_episodes = (
            None
            if analysis_window_max_episodes is None
            else max(1, int(analysis_window_max_episodes))
        )
        self.chat_style_profiler_enabled = bool(chat_style_profiler_enabled)
        # Optional local/Agent Adapter hooks.  They enrich classification or
        # union wording only; raw text and deterministic provenance survive if
        # a hook is unavailable or fails.
        self.classifier = classifier
        self.fusion_hook = fusion_hook
        self.context_budget_manager = context_budget_manager or AgentContextBudgetManager()
        self.batch_size = max(1, int(batch_size))
        self.in_memory_unit_limit = max(1, int(in_memory_unit_limit))
        self.max_source_bytes = max(1, int(max_source_bytes))
        self.material_reader = StreamingMaterialReader(legacy_json_max_bytes=self.max_source_bytes)
        self.deduplicator = PreLLMDeduplicator()
        self.candidate_index = GlobalCandidateIndex()
        self.similarity = EvidenceSimilarityAnalyzer()
        self.fusion = PersonaEvidenceFusionService()
        self.contradictions = PersonaContradictionAnalyzer(contradiction_max_variants_per_cluster)

    # ---- public lifecycle -------------------------------------------------
    def count_chat_target_units(self, persona_id: str) -> int:
        """Target chat messages for gate-size decisions (P1-C auto policy).

        Pure SQL count used by the per-task Semantic Gate resolver to tell a
        small chat from a large private chat.  It never reads message text.
        """

        row = self.database.conn.execute(
            "SELECT COUNT(*) FROM persona_evidence_units WHERE persona_id = ? "
            "AND source_kind IN ('chat', 'chat_import', 'guided_interview') "
            "AND speaker_role = 'target_persona'",
            (persona_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def create_job(
        self,
        persona_id: str,
        source_ids: Sequence[str] | None = None,
        *,
        runtime_snapshot: dict[str, Any] | None = None,
        incremental: bool = False,
    ) -> MaterialAnalysisJob:
        job = MaterialAnalysisJob(
            id=new_id("pmjob"),
            persona_id=persona_id,
            source_ids=list(source_ids or []),
            runtime_snapshot=dict(runtime_snapshot or {}),
            incremental=incremental,
            # Owner process: lets a later process tell a live job from one
            # orphaned by a restart.
            progress={"worker_pid": os.getpid()},
        )
        self._save_job(job)
        return job

    def get_job(self, job_id: str) -> MaterialAnalysisJob:
        row = self.database.conn.execute(
            "SELECT * FROM persona_material_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"material_job_not_found:{job_id}")
        return self._job_from_row(row)

    def list_jobs(self, persona_id: str | None = None) -> list[MaterialAnalysisJob]:
        if persona_id:
            rows = self.database.conn.execute(
                "SELECT * FROM persona_material_jobs WHERE persona_id = ? ORDER BY created_at DESC",
                (persona_id,),
            ).fetchall()
        else:
            rows = self.database.conn.execute(
                "SELECT * FROM persona_material_jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def analyze_sources(
        self,
        persona_id: str,
        source_ids: Sequence[str] | None = None,
        *,
        runtime_snapshot: dict[str, Any] | None = None,
        job_id: str | None = None,
        incremental: bool = False,
    ) -> MaterialAnalysisJob:
        job = (
            self.get_job(job_id)
            if job_id
            else self.create_job(
                persona_id, source_ids, runtime_snapshot=runtime_snapshot, incremental=incremental
            )
        )
        job.source_ids = list(
            source_ids or [source.id for source in self.personas.get_sources(persona_id)]
        )
        try:
            self._set_job(
                job,
                MaterialJobStatus.PARSING,
                stage="raw_sources",
                source_count=len(job.source_ids),
            )
            sources = [
                source
                for source in self.personas.get_sources(persona_id)
                if source.id in set(job.source_ids)
            ]
            self._set_job(job, MaterialJobStatus.SEGMENTING, stage="segments")
            existing_count = self._count_units(persona_id)
            sources_to_segment = self._sources_to_segment(
                persona_id, sources, incremental=incremental
            )
            if self._use_streaming_analysis(sources_to_segment, existing_count):
                self._segment_sources_batched(persona_id, sources_to_segment, job=job)
                if self._count_units(persona_id) > self.in_memory_unit_limit:
                    return self._finish_persisted_analysis(job, persona_id, incremental=incremental)
                existing_units = self._load_units(persona_id)
                new_units = [
                    item
                    for item in existing_units
                    if item.source_id in {source.id for source in sources_to_segment}
                ]
            else:
                existing_units = self._load_units(persona_id)
                new_units = self._segment_sources(persona_id, sources_to_segment)
            combined_by_id = {item.id: item for item in existing_units}
            combined_by_id.update({item.id: item for item in new_units})
            dedup = self.deduplicator.deduplicate(list(combined_by_id.values()))
            classified_by_id = {
                item.id: self._apply_classifier(item) for item in dedup.canonical_units
            }
            units = [classified_by_id.get(item.id, item) for item in dedup.units]
            units = self._propagate_canonical_intelligence(units, dedup.supporting_units)
            self._set_job(
                job,
                MaterialJobStatus.ANALYZING,
                stage="unit_classification",
                evidence_unit_count=len(units),
            )
            self._persist_units(units)
            all_units = self._load_units(persona_id)
            self._set_job(job, MaterialJobStatus.CLUSTERING, stage="similarity")
            clusters = self.similarity.cluster(all_units, persona_id)
            self._set_job(
                job, MaterialJobStatus.FUSING, stage="semantic_union", cluster_count=len(clusters)
            )
            canonical_ids = {item.id for item in dedup.canonical_units}
            relation_units = [item for item in all_units
                              if item.id in canonical_ids and _is_persona_semantic_unit(item)]
            candidate_groups = self.candidate_index.groups(
                relation_units,
                affected_ids={item.id for item in new_units} if incremental else None,
            )
            contradiction_rows = self.contradictions.analyze(
                all_units, persona_id, candidate_groups
            )
            fused = self.fusion.fuse(all_units, clusters, contradiction_rows, persona_id)
            if self.fusion_hook is not None:
                fused = self._apply_fusion_hook(fused, all_units)
            episodes = self._build_episodes(persona_id, all_units)
            self._replace_derived_atomic(persona_id, clusters, contradiction_rows, fused, episodes)
            self._set_job(
                job,
                MaterialJobStatus.INDEXING,
                stage="persona_evidence_index",
                fused_count=len(fused),
            )
            coverage = self.coverage(persona_id)
            self._set_job(
                job,
                MaterialJobStatus.GAP_ANALYSIS,
                stage="coverage_gate",
                coverage=coverage.model_dump(mode="json"),
            )
            self._set_job(
                job,
                MaterialJobStatus.READY_FOR_COMPILATION,
                stage="ready",
                coverage=coverage.model_dump(mode="json"),
            )
        except Exception as exc:
            job.error = str(exc)
            self._set_job(job, MaterialJobStatus.FAILED, stage="failed", error=str(exc))
            raise
        return job

    async def process_job(self, job_id: str) -> MaterialAnalysisJob:
        job = self.get_job(job_id)
        return await self.analyze_sources_async(
            job.persona_id,
            job.source_ids,
            runtime_snapshot=job.runtime_snapshot,
            job_id=job.id,
            incremental=job.incremental,
        )

    async def analyze_sources_async(
        self,
        persona_id: str,
        source_ids: Sequence[str] | None = None,
        *,
        runtime_snapshot: dict[str, Any] | None = None,
        job_id: str | None = None,
        incremental: bool = False,
        agent_analyzer: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
        agent_phases: Sequence[str] | None = None,
        shared_factual_cache: bool = False,
        progress_callback: Callable[[MaterialAnalysisJob], Awaitable[None]] | None = None,
        semantic_gate_mode: str | None = None,
        runtime_identity: dict[str, Any] | None = None,
    ) -> MaterialAnalysisJob:
        """Analyze material without running CPU-heavy stages on the event loop.

        Agent analysis is additive and schema constrained: it may classify
        evidence or improve fused wording, but it cannot change source ids,
        locators, verbatim evidence, or supporting provenance.
        ``semantic_gate_mode`` (P1-C) overrides the service-level Semantic
        Gate mode for this task; "auto" must already be resolved by the
        caller.
        """

        if semantic_gate_mode is not None and semantic_gate_mode not in {
            "full",
            "balanced",
            "fast",
        }:
            raise ValueError(f"Unknown semantic gate mode: {semantic_gate_mode}")

        job = (
            self.get_job(job_id)
            if job_id
            else self.create_job(
                persona_id,
                source_ids,
                runtime_snapshot=runtime_snapshot,
                incremental=incremental,
            )
        )
        job.source_ids = list(
            source_ids or [source.id for source in self.personas.get_sources(persona_id)]
        )

        async def stage(status: MaterialJobStatus, **progress: Any) -> None:
            self._set_job(job, status, **progress)
            if progress_callback is not None:
                await progress_callback(job)

        async def report_window_progress(info: dict[str, Any]) -> None:
            # Window-level progress keeps the 40% Material Classification stage
            # visibly moving instead of freezing at the stage floor percent.
            job.progress = {
                **job.progress,
                **info,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            self._save_job(job)
            if progress_callback is not None:
                await progress_callback(job)

        try:
            await stage(
                MaterialJobStatus.PARSING,
                stage="raw_sources",
                source_count=len(job.source_ids),
            )
            selected = set(job.source_ids)
            sources = [
                source for source in self.personas.get_sources(persona_id) if source.id in selected
            ]
            snapshot = dict(runtime_snapshot or {})
            if runtime_identity and not snapshot.get("runtime_identity"):
                # Local-only injection: the credential identity hash stays out
                # of the persisted job snapshot.
                snapshot["runtime_identity"] = dict(runtime_identity)
            snapshot.setdefault("workload_context_scope", "per_window")
            capability, capability_identity = self._resolve_independent_session_capability(
                snapshot
            )
            snapshot["independent_session_capability"] = capability.as_dict()
            if capability.verified and capability.effective_limit > 1:
                # A persistently verified capability may raise the width above
                # the adapter default; an unverified one never guesses 2/4.
                snapshot["parallel_independent_sessions"] = True
                snapshot["max_parallel_independent_sessions"] = capability.effective_limit
            profile = ResolvedExecutionProfile.resolve(
                snapshot,
                fallback_context_window=self.context_budget_manager.default_context_window_tokens,
            )
            if not profile.workload_context_scope or profile.workload_context_scope == "unknown":
                profile.workload_context_scope = "per_window"
            await stage(MaterialJobStatus.SEGMENTING, stage="segments")
            existing_count = await asyncio.to_thread(self._count_units, persona_id)
            sources_to_segment = self._sources_to_segment(
                persona_id, sources, incremental=incremental
            )
            if self._use_streaming_analysis(sources_to_segment, existing_count):
                await asyncio.to_thread(
                    self._segment_sources_batched, persona_id, sources_to_segment, job
                )
                if self._count_units(persona_id) > self.in_memory_unit_limit:
                    enabled_agent_phases = set(agent_phases or ("classify", "relate", "fuse"))
                    large_metrics = MaterialPipelineMetrics(
                        raw_evidence_units=self._count_units(persona_id),
                        unique_evidence_units=self._count_units(persona_id),
                        context_window=profile.context_window,
                        context_window_source=profile.context_window_source,
                        context_verified=profile.context_verified,
                        planning_context_window=profile.planning_context_window,
                        usable_context_budget=profile.usable_context_budget,
                        preferred_working_context=profile.preferred_working_context,
                    )
                    large_metrics.stamp_execution_profile(
                        profile, configured_max=self.max_llm_concurrency
                    )
                    if agent_analyzer is not None and "classify" in enabled_agent_phases:
                        await stage(
                            MaterialJobStatus.ANALYZING,
                            stage="evidence_intelligence",
                            evidence_unit_count=large_metrics.raw_evidence_units,
                        )
                        await self._classify_persisted_with_agent(
                            persona_id,
                            agent_analyzer,
                            execution_profile=profile,
                            metrics=large_metrics,
                            window_progress=report_window_progress,
                            semantic_gate_mode=semantic_gate_mode,
                            runtime_identity=capability_identity,
                        )
                    # Deterministic Expression DNA lane (P0-E): full target
                    # corpus statistics, zero model calls, survives any later
                    # Semantic Gate because it never depends on Agent output.
                    large_metrics.style_profile_status = await asyncio.to_thread(
                        self._run_chat_style_profiler, persona_id
                    )
                    job.progress["performance_metrics"] = large_metrics.model_dump(mode="json")
                    job.progress["chat_pipeline"] = self._chat_pipeline_progress(large_metrics)
                    result = await asyncio.to_thread(
                        self._finish_persisted_analysis,
                        job,
                        persona_id,
                        incremental,
                    )
                    if progress_callback is not None:
                        await progress_callback(result)
                    return result
                existing_units = await asyncio.to_thread(self._load_units, persona_id)
                existing_by_id = {item.id: item for item in existing_units}
                new_units = [
                    item
                    for item in existing_units
                    if item.source_id in {source.id for source in sources_to_segment}
                ]
            else:
                existing_units = await asyncio.to_thread(self._load_units, persona_id)
                existing_by_id = {item.id: item for item in existing_units}
                new_units = await asyncio.to_thread(
                    self._segment_sources, persona_id, sources_to_segment
                )
            combined_by_id = {item.id: item for item in existing_units}
            persisted_cache_hits: set[str] = set()
            for item in new_units:
                previous = existing_by_id.get(item.id)
                if previous is not None and self._has_classification(previous, profile):
                    combined_by_id[item.id] = previous
                    persisted_cache_hits.add(item.id)
                else:
                    combined_by_id[item.id] = item
            dedup = await asyncio.to_thread(
                self.deduplicator.deduplicate, list(combined_by_id.values())
            )
            units = list(dedup.units)
            new_ids = {item.id for item in new_units}
            canonical_to_analyze = [
                item
                for item in dedup.canonical_units
                if not self._has_classification(item, profile) or self._is_non_semantic_unit(item)
            ]
            canonical_to_analyze = [self._apply_classifier(item) for item in canonical_to_analyze]
            classifier_by_id = {item.id: item for item in canonical_to_analyze}
            units = [classifier_by_id.get(item.id, item) for item in units]
            metrics = MaterialPipelineMetrics(
                raw_evidence_units=len(units),
                unique_evidence_units=len(dedup.canonical_units),
                duplicate_evidence_units=max(0, len(units) - len(dedup.canonical_units)),
                context_window=profile.context_window,
                context_window_source=profile.context_window_source,
                context_verified=profile.context_verified,
                planning_context_window=profile.planning_context_window,
                usable_context_budget=profile.usable_context_budget,
                preferred_working_context=profile.preferred_working_context,
                cache_hits={"intelligence": len(persisted_cache_hits)},
            )
            metrics.stamp_execution_profile(profile, configured_max=self.max_llm_concurrency)
            # raw_message_count comes from the full ledger; target/context
            # counters arrive through the turn delta below so they are
            # never counted twice.
            for item in units:
                if item.source_kind in CHAT_SOURCE_KINDS:
                    metrics.raw_message_count += 1
            # target/context counters arrive through the turn delta in
            # _classify_with_agent, so they are never counted twice here.
            await stage(
                MaterialJobStatus.ANALYZING,
                stage="evidence_intelligence",
                evidence_unit_count=len(units),
                unique_evidence_unit_count=len(dedup.canonical_units),
            )
            enabled_agent_phases = set(agent_phases or ("classify", "relate", "fuse"))
            if (
                agent_analyzer is not None
                and canonical_to_analyze
                and "classify" in enabled_agent_phases
            ):
                cache_namespace = None
                if shared_factual_cache:
                    cache_namespace = json.dumps(
                        {
                            "schema": "source-intelligence-v1",
                            "model": (runtime_snapshot or {}).get("effective_model")
                            or (runtime_snapshot or {}).get("model_id"),
                            "reasoning": (runtime_snapshot or {}).get("effective_reasoning")
                            or (runtime_snapshot or {}).get("reasoning_effort"),
                        },
                        sort_keys=True,
                        default=str,
                    )
                classified = await self._classify_with_agent(
                    canonical_to_analyze,
                    agent_analyzer,
                    cache_namespace=cache_namespace,
                    execution_profile=profile,
                    metrics=metrics,
                    window_progress=report_window_progress,
                    gate_mode=semantic_gate_mode,
                )
                classified_by_id = {item.id: item for item in classified}
                for evidence_id, item in list(classified_by_id.items()):
                    metadata = dict(item.metadata)
                    metadata["intelligence_cache_key"] = self._material_intelligence_cache_key(
                        item, profile
                    )
                    classified_by_id[evidence_id] = item.model_copy(update={"metadata": metadata})
                units = [classified_by_id.get(item.id, item) for item in units]
                units = self._propagate_canonical_intelligence(units, dedup.supporting_units)
                job.progress["agent_classification"] = "completed"
            chat_counts = self._semantic_turn_counts(units)
            job.progress["classification_total"] = chat_counts["total"]
            job.progress["classification_completed"] = chat_counts["completed"]
            job.progress["classification_pending"] = chat_counts["pending"]
            units = self._propagate_canonical_intelligence(units, dedup.supporting_units)
            # Writes stay on the event-loop connection; the reload of a large
            # ledger is the slow part and reads on a worker-private connection.
            self._persist_units(units)
            all_units = await asyncio.to_thread(self._load_units, persona_id)
            canonical_ids = {item.id for item in dedup.canonical_units}
            relation_units = [item for item in all_units
                              if item.id in canonical_ids and _is_persona_semantic_unit(item)]

            metrics.style_profile_status = await asyncio.to_thread(
                self._run_chat_style_profiler, persona_id
            )
            await stage(MaterialJobStatus.CLUSTERING, stage="similarity")
            clusters = await asyncio.to_thread(self.similarity.cluster, all_units, persona_id)
            affected_seed_ids = new_ids if incremental else {item.id for item in all_units}
            candidate_groups = await asyncio.to_thread(
                self.candidate_index.groups,
                relation_units,
                affected_ids=affected_seed_ids if incremental else None,
            )
            contradictions = await asyncio.to_thread(
                self.contradictions.analyze,
                all_units,
                persona_id,
                candidate_groups,
            )
            candidate_evidence_ids = {
                evidence_id for group in candidate_groups for evidence_id in group.evidence_ids
            }
            affected = AffectedSet(
                new_evidence_ids=sorted(new_ids),
                neighbor_evidence_ids=sorted(candidate_evidence_ids - new_ids),
                affected_cluster_ids=sorted(
                    cluster.id
                    for cluster in clusters
                    if set(cluster.member_evidence_ids).intersection(candidate_evidence_ids)
                ),
                affected_dimensions=sorted(
                    {
                        dimension
                        for item in all_units
                        if item.id in candidate_evidence_ids or item.id in new_ids
                        for dimension in item.dimension_candidates
                    }
                ),
            )
            metrics.relation_candidate_groups = len(candidate_groups)
            metrics.relation_candidate_evidence = len(candidate_evidence_ids)
            metrics.incremental_affected_units = len(new_ids | candidate_evidence_ids)
            if agent_analyzer is not None and candidate_groups and "relate" in enabled_agent_phases:
                clusters, semantic_contradictions = await self._relate_with_agent(
                    all_units,
                    clusters,
                    agent_analyzer,
                    candidate_groups=candidate_groups,
                    execution_profile=profile,
                    metrics=metrics,
                )
                existing_contradictions = {item.id: item for item in contradictions}
                existing_contradictions.update({item.id: item for item in semantic_contradictions})
                contradictions = list(existing_contradictions.values())
                job.progress["agent_relations"] = "completed"
            await stage(
                MaterialJobStatus.FUSING,
                stage="semantic_union",
                cluster_count=len(clusters),
            )
            fused = await asyncio.to_thread(
                self.fusion.fuse, all_units, clusters, contradictions, persona_id
            )
            cluster_by_fused_id = {f"evf_{_sha(cluster.id)[:16]}": cluster for cluster in clusters}
            model_fusion_ids: set[str] = set()
            for fused_item in fused:
                cluster = cluster_by_fused_id.get(fused_item.id)
                needs_semantic_fusion = bool(
                    len(fused_item.supporting_evidence_ids) > 1
                    and cluster is not None
                    and (
                        cluster.similarity_type not in {"exact", "single"}
                        or fused_item.contradiction_ids
                    )
                )
                if needs_semantic_fusion and (
                    not incremental
                    or set(fused_item.supporting_evidence_ids).intersection(
                        new_ids | candidate_evidence_ids
                    )
                ):
                    model_fusion_ids.add(fused_item.id)
            metrics.deterministic_fusions = sum(
                1 for item in fused if item.id not in model_fusion_ids
            )
            metrics.model_assisted_fusions = len(model_fusion_ids)
            if agent_analyzer is not None and model_fusion_ids and "fuse" in enabled_agent_phases:
                fused = await self._fuse_with_agent(
                    fused,
                    all_units,
                    agent_analyzer,
                    selected_ids=model_fusion_ids,
                    execution_profile=profile,
                    metrics=metrics,
                )
                job.progress["agent_fusion"] = "completed"
            episodes = await asyncio.to_thread(self._build_episodes, persona_id, all_units)
            self._replace_derived_atomic(persona_id, clusters, contradictions, fused, episodes)
            await stage(
                MaterialJobStatus.INDEXING,
                stage="persona_evidence_index",
                fused_count=len(fused),
                affected_set=affected.model_dump(mode="json"),
                performance_metrics=metrics.model_dump(mode="json"),
                chat_pipeline=self._chat_pipeline_progress(metrics),
            )
            coverage = await asyncio.to_thread(self.coverage, persona_id)
            await stage(
                MaterialJobStatus.GAP_ANALYSIS,
                stage="coverage_gate",
                coverage=coverage.model_dump(mode="json"),
            )
            await stage(
                MaterialJobStatus.READY_FOR_COMPILATION,
                stage="ready",
                coverage=coverage.model_dump(mode="json"),
            )
        except Exception as exc:
            job.error = str(exc)
            await stage(
                MaterialJobStatus.FAILED,
                stage="failed",
                error=str(exc),
                prompt_state=MaterialPromptState.FAILED.value,
            )
            raise
        return job

    def resume_pending_jobs(self) -> list[str]:
        rows = self.database.conn.execute(
            "SELECT id FROM persona_material_jobs WHERE status NOT IN (?, ?) ORDER BY created_at",
            (MaterialJobStatus.READY_FOR_COMPILATION.value, MaterialJobStatus.FAILED.value),
        ).fetchall()
        resumed: list[str] = []
        for row in rows:
            try:
                self.process_job_sync(str(row["id"]))
                resumed.append(str(row["id"]))
            except Exception:
                continue
        return resumed

    def process_job_sync(self, job_id: str) -> MaterialAnalysisJob:
        job = self.get_job(job_id)
        return self.analyze_sources(
            job.persona_id,
            job.source_ids,
            runtime_snapshot=job.runtime_snapshot,
            job_id=job.id,
            incremental=job.incremental,
        )

    def get_index(self, persona_id: str) -> PersonaEvidenceIndex:
        return PersonaEvidenceIndex(self.database, persona_id)

    def coverage(self, persona_id: str) -> PrivateMaterialCoverage:
        # Called from worker threads as well as the event loop.
        conn = self.database.reader()

        def _count(sql: str, params: tuple[Any, ...] = ()) -> int:
            row = conn.execute(sql, params).fetchone()
            return int(row[0] if row is not None else 0)

        unit_count = _count(
            "SELECT COUNT(*) FROM persona_evidence_units WHERE persona_id = ?", (persona_id,)
        )
        source_count = _count(
            "SELECT COUNT(DISTINCT source_id) FROM persona_evidence_units WHERE persona_id = ?",
            (persona_id,),
        )
        message_count = _count(
            "SELECT COUNT(*) FROM persona_evidence_units WHERE persona_id = ? "
            "AND source_kind IN ('chat', 'chat_import', 'guided_interview')",
            (persona_id,),
        )
        episode_count = _count(
            "SELECT COUNT(*) FROM persona_conversation_episodes WHERE persona_id = ?",
            (persona_id,),
        )
        fused_count = _count(
            "SELECT COUNT(*) FROM persona_fused_evidence WHERE persona_id = ?", (persona_id,)
        )
        contradiction_count = _count(
            "SELECT COUNT(*) FROM persona_contradictions WHERE persona_id = ?", (persona_id,)
        )
        behavior_example_count = _count(
            "SELECT COUNT(*) FROM persona_evidence_units WHERE persona_id = ? "
            "AND evidence_type = 'behavioral_observation'",
            (persona_id,),
        )
        expression_sample_count = _count(
            "SELECT COUNT(*) FROM persona_evidence_units WHERE persona_id = ? "
            "AND evidence_type = 'expression_sample'",
            (persona_id,),
        )
        decision_example_count = _count(
            "SELECT COUNT(*) FROM persona_evidence_units WHERE persona_id = ? "
            "AND evidence_type = 'decision_example'",
            (persona_id,),
        )
        life_event_count = _count(
            "SELECT COUNT(*) FROM persona_evidence_units WHERE persona_id = ? "
            "AND event_time IS NOT NULL AND event_time != ''",
            (persona_id,),
        )
        relationship_row = conn.execute(
            "SELECT COUNT(DISTINCT json_each.value) AS count "
            "FROM persona_evidence_units, json_each(relationship_entities_json) "
            "WHERE persona_id = ? AND json_each.value IS NOT NULL AND json_each.value != ''",
            (persona_id,),
        ).fetchone()
        relationship_context_count = int(relationship_row["count"] if relationship_row else 0)
        dimension_rows = conn.execute(
            "SELECT json_each.key AS dimension, COUNT(*) AS count "
            "FROM persona_evidence_units, json_each(dimension_scores_json) "
            "WHERE persona_id = ? AND CAST(json_each.value AS REAL) > 0 "
            "AND COALESCE(json_extract(metadata_json, '$.semantic_status'), '') != 'context_only' "
            "GROUP BY json_each.key",
            (persona_id,),
        ).fetchall()
        dimensions = {dimension: 0 for dimension in REQUIRED_DIMENSIONS}
        for row in dimension_rows:
            key = str(row["dimension"])
            if key in dimensions:
                dimensions[key] = int(row["count"])
        min_dt: datetime | None = None
        max_dt: datetime | None = None
        stamp_cursor = conn.execute(
            "SELECT timestamp, event_time FROM persona_evidence_units WHERE persona_id = ?",
            (persona_id,),
        )
        for row in stamp_cursor:
            parsed = _parse_timestamp(row["event_time"] or row["timestamp"])
            if parsed is None:
                continue
            if min_dt is None or parsed < min_dt:
                min_dt = parsed
            if max_dt is None or parsed > max_dt:
                max_dt = parsed
        span = (
            (max_dt - min_dt).days
            if min_dt is not None and max_dt is not None and max_dt != min_dt
            else 0
        )
        gaps: list[str] = [dimension for dimension, count in dimensions.items() if count == 0]
        if episode_count == 0:
            gaps.append("conversation_episodes")
        expression_sources = _count(
            "SELECT COUNT(DISTINCT source_id) FROM persona_evidence_units "
            "WHERE persona_id = ? AND evidence_type = 'expression_sample'",
            (persona_id,),
        )
        if expression_sources == 0:
            gaps.append("expression_samples")
        if contradiction_count == 0:
            gaps.append("contradiction_or_change_evidence")
        return PrivateMaterialCoverage(
            source_count=source_count,
            evidence_unit_count=unit_count,
            message_count=message_count,
            conversation_time_span_days=max(0, span),
            episode_count=episode_count,
            behavior_example_count=behavior_example_count,
            relationship_context_count=relationship_context_count,
            life_event_count=life_event_count,
            expression_sample_count=expression_sample_count,
            decision_example_count=decision_example_count,
            contradiction_count=contradiction_count,
            fused_evidence_count=fused_count,
            dimension_coverage=dimensions,
            high_priority_gaps=gaps,
        )

    def gap_analysis(self, persona_id: str) -> dict[str, Any]:
        coverage = self.coverage(persona_id)
        questions = {
            "identity_and_timeline": "有哪些重要人生阶段或事件尚未记录？",
            "works_and_views": "哪些作品、观点或长期变化还缺乏具体材料？",
            "interviews_and_dialogue": "有哪些原话或对话样本可以补充？",
            "expression_dna": "这个人经常使用或刻意避免哪些词语？",
            "decisions_and_behavior": "请提供一次压力下做决定或失败后的具体经历。",
            "third_party_views": "是否有不同关系的人对其行为的可靠描述？",
            "affect_relationship_defense": "与哪些人的关系最能体现其情绪和防御模式？",
            "values_desires_contradictions": "在哪些情境下表现出相互冲突的价值或欲望？",
        }
        dimension_gaps = [gap for gap in coverage.high_priority_gaps if gap in REQUIRED_DIMENSIONS]
        evidence_gaps = [
            gap for gap in coverage.high_priority_gaps if gap not in REQUIRED_DIMENSIONS
        ]
        return {
            "coverage": coverage.model_dump(mode="json"),
            "gaps": [
                {"dimension": dimension, "question": questions[dimension]}
                for dimension in dimension_gaps
            ],
            "evidence_gaps": evidence_gaps,
        }

    def add_identity_alias(
        self,
        persona_id: str,
        alias: str,
        *,
        source_ids: Sequence[str] = (),
        confidence: float = 0.5,
        status: str = "suggested",
    ) -> PersonaIdentityAlias:
        normalized_confidence = safe_probability(confidence, default=0.5)
        item = PersonaIdentityAlias(
            id=f"alias_{_sha(persona_id + '|' + normalize_evidence_text(alias))[:16]}",
            persona_id=persona_id,
            alias=str(alias).strip(),
            normalized_alias=normalize_evidence_text(alias),
            source_ids=list(source_ids),
            confidence=(normalized_confidence if normalized_confidence is not None else 0.5),
            status=status,
        )
        self.database.conn.execute(
            "INSERT OR REPLACE INTO persona_identity_aliases "
            "(id, persona_id, alias, normalized_alias, confidence, status, "
            "source_ids_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.persona_id,
                item.alias,
                item.normalized_alias,
                item.confidence,
                item.status,
                dumps(item.source_ids),
                item.created_at,
            ),
        )
        self.database.conn.commit()
        return item

    def resolve_identity_alias(self, persona_id: str, alias: str) -> PersonaIdentityAlias | None:
        row = self.database.conn.execute(
            "SELECT * FROM persona_identity_aliases WHERE persona_id = ? AND normalized_alias = ?",
            (persona_id, normalize_evidence_text(alias)),
        ).fetchone()
        return PersonaIdentityAlias(**self._json_alias_row(row)) if row else None

    # ---- segmentation ----------------------------------------------------
    def _count_units(self, persona_id: str) -> int:
        row = self.database.conn.execute(
            "SELECT COUNT(*) AS count FROM persona_evidence_units WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def _source_ids_with_units(self, persona_id: str) -> set[str]:
        rows = self.database.conn.execute(
            "SELECT DISTINCT source_id FROM persona_evidence_units WHERE persona_id = ?",
            (persona_id,),
        ).fetchall()
        return {str(row["source_id"]) for row in rows}

    def _sources_to_segment(
        self, persona_id: str, sources: Sequence[Any], *, incremental: bool
    ) -> list[Any]:
        oversized = self._source_ids_with_oversized_units(persona_id)
        if oversized:
            self._delete_units_for_sources(persona_id, oversized)
        if not incremental:
            return list(sources)
        existing_source_ids = self._source_ids_with_units(persona_id)
        return [
            source
            for source in sources
            if source.id not in existing_source_ids or source.id in oversized
        ]

    def _source_ids_with_oversized_units(self, persona_id: str) -> set[str]:
        rows = self.database.conn.execute(
            "SELECT DISTINCT source_id FROM persona_evidence_units "
            "WHERE persona_id = ? AND length(text) > ?",
            (persona_id, 16000),
        ).fetchall()
        return {str(row["source_id"]) for row in rows}

    def _delete_units_for_sources(self, persona_id: str, source_ids: set[str]) -> None:
        if not source_ids:
            return
        placeholders = ",".join("?" for _ in source_ids)
        params = (persona_id, *source_ids)
        self.database.conn.execute(
            "DELETE FROM persona_evidence_units WHERE persona_id = ? "
            f"AND source_id IN ({placeholders})",
            params,
        )
        self.database.conn.commit()

    def _use_streaming_analysis(self, sources: Sequence[Any], existing_count: int) -> bool:
        if existing_count > self.in_memory_unit_limit:
            return True
        for source in sources:
            metadata = dict(getattr(source, "metadata", None) or {})
            if metadata.get("storage_mode") == "external_file":
                return True
            if metadata.get("normalized_path"):
                return True
            size = metadata.get("size")
            if isinstance(size, int) and size > self.max_source_bytes:
                return True
            if len(str(getattr(source, "content", "") or "")) > self.max_source_bytes:
                return True
        return False

    def _source_material_path(self, source: Any) -> Path | None:
        metadata = dict(getattr(source, "metadata", None) or {})
        raw = metadata.get("path") or getattr(source, "path", None)
        if not raw:
            return None
        path = Path(str(raw))
        if not path.is_file():
            return None
        data_dir = getattr(getattr(self.personas, "config", None), "data_dir", None)
        if data_dir is not None:
            try:
                return ensure_child_path(Path(data_dir), path)
            except Exception:
                return path
        return path

    def _iter_source_records(self, source: Any) -> Iterator[Any]:
        metadata = dict(getattr(source, "metadata", None) or {})
        filename = str(metadata.get("filename") or getattr(source, "title", "") or "")
        normalized = metadata.get("normalized_path")
        if normalized:
            path = Path(str(normalized))
            data_dir = getattr(getattr(self.personas, "config", None), "data_dir", None)
            if data_dir is not None:
                with contextlib.suppress(Exception):
                    path = ensure_child_path(Path(data_dir), path)
            if path.is_file():
                yield from self.material_reader.iter_normalized_jsonl(path)
                return
        suffix = str(getattr(source, "source_type", "")).lower().lstrip(".")
        content = str(getattr(source, "content", "") or "")
        structured = suffix in {"json", "jsonl", "csv", "txt", "md", "text"}
        if metadata.get("storage_mode") != "external_file" and structured and content:
            yield from self.material_reader.iter_text(content, suffix, filename=filename)
            return
        material_path = self._source_material_path(source)
        if structured and material_path is not None:
            yield from self.material_reader.iter_path(
                material_path, suffix, filename=filename or material_path.name
            )
            return

    def _iter_paragraphs(self, source: Any) -> Iterator[str]:
        material_path = self._source_material_path(source)
        metadata = dict(getattr(source, "metadata", None) or {})
        if metadata.get("storage_mode") == "external_file" and material_path is not None:
            buffer: list[str] = []
            with material_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        text = "".join(buffer).strip()
                        buffer = []
                        if text:
                            yield text
                        continue
                    buffer.append(line)
            tail = "".join(buffer).strip()
            if tail:
                yield tail
            return
        content = str(getattr(source, "content", "") or "")
        paragraphs = [
            value.strip()
            for value in re.split(r"\n\s*\n+|(?<=。)\s*(?=\S)", content)
            if value.strip()
        ]
        if not paragraphs and content.strip():
            paragraphs = [content.strip()]
        yield from paragraphs

    def _iter_source_units(self, persona_id: str, source: Any) -> Iterator[EvidenceUnit]:
        metadata = dict(getattr(source, "metadata", None) or {})
        suffix = str(getattr(source, "source_type", "")).lower().lstrip(".")
        produced = False
        # Production role assignment (P0-A / BLOCKER 2): the declared speaker
        # role map is parsed once per source from its real file header and
        # persisted onto every chat EvidenceUnit at segmentation time.
        speaker_roles = self._speaker_role_map(source)
        for record in self._iter_source_records(source):
            produced = True
            text = str(record.text or "").strip()
            if not text:
                continue
            row = {
                "speaker": record.speaker,
                "timestamp": record.timestamp,
                "conversation_id": record.conversation_id,
                "content": text,
                "line_start": record.locator.get("line"),
                "thread_id": record.fields.get("thread_id"),
                "source_kind": record.fields.get("source_kind")
                or record.locator.get("source_kind"),
            }
            yield self._make_unit(
                persona_id,
                source,
                text,
                record.row_index,
                row,
                metadata,
                speaker_roles=speaker_roles,
            )
        if produced:
            return
        for index, text in enumerate(self._iter_paragraphs(source)):
            speaker = None
            match = None
            if suffix != "guided_interview":
                match = re.match(r"^([^:：\n]{1,60})[:：]\s*(.+)$", text, flags=re.DOTALL)
            if match:
                speaker, text = match.group(1).strip(), match.group(2).strip()
            yield self._make_unit(
                persona_id,
                source,
                text,
                index,
                {"speaker": speaker} if speaker else {},
                metadata,
                speaker_roles=(
                    speaker_roles if suffix in {"txt", "md", "jsonl", "json", "csv"} else {}
                ),
            )

    def _segment_sources(self, persona_id: str, sources: Sequence[Any]) -> list[EvidenceUnit]:
        return list(self._iter_segment_units(persona_id, sources))

    def _iter_segment_units(
        self, persona_id: str, sources: Sequence[Any]
    ) -> Iterator[EvidenceUnit]:
        for source in sources:
            yield from self._iter_source_units(persona_id, source)

    def _segment_sources_batched(
        self,
        persona_id: str,
        sources: Sequence[Any],
        job: MaterialAnalysisJob | None = None,
    ) -> int:
        batch: list[EvidenceUnit] = []
        parsed = 0
        persisted = 0
        batches = 0
        for unit in self._iter_segment_units(persona_id, sources):
            batch.append(unit)
            parsed += 1
            if len(batch) >= self.batch_size:
                self._persist_units(batch)
                persisted += len(batch)
                batches += 1
                if job is not None:
                    self._set_job(
                        job,
                        MaterialJobStatus.SEGMENTING,
                        stage="segments",
                        records_parsed=parsed,
                        segmented_messages=persisted,
                        analysis_batches_completed=batches,
                    )
                batch = []
        if batch:
            self._persist_units(batch)
            persisted += len(batch)
            batches += 1
            if job is not None:
                self._set_job(
                    job,
                    MaterialJobStatus.SEGMENTING,
                    stage="segments",
                    records_parsed=parsed,
                    segmented_messages=persisted,
                    analysis_batches_completed=batches,
                )
        return persisted

    def _iter_units_batched(self, persona_id: str) -> Iterator[list[EvidenceUnit]]:
        cursor: tuple[str, int, str] | None = None
        while True:
            after = (
                f"AND (source_id, {EVIDENCE_ORDER_POSITION}, id) > (?, ?, ?) "
                if cursor is not None
                else ""
            )
            rows = self.database.conn.execute(
                "SELECT * FROM persona_evidence_units WHERE persona_id = ? "
                + after
                + f"ORDER BY source_id, {EVIDENCE_ORDER_POSITION}, id LIMIT ?",
                (persona_id, *(cursor or ()), self.batch_size),
            ).fetchall()
            if not rows:
                break
            units = [self._unit_from_row(row) for row in rows]
            last = units[-1]
            cursor = (last.source_id, int(last.source_locator.get("segment_index") or 0), last.id)
            yield units

    def _finish_persisted_analysis(
        self, job: MaterialAnalysisJob, persona_id: str, incremental: bool = False
    ) -> MaterialAnalysisJob:
        del incremental
        unit_count = self._count_units(persona_id)
        self._set_job(
            job,
            MaterialJobStatus.ANALYZING,
            stage="unit_classification",
            evidence_unit_count=unit_count,
            records_parsed=unit_count,
            segmented_messages=unit_count,
        )
        clusters = self._cluster_persisted(persona_id)
        self._set_job(
            job, MaterialJobStatus.CLUSTERING, stage="similarity", cluster_count=len(clusters)
        )
        contradictions = self._contradictions_from_clusters(persona_id, clusters)
        self._set_job(
            job, MaterialJobStatus.FUSING, stage="semantic_union", cluster_count=len(clusters)
        )
        fused = self._fuse_persisted(persona_id, clusters, contradictions)
        episodes = self._build_episodes_persisted(persona_id)
        self._replace_derived_atomic(persona_id, clusters, contradictions, fused, episodes)
        coverage = self.coverage(persona_id)
        self._set_job(
            job,
            MaterialJobStatus.INDEXING,
            stage="persona_evidence_index",
            fused_count=len(fused),
            evidence_unit_count=unit_count,
        )
        self._set_job(
            job,
            MaterialJobStatus.GAP_ANALYSIS,
            stage="coverage_gate",
            coverage=coverage.model_dump(mode="json"),
        )
        self._set_job(
            job,
            MaterialJobStatus.READY_FOR_COMPILATION,
            stage="ready",
            coverage=coverage.model_dump(mode="json"),
        )
        return job

    def _cluster_persisted(self, persona_id: str) -> list[EvidenceCluster]:
        if self._count_units(persona_id) <= self.in_memory_unit_limit:
            return self.similarity.cluster(self._load_units(persona_id), persona_id)
        groups: dict[int, list[str]] = {}
        exact_groups: dict[str, int] = {}
        canonical_text: dict[int, tuple[str, str]] = {}
        kind_by_group: dict[int, str] = {}
        token_groups: dict[str, set[int]] = defaultdict(set)
        next_index = 0
        cursor = self.database.conn.execute(
            "SELECT id, text, normalized_text FROM persona_evidence_units "
            "WHERE persona_id = ? "
            "AND COALESCE(json_extract(metadata_json, '$.semantic_status'), '') != 'context_only' "
            "ORDER BY id",
            (persona_id,),
        )
        for row in cursor:
            unit_id = str(row["id"])
            text = str(row["text"] or "")
            normalized = str(row["normalized_text"] or "")
            exact_index = exact_groups.get(normalized)
            if exact_index is not None:
                groups[exact_index].append(unit_id)
                if kind_by_group[exact_index] != "near_duplicate":
                    kind_by_group[exact_index] = "exact_duplicate"
                continue
            tokens = _token_set(text)
            candidate_indexes: set[int] = set()
            for token in sorted(
                tokens,
                key=lambda value: (
                    0 if token_groups.get(value) else 1,
                    len(token_groups.get(value, ())),
                    value,
                ),
            )[:24]:
                candidate_indexes.update(token_groups.get(token, ()))
                if len(candidate_indexes) >= 96:
                    break
            placed = False
            for group_index in sorted(candidate_indexes)[:96]:
                canonical_norm, canonical_text_value = canonical_text[group_index]
                unit_numbers = re.findall(r"\d+(?:\.\d+)?", normalized)
                canonical_numbers = re.findall(r"\d+(?:\.\d+)?", canonical_norm)
                similarity = (
                    0.0
                    if unit_numbers != canonical_numbers
                    else max(
                        _jaccard(text, canonical_text_value),
                        _contains_similarity(text, canonical_text_value),
                    )
                )
                if similarity >= 0.82:
                    groups[group_index].append(unit_id)
                    exact_groups[normalized] = group_index
                    kind_by_group[group_index] = "near_duplicate"
                    placed = True
                    for token in tokens:
                        token_groups[token].add(group_index)
                    break
            if not placed:
                group_index = next_index
                next_index += 1
                groups[group_index] = [unit_id]
                exact_groups[normalized] = group_index
                canonical_text[group_index] = (normalized, text)
                kind_by_group[group_index] = "related_but_distinct"
                for token in tokens:
                    token_groups[token].add(group_index)
        now = datetime.now(UTC).isoformat()
        result: list[EvidenceCluster] = []
        for group_index in sorted(groups):
            member_ids = groups[group_index]
            cluster_type = kind_by_group.get(group_index, "related_but_distinct")
            if len(member_ids) == 1:
                cluster_type = "related_but_distinct"
            result.append(
                EvidenceCluster(
                    id=f"evc_{_sha('|'.join(member_ids))[:16]}",
                    persona_id=persona_id,
                    cluster_type=cluster_type,
                    canonical_evidence_id=member_ids[0],
                    member_evidence_ids=member_ids,
                    similarity_type="exact"
                    if cluster_type == "exact_duplicate"
                    else ("near" if len(member_ids) > 1 else "single"),
                    confidence=1.0
                    if cluster_type == "exact_duplicate"
                    else (0.82 if len(member_ids) > 1 else 0.5),
                    created_at=now,
                    updated_at=now,
                )
            )
        return result

    def _units_by_ids(self, persona_id: str, ids: Sequence[str]) -> list[EvidenceUnit]:
        if not ids:
            return []
        units: list[EvidenceUnit] = []
        chunk_size = 400
        for offset in range(0, len(ids), chunk_size):
            chunk = list(ids[offset : offset + chunk_size])
            placeholders = ",".join("?" for _ in chunk)
            rows = self.database.conn.execute(
                "SELECT * FROM persona_evidence_units WHERE persona_id = ? "
                f"AND id IN ({placeholders})",
                (persona_id, *chunk),
            ).fetchall()
            found = {str(row["id"]): self._unit_from_row(row) for row in rows}
            units.extend(found[item_id] for item_id in chunk if item_id in found)
        return units

    def _contradictions_from_clusters(
        self, persona_id: str, clusters: Sequence[EvidenceCluster]
    ) -> list[PersonaContradiction]:
        result: list[PersonaContradiction] = []
        for cluster in clusters:
            if cluster.cluster_type == "exact_duplicate" or len(cluster.member_evidence_ids) < 2:
                continue
            members = self._units_by_ids(persona_id, cluster.member_evidence_ids)
            result.extend(self.contradictions.analyze(members, persona_id, None))
        return result

    def _fuse_persisted(
        self,
        persona_id: str,
        clusters: Sequence[EvidenceCluster],
        contradictions: Sequence[PersonaContradiction],
    ) -> list[FusedEvidence]:
        fused: list[FusedEvidence] = []
        for cluster in clusters:
            if len(cluster.member_evidence_ids) < 2:
                continue
            members = self._units_by_ids(persona_id, cluster.member_evidence_ids)
            fused.extend(self.fusion.fuse(members, [cluster], contradictions, persona_id))
        return fused

    def _build_episodes_persisted(self, persona_id: str) -> list[ConversationEpisode]:
        episodes: list[ConversationEpisode] = []
        current_key: tuple[str, str] | None = None
        bucket: list[EvidenceUnit] = []
        last_time: datetime | None = None
        episode_index = 0

        def flush() -> None:
            nonlocal bucket, episode_index, last_time
            if current_key is None or not bucket:
                bucket = []
                last_time = None
                return
            source_id, conversation = current_key
            episodes.append(
                self._episode(persona_id, source_id, conversation, episode_index, bucket)
            )
            episode_index += 1
            bucket = []
            last_time = None

        cursor = self.database.conn.execute(
            "SELECT * FROM persona_evidence_units WHERE persona_id = ? "
            "AND source_kind IN ('chat', 'chat_import', 'guided_interview') "
            f"ORDER BY source_id, {EVIDENCE_ORDER_POSITION}, id",
            (persona_id,),
        )
        for row in cursor:
            unit = self._unit_from_row(row)
            conversation = str(
                (unit.metadata.get("row") or {}).get("conversation_id")
                or unit.metadata.get("conversation_id")
                or (unit.metadata.get("row") or {}).get("thread_id")
                or "default"
            )
            key = (unit.source_id, conversation)
            current = _parse_timestamp(unit.timestamp)
            if current_key != key:
                flush()
                current_key = key
                episode_index = 0
            elif bucket and current and last_time and (current - last_time).total_seconds() > 7200:
                flush()
            bucket.append(unit)
            last_time = current or last_time
        flush()
        return episodes

    def _resolve_independent_session_capability(
        self, snapshot: dict[str, Any]
    ) -> tuple[Any, Any]:
        """Resolve the persistent verified capability for this runtime.

        Identity is taken from the full runtime identity when the caller
        supplied one (adapter + CLI binary/version + model + credential hash +
        origin); otherwise it degrades to the adapter-only identity, which
        simply will not match a credential-bound probe.
        """

        from persona_continuum.performance.concurrency_cache import default_concurrency_cache
        from persona_continuum.performance.runtime_capability_store import RuntimeIdentity

        raw = snapshot.get("runtime_identity")
        if isinstance(raw, dict) and raw.get("adapter_id"):
            identity = RuntimeIdentity(
                adapter_id=str(raw.get("adapter_id") or ""),
                binary_identity=str(raw.get("binary_identity") or ""),
                binary_version=str(raw.get("binary_version") or ""),
                model_id=str(raw.get("model_id") or ""),
                credential_identity_hash=str(raw.get("credential_identity_hash") or ""),
                runtime_origin=str(raw.get("runtime_origin") or ""),
                probe_version=(
                    str(raw.get("probe_version") or "")
                    or "independent-session-concurrency-v1"
                ),
            )
        else:
            adapter_id = str(snapshot.get("adapter_id") or snapshot.get("id") or "")
            identity = RuntimeIdentity(
                adapter_id=adapter_id,
                model_id=str(snapshot.get("effective_model") or snapshot.get("model_id") or ""),
                runtime_origin=adapter_id,
            )
        return default_concurrency_cache().resolution_for(identity), identity

    def _concurrency_downgrade_recorder(
        self, identity: Any
    ) -> Callable[[str, int], None]:
        """Persist a temporary runtime downgrade without erasing the probe."""

        def _record(reason: str, limit: int) -> None:
            try:
                from persona_continuum.performance.concurrency_cache import (
                    default_concurrency_cache,
                )

                default_concurrency_cache().store.record_runtime_downgrade(
                    identity, recommended=limit, reason=reason
                )
            except Exception:
                # Telemetry persistence must never fail a classification run.
                return

        return _record

    async def _classify_persisted_with_agent(
        self,
        persona_id: str,
        agent_analyzer: Callable[[str, dict[str, Any]], Awaitable[Any]],
        *,
        execution_profile: ResolvedExecutionProfile,
        metrics: MaterialPipelineMetrics,
        window_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        semantic_gate_mode: str | None = None,
        runtime_identity: Any | None = None,
    ) -> None:
        """Stream classification over ConversationTurns (Large Conversation Pipeline V2).

        Raw units are read in conversation order and folded into analysis
        turns across checkpoint-batch boundaries; only semantic (target) turns
        count as pending work, so context-only exporter messages can never
        keep the job permanently pending. A bounded window queue separates
        SQLite reads from workers; each window commits its own checkpoint.
        ``semantic_gate_mode`` is the per-task override (P1-C); None keeps the
        service default.
        """

        profile = execution_profile
        active_gate_mode = semantic_gate_mode or self.semantic_gate_mode
        gate = SemanticGate(active_gate_mode)
        metrics.semantic_gate_mode = active_gate_mode
        pending_total = 0
        done_total = 0
        for batch in self._iter_units_batched(persona_id):
            for item in batch:
                metrics.raw_message_count += 1
                if self._is_non_semantic_unit(item):
                    continue
                pending_total += 1
                if (
                    self._classification_done(item, profile, gate_mode=active_gate_mode)
                    and item.metadata.get("semantic_status") != SEMANTIC_GATE_SKIPPED
                ):
                    done_total += 1
        initial_completed = done_total
        source_exhausted = False
        rolling: list[tuple[float, int]] = [(time.perf_counter(), 0)]

        async def report(info: dict[str, Any]) -> None:
            nonlocal done_total
            done_total += int(info.get("newly_completed_units", 0))
            now = time.perf_counter()
            rolling.append((now, metrics.target_turns_reviewed))
            while len(rolling) > 2 and rolling[1][0] < now - 300:
                rolling.pop(0)
            rate = (metrics.target_turns_reviewed - rolling[0][1]) / max(1e-9, now - rolling[0][0])
            if window_progress is not None:
                await window_progress(
                    {
                        **info,
                        "classification_total": pending_total,
                        "classification_completed": min(done_total, pending_total),
                        "classification_cached": initial_completed,
                        "chat_pipeline": self._chat_pipeline_progress(metrics),
                        "classification_selected_total": metrics.semantic_selected,
                        "classification_reviewed_turns": metrics.target_turns_reviewed,
                        "classification_turns_per_hour": round(rate * 3600, 2),
                        "classification_eta_seconds": (
                            max(0, metrics.semantic_selected - metrics.target_turns_reviewed) / rate
                            if rate > 0 and source_exhausted else None
                        ),
                        "classification_pending": max(
                            0, pending_total - done_total - metrics.semantic_skipped_messages
                        ),
                    }
                )

        await report({"prompt_state": MaterialPromptState.PREPARING_INPUT.value})
        producer_budget = self.context_budget_manager.budget_for(
            model=profile.model_dump(mode="json"),
            phase="material_classification",
            expected_output=MATERIAL_AGENT_OUTPUT_SCHEMAS["classify"],
        )
        if producer_budget.phase_working_target is not None:
            profile.phase_working_target = producer_budget.phase_working_target
        producer_target_tokens = material_batch_target_tokens(
            profile,
            phase_usable_budget=producer_budget.evidence_token_budget,
            phase_policy=self.context_budget_manager.phase_policy,
        )
        request_estimator = ClassificationRequestTokenEstimator(
            self.context_budget_manager.estimate_tokens
        )
        producer_base = request_estimator.base_tokens(
            extra_contracts=[
                "Episodes are independent conversation blocks",
                "Return only target turns that contain independent persona evidence",
            ],
            allowed_dimensions=REQUIRED_DIMENSIONS,
        )
        producer_episode_overhead = request_estimator.episode_overhead()
        count_cap = self.analysis_window_max_units
        batches = iter(self._iter_units_batched(persona_id))
        buffer: list[ConversationTurn] = []
        group_units: dict[str, EvidenceUnit] = {}
        carry: ConversationTurn | None = None
        source_exhausted = False
        previous_tail: list[ConversationTurn] = []

        def advance_one() -> None:
            """Fold forward until at least one new completed turn is buffered."""

            nonlocal carry, source_exhausted
            while not buffer and not source_exhausted:
                batch = next(batches, None)
                if batch is None:
                    source_exhausted = True
                    if carry is not None:
                        buffer.append(carry)
                        carry = None
                    return
                for item in batch:
                    group_units[item.id] = item
                for turn in fold_conversation_turns(batch, gap_seconds=self.turn_gap_seconds):
                    merged = merge_turns(carry, turn, gap_seconds=self.turn_gap_seconds)
                    if merged is not None:
                        carry = merged
                        continue
                    if carry is not None:
                        buffer.append(carry)
                    carry = turn
            if not buffer and carry is not None and source_exhausted:
                buffer.append(carry)
                carry = None

        def producer_row(turn: ConversationTurn) -> dict[str, Any]:
            return {
                "id": turn.anchor_id,
                "evidence_ids": list(turn.evidence_unit_ids),
                "text": turn.text,
                "source_id": turn.source_id,
                "semantic_role": "target" if turn.is_target else "context",
                "speaker_role": turn.speaker_role,
                "speaker": turn.speaker,
                "timestamp": turn.end_time,
                "source_kind": turn.source_kind,
            }

        def episode_break(prev: ConversationTurn, nxt: ConversationTurn) -> bool:
            if prev.source_id != nxt.source_id:
                return True
            left = parse_turn_time(prev.end_time)
            right = parse_turn_time(nxt.start_time or nxt.end_time)
            if left is None or right is None:
                return False
            return (right - left).total_seconds() > self.conversation_episode_gap_seconds

        async with ClassificationDispatch(
            profile.classification_worker_count(self.max_llm_concurrency),
            metrics,
            retry_limit=self.concurrency_retry_limit,
            on_downgrade=(
                self._concurrency_downgrade_recorder(runtime_identity)
                if runtime_identity is not None
                else None
            ),
        ) as dispatch:
            while True:
                group: list[ConversationTurn] = []
                group_size = 0
                group_tokens = producer_base
                while True:
                    advance_one()
                    if not buffer:
                        break
                    nxt = buffer[0]
                    nxt_tokens = request_estimator.turn_tokens(producer_row(nxt))
                    extra_episode = (
                        producer_episode_overhead
                        if not group or episode_break(group[-1], nxt)
                        else 0
                    )
                    would_exceed_count = (
                        count_cap is not None
                        and group_size + int(nxt.is_target) > count_cap
                    )
                    would_exceed_budget = (
                        bool(group)
                        and group_tokens + nxt_tokens + extra_episode > producer_target_tokens
                    )
                    if would_exceed_count or would_exceed_budget:
                        break
                    buffer.pop(0)
                    group.append(nxt)
                    group_size += int(nxt.is_target)
                    group_tokens += nxt_tokens + extra_episode
                if not group:
                    break
                # P0-F: the first turns of the next group explain this group's
                # tail and vice versa; they stay queued so they are still billed
                # exactly once as their own work.
                lookahead: list[ConversationTurn] = []
                while len(lookahead) < 3:
                    advance_one()
                    if not buffer:
                        break
                    lookahead.append(buffer.pop(0))
                await self._classify_with_agent(
                    group,
                    agent_analyzer,
                    execution_profile=profile,
                    metrics=metrics,
                    window_progress=report,
                    raw_units=group_units,
                    persona_id=persona_id,
                    context_before=previous_tail,
                    context_after=lookahead,
                    gate=gate,
                    window_submit=dispatch.submit,
                    gate_mode=active_gate_mode,
                )
                previous_tail = group[-3:]
                # Look-ahead turns lead the next group unchanged.
                if lookahead:
                    buffer = [*lookahead, *buffer]
                # Bound memory: SQLite remains the authoritative ledger; drop
                # fully-processed rows from the working set so a 385k-message run
                # never accumulates every raw unit in RAM.  Anything still
                # pending (queued, carried, or borrowed as look-back context)
                # keeps its raw rows.
                keep: set[str] = set()
                for turn in [*previous_tail, *buffer]:
                    keep.update(turn.evidence_unit_ids)
                if carry is not None:
                    keep.update(carry.evidence_unit_ids)
                group_units = {key: value for key, value in group_units.items() if key in keep}

        metrics.average_turns_per_window = (
            metrics.target_turns_reviewed / max(1, metrics.classification_windows_completed)
        )
        metrics.finalize_window_metrics()
        await report({"prompt_state": MaterialPromptState.CHECKPOINTING.value})

    def _semantic_turn_counts(self, units: Sequence[EvidenceUnit]) -> dict[str, int]:
        """classification_total/pending for the in-memory dispatch path.

        Context-only and derived rows are excluded from the denominator,
        so a file where only the exporter speaks reports pending 0 instead
        of a permanently-unreachable backlog.
        """

        profile = ResolvedExecutionProfile()
        total = 0
        completed = 0
        for item in units:
            if self._is_non_semantic_unit(item):
                continue
            total += 1
            if self._classification_done(item, profile):
                completed += 1
        return {
            "total": total,
            "completed": completed,
            "pending": max(0, total - completed),
        }

    def _is_non_semantic_unit(self, item: EvidenceUnit) -> bool:
        """Context-only chat lines and derived profile rows skip classification."""

        if str(item.source_kind or "") == "deterministic_chat_profile":
            return True
        return str((item.metadata or {}).get("semantic_status") or "") == SEMANTIC_CONTEXT_ONLY

    def _classification_done(
        self,
        item: EvidenceUnit,
        profile: ResolvedExecutionProfile,
        *,
        gate_mode: str | None = None,
    ) -> bool:
        """True when no Agent re-analysis is owed for this unit.

        V4 rows are complete through their persisted semantic_status plus the
        v4 contract stamp (a model change intentionally does not re-bill
        corpus-wide review work); successful v3/legacy rows stay reusable
        under their own frozen keys and are never silently re-analyzed.  An
        empty failed row is still retried.  ``gate_mode`` is the *active*
        Semantic Gate mode for the current run (per-task override or the
        service default) and only affects whether a persisted
        ``semantic_gate_skipped`` stamp still counts as done.
        """

        if self._is_non_semantic_unit(item):
            return True
        metadata = item.metadata or {}
        status = str(metadata.get("semantic_status") or "")
        if status in {SEMANTIC_EVIDENCE_EXTRACTED, SEMANTIC_REVIEWED_NO_EVIDENCE} and (
            str(metadata.get("classification_contract") or "") == CLASSIFICATION_CONTRACT_V4
        ):
            return True
        if status == SEMANTIC_GATE_SKIPPED:
            return (
                metadata.get("semantic_gate_policy") == SEMANTIC_GATE_POLICY_VERSION
                and metadata.get("semantic_gate_mode") == (gate_mode or self.semantic_gate_mode)
            )
        key = metadata.get("intelligence_cache_key")
        if key == self._material_intelligence_cache_key(item, profile):
            return bool(
                metadata.get("classification_status") == "completed"
                or metadata.get("evidence_intelligence")
            )
        if key == self._material_intelligence_cache_key(item, profile, contract="v3"):
            return bool(
                metadata.get("classification_status") == "completed"
                or metadata.get("evidence_intelligence")
            )
        if key == self._material_intelligence_cache_key(item, profile, legacy=True):
            return bool(metadata.get("evidence_intelligence"))
        return False

    def _has_classification(self, item: EvidenceUnit, profile: ResolvedExecutionProfile) -> bool:
        return self._classification_done(item, profile)

    def _speaker_role_map(self, source: Any) -> dict[str, str]:
        """Declared chat speaker roles for one source; cached on the service.

        Production entry point for role headers such as
        "对方 = 目标 Persona": parsed from the real material at segmentation
        time and persisted onto every chat EvidenceUnit, never inferred per
        window and never allowed to disagree with the prompt annotation.
        """

        cache = self._material_source_context
        source_id = str(source.id)
        cached = cache.get(source_id)
        if isinstance(cached, dict) and cached.get("speaker_roles_resolved"):
            return dict(cached.get("speaker_roles") or {})
        context: dict[str, Any] = dict(cached or {})
        context["speaker_roles_resolved"] = True
        header = ""
        content = str(getattr(source, "content", "") or "")
        if content:
            header = chr(10).join(content.splitlines()[:64])
        else:
            path = self._source_material_path(source)
            if path is not None:
                lines: list[str] = []
                with (
                    contextlib.suppress(OSError),
                    path.open(encoding="utf-8", errors="replace") as handle,
                ):
                    for _ in range(64):
                        line = handle.readline(4096)
                        if not line:
                            break
                        lines.append(line)
                header = "".join(lines)
        roles = parse_speaker_role_map(header)
        if roles:
            context["speaker_roles"] = roles
        if chr(60) + "voice" in header:
            context["voice"] = "Automatic transcription; may contain recognition errors."
        if chr(60) + "emoji" in header:
            context["emoji"] = "Platform emoji label/count, interpret in conversation context."
        cache[source_id] = context
        return dict(context.get("speaker_roles") or {})

    def _mark_units_status(self, persona_id: str, ids: Sequence[str], status: str) -> None:
        """Persist a V4 semantic_status checkpoint stamp in place.

        Raw text, locators and every other provenance column stay untouched.
        extraction_method is promoted so a later re-segmentation cannot erase
        completed review accounting (the same upsert guard the Agent path
        relies on keeps deterministic rows sticky).
        """

        unique = [value for value in dict.fromkeys(str(item_id) for item_id in ids) if value]
        for offset in range(0, len(unique), 400):
            chunk = unique[offset : offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            self.database.conn.execute(
                "UPDATE persona_evidence_units SET extraction_method = 'deterministic_plus_agent', "
                "metadata_json = json_set(json_set(json_set(json_set("
                "COALESCE(NULLIF(metadata_json, ''), '{}'), "
                "'$.semantic_status', ?), '$.classification_status', 'completed'), "
                "'$.classification_contract', ?), '$.mark_source', 'checkpoint_v4') "
                f"WHERE persona_id = ? AND id IN ({placeholders}) "
                "AND COALESCE(json_extract(metadata_json, '$.semantic_status'), '') NOT IN (?, ?)",
                (
                    status,
                    CLASSIFICATION_CONTRACT_V4,
                    persona_id,
                    *chunk,
                    SEMANTIC_EVIDENCE_EXTRACTED,
                    status,
                ),
            )
        if unique:
            self.database.conn.commit()

    def _structured_rows(self, content: str, source_type: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for record in self.material_reader.iter_text(str(content or ""), source_type):
            row = dict(record.fields)
            row.setdefault("content", record.text)
            row.setdefault("text", record.text)
            if record.speaker:
                row.setdefault("speaker", record.speaker)
                row.setdefault("sender", record.speaker)
            if record.timestamp:
                row.setdefault("timestamp", record.timestamp)
            if record.conversation_id:
                row.setdefault("conversation_id", record.conversation_id)
            rows.append(row)
        return rows

    def _apply_classifier(self, unit: EvidenceUnit) -> EvidenceUnit:
        if self.classifier is None:
            return unit
        try:
            result = self.classifier(unit.model_dump(mode="json"))
            if not isinstance(result, dict):
                return unit
            typed = MaterialClassificationResult.model_validate(
                {
                    **result,
                    "id": result.get("id") or unit.id,
                    "content": result.get("content") or unit.text,
                    "source_id": result.get("source_id") or unit.source_id,
                }
            )
            allowed = {
                key: value
                for key, value in typed.model_dump(mode="json").items()
                if key
                in {
                    "evidence_type",
                    "dimension_candidates",
                    "dimension_scores",
                    "life_stage_candidates",
                    "relationship_entities",
                    "context_tags",
                    "confidence",
                    "speaker_role",
                }
            }
            metadata = dict(unit.metadata)
            metadata.update(typed.metadata)
            return unit.model_copy(
                update={
                    **allowed,
                    "metadata": metadata,
                    "extraction_method": "deterministic_plus_agent",
                }
            )
        except Exception:
            return unit

    def _apply_fusion_hook(
        self, fused: Sequence[FusedEvidence], units: Sequence[EvidenceUnit]
    ) -> list[FusedEvidence]:
        hook = self.fusion_hook
        if hook is None:
            return list(fused)
        by_id = {item.id: item for item in units}
        result: list[FusedEvidence] = []
        for item in fused:
            try:
                payload = hook(
                    {
                        "canonical_claim": item.canonical_claim,
                        "evidence": [
                            by_id[eid].model_dump(mode="json")
                            for eid in item.supporting_evidence_ids
                            if eid in by_id
                        ],
                    }
                )
                if not isinstance(payload, dict):
                    raise ValueError("fusion_result_not_object")
                typed = MaterialFusionResult.model_validate(
                    {
                        **payload,
                        "id": payload.get("id") or item.id,
                        "content": payload.get("content")
                        or payload.get("canonical_claim")
                        or item.canonical_claim,
                        "source_id": payload.get("source_id")
                        or (item.source_ids[0] if item.source_ids else item.id),
                    }
                )
                claim = str(typed.canonical_claim or typed.content).strip()
                evidence_text = " ".join(
                    by_id[eid].text for eid in item.supporting_evidence_ids if eid in by_id
                )
                if claim and _jaccard(claim, evidence_text) >= 0.18:
                    merged_scores = dict(item.dimension_scores)
                    for key, score in typed.dimension_scores.items():
                        merged_scores[key] = max(
                            safe_probability(merged_scores.get(key), default=0.0) or 0.0,
                            score,
                        )
                    item = item.model_copy(
                        update={
                            "canonical_claim": claim,
                            "dimension_scores": merged_scores,
                            "confidence": max(item.confidence, typed.confidence),
                            "synthesis_method": "deterministic_union_plus_agent",
                        }
                    )
            except Exception:
                pass
            result.append(item)
        return result

    def _request_over_budget(
        self,
        request: dict[str, Any],
        *,
        target_tokens: int,
        transport_safe_bytes: int | None,
    ) -> str | None:
        """Prompt Size Guard reason, or None when the request fits."""

        serialized = json.dumps(request, ensure_ascii=False)
        if self.context_budget_manager.estimate_tokens(serialized) > target_tokens:
            return "TOKEN_BUDGET_UNDERESTIMATED"
        if (
            transport_safe_bytes is not None
            and len(serialized.encode("utf-8")) > transport_safe_bytes
        ):
            return "TRANSPORT_CAP"
        return None

    def _request_fits_budget(
        self,
        request: dict[str, Any],
        *,
        target_tokens: int,
        transport_safe_bytes: int | None,
    ) -> bool:
        """Prompt Size Guard: the serialized request must fit both budgets.

        A too-large request must never be handed to the Adapter silently; the
        caller re-splits the batch (and only an unsplittable unit fails).
        """

        return self._request_over_budget(
            request, target_tokens=target_tokens, transport_safe_bytes=transport_safe_bytes
        ) is None

    def _serialized_prompt_size(self, request: dict[str, Any]) -> tuple[int, int]:
        serialized = json.dumps(request, ensure_ascii=False)
        return (
            self.context_budget_manager.estimate_tokens(serialized),
            len(serialized.encode("utf-8")),
        )

    def _source_context_for_units(self, units: Sequence[EvidenceUnit]) -> dict[str, Any]:
        """Material format notes for the Agent prompt (from cached headers).

        The speaker-role map now comes from the shared ingest-time parser
        (parse_speaker_role_map), so the prompt annotation and the persisted
        unit roles can never disagree.
        """

        contexts: dict[str, Any] = {}
        for item in units:
            if item.source_id in contexts:
                continue
            source = self._material_source_context.get(item.source_id) or {}
            context: dict[str, Any] = {}
            roles = dict(source.get("speaker_roles") or {})
            if not roles:
                raw_path = item.metadata.get("source_path") or item.source_locator.get(
                    "source_path"
                )
                if raw_path:
                    lines: list[str] = []
                    with (
                        contextlib.suppress(OSError),
                        Path(str(raw_path)).open(encoding="utf-8", errors="replace") as handle,
                    ):
                        for _ in range(64):
                            line = handle.readline(4096)
                            if not line or (line.strip() and not line.lstrip().startswith("#")):
                                break
                            lines.append(line)
                    header = "".join(lines)
                    roles = parse_speaker_role_map(header)
                    if "<voice" in header:
                        context["voice"] = (
                            "Automatic transcription; may contain recognition errors."
                        )
                    if "<emoji" in header:
                        context["emoji"] = (
                            "Platform emoji label/count, interpret in conversation context."
                        )
            if roles:
                context["speaker_roles"] = roles
            if context:
                contexts[item.source_id] = context
        return contexts

    def _run_chat_style_profiler(self, persona_id: str) -> str:
        """Deterministic Expression DNA profile over target chat messages (P0-E).

        Streams raw units in conversation order; no model is called.  The
        result is one persisted expression_profile record (never per-statistic
        EvidenceUnits) that the Evidence Index feeds into expression_dna.
        """

        if not self.chat_style_profiler_enabled:
            return "disabled"
        try:
            profile = ChatStyleProfiler().profile(persona_id, self._iter_chat_units(persona_id))
        except Exception:
            return "failed"
        if profile is None:
            return "no_target_chat"
        self.database.conn.execute(
            "INSERT OR REPLACE INTO persona_chat_style_profiles "
            "(persona_id, contract, corpus_size, time_range_json, statistics_json, "
            "representative_evidence_ids_json, profile_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                persona_id,
                profile.contract,
                profile.corpus_size,
                dumps(profile.time_range),
                dumps(profile.statistics),
                dumps(profile.representative_evidence_ids),
                dumps(profile.model_dump(mode="json")),
                profile.created_at,
            ),
        )
        self.database.conn.commit()
        return "completed"

    def _iter_chat_units(self, persona_id: str) -> Iterator[EvidenceUnit]:
        cursor: tuple[str, int, str] | None = None
        while True:
            after = (
                f"AND (source_id, {EVIDENCE_ORDER_POSITION}, id) > (?, ?, ?) "
                if cursor is not None
                else ""
            )
            rows = self.database.conn.execute(
                "SELECT * FROM persona_evidence_units WHERE persona_id = ? AND source_kind "
                "IN ('chat', 'chat_import', 'guided_interview') "
                + after
                + f"ORDER BY source_id, {EVIDENCE_ORDER_POSITION}, id LIMIT ?",
                (persona_id, *(cursor or ()), self.batch_size),
            ).fetchall()
            if not rows:
                break
            units = [self._unit_from_row(row) for row in rows]
            last = units[-1]
            cursor = (last.source_id, int(last.source_locator.get("segment_index") or 0), last.id)
            yield from units

    def get_style_profile(self, persona_id: str) -> dict[str, Any] | None:
        row = self.database.conn.execute(
            "SELECT profile_json FROM persona_chat_style_profiles WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            value = loads(str(row["profile_json"]))
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _chat_pipeline_progress(metrics: MaterialPipelineMetrics) -> dict[str, Any]:
        """Compact V2 counters for the task center (P0-H).

        The frontend must be able to tell whether the optimization actually
        took effect, so these come straight from the real pipeline state and
        never from schema defaults.
        """

        return {
            **{key: getattr(metrics, key) for key in (
                "semantic_gate_mode", "semantic_selected", "semantic_skipped",
                "semantic_reserve_selected", "semantic_bypass_count",
                "classification_worker_count", "effective_classification_workers",
                "active_classification_workers",
                "peak_active_classification_workers", "average_active_classification_workers",
                "active_independent_sessions", "peak_independent_sessions",
                "window_queue_depth", "peak_window_queue_depth", "window_queue_capacity",
                "classification_windows_dispatched",
                "window_retries", "window_retry_limit", "concurrency_downgrades",
                "concurrency_generation", "effective_concurrency",
                "stale_rejection_count", "stale_replayed_windows",
                "terminal_concurrency_failures", "rate_limit_replays",
                "provider_failure_kind",
                "last_concurrency_failure_kind", "non_retriable_capacity_failures",
                "independent_session_capability",
                "workload_context_scope", "adapter_session_mode",
                "parallel_turns_same_session", "parallel_independent_sessions",
                "max_parallel_independent_sessions",
                "runtime_pool_leases", "runtime_pool_wait_ms",
                "context_capability_revision", "context_remaining_revision",
                "remaining_source", "remaining_verified",
            )},
            "raw_message_count": metrics.raw_message_count,
            "target_message_count": metrics.target_message_count,
            "context_message_count": metrics.context_message_count,
            "conversation_turn_count": metrics.conversation_turn_count,
            "target_turn_count": metrics.target_turn_count,
            "classification_windows_total": metrics.classification_windows_total,
            "classification_windows_completed": metrics.classification_windows_completed,
            "target_turns_reviewed": metrics.target_turns_reviewed,
            "evidence_turns_extracted": metrics.evidence_turns_extracted,
            "reviewed_no_independent_evidence": metrics.reviewed_no_independent_evidence,
            "average_turns_per_window": metrics.average_turns_per_window,
            "max_turns_per_window": metrics.max_turns_per_window,
            # P0.3-C: episode-aware packing quality counters.
            "episodes_total": metrics.episodes_total,
            "episodes_per_window_avg": metrics.episodes_per_window_avg,
            "episodes_per_window_max": metrics.episodes_per_window_max,
            "prompt_budget_utilization_avg": metrics.prompt_budget_utilization_avg,
            "prompt_budget_utilization_p50": metrics.prompt_budget_utilization_p50,
            "prompt_budget_utilization_p95": metrics.prompt_budget_utilization_p95,
            "agent_calls": metrics.agent_calls,
            "input_tokens": metrics.input_tokens.get("intelligence", 0),
            "output_tokens": metrics.output_tokens.get("intelligence", 0),
            "style_profile_status": metrics.style_profile_status,
            "initial_analysis_windows": (
                metrics.initial_analysis_windows or metrics.analysis_windows
            ),
            "rebatched_windows": metrics.rebatched_windows,
            "rebatched_window_ratio": metrics.rebatched_window_ratio,
            "packing_accuracy": metrics.packing_accuracy,
            "rebatch_reasons": dict(metrics.rebatch_reasons),
        }

    def _classification_values(
        self,
        payload: Any,
        part: Sequence[Any],
        allowed_support_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate one sparse V4 result (conversation-evidence-v4).

        Absence of an input Turn from the units array is NOT an error: a
        structurally successful call means every target Turn in the window was
        reviewed.  Only malformed payloads, out-of-window ids, unknown
        dimensions or schema corruption raise (and therefore retry).
        """

        expected: dict[str, Any] = {}
        for item in part:
            expected[str(item.id)] = item
            for member_id in getattr(item, "evidence_unit_ids", None) or [str(item.id)]:
                expected.setdefault(str(member_id), item)
        if not isinstance(payload, dict) or not isinstance(payload.get("units"), list):
            raise AgentStructuredOutputError("material_classification_missing_units")
        support_scope = allowed_support_ids if allowed_support_ids is not None else set(expected)
        values: dict[str, dict[str, Any]] = {}
        for raw in payload["units"]:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
                raise AgentStructuredOutputError("material_classification_invalid_record")
            evidence_id = raw["id"]
            anchor = expected.get(evidence_id)
            if anchor is None or evidence_id in values:
                raise AgentStructuredOutputError("material_classification_unknown_or_duplicate_id")
            support = raw.get("supporting_evidence_ids", [evidence_id])
            if (
                not isinstance(support, list)
                or not support
                or any(not isinstance(value, str) for value in support)
                or len(support) != len(set(support))
                or not set(support).issubset(support_scope)
            ):
                raise AgentStructuredOutputError("material_classification_invalid_support")
            try:
                typed = MaterialClassificationResult.model_validate(
                    {
                        **raw,
                        "content": str(anchor.text or ""),
                        "source_id": str(anchor.source_id or ""),
                    }
                )
            except (ValueError, TypeError):
                # Prompt-only models still emit rare leftover shapes after
                # coercion.  Omit the unit (V4: absent = reviewed) instead of
                # failing a window that already produced valid evidence.
                continue
            if not set(typed.dimension_scores).issubset(REQUIRED_DIMENSIONS):
                raise AgentStructuredOutputError("material_classification_unknown_dimension")
            value = typed.model_dump(mode="json")
            value["supporting_evidence_ids"] = support
            values[evidence_id] = value
        return list(values.values())

    @staticmethod
    def _value_has_independent_evidence(value: dict[str, Any]) -> bool:
        for key in (
            "claims",
            "events",
            "entities",
            "quotes",
            "decisions",
            "behaviors",
            "relationships",
            "emotions",
            "motivations",
            "beliefs",
            "values",
            "communication_patterns",
            "contradictions",
            "negative_evidence",
            "uncertainties",
            "life_stage",
        ):
            if value.get(key):
                return True
        return bool(value.get("dimension_scores"))

    def _apply_classification_value(
        self,
        item: EvidenceUnit,
        value: dict[str, Any],
        profile: ResolvedExecutionProfile,
        *,
        semantic_status: str = SEMANTIC_EVIDENCE_EXTRACTED,
    ) -> EvidenceUnit:
        typed = MaterialClassificationResult.model_validate(
            {
                **value,
                "id": item.id,
                "content": item.text,
                "source_id": item.source_id,
            }
        )
        intelligence = dict(item.metadata.get("evidence_intelligence") or {})
        for key in (
            "claims",
            "events",
            "dates",
            "entities",
            "quotes",
            "decisions",
            "behaviors",
            "relationships",
            "emotions",
            "motivations",
            "beliefs",
            "values",
            "communication_patterns",
            "contradictions",
            "negative_evidence",
            "uncertainties",
            "life_stage",
        ):
            incoming = getattr(typed, key)
            if not incoming:
                continue
            if isinstance(incoming, list):
                combined = list(intelligence.get(key) or [])
                for entry in incoming:
                    if entry not in combined:
                        combined.append(entry)
                intelligence[key] = combined
            else:
                intelligence[key] = incoming
        dimensions = dict(item.dimension_scores)
        for key, score in typed.dimension_scores.items():
            dimensions[key] = max(dimensions.get(key, 0.0), score)
        metadata: dict[str, Any] = {
            **item.metadata,
            "evidence_intelligence": intelligence,
            "classification_status": "completed",
            "semantic_status": semantic_status,
            "classification_contract": CLASSIFICATION_CONTRACT_V4,
            "intelligence_cache_key": self._material_intelligence_cache_key(item, profile),
            "intelligence_supporting_ids": value.get("supporting_evidence_ids", [item.id]),
        }
        return item.model_copy(
            update={
                "evidence_type": value.get("evidence_type") or item.evidence_type,
                "dimension_scores": dimensions,
                "dimension_candidates": sorted(dimensions),
                "relationship_entities": sorted(
                    set(item.relationship_entities + typed.relationship_entities)
                ),
                "context_tags": sorted(set(item.context_tags + typed.context_tags)),
                "confidence": max(item.confidence, typed.confidence),
                "metadata": metadata,
                "extraction_method": "deterministic_plus_agent",
            }
        )

    def _mark_unit_reviewed_no_evidence(
        self, item: EvidenceUnit, profile: ResolvedExecutionProfile
    ) -> EvidenceUnit:
        """Sparse-V4 bookkeeping: reviewed, no independent persona evidence.

        This is a terminal checkpoint state, not a quality judgement: the raw
        text stays in the ledger, in the style lane and in episode/relationship
        analysis untouched.
        """

        return item.model_copy(
            update={
                "metadata": {
                    **item.metadata,
                    "semantic_status": SEMANTIC_REVIEWED_NO_EVIDENCE,
                    "classification_status": "completed",
                    "classification_contract": CLASSIFICATION_CONTRACT_V4,
                    "intelligence_cache_key": self._material_intelligence_cache_key(item, profile),
                },
                "extraction_method": "deterministic_plus_agent",
            }
        )

    def _apply_semantic_gate(
        self,
        turns: Sequence[ConversationTurn],
        by_id: dict[str, EvidenceUnit],
        profile: ResolvedExecutionProfile,
        gate: SemanticGate,
        metrics: MaterialPipelineMetrics | None,
        previous: ConversationTurn | None,
        *,
        gate_mode: str | None = None,
    ) -> None:
        active_mode = gate_mode or gate.mode or self.semantic_gate_mode
        changed: list[EvidenceUnit] = []
        for turn in turns:
            decision = gate.decide(turn, previous)
            previous = turn
            if not turn.is_target or turn.anchor_id not in by_id:
                continue
            base = by_id[turn.anchor_id]
            done = self._classification_done(base, profile, gate_mode=active_mode)
            cached_skip = base.metadata.get("semantic_status") == SEMANTIC_GATE_SKIPPED
            if done and not cached_skip:
                continue
            if done:
                if metrics is not None:
                    metrics.semantic_skipped += 1
                    metrics.semantic_skipped_messages += len(turn.evidence_unit_ids)
                continue
            if metrics is not None:
                metrics.semantic_selected += int(decision.selected)
                metrics.semantic_skipped += int(not decision.selected)
                if not decision.selected:
                    metrics.semantic_skipped_messages += len(turn.evidence_unit_ids)
                metrics.semantic_reserve_selected += int(decision.reserve)
                metrics.semantic_bypass_count += int(decision.bypass)
            if decision.reason in {"full", "not_chat_target", "ambiguous_role"}:
                continue
            for member_id in turn.evidence_unit_ids:
                unit = by_id.get(member_id)
                if unit is None or self._classification_done(unit, profile, gate_mode=active_mode):
                    continue
                marked = unit.model_copy(update={"metadata": {
                    **unit.metadata,
                    "semantic_status": (
                        SEMANTIC_GATE_SELECTED if decision.selected else SEMANTIC_GATE_SKIPPED
                    ),
                    "semantic_gate_policy": SEMANTIC_GATE_POLICY_VERSION,
                    "semantic_gate_mode": gate.mode,
                    "semantic_gate_reason": decision.reason,
                }, "extraction_method": "deterministic_plus_agent"})
                by_id[member_id] = marked
                changed.append(marked)
        if changed:
            self._persist_units(changed)

    async def _classify_with_agent(
        self,
        items: Sequence[Any],
        analyzer: Callable[[str, dict[str, Any]], Awaitable[Any]],
        *,
        cache_namespace: str | None = None,
        execution_profile: ResolvedExecutionProfile | None = None,
        metrics: MaterialPipelineMetrics | None = None,
        window_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        context_units: Sequence[Any] = (),
        raw_units: dict[str, EvidenceUnit] | None = None,
        persona_id: str | None = None,
        context_before: Sequence[Any] = (),
        context_after: Sequence[Any] = (),
        gate: SemanticGate | None = None,
        window_submit: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]] | None = None,
        gate_mode: str | None = None,
    ) -> list[EvidenceUnit]:
        """Sparse conversation-evidence-v4 classification over analysis turns.

        Accepts raw EvidenceUnits (in-memory document path; folded into turns
        here) or pre-folded ConversationTurns (streaming chat path with the
        raw-unit snapshot in raw_units).  Windows carry target turns plus the
        context-only turns that explain them; only target turns are semantic
        work.  A structurally successful window means every target turn inside
        it was reviewed: turns absent from the model output become
        reviewed_no_independent_evidence locally and are NEVER a retry
        trigger.  Retries stay reserved for parse failures, unknown ids,
        out-of-window support ids, unknown dimensions and runtime errors.
        """

        started_at = time.perf_counter()
        profile = execution_profile or ResolvedExecutionProfile()
        active_gate_mode = gate_mode or self.semantic_gate_mode
        if not items:
            return []
        from_turns = hasattr(items[0], "evidence_unit_ids") and hasattr(items[0], "anchor_id")
        seed_units: list[EvidenceUnit] = []
        if from_turns:
            all_turns: list[ConversationTurn] = list(items)
            by_id: dict[str, EvidenceUnit] = dict(raw_units or {})
        else:
            seed_units = [item for item in items]
            by_id = {unit.id: unit for unit in seed_units}
            # Conversation order anchored at the caller's source order
            # (not the random source id): window composition is stable
            # across reruns, so a completed window's checkpoint matches
            # the next dispatch's window split exactly.
            source_order: dict[str, int] = {}
            for unit in seed_units:
                source_order.setdefault(str(unit.source_id), len(source_order))
            all_turns = fold_conversation_turns(
                sorted(
                    seed_units,
                    key=lambda unit: (
                        source_order.get(str(unit.source_id), 0),
                        int(unit.source_locator.get("segment_index") or 0),
                        unit.id,
                    ),
                ),
                gap_seconds=self.turn_gap_seconds,
            )
        turn_member_ids: dict[str, list[str]] = {
            turn.id: list(turn.evidence_unit_ids) for turn in all_turns
        }
        if persona_id is None:
            persona_id = next((unit.persona_id for unit in by_id.values()), None)
        if metrics is not None:
            metrics.semantic_gate_mode = active_gate_mode
            for key, value in self._turn_metrics_delta(all_turns).items():
                setattr(metrics, key, getattr(metrics, key) + value)
        self._apply_semantic_gate(
            all_turns, by_id, profile, gate or SemanticGate(active_gate_mode),
            metrics, context_before[-1] if context_before else None,
            gate_mode=active_gate_mode,
        )
        selected = [
            turn
            for turn in all_turns
            if turn.is_target
            and turn.anchor_id in by_id
            and not self._classification_done(
                by_id[turn.anchor_id], profile, gate_mode=active_gate_mode
            )
        ]
        selected_ids = {turn.id for turn in selected}
        if metrics is not None:
            metrics.cache_hits["intelligence"] = metrics.cache_hits.get("intelligence", 0) + (
                sum(1 for turn in all_turns if turn.is_target) - len(selected)
            )
        # Borrowed context turns need raw rows for rendering; pull the few
        # anchors we do not already hold.
        missing_context = [
            turn.anchor_id
            for turn in [*context_before, *context_after]
            if turn.anchor_id not in by_id
        ]
        if persona_id and missing_context:
            for unit in self._units_by_ids(persona_id, missing_context):
                by_id[unit.id] = unit
        if not selected:
            return self._classification_result(from_turns, all_turns, seed_units, by_id)

        shared_cache = None
        if cache_namespace:
            from persona_continuum.performance.research_cache import (
                default_source_intelligence_cache,
            )

            shared_cache = default_source_intelligence_cache()

        own_turn_ids = {turn.id for turn in selected}

        def render_turn(turn: ConversationTurn) -> dict[str, Any]:
            anchor = by_id.get(turn.anchor_id)
            role = (anchor.speaker_role if anchor is not None else None) or turn.speaker_role
            # A borrowed neighbour turn explains this window; it is billed
            # (and marked complete) by its own group, never twice.
            own_target = turn.is_target and turn.id in own_turn_ids
            row: dict[str, Any] = {
                "id": turn.anchor_id,
                "evidence_ids": list(turn.evidence_unit_ids),
                "text": turn.text,
                "source_id": turn.source_id,
                "semantic_role": "target" if own_target else "context",
            }
            if role:
                row["speaker_role"] = role
            if turn.speaker:
                row["speaker"] = turn.speaker
            if turn.end_time:
                row["timestamp"] = turn.end_time
            if turn.source_kind:
                row["source_kind"] = turn.source_kind
            return row

        budget = self.context_budget_manager.budget_for(
            model=profile.model_dump(mode="json"),
            phase="material_classification",
            expected_output=MATERIAL_AGENT_OUTPUT_SCHEMAS["classify"],
        )
        if budget.phase_working_target is not None:
            profile.phase_working_target = budget.phase_working_target
        target_tokens = material_batch_target_tokens(
            profile,
            phase_usable_budget=budget.evidence_token_budget,
            phase_policy=self.context_budget_manager.phase_policy,
        )
        units_cap = auto_units_cap(
            target_tokens,
            configured=self.analysis_window_max_units,
            token_budget_verified=bool(profile.context_verified),
        )
        episodes_cap = auto_episodes_cap(
            target_tokens,
            configured=self.analysis_window_max_episodes,
            token_budget_verified=bool(profile.context_verified),
        )
        transport = profile.prompt_transport
        safe_bytes = transport.safe_prompt_bytes if transport else None
        request_estimator = ClassificationRequestTokenEstimator(
            self.context_budget_manager.estimate_tokens
        )
        extra_contracts = (
            "Episodes are independent conversation blocks: never connect "
            "question/answer or reference relations across episodes; "
            "preserve original order inside each episode.",
            "Return only target turns that contain independent persona "
            "evidence. Omitted target turns are considered reviewed with "
            "no independent evidence. Do not return reviewed_ids.",
        )
        # Windows pack turns in conversation order; context-only turns ride
        # along and never consume the target ceiling (P0-C/F).
        # P0-F: adjacent turns from the neighboring groups ride along as
        # context so a window never sees a bare fragment of a conversation.
        # P0.3-D: only *selected* target turns are semantic work for this
        # dispatch.  Skipped (balanced gate) and already-reviewed turns that
        # ride along as ±1 neighbours are demoted to context_only so they
        # never consume the max_units ceiling, the target counts, or the
        # semantic billing metrics.
        dispatch_turns = [
            *(turn.model_copy(update={"semantic_role": "context_only"}) for turn in context_before),
            *(
                turn
                if turn.id in selected_ids
                else turn.model_copy(update={"semantic_role": "context_only"})
                for turn in all_turns
            ),
            *(turn.model_copy(update={"semantic_role": "context_only"}) for turn in context_after),
        ]
        if active_gate_mode != "full":
            keep_indices = {
                nearby
                for index, turn in enumerate(dispatch_turns) if turn.id in selected_ids
                for nearby in (index - 1, index, index + 1)
                if 0 <= nearby < len(dispatch_turns)
            }
            dispatch_turns = [
                turn for index, turn in enumerate(dispatch_turns) if index in keep_indices
            ]
        for extra in [*context_before, *context_after]:
            turn_member_ids.setdefault(extra.id, list(extra.evidence_unit_ids))
        source_context = self._source_context_for_units(list(by_id.values()))
        base_tokens = request_estimator.base_tokens(
            source_context=source_context,
            extra_contracts=extra_contracts,
            allowed_dimensions=REQUIRED_DIMENSIONS,
        )
        # P0.3-B/C: a 2h+ gap starts a new ConversationEpisode (semantic
        # atomic block) but no longer forces a model dispatch flush.  Many
        # episodes pack into one window until a budget/ceiling is reached.
        windows = build_analysis_windows(
            dispatch_turns,
            estimate_tokens=self.context_budget_manager.estimate_tokens,
            target_tokens=target_tokens,
            max_units=units_cap,
            estimate_unit_tokens=lambda turn: request_estimator.turn_tokens(render_turn(turn)),
            episode_gap_seconds=self.conversation_episode_gap_seconds,
            max_episodes_per_window=episodes_cap,
            base_tokens=base_tokens,
            episode_overhead_tokens=request_estimator.episode_overhead(),
        )
        if metrics is not None:
            metrics.analysis_windows += len(windows)
            metrics.initial_analysis_windows += len(windows)
            metrics.classification_windows_total += len(windows)
            metrics.max_batch_target_tokens = target_tokens
            metrics.transport_mode = transport.transport_mode if transport else None
            metrics.transport_max_prompt_bytes = transport.max_prompt_bytes if transport else None
            metrics.record_analysis_windows(windows, target_tokens=target_tokens)
        completed_windows = 0
        completed_units = 0
        dispatch_by_id = {turn.id: turn for turn in dispatch_turns}
        # Only pending target turns are billed this dispatch; an already-done
        # neighbour keeps its row in the conversation flow but renders as
        # context, so a resume run never re-sends finished work as a target.
        own_turn_ids = {turn.id for turn in selected}

        target_turn_total = sum(1 for turn in all_turns if turn.is_target)
        semantic_completed = target_turn_total - len(selected)

        async def report(state: str, **info: Any) -> None:
            if window_progress is not None:
                await window_progress(
                    {
                        "prompt_state": state,
                        "windows_total": len(windows),
                        "windows_completed": completed_windows,
                        "units_completed": completed_units,
                        "classification_total": target_turn_total,
                        "classification_completed": min(target_turn_total, semantic_completed),
                        "classification_pending": max(0, target_turn_total - semantic_completed),
                        **info,
                    }
                )

        await report(MaterialPromptState.PREPARING_INPUT.value)
        semaphore = asyncio.Semaphore(
            profile.classification_worker_count(self.max_llm_concurrency)
        )

        def make_request(
            window: Any, part: Sequence[ConversationTurn], index: int
        ) -> dict[str, Any]:
            # P0-F: one row per turn (evidence body appears exactly once);
            # target_units/context_units stay id-only, so nothing is billed
            # twice and speaker_role/semantic_role travel with every row.
            rows = [render_turn(turn) for turn in part]
            target_ids = [row["id"] for row in rows if row["semantic_role"] == "target"]
            context_ids = [row["id"] for row in rows if row["semantic_role"] == "context"]
            source_ids = sorted({turn.source_id for turn in part})
            part_ids = {turn.id for turn in part}
            # P0.3-B: the episode structure travels with the request so the
            # model knows which rows form one continuous conversation.  A
            # budget-split episode reports the contained slice only.
            episodes = [
                {**episode, "unit_ids": [uid for uid in episode["unit_ids"] if uid in part_ids]}
                for episode in getattr(window, "episodes", [])
            ]
            episodes = [episode for episode in episodes if episode["unit_ids"]]
            return {
                "_participant_id": f"persona_material_intelligence:{window.id}",
                "analysis_window": {
                    "id": window.id,
                    "source_ids": source_ids,
                    "episode_count": len(episodes),
                },
                "episodes": episodes,
                "target_units": target_ids,
                "context_units": context_ids,
                "units": rows,
                "episode_contract": (
                    "Episodes are independent conversation blocks: never connect "
                    "question/answer or reference relations across episodes; "
                    "preserve original order inside each episode."
                ),
                "source_context": {
                    key: value for key, value in source_context.items() if key in set(source_ids)
                },
                "allowed_dimensions": list(REQUIRED_DIMENSIONS),
                "output_contract": (
                    "Return only target turns that contain independent persona "
                    "evidence. Omitted target turns are considered reviewed with "
                    "no independent evidence. Do not return reviewed_ids."
                ),
            }

        async def analyze_window(index: int, window: Any) -> None:
            nonlocal completed_windows, completed_units, semantic_completed, target_tokens
            if profile.persistent_session:
                live_budget = self.context_budget_manager.budget_for(
                    model=profile.model_dump(mode="json"),
                    phase="material_classification",
                    expected_output=MATERIAL_AGENT_OUTPUT_SCHEMAS["classify"],
                )
                target_tokens = material_batch_target_tokens(
                    profile,
                    phase_usable_budget=live_budget.evidence_token_budget,
                    phase_policy=self.context_budget_manager.phase_policy,
                )
            part_all = [
                dispatch_by_id[tid] for tid in window.evidence_unit_ids if tid in dispatch_by_id
            ]
            chunk_owner: dict[str, str] = {}
            remaining_chunks: dict[str, set[str]] = {}

            async def run_part(part: list[ConversationTurn], attempts: int = 0) -> None:
                nonlocal completed_units
                request = make_request(window, part, index)
                if not request["target_units"]:
                    return
                over_reason = self._request_over_budget(
                    request, target_tokens=target_tokens, transport_safe_bytes=safe_bytes
                )
                if over_reason:
                    if len(part) > 1:
                        mid = len(part) // 2
                        if metrics is not None:
                            metrics.record_rebatch(over_reason)
                        await run_part(part[:mid], attempts)
                        await run_part(part[mid:], attempts)
                        return
                    anchor = by_id.get(part[0].anchor_id)
                    if anchor is None:
                        return
                    if anchor.id in chunk_owner:
                        raise PromptTransportLimitExceededError(
                            "material_chunk_exceeds_budget", phase="material_classification"
                        )
                    chunks = _chunk_oversized_unit(
                        anchor,
                        estimate_tokens=self.context_budget_manager.estimate_tokens,
                        max_piece_tokens=max(
                            64,
                            min(
                                target_tokens // 4,
                                transport.prompt_token_budget() // 2
                                if transport
                                else target_tokens // 4,
                            ),
                        ),
                    )
                    if not chunks:
                        raise PromptTransportLimitExceededError(
                            "material_unit_exceeds_budget", phase="material_classification"
                        )
                    remaining_chunks[anchor.id] = {chunk.id for chunk in chunks}
                    chunk_owner.update({chunk.id: anchor.id for chunk in chunks})
                    mini_turns: list[ConversationTurn] = []
                    for chunk in chunks:
                        own_turn_ids.add(chunk.id)
                        selected_ids.add(chunk.id)
                        by_id[chunk.id] = chunk
                        turn_member_ids[chunk.id] = [chunk.id]
                        mini_turns.append(turn_from_units([chunk]))
                    if metrics is not None:
                        metrics.chunked_oversized_units += 1
                    for one in mini_turns:
                        await run_part([one], attempts)
                    return
                if shared_cache is not None and cache_namespace:
                    pending_turns: list[ConversationTurn] = []
                    for turn in part:
                        base = by_id.get(turn.anchor_id)
                        cached = None
                        if base is not None and base.source_kind not in {
                            "chat",
                            "chat_import",
                            "guided_interview",
                        }:
                            cached = shared_cache.get(cache_namespace, base.normalized_text)
                        if cached is None or base is None:
                            pending_turns.append(turn)
                            continue
                        applied = self._apply_classification_value(
                            base, {**cached, "id": base.id}, profile
                        )
                        by_id[base.id] = applied
                        self._persist_units([applied])
                        if metrics is not None:
                            metrics.cache_hits["intelligence"] = (
                                metrics.cache_hits.get("intelligence", 0) + 1
                            )
                            metrics.evidence_turns_extracted += 1
                    if not pending_turns:
                        return
                    part = pending_turns
                    request = make_request(window, part, index)
                await report(MaterialPromptState.PROMPT_BUILDING.value)
                prompt_tokens, prompt_bytes = self._serialized_prompt_size(request)
                if metrics is not None:
                    metrics.record_prompt_estimate(
                        request_estimator.estimate_request(
                            system_prompt=MATERIAL_AGENT_SYSTEM_PROMPTS["classify"],
                            schema=MATERIAL_AGENT_OUTPUT_SCHEMAS["classify"],
                            source_context=request.get("source_context"),
                            extra_contracts=extra_contracts,
                            allowed_dimensions=REQUIRED_DIMENSIONS,
                            rows=request.get("units") or [],
                            episode_count=len(request.get("episodes") or []),
                        ),
                        prompt_tokens,
                    )
                    metrics.agent_turns["intelligence"] = (
                        metrics.agent_turns.get("intelligence", 0) + 1
                    )
                    metrics.input_tokens["intelligence"] = (
                        metrics.input_tokens.get("intelligence", 0) + prompt_tokens
                    )
                    metrics.agent_calls += 1
                    metrics.max_serialized_prompt_tokens = max(
                        metrics.max_serialized_prompt_tokens or 0, prompt_tokens
                    )
                    metrics.max_serialized_prompt_bytes = max(
                        metrics.max_serialized_prompt_bytes or 0, prompt_bytes
                    )
                allowed_support: set[str] = set(chunk_owner)
                for turn in part:
                    allowed_support.update(turn_member_ids.get(turn.id, [turn.id]))
                try:
                    payload = await analyzer("classify", request)
                    values = self._classification_values(
                        payload,
                        [turn for turn in part if turn.is_target and turn.id in own_turn_ids],
                        allowed_support,
                    )
                except AgentRuntimeError as exc:
                    retryable = isinstance(exc, AgentStructuredOutputError) or exc.code in {
                        "AGENT_PROCESS_EXITED_WITH_PARTIAL_OUTPUT",
                        "STRUCTURED_OUTPUT_PARSE_FAILED",
                        "STRUCTURED_OUTPUT_SCHEMA_FAILED",
                        "STRUCTURED_OUTPUT_REPAIR_FAILED",
                    }
                    if not retryable or attempts >= 2:
                        raise
                    mid = max(1, len(part) // 2)
                    await run_part(part[:mid], attempts + 1)
                    if part[mid:]:
                        await run_part(part[mid:], attempts + 1)
                    return
                if metrics is not None:
                    metrics.output_tokens["intelligence"] = metrics.output_tokens.get(
                        "intelligence", 0
                    ) + self.context_budget_manager.estimate_tokens(payload)
                durable: list[EvidenceUnit] = []
                emitted: set[str] = set()
                for value in values:
                    raw_id = value["id"]
                    owner = chunk_owner.get(raw_id, raw_id)
                    base = by_id.get(owner)
                    if base is None:
                        continue
                    value["supporting_evidence_ids"] = list(
                        dict.fromkeys(
                            chunk_owner.get(eid, eid)
                            for eid in value.get("supporting_evidence_ids", [owner])
                        )
                    )
                    independent = self._value_has_independent_evidence(value)
                    applied = self._apply_classification_value(
                        base,
                        value,
                        profile,
                        semantic_status=(
                            SEMANTIC_EVIDENCE_EXTRACTED
                            if independent
                            else SEMANTIC_REVIEWED_NO_EVIDENCE
                        ),
                    )
                    by_id[owner] = applied
                    if owner not in emitted:
                        emitted.add(owner)
                        durable.append(applied)
                        if metrics is not None and independent:
                            metrics.evidence_turns_extracted += 1
                    if (
                        shared_cache is not None
                        and cache_namespace
                        and applied.source_kind not in {"chat", "chat_import", "guided_interview"}
                    ):
                        shared_cache.put(cache_namespace, applied.normalized_text, value)
                # Members of an emitted turn are covered by that turn id
                # (traceable through supporting_evidence_ids): stamp them so no
                # raw row stays target_pending after a successful review.
                for turn in part:
                    if not turn.is_target:
                        continue
                    members = turn_member_ids.get(turn.id, [turn.id])
                    if turn.anchor_id not in emitted or len(members) < 2:
                        continue
                    for member_id in members:
                        if member_id == turn.anchor_id or "#c" in member_id:
                            continue
                        base = by_id.get(member_id)
                        if base is None or self._classification_done(base, profile):
                            continue
                        marked = base.model_copy(
                            update={
                                "metadata": {
                                    **base.metadata,
                                    "semantic_status": SEMANTIC_EVIDENCE_EXTRACTED,
                                    "classification_status": "completed",
                                    "classification_contract": CLASSIFICATION_CONTRACT_V4,
                                    "intelligence_member_of": turn.anchor_id,
                                },
                                "extraction_method": "deterministic_plus_agent",
                            }
                        )
                        by_id[member_id] = marked
                        durable.append(marked)

                # Sparse V4 core: every target turn of a structurally
                # successful window that the model did not emit is locally
                # marked reviewed_no_independent_evidence.  This is a
                # checkpoint, not an error: no re-request, no re-billing.
                reviewed_turn_ids: list[str] = []
                reviewed_member_ids: list[str] = []
                for turn in part:
                    if (
                        not turn.is_target
                        or turn.id not in selected_ids
                        or turn.id not in own_turn_ids
                    ):
                        continue
                    members = turn_member_ids.get(turn.id, [turn.id])
                    if any(member in emitted for member in members):
                        continue
                    reviewed_turn_ids.append(turn.id)
                    for member_id in members:
                        if "#c" in member_id or member_id in emitted:
                            continue
                        reviewed_member_ids.append(member_id)
                        base = by_id.get(member_id)
                        if base is None or self._classification_done(base, profile):
                            continue
                        marked = self._mark_unit_reviewed_no_evidence(base, profile)
                        by_id[member_id] = marked
                        durable.append(marked)
                if reviewed_member_ids and persona_id:
                    self._mark_units_status(
                        persona_id, reviewed_member_ids, SEMANTIC_REVIEWED_NO_EVIDENCE
                    )
                if metrics is not None:
                    metrics.reviewed_no_independent_evidence += len(reviewed_turn_ids)
                if durable:
                    self._persist_units(durable)
                progressed = len(durable)
                if progressed:
                    completed_units += progressed
                    await report(
                        MaterialPromptState.CHECKPOINTING.value,
                        newly_completed_units=progressed,
                    )

            async with semaphore:
                if not any(turn.id in selected_ids for turn in part_all):
                    # Pure-context window: nothing to bill; the pending
                    # targets inside belong to another dispatch group.
                    completed_windows += 1
                    if metrics is not None:
                        metrics.classification_windows_completed += 1
                    return
                try:
                    await run_part(part_all)
                    completed_windows += 1
                    if metrics is not None:
                        metrics.classification_windows_completed += 1
                        target_in_window = sum(
                            1 for turn in part_all if turn.is_target and turn.id in own_turn_ids
                        )
                        metrics.max_turns_per_window = max(
                            metrics.max_turns_per_window, target_in_window
                        )
                        metrics.target_turns_reviewed += target_in_window
                    await report(MaterialPromptState.CHECKPOINTING.value)
                except BaseException:
                    raise

        if window_submit is not None:
            for index, window in enumerate(windows):
                if not any(tid in selected_ids for tid in window.evidence_unit_ids):
                    continue
                async def work(index: int = index, window: Any = window) -> None:
                    await analyze_window(index, window)
                await window_submit(work)
            return []
        tasks = [
            asyncio.create_task(analyze_window(index, window))
            for index, window in enumerate(windows)
        ]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, asyncio.CancelledError
                ):
                    raise outcome
            if any(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes):
                raise asyncio.CancelledError
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if metrics is not None:
            metrics.elapsed_ms["intelligence"] = (
                metrics.elapsed_ms.get("intelligence", 0)
                + (time.perf_counter() - started_at) * 1000
            )
            windows_done = metrics.classification_windows_completed or 1
            if metrics.target_turn_count:
                metrics.average_turns_per_window = round(
                    metrics.target_turn_count / windows_done, 3
                )
            metrics.finalize_window_metrics()
        return self._classification_result(from_turns, all_turns, seed_units, by_id)

    def _classification_result(
        self,
        from_turns: bool,
        all_turns: Sequence[ConversationTurn],
        seed_units: Sequence[EvidenceUnit],
        by_id: dict[str, EvidenceUnit],
    ) -> list[EvidenceUnit]:
        if from_turns:
            anchor_ids = list(dict.fromkeys(turn.anchor_id for turn in all_turns))
            return [by_id[mid] for mid in anchor_ids if mid in by_id and "#c" not in mid]
        return [by_id.get(unit.id, unit) for unit in seed_units]

    def _turn_metrics_delta(self, turns: Sequence[ConversationTurn]) -> dict[str, int]:
        target_turns = sum(1 for turn in turns if turn.is_target)
        target_messages = sum(len(turn.evidence_unit_ids) for turn in turns if turn.is_target)
        context_messages = sum(len(turn.evidence_unit_ids) for turn in turns if not turn.is_target)
        return {
            "conversation_turn_count": len(turns),
            "target_turn_count": target_turns,
            "target_message_count": target_messages,
            "context_message_count": context_messages,
        }

    @staticmethod
    def _material_intelligence_cache_key(
        unit: EvidenceUnit,
        profile: ResolvedExecutionProfile,
        *,
        legacy: bool = False,
        contract: str | None = None,
    ) -> str:
        """Versioned checkpoint key (P0-G / BLOCKER 12).

        Three explicit generations coexist so an interrupted run never
        re-bills finished work and never mixes contracts silently:

        * legacy:    the original pre-v3 key shape (no evidence scope);
        * v3:        byte-identical reproduction of the historical
          conversation-evidence-v3 key, so already-successful v3 rows are read
          (and optionally migrated) instead of re-analyzed;
        * default:   the conversation-evidence-v4 key, which additionally
          hashes the classification contract and the turn/window policy
          version.  Changing the folding/window policy bumps
          TURN_POLICY_VERSION and produces a new namespace instead of
          silently reusing an incompatible cached result.
        """

        base = {
            "material_hash": _sha(unit.normalized_text),
            "parser_version": "material-parser-v2",
            "intelligence_schema_version": "evidence-intelligence-v2",
            "model_id": profile.model_id,
            "reasoning": profile.selected_reasoning_effort,
        }
        if legacy:
            payload = {**base, "prompt_version": _sha(LEGACY_CLASSIFICATION_PROMPT)}
            return _sha(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        evidence_scope = _sha(
            json.dumps([unit.id, unit.source_id, unit.speaker, unit.timestamp], ensure_ascii=False)
        )
        if contract == "v3":
            payload = {
                **base,
                "prompt_version": _sha(CLASSIFICATION_PROMPT_V3),
                "evidence_scope": evidence_scope,
            }
            return _sha(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        payload = {
            **base,
            "prompt_version": _sha(MATERIAL_AGENT_SYSTEM_PROMPTS["classify"]),
            "evidence_scope": evidence_scope,
            "classification_contract": CLASSIFICATION_CONTRACT_V4,
            "turn_policy": TURN_POLICY_VERSION,
        }
        return _sha(json.dumps(payload, sort_keys=True, ensure_ascii=False))

    @staticmethod
    def _propagate_canonical_intelligence(
        units: Sequence[EvidenceUnit], supporting_units: dict[str, list[str]]
    ) -> list[EvidenceUnit]:
        """Copy derived intelligence to duplicates while retaining their locators."""

        by_id = {item.id: item for item in units}
        for canonical_id, member_ids in supporting_units.items():
            canonical = by_id.get(canonical_id)
            if canonical is None:
                continue
            for member_id in member_ids:
                member = by_id.get(member_id)
                if member is None or member_id == canonical_id:
                    continue
                metadata = dict(member.metadata)
                if canonical.metadata.get("evidence_intelligence"):
                    metadata["evidence_intelligence"] = canonical.metadata["evidence_intelligence"]
                metadata["intelligence_reused_from"] = canonical_id
                by_id[member_id] = member.model_copy(
                    update={
                        "evidence_type": canonical.evidence_type,
                        "dimension_candidates": list(canonical.dimension_candidates),
                        "dimension_scores": dict(canonical.dimension_scores),
                        "life_stage_candidates": list(canonical.life_stage_candidates),
                        "relationship_entities": list(canonical.relationship_entities),
                        "context_tags": list(canonical.context_tags),
                        "confidence": canonical.confidence,
                        "extraction_method": "canonical_intelligence_reuse",
                        "metadata": metadata,
                    }
                )
        return [by_id[item.id] for item in units]

    async def _relate_with_agent(
        self,
        units: Sequence[EvidenceUnit],
        clusters: Sequence[EvidenceCluster],
        analyzer: Callable[[str, dict[str, Any]], Awaitable[Any]],
        *,
        candidate_groups: Sequence[CandidateGroup] | None = None,
        execution_profile: ResolvedExecutionProfile | None = None,
        metrics: MaterialPipelineMetrics | None = None,
    ) -> tuple[list[EvidenceCluster], list[PersonaContradiction]]:
        started_at = time.perf_counter()
        groups = list(candidate_groups or self.candidate_index.groups(units))
        known_ids = {item.id for item in units}
        by_id = {item.id: item for item in units}
        parent = {item.id: item.id for item in units}

        def compact_relation_value(value: Any, *, max_chars: int = 512) -> Any:
            """Bound derived fields while preserving relation-relevant structure."""

            if isinstance(value, str):
                return value[:max_chars]
            if isinstance(value, dict):
                return {
                    str(key)[:64]: compact_relation_value(item, max_chars=max_chars)
                    for key, item in list(value.items())[:12]
                }
            if isinstance(value, list):
                return [compact_relation_value(item, max_chars=max_chars) for item in value[:12]]
            if value is None or isinstance(value, (bool, int, float)):
                return value
            return str(value)[:max_chars]

        def relation_row(item: EvidenceUnit) -> dict[str, Any]:
            intelligence = item.metadata.get("evidence_intelligence")
            if not isinstance(intelligence, dict):
                intelligence = {}
            return {
                "id": item.id,
                "claim_fingerprint": item.normalized_text[:320],
                "claims": compact_relation_value(list(intelligence.get("claims") or [])[:8]),
                "events": compact_relation_value(list(intelligence.get("events") or [])[:8]),
                "dates": compact_relation_value(list(intelligence.get("dates") or [])[:8]),
                "entities": compact_relation_value(list(intelligence.get("entities") or [])[:12]),
                "contradiction_hints": compact_relation_value(
                    list(intelligence.get("contradictions") or [])[:8]
                ),
                "timestamp": item.timestamp,
                "context_tags": item.context_tags,
                "source_kind": item.source_kind,
            }

        def find(value: str) -> str:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for cluster in clusters:
            members = [value for value in cluster.member_evidence_ids if value in known_ids]
            for member in members[1:]:
                union(members[0], member)

        semantic_contradictions: list[PersonaContradiction] = []
        phase = "relate"
        profile = execution_profile or ResolvedExecutionProfile()
        budget = self.context_budget_manager.budget_for(
            model=profile.model_dump(mode="json"),
            phase="semantic_relation",
            expected_output=MATERIAL_AGENT_OUTPUT_SCHEMAS[phase],
        )
        if budget.phase_working_target is not None:
            profile.phase_working_target = budget.phase_working_target
        target_tokens = material_batch_target_tokens(
            profile,
            phase_usable_budget=budget.evidence_token_budget,
            phase_policy=self.context_budget_manager.phase_policy,
        )
        transport = profile.prompt_transport
        transport_payload_bytes = None
        if transport is not None:
            # RuntimeExecutor measures system + user bytes.  The request guard
            # owns only the JSON user payload, so reserve the fixed system
            # prompt here before checking the transport ceiling.
            transport_payload_bytes = max(
                1,
                transport.safe_prompt_bytes
                - len(MATERIAL_AGENT_SYSTEM_PROMPTS[phase].encode("utf-8")),
            )

        def make_request(batch: Sequence[CandidateGroup]) -> dict[str, Any]:
            return {
                "units": [
                    relation_row(by_id[evidence_id])
                    for evidence_id in sorted(
                        {
                            evidence_id
                            for group in batch
                            for evidence_id in group.evidence_ids
                            if evidence_id in by_id
                        }
                    )
                ],
                "candidate_groups": [
                    {
                        "id": group.id,
                        "signals": group.signals,
                        "units": [
                            relation_row(by_id[evidence_id])
                            for evidence_id in group.evidence_ids
                            if evidence_id in by_id
                        ],
                    }
                    for group in batch
                ],
            }

        def transport_safe_batches(
            batch: list[CandidateGroup],
        ) -> list[tuple[list[CandidateGroup], dict[str, Any]]]:
            request = make_request(batch)
            if self._request_fits_budget(
                request,
                target_tokens=target_tokens,
                transport_safe_bytes=transport_payload_bytes,
            ):
                return [(batch, request)]
            if len(batch) > 1:
                midpoint = len(batch) // 2
                return transport_safe_batches(batch[:midpoint]) + transport_safe_batches(
                    batch[midpoint:]
                )
            if batch and len(batch[0].evidence_ids) > 2:
                # Keep one boundary item in both halves so adjacent relation
                # evidence is not lost when a single candidate group is too
                # large for the carrier.
                group = batch[0]
                midpoint = len(group.evidence_ids) // 2
                left = group.model_copy(
                    update={
                        "id": f"{group.id}#p1",
                        "evidence_ids": group.evidence_ids[: midpoint + 1],
                    }
                )
                right = group.model_copy(
                    update={
                        "id": f"{group.id}#p2",
                        "evidence_ids": group.evidence_ids[midpoint:],
                    }
                )
                return transport_safe_batches([left]) + transport_safe_batches([right])
            prompt_tokens, prompt_bytes = self._serialized_prompt_size(request)
            raise PromptTransportLimitExceededError(
                "PROMPT_TRANSPORT_LIMIT_EXCEEDED: relation candidate group cannot be split",
                phase="semantic_relation",
                diagnostics={
                    "candidate_group_id": batch[0].id if batch else None,
                    "transport_mode": transport.transport_mode if transport else None,
                    "transport_safe_prompt_bytes": (
                        transport.safe_prompt_bytes if transport else None
                    ),
                    "estimated_payload_bytes": prompt_bytes,
                    "estimated_payload_tokens": prompt_tokens,
                    "target_tokens": target_tokens,
                },
            )

        for context_batch in self.context_budget_manager.iter_batches(
            groups,
            item_text=lambda group: json.dumps(
                {
                    "candidate_group_id": group.id,
                    "signals": group.signals,
                    "units": [
                        relation_row(by_id[evidence_id])
                        for evidence_id in group.evidence_ids
                        if evidence_id in by_id
                    ],
                },
                ensure_ascii=False,
            ),
            max_items=12,
            phase="semantic_relation",
            model=profile.model_dump(mode="json"),
            base_text=json.dumps({"operation": phase}, ensure_ascii=False),
            system_prompt=MATERIAL_AGENT_SYSTEM_PROMPTS[phase],
            expected_output=MATERIAL_AGENT_OUTPUT_SCHEMAS[phase],
        ):
            for batch, request in transport_safe_batches(context_batch):
                prompt_tokens, prompt_bytes = self._serialized_prompt_size(request)
                if metrics is not None:
                    metrics.max_serialized_prompt_tokens = max(
                        metrics.max_serialized_prompt_tokens or 0, prompt_tokens
                    )
                    metrics.max_serialized_prompt_bytes = max(
                        metrics.max_serialized_prompt_bytes or 0, prompt_bytes
                    )
                    metrics.transport_mode = transport.transport_mode if transport else None
                    metrics.transport_max_prompt_bytes = (
                        transport.max_prompt_bytes if transport else None
                    )
                payload = await analyzer("relate", request)
                if metrics is not None:
                    metrics.agent_turns["relation"] = metrics.agent_turns.get("relation", 0) + 1
                    metrics.input_tokens["relation"] = metrics.input_tokens.get(
                        "relation", 0
                    ) + sum(
                        self.context_budget_manager.estimate_tokens(
                            " ".join(
                                by_id[item].text for item in group.evidence_ids if item in by_id
                            )
                        )
                        for group in batch
                    )
                    metrics.output_tokens["relation"] = metrics.output_tokens.get(
                        "relation", 0
                    ) + self.context_budget_manager.estimate_tokens(payload)
                if not isinstance(payload, dict):
                    continue
                for group in payload.get("clusters", []):
                    ids = [
                        str(value)
                        for value in (
                            group.get("evidence_ids", []) if isinstance(group, dict) else []
                        )
                        if str(value) in known_ids
                    ]
                    for member in ids[1:]:
                        union(ids[0], member)
                for value in payload.get("contradictions", []):
                    if not isinstance(value, dict):
                        continue
                    ids = [
                        str(item)
                        for item in value.get("evidence_ids", [])
                        if str(item) in known_ids
                    ]
                    if len(ids) < 2:
                        continue
                    try:
                        typed = MaterialRelationResult.model_validate(
                            {
                                **value,
                                "id": value.get("id")
                                or f"relation_{_sha('|'.join(sorted(ids)))[:16]}",
                                "content": value.get("content")
                                or value.get("summary")
                                or "evidence relation",
                                "source_id": value.get("source_id") or ids[0],
                            }
                        )
                    except Exception:
                        continue
                    contradiction_type = str(value.get("contradiction_type") or "FACT_CONFLICT")
                    if contradiction_type not in {
                        "FACT_CONFLICT",
                        "TEMPORAL_CHANGE",
                        "CONTEXT_DEPENDENT",
                        "SELF_VS_THIRD_PARTY",
                    }:
                        contradiction_type = "FACT_CONFLICT"
                    semantic_contradictions.append(
                        PersonaContradiction(
                            id=f"cn_{_sha('|'.join(sorted(ids)))[:16]}",
                            persona_id=units[0].persona_id,
                            contradiction_type=contradiction_type,
                            evidence_ids=ids,
                            summary=str(
                                typed.summary
                                or typed.content
                                or "Agent identified an evidence conflict"
                            )[:1000],
                            conditions=[str(item) for item in typed.conditions if str(item)],
                            resolution="preserve_both_with_scope",
                            confidence=typed.confidence,
                        )
                    )

        grouped: dict[str, list[EvidenceUnit]] = defaultdict(list)
        for unit in units:
            grouped[find(unit.id)].append(unit)
        original_groups = {frozenset(item.member_evidence_ids): item for item in clusters}
        now = datetime.now(UTC).isoformat()
        merged: list[EvidenceCluster] = []
        for evidence_members in grouped.values():
            member_ids = sorted(item.id for item in evidence_members)
            previous = original_groups.get(frozenset(member_ids))
            merged.append(
                EvidenceCluster(
                    id=f"evc_{_sha('|'.join(member_ids))[:16]}",
                    persona_id=evidence_members[0].persona_id,
                    cluster_type=(
                        previous.cluster_type if previous is not None else "semantic_related"
                    ),
                    canonical_evidence_id=(
                        previous.canonical_evidence_id if previous is not None else member_ids[0]
                    ),
                    member_evidence_ids=member_ids,
                    similarity_type=(
                        previous.similarity_type if previous is not None else "agent_semantic"
                    ),
                    confidence=(previous.confidence if previous is not None else 0.82),
                    created_at=now,
                    updated_at=now,
                )
            )
        if metrics is not None:
            metrics.elapsed_ms["relation"] = (time.perf_counter() - started_at) * 1000
        return merged, semantic_contradictions

    async def _fuse_with_agent(
        self,
        fused: Sequence[FusedEvidence],
        units: Sequence[EvidenceUnit],
        analyzer: Callable[[str, dict[str, Any]], Awaitable[Any]],
        *,
        selected_ids: set[str] | None = None,
        execution_profile: ResolvedExecutionProfile | None = None,
        metrics: MaterialPipelineMetrics | None = None,
    ) -> list[FusedEvidence]:
        started_at = time.perf_counter()
        by_evidence = {item.id: item for item in units}
        by_id = {item.id: item for item in fused}
        selected = [
            item
            for item in fused
            if len(item.supporting_evidence_ids) > 1
            and (selected_ids is None or item.id in selected_ids)
        ]
        phase = "fuse"
        for batch in self.context_budget_manager.iter_batches(
            selected,
            item_text=lambda item: json.dumps(
                {
                    "id": item.id,
                    "deterministic_claim": item.canonical_claim,
                    "evidence": [
                        {
                            "id": evidence_id,
                            "text": by_evidence[evidence_id].text,
                            "source_id": by_evidence[evidence_id].source_id,
                        }
                        for evidence_id in item.supporting_evidence_ids
                        if evidence_id in by_evidence
                    ],
                },
                ensure_ascii=False,
            ),
            max_items=96,
            phase="evidence_fusion",
            model=(execution_profile or ResolvedExecutionProfile()).model_dump(mode="json"),
            base_text=json.dumps({"operation": phase}, ensure_ascii=False),
            system_prompt=MATERIAL_AGENT_SYSTEM_PROMPTS[phase],
            expected_output=MATERIAL_AGENT_OUTPUT_SCHEMAS[phase],
        ):
            payload = await analyzer(
                "fuse",
                {
                    "claims": [
                        {
                            "id": item.id,
                            "deterministic_claim": item.canonical_claim,
                            "evidence": [
                                {
                                    "id": evidence_id,
                                    "text": by_evidence[evidence_id].text,
                                    "source_id": by_evidence[evidence_id].source_id,
                                }
                                for evidence_id in item.supporting_evidence_ids
                                if evidence_id in by_evidence
                            ],
                        }
                        for item in batch
                    ]
                },
            )
            if metrics is not None:
                metrics.agent_turns["fusion"] = metrics.agent_turns.get("fusion", 0) + 1
                metrics.input_tokens["fusion"] = metrics.input_tokens.get("fusion", 0) + sum(
                    self.context_budget_manager.estimate_tokens(
                        item.canonical_claim
                        + " ".join(
                            by_evidence[evidence_id].text
                            for evidence_id in item.supporting_evidence_ids
                            if evidence_id in by_evidence
                        )
                    )
                    for item in batch
                )
                metrics.output_tokens["fusion"] = metrics.output_tokens.get(
                    "fusion", 0
                ) + self.context_budget_manager.estimate_tokens(payload)
            values = payload.get("claims", []) if isinstance(payload, dict) else []
            for value in values:
                if not isinstance(value, dict) or str(value.get("id")) not in by_id:
                    continue
                item = by_id[str(value["id"])]
                try:
                    typed = MaterialFusionResult.model_validate(
                        {
                            **value,
                            "id": value.get("id") or item.id,
                            "content": value.get("content")
                            or value.get("canonical_claim")
                            or item.canonical_claim,
                            "source_id": value.get("source_id")
                            or (item.source_ids[0] if item.source_ids else item.id),
                        }
                    )
                except Exception:
                    continue
                claim = str(typed.canonical_claim or typed.content).strip()
                evidence_text = " ".join(
                    by_evidence[evidence_id].text
                    for evidence_id in item.supporting_evidence_ids
                    if evidence_id in by_evidence
                )
                if not claim or _jaccard(claim, evidence_text) < 0.18:
                    continue
                merged_scores = dict(item.dimension_scores)
                for key, score in typed.dimension_scores.items():
                    merged_scores[key] = max(
                        safe_probability(merged_scores.get(key), default=0.0) or 0.0,
                        score,
                    )
                by_id[item.id] = item.model_copy(
                    update={
                        "canonical_claim": claim,
                        "dimension_scores": merged_scores,
                        "confidence": max(item.confidence, typed.confidence),
                        "synthesis_method": "deterministic_union_plus_agent",
                    }
                )
        if metrics is not None:
            metrics.elapsed_ms["fusion"] = (time.perf_counter() - started_at) * 1000
        return [by_id[item.id] for item in fused]

    @staticmethod
    def _representative_items(items: Sequence[Any], *, limit: int) -> list[Any]:
        del limit
        # The context manager, rather than sampling, owns the bounded request
        # size.  Returning every item keeps early/late evidence and source
        # diversity in the durable analysis path.
        return list(items)

    def _make_unit(
        self,
        persona_id: str,
        source: Any,
        text: str,
        index: int,
        row: dict[str, Any],
        metadata: dict[str, Any],
        speaker_roles: dict[str, str] | None = None,
    ) -> EvidenceUnit:
        timestamp = (
            row.get("timestamp")
            or row.get("time")
            or row.get("created_at")
            or metadata.get("event_time")
        )
        speaker = row.get("speaker") or row.get("sender") or row.get("author")
        recipient = row.get("recipient") or row.get("to")
        context_tags = [
            str(value)
            for value in [row.get("context"), metadata.get("context"), row.get("conversation_id")]
            if value
        ]
        relationships = [
            str(value)
            for value in [
                recipient,
                row.get("relationship"),
                metadata.get("relationship"),
                row.get("participant"),
            ]
            if value
        ]
        unit_metadata = {
            "row": row,
            "source_title": source.title,
            "source_path": source.path,
            "verbatim": text,
        }
        unit_metadata.update(
            {key: value for key, value in metadata.items() if key not in {"content"}}
        )
        scores = _dimension_scores(text, {**metadata, **row})
        event_time = (
            str(row.get("event_time") or metadata.get("event_time") or timestamp or "") or None
        )
        source_type = str(source.source_type).lower().lstrip(".")
        source_kind = str(
            row.get("source_kind")
            or metadata.get("source_kind")
            or metadata.get("provenance")
            or source_type
            or "user_provided"
        )
        if source_type == "guided_interview":
            source_kind = "guided_interview"
        elif source_kind == "user_provided" and (
            source_type in {"jsonl", "json", "csv"}
            or (source_type in {"txt", "md"} and row.get("speaker"))
        ):
            source_kind = "chat_import"
        semantic_status: str | None = None
        declared_role: str | None = None
        if speaker and speaker_roles and source_kind in CHAT_SOURCE_KINDS:
            declared_role = speaker_roles.get(str(speaker))
            if declared_role == ROLE_TARGET:
                semantic_status = SEMANTIC_TARGET_PENDING
            elif declared_role == ROLE_EXPORTER:
                semantic_status = SEMANTIC_CONTEXT_ONLY
            # Unknown speaker: conservative — no role, no status, keeps the
            # pre-V2 behavior of being classified as ordinary evidence.
        effective_speaker_role = (
            declared_role
            or str(row.get("speaker_role") or metadata.get("speaker_role") or "")
            or None
        )
        if semantic_status:
            # Persist the routing decision onto the raw unit so resume,
            # progress accounting and downstream lanes all read one source of
            # truth.  Raw text/provenance stay untouched.
            unit_metadata["semantic_status"] = semantic_status
        unit_key = source.id + "|" + str(index) + "|" + normalize_evidence_text(text)
        raw_confidence = (
            row.get("confidence")
            if row.get("confidence") is not None
            else metadata.get("confidence")
        )
        normalized_confidence = safe_probability(raw_confidence, default=0.6)
        return EvidenceUnit(
            id=f"evu_{_sha(unit_key)[:16]}",
            persona_id=persona_id,
            source_id=source.id,
            source_locator={
                "source_id": source.id,
                "source_path": str(source.path),
                "segment_index": index,
                "row": index,
                "line_start": row.get("line_start") or row.get("line") or index + 1,
                "page": row.get("page") or metadata.get("page"),
                "filename": metadata.get("filename") or source.title,
            },
            speaker=str(speaker) if speaker else None,
            speaker_role=effective_speaker_role,
            timestamp=str(timestamp) if timestamp else None,
            text=text,
            normalized_text=normalize_evidence_text(text),
            evidence_type=_evidence_type(
                text,
                {**metadata, **row, "source_type": str(source.source_type).lower().lstrip(".")},
            ),
            dimension_candidates=sorted(scores),
            dimension_scores=scores,
            life_stage_candidates=[
                str(value) for value in [row.get("life_stage"), metadata.get("life_stage")] if value
            ],
            relationship_entities=sorted(set(relationships)),
            context_tags=sorted(set(context_tags)),
            confidence=(normalized_confidence if normalized_confidence is not None else 0.6),
            extraction_method="structured_message_segmenter" if row else "paragraph_segmenter",
            source_kind=source_kind,
            event_time=event_time,
            metadata=unit_metadata,
        )

    def _build_episodes(
        self, persona_id: str, units: Sequence[EvidenceUnit]
    ) -> list[ConversationEpisode]:
        grouped: dict[tuple[str, str], list[EvidenceUnit]] = defaultdict(list)
        for unit in units:
            if unit.source_kind not in {"chat", "chat_import", "guided_interview"}:
                continue
            conversation = str(
                (unit.metadata.get("row") or {}).get("conversation_id")
                or (unit.metadata.get("row") or {}).get("thread_id")
                or "default"
            )
            grouped[(unit.source_id, conversation)].append(unit)
        episodes: list[ConversationEpisode] = []
        for (source_id, conversation), members in grouped.items():
            members = sorted(
                members,
                key=lambda item: (
                    _parse_timestamp(item.timestamp) or datetime.min.replace(tzinfo=UTC)
                ),
            )
            bucket: list[EvidenceUnit] = []
            last_time: datetime | None = None
            episode_index = 0
            for unit in members:
                current = _parse_timestamp(unit.timestamp)
                if (
                    bucket
                    and current
                    and last_time
                    and (current - last_time).total_seconds() > 7200
                ):
                    episodes.append(
                        self._episode(persona_id, source_id, conversation, episode_index, bucket)
                    )
                    episode_index += 1
                    bucket = []
                bucket.append(unit)
                last_time = current or last_time
            if bucket:
                episodes.append(
                    self._episode(persona_id, source_id, conversation, episode_index, bucket)
                )
        return episodes

    def _episode(
        self,
        persona_id: str,
        source_id: str,
        conversation: str,
        index: int,
        members: Sequence[EvidenceUnit],
    ) -> ConversationEpisode:
        parsed_dates = [_parse_timestamp(item.timestamp) for item in members]
        dates = [item for item in parsed_dates if item is not None]
        return ConversationEpisode(
            id=f"ep_{_sha(source_id + '|' + conversation + '|' + str(index))[:16]}",
            persona_id=persona_id,
            source_id=source_id,
            participants=sorted(
                {
                    value
                    for item in members
                    for value in ([item.speaker] if item.speaker else [])
                    + item.relationship_entities
                }
            ),
            start_time=min(dates).isoformat() if dates else None,
            end_time=max(dates).isoformat() if dates else None,
            topics=sorted(
                {dimension for item in members for dimension in item.dimension_candidates}
            ),
            emotional_context=sorted({tag for item in members for tag in item.context_tags if tag}),
            message_ids=[item.id for item in members],
            evidence_unit_ids=[item.id for item in members],
            metadata={"conversation_id": conversation},
        )

    # ---- persistence -----------------------------------------------------
    def reclaim_interrupted_jobs(self) -> int:
        """Fail analysis jobs that a previous process left mid-run.

        Nothing resumes them after a restart, so they would otherwise report an
        active stage forever.  Agent classifications are cached per unit, so a
        retry skips the work that already finished.
        """

        terminal = (MaterialJobStatus.READY_FOR_COMPILATION.value, MaterialJobStatus.FAILED.value)
        rows = self.database.conn.execute(
            "SELECT * FROM persona_material_jobs WHERE status NOT IN (?, ?)", terminal
        ).fetchall()
        now = datetime.now(UTC)
        reclaimed = 0
        for row in rows:
            job = self._job_from_row(row)
            # Every CLI command initialises the container too; a job still
            # owned by a live Web server must never be failed underneath it.
            if not _material_job_orphaned(job, now):
                continue
            job.progress = {
                **job.progress,
                "interrupted_stage": job.progress.get("stage") or job.status.value,
                "stage": "interrupted",
            }
            job.status = MaterialJobStatus.FAILED
            job.error = (
                "MATERIAL_JOB_INTERRUPTED: the process restarted before analysis "
                "finished; retry to resume from cached classifications"
            )
            job.updated_at = now.isoformat()
            self._save_job(job)
            reclaimed += 1
        return reclaimed

    def _save_job(self, job: MaterialAnalysisJob) -> None:
        self.database.conn.execute(
            "INSERT OR REPLACE INTO persona_material_jobs "
            "(id, persona_id, status, source_ids_json, progress_json, coverage_json, "
            "error, runtime_snapshot_json, incremental, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.id,
                job.persona_id,
                job.status.value,
                dumps(job.source_ids),
                dumps(job.progress),
                dumps(job.coverage),
                job.error,
                dumps(job.runtime_snapshot),
                int(job.incremental),
                job.created_at,
                job.updated_at,
            ),
        )
        self.database.conn.commit()

    def _set_job(
        self, job: MaterialAnalysisJob, status: MaterialJobStatus, **progress: Any
    ) -> None:
        job.status = status
        job.progress = {
            **job.progress,
            **progress,
            "status": status.value,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if "coverage" in progress:
            job.coverage = dict(progress["coverage"])
        if "error" in progress:
            job.error = str(progress["error"])
        job.updated_at = datetime.now(UTC).isoformat()
        self._save_job(job)

    @staticmethod
    def _row_json(row: Any, key: str, default: Any) -> Any:
        try:
            value = row[key]
        except Exception:
            value = None
        if value in (None, ""):
            return default
        try:
            return loads(str(value))
        except Exception:
            return default

    @classmethod
    def _unit_from_row(cls, row: Any) -> EvidenceUnit:
        data = dict(row)
        for column, default in (
            ("source_locator_json", {}),
            ("dimension_candidates_json", []),
            ("dimension_scores_json", {}),
            ("life_stage_candidates_json", []),
            ("relationship_entities_json", []),
            ("context_tags_json", []),
            ("metadata_json", {}),
        ):
            target = column.removesuffix("_json")
            data[target] = cls._row_json(row, column, default)
        data.pop("normalized_text_hash", None)
        return EvidenceUnit(
            **{key: value for key, value in data.items() if key in EvidenceUnit.model_fields}
        )

    @classmethod
    def _fused_from_row(cls, row: Any) -> FusedEvidence:
        data = dict(row)
        for column, default in (
            ("dimension_scores_json", {}),
            ("supporting_evidence_ids_json", []),
            ("unique_evidence_ids_json", []),
            ("contradiction_ids_json", []),
            ("conditions_json", []),
            ("temporal_scope_json", []),
            ("relationship_scope_json", []),
            ("verbatim_samples_json", []),
            ("source_ids_json", []),
        ):
            data[column.removesuffix("_json")] = cls._row_json(row, column, default)
        return FusedEvidence(
            **{key: value for key, value in data.items() if key in FusedEvidence.model_fields}
        )

    @classmethod
    def _contradiction_from_row(cls, row: Any) -> PersonaContradiction:
        data = dict(row)
        defaults: tuple[tuple[str, Any], ...] = (
            ("evidence_ids_json", []),
            ("conditions_json", []),
        )
        for column, default in defaults:
            data[column.removesuffix("_json")] = cls._row_json(row, column, default)
        return PersonaContradiction(
            **{
                key: value
                for key, value in data.items()
                if key in PersonaContradiction.model_fields
            }
        )

    @classmethod
    def _episode_from_row(cls, row: Any) -> ConversationEpisode:
        data = dict(row)
        for column, default in (
            ("participants_json", []),
            ("topics_json", []),
            ("emotional_context_json", []),
            ("message_ids_json", []),
            ("evidence_unit_ids_json", []),
            ("metadata_json", {}),
        ):
            data[column.removesuffix("_json")] = cls._row_json(row, column, default)
        return ConversationEpisode(
            **{key: value for key, value in data.items() if key in ConversationEpisode.model_fields}
        )

    @classmethod
    def _job_from_row(cls, row: Any) -> MaterialAnalysisJob:
        data = dict(row)
        for column, default in (
            ("source_ids_json", []),
            ("progress_json", {}),
            ("coverage_json", {}),
            ("runtime_snapshot_json", {}),
        ):
            data[column.removesuffix("_json")] = cls._row_json(row, column, default)
        data["status"] = MaterialJobStatus(
            str(data.get("status", MaterialJobStatus.UPLOADED.value))
        )
        data["incremental"] = bool(data.get("incremental"))
        return MaterialAnalysisJob(
            **{key: value for key, value in data.items() if key in MaterialAnalysisJob.model_fields}
        )

    @staticmethod
    def _json_alias_row(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["source_ids"] = loads(str(data.pop("source_ids_json", "[]")))
        return data

    def _load_units(self, persona_id: str) -> list[EvidenceUnit]:
        # Runs on worker threads; every writer commits before it is called.
        rows = self.database.reader().execute(
            "SELECT * FROM persona_evidence_units WHERE persona_id = ? ORDER BY rowid",
            (persona_id,),
        ).fetchall()
        return [self._unit_from_row(row) for row in rows]

    def _persist_units(self, units: Sequence[EvidenceUnit]) -> None:
        if not units:
            return
        self.database.conn.executemany(
            "INSERT INTO persona_evidence_units "
            "(id, persona_id, source_id, source_locator_json, speaker, speaker_role, "
            "timestamp, text, normalized_text, normalized_text_hash, evidence_type, "
            "dimension_candidates_json, dimension_scores_json, life_stage_candidates_json, "
            "relationship_entities_json, context_tags_json, confidence, extraction_method, "
            "source_kind, event_time, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ", ".join(
                f"{column}=excluded.{column}"
                for column in (
                    "source_locator_json",
                    "speaker",
                    "speaker_role",
                    "timestamp",
                    "text",
                    "normalized_text",
                    "normalized_text_hash",
                    "evidence_type",
                    "dimension_candidates_json",
                    "dimension_scores_json",
                    "life_stage_candidates_json",
                    "relationship_entities_json",
                    "context_tags_json",
                    "confidence",
                    "extraction_method",
                    "source_kind",
                    "event_time",
                    "metadata_json",
                )
            )
            + " WHERE excluded.extraction_method = 'deterministic_plus_agent' "
            "OR persona_evidence_units.extraction_method != 'deterministic_plus_agent'",
            [
                (
                    item.id,
                    item.persona_id,
                    item.source_id,
                    dumps(item.source_locator),
                    item.speaker,
                    item.speaker_role,
                    item.timestamp,
                    item.text,
                    item.normalized_text,
                    _sha(item.normalized_text),
                    item.evidence_type,
                    dumps(item.dimension_candidates),
                    dumps(item.dimension_scores),
                    dumps(item.life_stage_candidates),
                    dumps(item.relationship_entities),
                    dumps(item.context_tags),
                    item.confidence,
                    item.extraction_method,
                    item.source_kind,
                    item.event_time,
                    dumps(item.metadata),
                    item.created_at,
                )
                for item in units
            ],
        )
        self.database.conn.commit()

    def _persist_clusters(
        self, clusters: Sequence[EvidenceCluster], *, commit: bool = True
    ) -> None:
        for item in clusters:
            self.database.conn.execute(
                "INSERT OR REPLACE INTO persona_evidence_clusters "
                "(id, persona_id, cluster_type, canonical_evidence_id, "
                "member_evidence_ids_json, similarity_type, confidence, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.persona_id,
                    item.cluster_type,
                    item.canonical_evidence_id,
                    dumps(item.member_evidence_ids),
                    item.similarity_type,
                    item.confidence,
                    item.created_at,
                    item.updated_at,
                ),
            )
        if commit:
            self.database.conn.commit()

    def _persist_fused(self, fused: Sequence[FusedEvidence], *, commit: bool = True) -> None:
        for item in fused:
            self.database.conn.execute(
                "INSERT OR REPLACE INTO persona_fused_evidence "
                "(id, persona_id, canonical_claim, evidence_type, dimension_scores_json, "
                "supporting_evidence_ids_json, unique_evidence_ids_json, contradiction_ids_json, "
                "conditions_json, temporal_scope_json, relationship_scope_json, confidence, "
                "synthesis_method, verbatim_samples_json, source_ids_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.persona_id,
                    item.canonical_claim,
                    item.evidence_type,
                    dumps(item.dimension_scores),
                    dumps(item.supporting_evidence_ids),
                    dumps(item.unique_evidence_ids),
                    dumps(item.contradiction_ids),
                    dumps(item.conditions),
                    dumps(item.temporal_scope),
                    dumps(item.relationship_scope),
                    item.confidence,
                    item.synthesis_method,
                    dumps(item.verbatim_samples),
                    dumps(item.source_ids),
                    item.created_at,
                ),
            )
        if commit:
            self.database.conn.commit()

    def _persist_contradictions(
        self, rows: Sequence[PersonaContradiction], *, commit: bool = True
    ) -> None:
        for item in rows:
            self.database.conn.execute(
                "INSERT OR REPLACE INTO persona_contradictions "
                "(id, persona_id, contradiction_type, evidence_ids_json, summary, "
                "conditions_json, resolution, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.persona_id,
                    item.contradiction_type,
                    dumps(item.evidence_ids),
                    item.summary,
                    dumps(item.conditions),
                    item.resolution,
                    item.confidence,
                    item.created_at,
                ),
            )
        if commit:
            self.database.conn.commit()

    def _persist_episodes(
        self, rows: Sequence[ConversationEpisode], *, commit: bool = True
    ) -> None:
        for item in rows:
            self.database.conn.execute(
                "INSERT OR REPLACE INTO persona_conversation_episodes "
                "(id, persona_id, source_id, participants_json, start_time, end_time, topics_json, "
                "emotional_context_json, message_ids_json, evidence_unit_ids_json, metadata_json, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.persona_id,
                    item.source_id,
                    dumps(item.participants),
                    item.start_time,
                    item.end_time,
                    dumps(item.topics),
                    dumps(item.emotional_context),
                    dumps(item.message_ids),
                    dumps(item.evidence_unit_ids),
                    dumps(item.metadata),
                    item.created_at,
                ),
            )
        if commit:
            self.database.conn.commit()

    def _replace_derived_atomic(
        self,
        persona_id: str,
        clusters: Sequence[EvidenceCluster],
        contradictions: Sequence[PersonaContradiction],
        fused: Sequence[FusedEvidence],
        episodes: Sequence[ConversationEpisode],
    ) -> None:
        connection = self.database.conn
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table in (
                "persona_evidence_clusters",
                "persona_fused_evidence",
                "persona_contradictions",
                "persona_conversation_episodes",
            ):
                connection.execute(f"DELETE FROM {table} WHERE persona_id = ?", (persona_id,))
            self._persist_clusters(clusters, commit=False)
            self._persist_contradictions(contradictions, commit=False)
            self._persist_fused(fused, commit=False)
            self._persist_episodes(episodes, commit=False)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


class _suppress_value_error:
    def __enter__(self) -> None:
        return None

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any
    ) -> bool:
        return exc_type in {ValueError, TypeError, json.JSONDecodeError}


__all__ = [
    "AtomicEvidenceUnit",
    "ConversationEpisode",
    "EvidenceCluster",
    "EvidenceSimilarityAnalyzer",
    "EvidenceUnit",
    "EvidenceUnitCluster",
    "FusedEvidence",
    "MaterialAnalysisJob",
    "MATERIAL_AGENT_OUTPUT_SCHEMAS",
    "MATERIAL_AGENT_SYSTEM_PROMPTS",
    "MaterialClassificationResult",
    "MaterialFusionResult",
    "MaterialIntelligenceService",
    "MaterialJobStatus",
    "MaterialRelationResult",
    "PersonaContradiction",
    "PersonaEvidenceIndex",
    "PersonaIdentityAlias",
    "PersonaMaterialJob",
    "PrivateMaterialCoverage",
]
