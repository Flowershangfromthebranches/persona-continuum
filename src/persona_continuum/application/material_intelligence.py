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
import csv
import difflib
import hashlib
import json
import re
import time
import unicodedata
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.errors import PromptTransportLimitExceededError
from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.application.material_pipeline import (
    AffectedSet,
    CandidateGroup,
    GlobalCandidateIndex,
    MaterialPipelineMetrics,
    MaterialPromptState,
    PreLLMDeduplicator,
    ResolvedExecutionProfile,
    build_analysis_windows,
    material_batch_target_tokens,
)
from persona_continuum.numeric import (
    append_skipped_numeric_metadata,
    normalize_probability_map,
    safe_int,
    safe_probability,
)

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
    "classify": {"type": "object", "properties": {"units": {"type": "array"}}},
    "relate": {
        "type": "object",
        "properties": {
            "clusters": {"type": "array"},
            "contradictions": {"type": "array"},
        },
    },
    "fuse": {"type": "object", "properties": {"claims": {"type": "array"}}},
}


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
    content: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
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
        return _normalise_material_numeric_payload(
            value,
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


class PersonaEvidenceIndex:
    """Full-corpus retrieval over derived evidence, not a last-N source slice."""

    def __init__(self, database: Any, persona_id: str) -> None:
        self.database = database
        self.persona_id = persona_id

    def _rows(self, table: str, order: str = "rowid") -> list[Any]:
        return list(
            self.database.conn.execute(
                f"SELECT * FROM {table} WHERE persona_id = ? ORDER BY {order}",
                (self.persona_id,),
            ).fetchall()
        )

    def units(self) -> list[EvidenceUnit]:
        return [
            MaterialIntelligenceService._unit_from_row(row)
            for row in self._rows("persona_evidence_units")
        ]

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

        units = self.units()
        unit_by_id = {item.id: item for item in units}
        fused = self.fused()
        candidates: list[dict[str, Any]] = []
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
                        unit_by_id[evidence_id].metadata.get("evidence_intelligence")
                        or unit_by_id[evidence_id].metadata.get("source_factual_intelligence", {})
                        for evidence_id in fused_item.supporting_evidence_ids
                        if evidence_id in unit_by_id
                        and (
                            unit_by_id[evidence_id].metadata.get("evidence_intelligence")
                            or unit_by_id[evidence_id].metadata.get("source_factual_intelligence")
                        )
                    ],
                }
            )
        distinct_unit_ids = {
            evidence_id for item in fused for evidence_id in item.unique_evidence_ids[1:]
        }
        if not candidates:
            unit_candidates = units
        else:
            unit_candidates = [
                unit_by_id[evidence_id]
                for evidence_id in distinct_unit_ids
                if evidence_id in unit_by_id
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


class EvidenceSimilarityAnalyzer:
    """Exact and conservative near-duplicate clustering without embeddings."""

    def cluster(self, units: Sequence[EvidenceUnit], persona_id: str) -> list[EvidenceCluster]:
        groups: list[list[EvidenceUnit]] = []
        exact_groups: dict[str, int] = {}
        token_groups: dict[str, set[int]] = defaultdict(set)
        for unit in sorted(units, key=lambda item: item.id):
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
        by_id = {item.id: item for item in units}

        contradiction_by_evidence: dict[str, list[str]] = defaultdict(list)
        for contradiction in contradictions:
            for evidence_id in contradiction.evidence_ids:
                contradiction_by_evidence[evidence_id].append(contradiction.id)
        fused: list[FusedEvidence] = []
        for cluster in clusters:
            members = [by_id[item] for item in cluster.member_evidence_ids if item in by_id]
            if not members:
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

    def analyze(
        self,
        units: Sequence[EvidenceUnit],
        persona_id: str,
        candidate_groups: Sequence[CandidateGroup] | None = None,
    ) -> list[PersonaContradiction]:
        result: list[PersonaContradiction] = []
        by_id = {item.id: item for item in units}
        pairs: list[tuple[str, str]]
        if candidate_groups is None:
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
                    for index, left in enumerate(group.evidence_ids)
                    for right in group.evidence_ids[index + 1 :]
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
    ) -> None:
        self.database = database
        self.personas = personas
        # Optional local/Agent Adapter hooks.  They enrich classification or
        # union wording only; raw text and deterministic provenance survive if
        # a hook is unavailable or fails.
        self.classifier = classifier
        self.fusion_hook = fusion_hook
        self.context_budget_manager = context_budget_manager or AgentContextBudgetManager()
        self.deduplicator = PreLLMDeduplicator()
        self.candidate_index = GlobalCandidateIndex()
        self.similarity = EvidenceSimilarityAnalyzer()
        self.fusion = PersonaEvidenceFusionService()
        self.contradictions = PersonaContradictionAnalyzer()

    # ---- public lifecycle -------------------------------------------------
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
            existing_units = self._load_units(persona_id)
            existing_source_ids = {item.source_id for item in existing_units}
            sources_to_segment = (
                [source for source in sources if source.id not in existing_source_ids]
                if incremental
                else sources
            )
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
            relation_units = [item for item in all_units if item.id in canonical_ids]
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
    ) -> MaterialAnalysisJob:
        """Analyze material without running CPU-heavy stages on the event loop.

        Agent analysis is additive and schema constrained: it may classify
        evidence or improve fused wording, but it cannot change source ids,
        locators, verbatim evidence, or supporting provenance.
        """

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
            profile = ResolvedExecutionProfile.resolve(
                runtime_snapshot,
                fallback_context_window=self.context_budget_manager.default_context_window_tokens,
            )
            await stage(MaterialJobStatus.SEGMENTING, stage="segments")
            existing_units = await asyncio.to_thread(self._load_units, persona_id)
            existing_by_id = {item.id: item for item in existing_units}
            existing_source_ids = {item.source_id for item in existing_units}
            sources_to_segment = (
                [source for source in sources if source.id not in existing_source_ids]
                if incremental
                else sources
            )
            new_units = await asyncio.to_thread(
                self._segment_sources, persona_id, sources_to_segment
            )
            combined_by_id = {item.id: item for item in existing_units}
            persisted_cache_hits: set[str] = set()
            for item in new_units:
                previous = existing_by_id.get(item.id)
                cache_key = self._material_intelligence_cache_key(item, profile)
                if (
                    previous is not None
                    and previous.metadata.get("intelligence_cache_key") == cache_key
                    and previous.metadata.get("evidence_intelligence")
                ):
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
                if (not incremental or item.id in new_ids) and item.id not in persisted_cache_hits
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
            units = self._propagate_canonical_intelligence(units, dedup.supporting_units)
            self._persist_units(units)
            all_units = self._load_units(persona_id)
            canonical_ids = {item.id for item in dedup.canonical_units}
            relation_units = [item for item in all_units if item.id in canonical_ids]

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
            )
            coverage = self.coverage(persona_id)
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
        units = self._load_units(persona_id)
        episodes = self.get_index(persona_id).episodes()
        fused = self.get_index(persona_id).fused()
        contradictions = self.get_index(persona_id).contradictions()
        source_ids = {item.source_id for item in units}
        dates = [_parse_timestamp(item.event_time or item.timestamp) for item in units]
        valid_dates = [item for item in dates if item is not None]
        span = (max(valid_dates) - min(valid_dates)).days if len(valid_dates) > 1 else 0
        dimensions = {
            dimension: len(
                {
                    item.id
                    for item in units
                    if (safe_probability(item.dimension_scores.get(dimension), default=0.0) or 0.0)
                    > 0
                }
            )
            for dimension in REQUIRED_DIMENSIONS
        }
        gaps: list[str] = [dimension for dimension, count in dimensions.items() if count == 0]
        if not episodes:
            gaps.append("conversation_episodes")
        if (
            len({item.source_id for item in units if item.evidence_type == "expression_sample"})
            == 0
        ):
            gaps.append("expression_samples")
        if not contradictions:
            gaps.append("contradiction_or_change_evidence")
        return PrivateMaterialCoverage(
            source_count=len(source_ids),
            evidence_unit_count=len(units),
            message_count=sum(
                1
                for item in units
                if item.source_kind in {"chat", "chat_import", "guided_interview"}
            ),
            conversation_time_span_days=max(0, span),
            episode_count=len(episodes),
            behavior_example_count=sum(
                1 for item in units if item.evidence_type == "behavioral_observation"
            ),
            relationship_context_count=len(
                {value for item in units for value in item.relationship_entities}
            ),
            life_event_count=sum(1 for item in units if item.event_time),
            expression_sample_count=sum(
                1 for item in units if item.evidence_type == "expression_sample"
            ),
            decision_example_count=sum(
                1 for item in units if item.evidence_type == "decision_example"
            ),
            contradiction_count=len(contradictions),
            fused_evidence_count=len(fused),
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
    def _segment_sources(self, persona_id: str, sources: Sequence[Any]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for source in sources:
            metadata = dict(source.metadata or {})
            rows = self._structured_rows(source.content, source.source_type)
            if rows:
                for index, row in enumerate(rows):
                    text = str(
                        row.get("content")
                        or row.get("text")
                        or row.get("message")
                        or row.get("body")
                        or ""
                    ).strip()
                    if not text:
                        continue
                    unit = self._make_unit(persona_id, source, text, index, row, metadata)
                    units.append(unit)
                continue
            paragraphs = [
                value.strip()
                for value in re.split(r"\n\s*\n+|(?<=。)\s*(?=\S)", source.content)
                if value.strip()
            ]
            if not paragraphs:
                paragraphs = [source.content.strip()] if source.content.strip() else []
            for index, text in enumerate(paragraphs):
                speaker = None
                match = None
                if str(source.source_type).lower().lstrip(".") != "guided_interview":
                    match = re.match(r"^([^:：\n]{1,60})[:：]\s*(.+)$", text, flags=re.DOTALL)
                if match:
                    speaker, text = match.group(1).strip(), match.group(2).strip()
                unit = self._make_unit(
                    persona_id,
                    source,
                    text,
                    index,
                    {"speaker": speaker} if speaker else {},
                    metadata,
                )
                units.append(unit)
        return units

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

        serialized = json.dumps(request, ensure_ascii=False)
        if self.context_budget_manager.estimate_tokens(serialized) > target_tokens:
            return False
        return not (
            transport_safe_bytes is not None
            and len(serialized.encode("utf-8")) > transport_safe_bytes
        )

    def _serialized_prompt_size(self, request: dict[str, Any]) -> tuple[int, int]:
        serialized = json.dumps(request, ensure_ascii=False)
        return (
            self.context_budget_manager.estimate_tokens(serialized),
            len(serialized.encode("utf-8")),
        )

    async def _classify_with_agent(
        self,
        units: Sequence[EvidenceUnit],
        analyzer: Callable[[str, dict[str, Any]], Awaitable[Any]],
        *,
        cache_namespace: str | None = None,
        execution_profile: ResolvedExecutionProfile | None = None,
        metrics: MaterialPipelineMetrics | None = None,
        window_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> list[EvidenceUnit]:
        started_at = time.perf_counter()
        by_id = {item.id: item for item in units}
        selected: list[EvidenceUnit] = []
        intelligence_cache = None
        if cache_namespace:
            from persona_continuum.performance.research_cache import (
                default_source_intelligence_cache,
            )

            intelligence_cache = default_source_intelligence_cache()
        for item in units:
            cached = (
                intelligence_cache.get(cache_namespace, item.normalized_text)
                if intelligence_cache is not None and cache_namespace is not None
                else None
            )
            if cached is None:
                selected.append(item)
                continue
            by_id[item.id] = item.model_copy(
                update={
                    "evidence_type": str(cached.get("evidence_type") or item.evidence_type),
                    "relationship_entities": sorted(
                        set(item.relationship_entities + list(cached.get("entities") or []))
                    ),
                    "context_tags": sorted(
                        set(item.context_tags + list(cached.get("context_tags") or []))
                    ),
                    "life_stage_candidates": sorted(
                        set(
                            item.life_stage_candidates
                            + ([str(cached["life_stage"])] if cached.get("life_stage") else [])
                        )
                    ),
                    "metadata": {
                        **item.metadata,
                        "source_factual_intelligence": cached,
                        "source_intelligence_cache_hit": True,
                    },
                    "extraction_method": "deterministic_plus_shared_factual_cache",
                }
            )
            if metrics is not None:
                metrics.cache_hits["intelligence"] = metrics.cache_hits.get("intelligence", 0) + 1
        if not selected:
            if metrics is not None:
                metrics.elapsed_ms["intelligence"] = (time.perf_counter() - started_at) * 1000
            return [by_id[item.id] for item in units]
        phase = "classify"
        profile = execution_profile or ResolvedExecutionProfile()
        budget = self.context_budget_manager.budget_for(
            model=profile.model_dump(mode="json"),
            phase="material_classification",
            expected_output=MATERIAL_AGENT_OUTPUT_SCHEMAS[phase],
        )
        # The batch target is the minimum of the three per-pass budgets:
        # phase usable budget, preferred working context and the prompt
        # transport budget.  A 1M model window alone must never drive it up;
        # ``evidence_token_budget * 0.82`` did exactly that and packed
        # near-1M single windows behind small transports.
        target_tokens = material_batch_target_tokens(
            profile, phase_usable_budget=budget.evidence_token_budget
        )
        transport = profile.prompt_transport
        transport_safe_bytes = (
            transport.safe_prompt_bytes if transport is not None else None
        )
        if metrics is not None:
            metrics.max_batch_target_tokens = target_tokens
            metrics.transport_mode = transport.transport_mode if transport else None
            metrics.transport_max_prompt_bytes = (
                transport.max_prompt_bytes if transport else None
            )
        windows = build_analysis_windows(
            selected,
            estimate_tokens=self.context_budget_manager.estimate_tokens,
            target_tokens=target_tokens,
        )
        if metrics is not None:
            metrics.analysis_windows = len(windows)

        async def report_window(prompt_state: str, **info: Any) -> None:
            if window_progress is None:
                return
            await window_progress({"prompt_state": prompt_state, **info})

        await report_window(
            MaterialPromptState.PREPARING_INPUT.value,
            windows_total=len(windows),
            windows_completed=0,
        )
        selected_by_id = {item.id: item for item in selected}
        concurrency = 4 if profile.parallel_safe else 1
        semaphore = asyncio.Semaphore(concurrency)

        def make_request(
            window: Any, index: int, part: Sequence[EvidenceUnit]
        ) -> dict[str, Any]:
            # Body text appears exactly once, in units[].text.  The analysis
            # window carries metadata only: shipping the full window text as
            # well doubled every prompt and helped wedge the 40% stage.
            return {
                "_participant_id": f"persona_material_intelligence:{index}",
                "analysis_window": window.model_dump(mode="json", exclude={"text"}),
                "units": [
                    {
                        "id": item.id,
                        "text": item.text,
                        "source_id": item.source_id,
                        "speaker": item.speaker,
                        "timestamp": item.timestamp,
                        "source_kind": item.source_kind,
                        "deterministic_dimensions": item.dimension_scores,
                    }
                    for item in part
                ],
                "allowed_dimensions": list(REQUIRED_DIMENSIONS),
            }

        async def analyze_window(
            index: int, window: Any
        ) -> tuple[Any, list[EvidenceUnit], list[dict[str, Any]], dict[str, str], int, int]:
            batch = [
                selected_by_id[evidence_id]
                for evidence_id in window.evidence_unit_ids
                if evidence_id in selected_by_id
            ]
            # Chunk-id -> original evidence id.  Chunked results merge back
            # into their original provenance row.
            id_owner: dict[str, str] = {}
            max_prompt_tokens = 0
            max_prompt_bytes = 0

            def split_part(part: list[EvidenceUnit]) -> list[list[EvidenceUnit]]:
                request = make_request(window, index, part)
                if self._request_fits_budget(
                    request,
                    target_tokens=target_tokens,
                    transport_safe_bytes=transport_safe_bytes,
                ):
                    return [part]
                if len(part) == 1:
                    # A single evidence unit that cannot fit: semantic chunk
                    # it (never drop it, never wedge the dispatch).
                    piece_budget = max(64, target_tokens // 4)
                    if transport is not None:
                        piece_budget = max(
                            64, min(piece_budget, transport.prompt_token_budget() // 2)
                        )
                    chunks = _chunk_oversized_unit(
                        part[0],
                        estimate_tokens=self.context_budget_manager.estimate_tokens,
                        max_piece_tokens=piece_budget,
                    )
                    if not chunks:
                        raise PromptTransportLimitExceededError(
                            "PROMPT_TRANSPORT_LIMIT_EXCEEDED: evidence unit cannot be split",
                            phase="material_classification",
                            diagnostics={
                                "evidence_id": part[0].id,
                                "transport_mode": transport.transport_mode if transport else None,
                                "transport_safe_prompt_bytes": transport_safe_bytes,
                                "target_tokens": target_tokens,
                            },
                        )
                    if metrics is not None:
                        metrics.chunked_oversized_units += 1
                    for chunk in chunks:
                        id_owner[chunk.id] = part[0].id
                    if all(
                        self._request_fits_budget(
                            make_request(window, index, [chunk]),
                            target_tokens=target_tokens,
                            transport_safe_bytes=transport_safe_bytes,
                        )
                        for chunk in chunks
                    ):
                        return [[chunk] for chunk in chunks]
                    raise PromptTransportLimitExceededError(
                        "PROMPT_TRANSPORT_LIMIT_EXCEEDED: chunk still exceeds transport budget",
                        phase="material_classification",
                        diagnostics={
                            "evidence_id": part[0].id,
                            "transport_mode": transport.transport_mode if transport else None,
                            "transport_safe_prompt_bytes": transport_safe_bytes,
                            "target_tokens": target_tokens,
                        },
                    )
                if metrics is not None:
                    metrics.rebatched_windows += 1
                mid = len(part) // 2
                return split_part(part[:mid]) + split_part(part[mid:])

            parts = split_part(batch)
            payloads: list[dict[str, Any]] = []
            async with semaphore:
                await report_window(
                    MaterialPromptState.PROMPT_BUILDING.value,
                    current_window=index + 1,
                    windows_total=len(windows),
                    windows_completed=len(payloads),
                )
                for part in parts:
                    request = make_request(window, index, part)
                    prompt_tokens, prompt_bytes = self._serialized_prompt_size(request)
                    max_prompt_tokens = max(max_prompt_tokens, prompt_tokens)
                    max_prompt_bytes = max(max_prompt_bytes, prompt_bytes)
                    payloads.append(await analyzer("classify", request))
            await report_window(
                MaterialPromptState.VALIDATING_OUTPUT.value,
                current_window=index + 1,
                windows_total=len(windows),
            )
            return window, batch, payloads, id_owner, max_prompt_tokens, max_prompt_bytes

        tasks = [
            asyncio.create_task(analyze_window(index, window))
            for index, window in enumerate(windows)
        ]

        def cancel_sibling_windows(completed: asyncio.Task[Any]) -> None:
            if completed.cancelled() or completed.exception() is None:
                return
            for sibling in tasks:
                if sibling is not completed and not sibling.done():
                    sibling.cancel()

        for task in tasks:
            task.add_done_callback(cancel_sibling_windows)
        completed_windows = 0
        for completed in asyncio.as_completed(tasks):
            (
                window,
                batch,
                payloads,
                id_owner,
                window_prompt_tokens,
                window_prompt_bytes,
            ) = await completed
            if metrics is not None:
                metrics.agent_turns["intelligence"] = metrics.agent_turns.get(
                    "intelligence", 0
                ) + max(1, len(payloads))
                metrics.input_tokens["intelligence"] = metrics.input_tokens.get(
                    "intelligence", 0
                ) + (window_prompt_tokens or window.token_estimate)
                metrics.max_serialized_prompt_tokens = max(
                    metrics.max_serialized_prompt_tokens or 0, window_prompt_tokens
                )
                metrics.max_serialized_prompt_bytes = max(
                    metrics.max_serialized_prompt_bytes or 0, window_prompt_bytes
                )
                metrics.output_tokens["intelligence"] = metrics.output_tokens.get(
                    "intelligence", 0
                ) + self.context_budget_manager.estimate_tokens(payloads)
            for payload in payloads:
                values = payload.get("units", []) if isinstance(payload, dict) else []
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    raw_id = str(value.get("id"))
                    owner_id = id_owner.get(raw_id, raw_id)
                    if owner_id not in by_id:
                        continue
                    item = by_id[owner_id]
                    try:
                        typed = MaterialClassificationResult.model_validate(
                            {
                                **value,
                                "id": value.get("id") or item.id,
                                "content": value.get("content")
                                or value.get("text")
                                or item.text,
                                "source_id": value.get("source_id") or item.source_id,
                            }
                        )
                    except Exception:
                        continue
                    dimensions = typed.dimension_scores
                    relationships = [
                        str(entry).strip()
                        for entry in typed.relationship_entities
                        if str(entry).strip()
                    ]
                    contexts = [
                        str(entry).strip() for entry in typed.context_tags if str(entry).strip()
                    ]
                    allowed_types = {
                        "behavioral_observation",
                        "self_report",
                        "third_party_report",
                        "decision_example",
                        "relationship_context",
                        "expression_sample",
                    }
                    evidence_type = str(typed.evidence_type or item.evidence_type)
                    if evidence_type not in allowed_types:
                        evidence_type = item.evidence_type
                    merged_dimensions = dict(item.dimension_scores)
                    for key, score in dimensions.items():
                        merged_dimensions[key] = max(merged_dimensions.get(key, 0.0), score)
                    by_id[item.id] = item.model_copy(
                        update={
                            "evidence_type": evidence_type,
                            "dimension_scores": merged_dimensions,
                            "dimension_candidates": sorted(merged_dimensions),
                            "relationship_entities": sorted(
                                set(item.relationship_entities + relationships)
                            ),
                            "context_tags": sorted(set(item.context_tags + contexts)),
                            "confidence": max(item.confidence, typed.confidence),
                            "metadata": {
                                **item.metadata,
                                **typed.metadata,
                                "intelligence_cache_key": self._material_intelligence_cache_key(
                                    item, profile
                                ),
                                "evidence_intelligence": {
                                    key: getattr(typed, key)
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
                                    )
                                    if getattr(typed, key)
                                },
                            },
                            "extraction_method": "deterministic_plus_agent",
                        }
                    )
                    if intelligence_cache is not None and cache_namespace is not None:
                        factual = {
                            key: getattr(typed, key)
                            for key in (
                                "claims",
                                "events",
                                "dates",
                                "entities",
                                "quotes",
                                "decisions",
                                "behaviors",
                                "relationships",
                                "communication_patterns",
                                "contradictions",
                                "negative_evidence",
                                "uncertainties",
                                "life_stage",
                                "context_tags",
                                "evidence_type",
                            )
                            if getattr(typed, key)
                        }
                        intelligence_cache.put(
                            cache_namespace, item.normalized_text, factual
                        )
            # Each completed AnalysisWindow is a durable checkpoint.  A retry
            # can reuse these rows through the semantic cache key instead of
            # restarting the complete material corpus.
            completed_windows += 1
            self._persist_units([by_id[item.id] for item in batch if item.id in by_id])
            await report_window(
                MaterialPromptState.CHECKPOINTING.value,
                windows_completed=completed_windows,
                windows_total=len(windows),
                current_window=completed_windows,
            )
        if metrics is not None:
            metrics.elapsed_ms["intelligence"] = (time.perf_counter() - started_at) * 1000
        return [by_id[item.id] for item in units]

    @staticmethod
    def _material_intelligence_cache_key(
        unit: EvidenceUnit, profile: ResolvedExecutionProfile
    ) -> str:
        payload = {
            "material_hash": _sha(unit.normalized_text),
            "parser_version": "material-parser-v2",
            "intelligence_schema_version": "evidence-intelligence-v2",
            "model_id": profile.model_id,
            "reasoning": profile.selected_reasoning_effort,
            "prompt_version": _sha(MATERIAL_AGENT_SYSTEM_PROMPTS["classify"]),
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
                return [
                    compact_relation_value(item, max_chars=max_chars) for item in value[:12]
                ]
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
                "entities": compact_relation_value(
                    list(intelligence.get("entities") or [])[:12]
                ),
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
        target_tokens = material_batch_target_tokens(
            profile, phase_usable_budget=budget.evidence_token_budget
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
                    metrics.agent_turns["relation"] = (
                        metrics.agent_turns.get("relation", 0) + 1
                    )
                    metrics.input_tokens["relation"] = metrics.input_tokens.get(
                        "relation", 0
                    ) + sum(
                        self.context_budget_manager.estimate_tokens(
                            " ".join(
                                by_id[item].text
                                for item in group.evidence_ids
                                if item in by_id
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
                    contradiction_type = str(
                        value.get("contradiction_type") or "FACT_CONFLICT"
                    )
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

    def _structured_rows(self, content: str, source_type: str) -> list[dict[str, Any]]:
        suffix = str(source_type).lower().lstrip(".")
        if suffix in {"jsonl", "csv"}:
            if suffix == "jsonl":
                rows: list[dict[str, Any]] = []
                for line in content.splitlines():
                    with _suppress_value_error():
                        item = json.loads(line)
                        if isinstance(item, dict):
                            rows.append(item)
                return rows
            try:
                return [dict(row) for row in csv.DictReader(content.splitlines())]
            except Exception:
                return []
        if suffix == "json":
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return []
            if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
                return [dict(item) for item in parsed]
            if isinstance(parsed, dict):
                for key in ("messages", "conversation", "turns", "rows", "items"):
                    value = parsed.get(key)
                    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                        return [dict(item) for item in value]
        return []

    def _make_unit(
        self,
        persona_id: str,
        source: Any,
        text: str,
        index: int,
        row: dict[str, Any],
        metadata: dict[str, Any],
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
        elif source_kind == "user_provided" and source_type in {"jsonl", "json", "csv"}:
            source_kind = "chat_import"
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
                "source_path": str(source.path),
                "segment_index": index,
                "row": index,
                "line_start": row.get("line_start") or row.get("line") or index + 1,
                "page": row.get("page") or metadata.get("page"),
            },
            speaker=str(speaker) if speaker else None,
            speaker_role=str(row.get("speaker_role") or metadata.get("speaker_role") or "") or None,
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
        rows = self.database.conn.execute(
            "SELECT * FROM persona_evidence_units WHERE persona_id = ? ORDER BY rowid",
            (persona_id,),
        ).fetchall()
        return [self._unit_from_row(row) for row in rows]

    def _persist_units(self, units: Sequence[EvidenceUnit]) -> None:
        for item in units:
            self.database.conn.execute(
                "INSERT OR REPLACE INTO persona_evidence_units "
                "(id, persona_id, source_id, source_locator_json, speaker, speaker_role, "
                "timestamp, text, normalized_text, normalized_text_hash, evidence_type, "
                "dimension_candidates_json, dimension_scores_json, life_stage_candidates_json, "
                "relationship_entities_json, context_tags_json, confidence, extraction_method, "
                "source_kind, event_time, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
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
