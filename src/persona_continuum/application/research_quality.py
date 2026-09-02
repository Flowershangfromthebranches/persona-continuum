"""Evidence quality primitives used by Persona Creation Runtime.

The module intentionally stays in the application layer.  It does not create a
second persona format and it never replaces ``EvidenceSource`` or the existing
compiler.  The objects here are small, serialisable quality records that make
the research loop auditable and resumable.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona_continuum.numeric import safe_int, safe_probability


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _metadata(item: Any) -> dict[str, Any]:
    raw = _value(item, "metadata", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def _normalise_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _normalise_numeric_fields(
    value: Any,
    *,
    counts: tuple[str, ...] = (),
    probabilities: tuple[str, ...] = (),
    probability_defaults: Mapping[str, float] | None = None,
) -> Any:
    if not isinstance(value, dict):
        return value
    data = dict(value)
    for field in counts:
        data[field] = safe_int(data.get(field), default=0, minimum=0, field=field)
    defaults = probability_defaults or {}
    for field in probabilities:
        normalized = safe_probability(
            data.get(field), default=defaults.get(field, 0.0), field=field
        )
        data[field] = normalized if normalized is not None else defaults.get(field, 0.0)
    return data


def _canonical_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if not parts.netloc:
            return raw.rstrip("/")
        query = [
            (key, val)
            for key, val in parse_qsl(parts.query)
            if not key.lower().startswith(("utm_", "fbclid"))
        ]
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip("/"),
                urlencode(sorted(query)),
                "",
            )
        )
    except ValueError:
        return raw.rstrip("/")


class ResearchGap(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    target: str
    severity: str = "medium"
    evidence_count: int = 0
    priority: int = 50
    details: str = ""
    suggested_queries: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(value, counts=("evidence_count", "priority"))

    @property
    def high_priority(self) -> bool:
        return self.priority >= 80 or self.severity == "high"


class DimensionCoverage(BaseModel):
    dimension: str
    evidence_count: int = 0
    independent_evidence_count: int = 0
    claim_count: int = 0
    contradiction_count: int = 0
    confidence: float = 0.0
    information_gain: float = 0.0
    remaining_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(
            value,
            counts=(
                "evidence_count",
                "independent_evidence_count",
                "claim_count",
                "contradiction_count",
            ),
            probabilities=("confidence", "information_gain"),
        )


class LifeStage(BaseModel):
    id: str
    title: str
    start: str | None = None
    end: str | None = None
    significance: str = ""
    required_evidence: int = 1

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(value, counts=("required_evidence",))


class LifeStageModel(BaseModel):
    """Dynamic life-stage plan returned by the planning agent.

    The model contains no person-specific stages.  A research plan can provide
    any stages and source mapping is based on event evidence in the content,
    never merely on the publication year.
    """

    model_config = ConfigDict(extra="allow")

    life_stages: list[LifeStage] = Field(default_factory=list)

    @classmethod
    def from_plan(cls, plan: Mapping[str, Any] | None) -> LifeStageModel:
        raw = (plan or {}).get("life_stages", []) if isinstance(plan, Mapping) else []
        stages: list[LifeStage] = []
        for index, item in enumerate(raw if isinstance(raw, list) else []):
            if not isinstance(item, Mapping):
                continue
            stage_id = str(item.get("id") or item.get("stage_id") or f"stage_{index + 1}")
            stages.append(
                LifeStage(
                    id=stage_id,
                    title=str(item.get("title") or item.get("name") or stage_id),
                    start=str(item.get("start") or item.get("start_date") or "") or None,
                    end=str(item.get("end") or item.get("end_date") or "") or None,
                    significance=str(item.get("significance") or ""),
                    required_evidence=max(
                        1, safe_int(item.get("required_evidence"), default=1, minimum=1) or 1
                    ),
                )
            )
        return cls(life_stages=stages)

    def map_source(self, source: Any) -> list[str]:
        """Map one source to all stages supported by its *event* evidence.

        ``published_at`` is deliberately ignored.  A 2024 biography can
        describe a 1976 event, so publication time must not be mistaken for
        life-stage time.
        """

        if not self.life_stages:
            return []
        meta = _metadata(source)
        content = str(_value(source, "content", ""))
        event_dates = [
            str(meta.get(key) or "")
            for key in ("event_time", "event_date", "life_stage_date", "occurred_at")
        ]
        event_dates.extend(re.findall(r"\b(?:18|19|20)\d{2}\b", content))
        event_years = {
            safe_int(value[:4], default=0, minimum=0) or 0
            for value in event_dates
            if re.match(r"^(?:18|19|20)\d{2}", value)
        }
        explicit = meta.get("life_stage_ids") or meta.get("life_stages") or meta.get("life_stage")
        explicit_ids = (
            {str(value) for value in explicit}
            if isinstance(explicit, (list, tuple, set))
            else ({str(explicit)} if explicit else set())
        )
        matched: list[str] = []
        for stage in self.life_stages:
            if stage.id in explicit_ids:
                matched.append(stage.id)
                continue
            start_year = self._year(stage.start)
            end_year = self._year(stage.end)
            if event_years and any(
                (start_year is None or year >= start_year)
                and (end_year is None or year <= end_year)
                for year in event_years
            ):
                matched.append(stage.id)
                continue
            # A source without a year can still be mapped when the agent or
            # ingestion layer explicitly tagged the stage in metadata.
            if (
                not event_years
                and not explicit_ids
                and _normalise_text(stage.title) in _normalise_text(content)
            ):
                matched.append(stage.id)
        return matched

    @staticmethod
    def _year(value: str | None) -> int | None:
        match = re.search(r"\b(?:18|19|20)\d{2}\b", str(value or ""))
        return safe_int(match.group(0), default=None, minimum=0) if match else None


class SourceCluster(BaseModel):
    model_config = ConfigDict(extra="allow")

    cluster_id: str
    origin_type: str = "unknown"
    origin_identifier: str = ""
    member_source_ids: list[str] = Field(default_factory=list)
    canonical_source_id: str = ""
    independence_confidence: float = 0.0
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(value, probabilities=("independence_confidence",))

    @property
    def independent(self) -> bool:
        return bool(self.member_source_ids)


class SourceQualityAssessment(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_id: str
    quality_score: float = 0.0
    source_authority: float = 0.0
    primary_or_secondary: str = "unknown"
    specificity: float = 0.0
    independence: float = 0.0
    historical_relevance: float = 0.0
    metadata_completeness: float = 0.0
    cluster_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(
            value,
            probabilities=(
                "quality_score",
                "source_authority",
                "specificity",
                "independence",
                "historical_relevance",
                "metadata_completeness",
            ),
        )


class ContradictionCoverage(BaseModel):
    model_config = ConfigDict(extra="allow")

    search_executed: bool = False
    queries_executed: list[str] = Field(default_factory=list)
    conflicts_found: int = 0
    unresolved_conflicts: int = 0
    negative_evidence_found: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(value, counts=("conflicts_found", "unresolved_conflicts"))


class InformationGainSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")

    round_index: int = 0
    new_claims: int = 0
    new_supported_claims: int = 0
    new_contradictions: int = 0
    new_behavior_patterns: int = 0
    new_expression_patterns: int = 0
    new_relationship_evidence: int = 0
    new_life_stage_evidence: int = 0
    new_source_categories: int = 0
    new_primary_evidence: int = 0
    new_dimension_coverage: int = 0
    marginal_gain_score: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(
            value,
            counts=(
                "round_index",
                "new_claims",
                "new_supported_claims",
                "new_contradictions",
                "new_behavior_patterns",
                "new_expression_patterns",
                "new_relationship_evidence",
                "new_life_stage_evidence",
                "new_source_categories",
                "new_primary_evidence",
                "new_dimension_coverage",
            ),
            probabilities=("marginal_gain_score",),
        )


class ResearchCheckpoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    job_id: str
    round_index: int
    raw_source_count: int = 0
    independent_source_count: int = 0
    quality_source_count: int = 0
    dimension_coverage: dict[str, Any] = Field(default_factory=dict)
    life_stage_coverage: dict[str, Any] = Field(default_factory=dict)
    primary_secondary_balance: dict[str, Any] = Field(default_factory=dict)
    contradiction_coverage: dict[str, Any] = Field(default_factory=dict)
    information_gain: dict[str, Any] = Field(default_factory=dict)
    research_gaps: list[dict[str, Any]] = Field(default_factory=list)
    continue_reason: str | None = None
    stop_reason: str | None = None
    created_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(
            value,
            counts=(
                "round_index",
                "raw_source_count",
                "independent_source_count",
                "quality_source_count",
            ),
        )


class PrivatePersonaCoveragePolicy(BaseModel):
    message_volume_target: int = 100
    min_time_span_days: int = 30
    min_interaction_contexts: int = 3
    min_relationship_contexts: int = 2
    min_behavioral_episodes: int = 8
    min_guided_questions_answered: int = 4
    required_dimensions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(
            value,
            counts=(
                "message_volume_target",
                "min_time_span_days",
                "min_interaction_contexts",
                "min_relationship_contexts",
                "min_behavioral_episodes",
                "min_guided_questions_answered",
            ),
        )


class PrivatePersonaCoverage(BaseModel):
    conversation_message_count: int = 0
    conversation_time_span_days: int = 0
    distinct_context_count: int = 0
    distinct_relationship_contexts: int = 0
    behavioral_episode_count: int = 0
    guided_questions_answered: int = 0
    dimension_coverage: dict[str, int] = Field(default_factory=dict)
    high_priority_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(
            value,
            counts=(
                "conversation_message_count",
                "conversation_time_span_days",
                "distinct_context_count",
                "distinct_relationship_contexts",
                "behavioral_episode_count",
                "guided_questions_answered",
            ),
        )


class FictionalPersonaCoverage(BaseModel):
    work_text_characters: int = 0
    scene_count: int = 0
    dialogue_count: int = 0
    behavioral_event_count: int = 0
    relationship_count: int = 0
    user_setting_count: int = 0
    dimension_coverage: dict[str, int] = Field(default_factory=dict)
    high_priority_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_numeric_fields(
            value,
            counts=(
                "work_text_characters",
                "scene_count",
                "dialogue_count",
                "behavioral_event_count",
                "relationship_count",
                "user_setting_count",
            ),
        )


class AdaptiveResearchStopResult(BaseModel):
    passed: bool = False
    continue_research: bool = True
    completed_with_gaps: bool = False
    stop_reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ResearchRichnessEstimator:
    @staticmethod
    def estimate(sources: Sequence[Any]) -> str:
        count = len(sources)
        categories = {
            _normalise_text(_metadata(source).get("category") or _value(source, "source_type", ""))
            for source in sources
        }
        years = set(
            re.findall(
                r"\b(?:18|19|20)\d{2}\b",
                " ".join(str(_value(source, "content", "")) for source in sources),
            )
        )
        if count >= 50 or (count >= 30 and len(categories) >= 6 and len(years) >= 5):
            return "very_rich"
        if count >= 20 or (count >= 12 and len(categories) >= 4):
            return "rich"
        if count >= 6 or len(categories) >= 3:
            return "moderate"
        return "sparse"


class SourceIndependenceAnalyzer:
    """Cluster reposts and citation chains without cloud embeddings."""

    def cluster_sources(self, sources: Sequence[Any]) -> list[SourceCluster]:
        source_list = list(sources)
        clusters: list[list[Any]] = []
        fingerprints: list[dict[str, Any]] = []
        for source in source_list:
            meta = _metadata(source)
            content = str(_value(source, "content", ""))
            normalized_content = _normalise_text(content)
            fingerprints.append(
                {
                    "url": _canonical_url(meta.get("canonical_url") or _value(source, "path", "")),
                    "hash": str(
                        _value(source, "hash", "") or hashlib.sha256(content.encode()).hexdigest()
                    ),
                    "normalized_hash": hashlib.sha256(normalized_content.encode()).hexdigest()
                    if normalized_content
                    else "",
                    "title": _normalise_text(_value(source, "title", "")),
                    "publisher": _normalise_text(
                        meta.get("publisher") or _value(source, "source_type", "")
                    ),
                    "origin": str(
                        meta.get("origin_identifier")
                        or meta.get("source_chain")
                        or meta.get("original_url")
                        or ""
                    ),
                }
            )
            assigned = None
            for index, members in enumerate(clusters):
                for member_index in members:
                    left = fingerprints[member_index]
                    same_origin = (
                        fingerprints[-1]["origin"] and fingerprints[-1]["origin"] == left["origin"]
                    )
                    same_url = fingerprints[-1]["url"] and fingerprints[-1]["url"] == left["url"]
                    same_hash = (
                        bool(fingerprints[-1]["hash"])
                        and fingerprints[-1]["hash"] == left["hash"]
                        or bool(fingerprints[-1]["normalized_hash"])
                        and fingerprints[-1]["normalized_hash"] == left["normalized_hash"]
                    )
                    title_similar = (
                        bool(fingerprints[-1]["title"] and left["title"])
                        and SequenceMatcher(None, fingerprints[-1]["title"], left["title"]).ratio()
                        >= 0.93
                    )
                    other_content = _normalise_text(
                        _value(source_list[member_index], "content", "")
                    )
                    content_similar = (
                        bool(normalized_content)
                        and len(normalized_content) >= 160
                        and len(other_content) >= 160
                        and SequenceMatcher(
                            None, normalized_content[:6000], other_content[:6000]
                        ).ratio()
                        >= 0.90
                    )
                    if (
                        same_origin
                        or same_url
                        or same_hash
                        or content_similar
                        or (title_similar and fingerprints[-1]["publisher"] == left["publisher"])
                    ):
                        assigned = index
                        break
                if assigned is not None:
                    break
            if assigned is None:
                clusters.append([len(fingerprints) - 1])
            else:
                clusters[assigned].append(len(fingerprints) - 1)
        result: list[SourceCluster] = []
        for members in clusters:
            first = fingerprints[members[0]]
            ids = [str(_value(source_list[member], "id", member)) for member in members]
            origin_type = (
                "primary"
                if any(self._is_primary(source_list[member]) for member in members)
                else "secondary"
            )
            origin = first["origin"] or first["url"] or first["hash"]
            result.append(
                SourceCluster(
                    cluster_id="cluster_" + hashlib.sha256(origin.encode()).hexdigest()[:16],
                    origin_type=origin_type,
                    origin_identifier=origin,
                    member_source_ids=ids,
                    canonical_source_id=ids[0],
                    independence_confidence=1.0 if len(members) == 1 else 0.92,
                    reason=(
                        "same canonical origin, content hash, citation chain, "
                        "or near-identical repost"
                    )
                    if len(members) > 1
                    else "no matching origin found",
                )
            )
        return result

    analyze = cluster_sources
    analyze_sources = cluster_sources
    cluster = cluster_sources

    def independent_source_count(self, sources: Sequence[Any]) -> int:
        return len(self.cluster_sources(sources))

    def assess_quality(
        self, source: Any, cluster: SourceCluster | None = None
    ) -> SourceQualityAssessment:
        meta = _metadata(source)
        source_type = _normalise_text(meta.get("source_type") or _value(source, "source_type", ""))
        content = str(_value(source, "content", ""))
        primary = self._is_primary(source)
        authority = 0.75 if meta.get("author") or _value(source, "author", None) else 0.45
        if any(
            token in source_type
            for token in ("interview", "speech", "official", "book", "archive", "first_person")
        ):
            authority += 0.15
        if any(token in source_type for token in ("social", "snippet", "aggregate")):
            authority -= 0.1
        completeness = (
            sum(
                bool(meta.get(key) or _value(source, key, None))
                for key in ("canonical_url", "publisher", "author", "published_at")
            )
            / 4
        )
        specificity = min(1.0, len(content) / 3500)
        historical = (
            0.8 if re.search(r"\b(?:18|19|20)\d{2}\b", content) or meta.get("event_time") else 0.55
        )
        independence = cluster.independence_confidence if cluster else 0.5
        score = max(
            0.0, min(1.0, (authority + specificity + historical + completeness + independence) / 5)
        )
        return SourceQualityAssessment(
            source_id=str(_value(source, "id", "")),
            quality_score=score,
            source_authority=max(0.0, min(1.0, authority)),
            primary_or_secondary="primary" if primary else "secondary",
            specificity=specificity,
            independence=independence,
            historical_relevance=historical,
            metadata_completeness=completeness,
            cluster_id=cluster.cluster_id if cluster else None,
        )

    @staticmethod
    def _is_primary(source: Any) -> bool:
        meta = _metadata(source)
        kind = _normalise_text(meta.get("source_type") or _value(source, "source_type", ""))
        return bool(
            meta.get("is_primary")
            or meta.get("first_person")
            or any(
                token in kind
                for token in ("interview", "speech", "official", "archive", "book", "first_person")
            )
        )


class MarginalInformationGainTracker:
    def __init__(self, *, window: int = 10, threshold: float = 0.08) -> None:
        self.window = max(1, safe_int(window, default=10, minimum=1) or 10)
        normalized_threshold = safe_probability(threshold, default=0.08)
        self.threshold = (
            normalized_threshold if normalized_threshold is not None else 0.08
        )
        self.snapshots: list[InformationGainSnapshot] = []

    def calculate(
        self,
        round_index: int,
        sources: Sequence[Any],
        artifacts: Sequence[Mapping[str, Any]] | None = None,
        previous_sources: Sequence[Any] | None = None,
        previous_artifacts: Sequence[Mapping[str, Any]] | None = None,
        dimension_progress: Mapping[str, int] | None = None,
        previous_dimension_progress: Mapping[str, int] | None = None,
        life_stage_progress: Mapping[str, int] | None = None,
        previous_life_stage_progress: Mapping[str, int] | None = None,
    ) -> InformationGainSnapshot:
        current_sources = list(sources)
        old_sources = list(previous_sources or [])
        current_artifacts = list(artifacts or [])
        old_artifacts = list(previous_artifacts or [])
        old_ids = {str(_value(source, "id", "")) for source in old_sources}
        new_sources = [
            source for source in current_sources if str(_value(source, "id", "")) not in old_ids
        ]
        old_claims = {
            str(claim.get("content") or claim.get("id") or "")
            for artifact in old_artifacts
            for claim in (artifact.get("claims") or [])
            if isinstance(claim, Mapping)
        }
        current_claims = {
            str(claim.get("content") or claim.get("id") or "")
            for artifact in current_artifacts
            for claim in (artifact.get("claims") or [])
            if isinstance(claim, Mapping)
        }
        new_claims = current_claims - old_claims
        old_conflicts = sum(len(artifact.get("conflicts") or []) for artifact in old_artifacts)
        current_conflicts = sum(
            len(artifact.get("conflicts") or []) for artifact in current_artifacts
        )
        old_categories = {
            _normalise_text(_metadata(source).get("category") or _value(source, "source_type", ""))
            for source in old_sources
        }
        current_categories = {
            _normalise_text(_metadata(source).get("category") or _value(source, "source_type", ""))
            for source in current_sources
        }
        old_primary = sum(
            1 for source in old_sources if SourceIndependenceAnalyzer._is_primary(source)
        )
        current_primary = sum(
            1 for source in current_sources if SourceIndependenceAnalyzer._is_primary(source)
        )
        dimension_delta = sum(
            max(
                0,
                (safe_int((dimension_progress or {}).get(key), default=0, minimum=0) or 0)
                - (
                    safe_int(
                        (previous_dimension_progress or {}).get(key),
                        default=0,
                        minimum=0,
                    )
                    or 0
                ),
            )
            for key in set(dimension_progress or {}) | set(previous_dimension_progress or {})
        )
        life_delta = sum(
            max(
                0,
                (safe_int((life_stage_progress or {}).get(key), default=0, minimum=0) or 0)
                - (
                    safe_int(
                        (previous_life_stage_progress or {}).get(key),
                        default=0,
                        minimum=0,
                    )
                    or 0
                ),
            )
            for key in set(life_stage_progress or {}) | set(previous_life_stage_progress or {})
        )
        behavior = sum(
            1
            for claim in new_claims
            if any(
                token in _normalise_text(claim)
                for token in ("decid", "behavio", "pressure", "failure")
            )
        )
        expression = sum(
            1
            for claim in new_claims
            if any(
                token in _normalise_text(claim) for token in ("said", "phrase", "style", "voice")
            )
        )
        relationship = sum(
            1
            for claim in new_claims
            if any(
                token in _normalise_text(claim)
                for token in ("relationship", "family", "colleague", "rival")
            )
        )
        snapshot = InformationGainSnapshot(
            round_index=safe_int(round_index, default=0, minimum=0) or 0,
            new_claims=len(new_claims),
            new_supported_claims=len(new_claims),
            new_contradictions=max(0, current_conflicts - old_conflicts),
            new_behavior_patterns=behavior,
            new_expression_patterns=expression,
            new_relationship_evidence=relationship,
            new_life_stage_evidence=life_delta,
            new_source_categories=len(current_categories - old_categories),
            new_primary_evidence=max(0, current_primary - old_primary),
            new_dimension_coverage=dimension_delta,
            marginal_gain_score=self._score(
                len(new_sources),
                len(new_claims),
                dimension_delta,
                life_delta,
                max(0, current_conflicts - old_conflicts),
                len(current_categories - old_categories),
            ),
        )
        self.snapshots.append(snapshot)
        return snapshot

    snapshot = calculate

    @staticmethod
    def _score(
        new_sources: int,
        new_claims: int,
        dimensions: int,
        life_stages: int,
        contradictions: int,
        categories: int,
    ) -> float:
        raw = (
            new_claims * 0.06
            + new_sources * 0.02
            + dimensions * 0.08
            + life_stages * 0.08
            + contradictions * 0.07
            + categories * 0.05
        )
        return max(0.0, min(1.0, raw))

    @property
    def low_gain_windows(self) -> int:
        if not self.snapshots:
            return 0
        windows = 0
        for index in range(len(self.snapshots) - self.window + 1):
            window = self.snapshots[index : index + self.window]
            if sum(item.marginal_gain_score for item in window) / len(window) < self.threshold:
                windows += 1
        return windows


class ResearchGapAnalyzer:
    """Finds the next evidence gaps and produces targeted, non-repeating queries."""

    def analyze(
        self,
        sources: Sequence[Any],
        required_dimensions: Sequence[str],
        min_sources_per_dimension: int,
        dimension_progress: Mapping[str, int] | None = None,
        life_stage_model: LifeStageModel | None = None,
        life_stage_progress: Mapping[str, int] | None = None,
        min_evidence_per_life_stage: int = 1,
        category_count: int | None = None,
        min_source_categories: int = 0,
        primary_count: int | None = None,
        secondary_count: int | None = None,
        contradiction_coverage: ContradictionCoverage | Mapping[str, Any] | None = None,
        relationship_evidence: int | None = None,
        behavior_evidence: int | None = None,
        expression_evidence: int | None = None,
        decision_evidence: int | None = None,
    ) -> list[ResearchGap]:
        min_sources_per_dimension = safe_int(
            min_sources_per_dimension, default=0, minimum=0
        ) or 0
        min_evidence_per_life_stage = safe_int(
            min_evidence_per_life_stage, default=1, minimum=0
        ) or 0
        min_source_categories = safe_int(min_source_categories, default=0, minimum=0) or 0

        def optional_count(value: Any) -> int | None:
            if value is None:
                return None
            return safe_int(value, default=0, minimum=0) or 0

        category_count = optional_count(category_count)
        primary_count = optional_count(primary_count)
        secondary_count = optional_count(secondary_count)
        relationship_evidence = optional_count(relationship_evidence)
        behavior_evidence = optional_count(behavior_evidence)
        expression_evidence = optional_count(expression_evidence)
        decision_evidence = optional_count(decision_evidence)
        gaps: list[ResearchGap] = []
        progress = dimension_progress or {}
        for dimension in required_dimensions:
            count = safe_int(progress.get(dimension), default=0, minimum=0) or 0
            if count < min_sources_per_dimension:
                gaps.append(
                    ResearchGap(
                        type="dimension",
                        target=dimension,
                        severity="high",
                        priority=95,
                        evidence_count=count,
                        details=(
                            f"{dimension} requires {min_sources_per_dimension} "
                            "independent evidence items"
                        ),
                        suggested_queries=[
                            f"{{name}} {dimension.replace('_', ' ')} first person "
                            "interview failure criticism"
                        ],
                    )
                )
        if life_stage_model:
            life_progress = life_stage_progress or {}
            for stage in life_stage_model.life_stages:
                count = safe_int(life_progress.get(stage.id), default=0, minimum=0) or 0
                required = max(min_evidence_per_life_stage, stage.required_evidence)
                if count < required:
                    gaps.append(
                        ResearchGap(
                            type="life_stage",
                            target=stage.id,
                            severity="high",
                            priority=90,
                            evidence_count=count,
                            details=f"{stage.title} requires event evidence",
                            suggested_queries=[
                                f"{{name}} {stage.title} event timeline interview failure"
                            ],
                        )
                    )
        if category_count is not None and category_count < min_source_categories:
            gaps.append(
                ResearchGap(
                    type="source_category",
                    target="diverse_source_categories",
                    severity="medium",
                    priority=72,
                    evidence_count=category_count,
                    suggested_queries=["{{name}} official archive interview biography criticism"],
                )
            )
        if (
            primary_count is not None
            and secondary_count is not None
            and (primary_count == 0 or secondary_count == 0)
        ):
            gaps.append(
                ResearchGap(
                    type="primary_secondary_balance",
                    target="primary_and_secondary_sources",
                    severity="high",
                    priority=84,
                    evidence_count=primary_count + secondary_count,
                    suggested_queries=[
                        "{{name}} first-person interview speech transcript",
                        "{{name}} long-form critical profile",
                    ],
                )
            )
        for gap_type, target, signal_count, queries in (
            (
                "relationships",
                "relationship_evidence",
                relationship_evidence,
                ["{{name}} relationships collaborators rivals family"],
            ),
            (
                "behavior",
                "behavior_under_pressure",
                behavior_evidence,
                ["{{name}} decision behavior failure under pressure"],
            ),
            (
                "expression",
                "expression_dna",
                expression_evidence,
                ["{{name}} interview transcript characteristic phrases"],
            ),
            (
                "decisions",
                "decision_evidence",
                decision_evidence,
                ["{{name}} key decisions tradeoffs outcomes"],
            ),
        ):
            if signal_count is not None and signal_count <= 0:
                gaps.append(
                    ResearchGap(
                        type=gap_type,
                        target=target,
                        severity="medium",
                        priority=76,
                        evidence_count=signal_count,
                        suggested_queries=queries,
                    )
                )
        contradiction = ContradictionCoverage.model_validate(contradiction_coverage or {})
        if not contradiction.search_executed:
            gaps.append(
                ResearchGap(
                    type="contradiction_search",
                    target="failures_changes_criticism",
                    severity="high",
                    priority=88,
                    suggested_queries=[
                        "{{name}} failure controversy criticism changed views",
                        "{{name}} opposing accounts decision error",
                    ],
                )
            )
        return sorted(gaps, key=lambda gap: gap.priority, reverse=True)

    def build_gap_queries(
        self,
        name: str,
        gaps: Sequence[ResearchGap],
        *,
        query_history: Mapping[str, Any] | None = None,
        limit: int = 12,
    ) -> list[str]:
        limit = safe_int(limit, default=12, minimum=0, maximum=1000) or 0
        if limit == 0:
            return []
        history = query_history or {}
        candidates: list[str] = []
        for gap in gaps:
            for template in gap.suggested_queries:
                query = str(template).replace("{name}", name).strip()
                stats = history.get(query) if isinstance(history, Mapping) else None
                if isinstance(stats, Mapping) and (
                    (safe_probability(stats.get("duplicate_rate"), default=0.0) or 0.0) >= 0.8
                    or (safe_int(stats.get("attempts"), default=0, minimum=0) or 0) >= 2
                    and (safe_probability(stats.get("information_gain"), default=0.0) or 0.0)
                    < 0.05
                ):
                    continue
                if _normalise_text(query) not in {_normalise_text(item) for item in candidates}:
                    candidates.append(query)
                if len(candidates) >= limit:
                    return candidates
        return candidates

    def should_repeat(self, query: str, query_history: Mapping[str, Any]) -> bool:
        stats = query_history.get(query, {}) if isinstance(query_history, Mapping) else {}
        return not (
            isinstance(stats, Mapping)
            and (safe_int(stats.get("attempts"), default=0, minimum=0) or 0) >= 2
            and (safe_probability(stats.get("information_gain"), default=0.0) or 0.0) < 0.05
        )

    find_gaps = analyze


class AdaptiveResearchStopGate:
    def evaluate(
        self,
        policy: Any,
        coverage: Mapping[str, Any],
        marginal_gain: Sequence[InformationGainSnapshot]
        | MarginalInformationGainTracker
        | None = None,
        source_count: int = 0,
        round_index: int = 0,
        high_priority_gaps: Sequence[ResearchGap | Mapping[str, Any]] | None = None,
        subject_richness: str = "moderate",
        source_space_exhausted: bool = False,
    ) -> AdaptiveResearchStopResult:
        source_count = safe_int(source_count, default=0, minimum=0) or 0
        min_unique_sources = safe_int(
            getattr(policy, "min_unique_sources", 0), default=0, minimum=0
        ) or 0
        min_sources_per_dimension = safe_int(
            getattr(policy, "min_sources_per_dimension", 0), default=0, minimum=0
        ) or 0
        min_source_categories = safe_int(
            getattr(policy, "min_source_categories", 0), default=0, minimum=0
        ) or 0
        marginal_window = safe_int(
            getattr(policy, "marginal_gain_window", 10), default=10, minimum=1
        ) or 10
        required_low_gain_windows = safe_int(
            getattr(policy, "required_low_gain_windows", 2), default=2, minimum=0
        ) or 0
        marginal_threshold = safe_probability(
            getattr(policy, "marginal_gain_stop_threshold", 0.08), default=0.08
        )
        marginal_threshold = marginal_threshold if marginal_threshold is not None else 0.08
        minimums = bool(
            (safe_int(
                coverage.get("independent_sources", coverage.get("unique_sources", 0)),
                default=0,
                minimum=0,
            ) or 0)
            >= min_unique_sources
        )
        quality_sources = bool(
            (safe_int(
                coverage.get(
                    "quality_source_count",
                    coverage.get("independent_sources", coverage.get("unique_sources", 0)),
                ),
                default=0,
                minimum=0,
            ) or 0)
            >= min_unique_sources
        )
        dimensions = coverage.get("dimensions_complete")
        if dimensions is None:
            dimension_rows = coverage.get("dimension_coverage") or {}
            dimensions = bool(dimension_rows) and all(
                (safe_int(
                    (row or {}).get(
                        "independent_evidence_count", (row or {}).get("evidence_count", 0)
                    ),
                    default=0,
                    minimum=0,
                ) or 0)
                >= min_sources_per_dimension
                for row in dimension_rows.values()
            )
        dimensions = bool(dimensions)
        categories = bool(
            (safe_int(coverage.get("source_category_count"), default=0, minimum=0) or 0)
            >= min_source_categories
        )
        life = bool(
            coverage.get(
                "life_stages_complete", not getattr(policy, "require_life_stage_coverage", False)
            )
        )
        balance = bool(coverage.get("primary_secondary_balanced", True))
        contradiction = bool(
            coverage.get(
                "contradiction_search_executed",
                not getattr(policy, "require_contradiction_search", False),
            )
        )
        gaps = [
            gap
            for gap in (high_priority_gaps or [])
            if (
                gap.high_priority
                if isinstance(gap, ResearchGap)
                else (safe_int((gap or {}).get("priority"), default=0, minimum=0) or 0) >= 80
            )
        ]
        no_high_gaps = not gaps
        if isinstance(marginal_gain, MarginalInformationGainTracker):
            tracker = marginal_gain
            snapshots = tracker.snapshots
        else:
            tracker = None
            snapshots = list(marginal_gain or [])
        low_windows = (
            tracker.low_gain_windows
            if tracker
            else self._low_gain_windows(
                snapshots,
                marginal_window,
                marginal_threshold,
            )
        )
        effective_target = (
            safe_int(
                getattr(
                    policy,
                    "effective_target",
                    lambda richness="moderate": getattr(policy, "preferred_source_target", 0),
                )(subject_richness),
                default=0,
                minimum=0,
            ) or 0
            if callable(getattr(policy, "effective_target", None))
            else safe_int(getattr(policy, "preferred_source_target", 0), default=0, minimum=0) or 0
        )
        hard_max = safe_int(
            getattr(policy, "hard_max_sources", getattr(policy, "max_sources", 0)),
            default=0,
            minimum=0,
        ) or 0
        all_coverage = (
            minimums
            and quality_sources
            and dimensions
            and categories
            and life
            and balance
            and contradiction
            and no_high_gaps
        )
        marginal_pass = not bool(
            getattr(policy, "require_marginal_gain_gate", True)
        ) or low_windows >= required_low_gain_windows
        high_gain = bool(
            snapshots
            and snapshots[-1].marginal_gain_score
            >= marginal_threshold
        )
        if hard_max and source_count >= hard_max:
            return AdaptiveResearchStopResult(
                passed=all_coverage,
                continue_research=False,
                completed_with_gaps=not all_coverage,
                stop_reason="hard_budget_exhausted",
                details={
                    "low_gain_windows": low_windows,
                    "effective_target": effective_target,
                    "high_priority_gaps": [
                        gap.target if isinstance(gap, ResearchGap) else gap.get("target")
                        for gap in gaps
                    ],
                },
            )
        if source_space_exhausted:
            return AdaptiveResearchStopResult(
                passed=all_coverage,
                continue_research=False,
                completed_with_gaps=not all_coverage,
                stop_reason="source_space_exhausted",
                details={"low_gain_windows": low_windows, "effective_target": effective_target},
            )
        if (
            all_coverage
            and marginal_pass
            and (
                source_count >= effective_target
                or low_windows >= required_low_gain_windows
            )
        ):
            return AdaptiveResearchStopResult(
                passed=True,
                continue_research=False,
                stop_reason="coverage_satisfied_and_low_gain",
                details={"low_gain_windows": low_windows, "effective_target": effective_target},
            )
        if all_coverage and source_count >= effective_target and low_windows == 0 and not high_gain:
            return AdaptiveResearchStopResult(
                passed=False,
                continue_research=True,
                stop_reason=None,
                details={
                    "continue_reason": "target_reached_but_marginal_gain_window_not_satisfied",
                    "effective_target": effective_target,
                },
            )
        return AdaptiveResearchStopResult(
            passed=False,
            continue_research=True,
            completed_with_gaps=False,
            details={
                "continue_reason": "high_priority_gap"
                if gaps
                else "coverage_or_marginal_gain_incomplete",
                "effective_target": effective_target,
                "low_gain_windows": low_windows,
                "high_gain": high_gain,
                "subject_richness": subject_richness,
            },
        )

    @staticmethod
    def _low_gain_windows(
        snapshots: Sequence[InformationGainSnapshot], window: int, threshold: float
    ) -> int:
        if not snapshots:
            return 0
        return sum(
            1
            for index in range(max(0, len(snapshots) - window + 1))
            if sum(item.marginal_gain_score for item in snapshots[index : index + window])
            / len(snapshots[index : index + window])
            < threshold
        )

    should_stop = evaluate


__all__ = [
    "AdaptiveResearchStopGate",
    "AdaptiveResearchStopResult",
    "ContradictionCoverage",
    "DimensionCoverage",
    "FictionalPersonaCoverage",
    "InformationGainSnapshot",
    "LifeStage",
    "LifeStageModel",
    "MarginalInformationGainTracker",
    "PrivatePersonaCoverage",
    "PrivatePersonaCoveragePolicy",
    "ResearchCheckpoint",
    "ResearchGap",
    "ResearchGapAnalyzer",
    "ResearchRichnessEstimator",
    "SourceCluster",
    "SourceIndependenceAnalyzer",
    "SourceQualityAssessment",
]
