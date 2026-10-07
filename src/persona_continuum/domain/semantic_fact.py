"""Semantic Facts: durable, provenance-backed, time-scoped memory.

An Episode records *what happened*; a Semantic Fact records *what is true*
about a counterpart, distilled from one or more Episodes.  Three properties
carry the whole design:

1. **Provenance-backed.**  Every fact points at the Episodes (and the turns
   inside them) that support it, so "why does the system believe this?" always
   has an answer that ends in raw dialogue.
2. **Temporally valid.**  A fact that stops being true is not overwritten; it
   gets ``valid_until`` and ``superseded_by_fact_id``, and the new value is a
   new row.  "喜欢茉莉奶绿" then "改喝美式" is history, not a mutation.
3. **Origin-separated.**  What the user asserted is never merged with what the
   persona guessed.  ``persona_stated`` interpretations stay ``inferred``, and
   by default inferred-only candidates are not persisted as long-term facts at
   all -- storing less beats turning 苏禾's guess into the user's history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from persona_continuum.domain.persona import utc_now
from persona_continuum.numeric import safe_float

#: Version of the extraction contract.  Bump when the candidate schema or the
#: consolidation rules change in a way that makes older facts incomparable.
FACT_EXTRACTION_VERSION = 1

_MAX_TEXT_CHARS = 400
_MAX_DISPLAY_CHARS = 240
_MAX_FACTS_PER_EXTRACTION = 32


class FactCategory(StrEnum):
    """Deliberately coarse: this is for de-duplication and future retrieval, not
    an ontology."""

    IDENTITY = "identity"
    PREFERENCE = "preference"
    PLAN = "plan"
    PROJECT = "project"
    RELATION = "relation"
    HABIT = "habit"
    POSSESSION = "possession"
    LOCATION = "location"
    GOAL = "goal"
    COMMITMENT = "commitment"
    OTHER = "other"

    @classmethod
    def from_raw(cls, value: Any) -> FactCategory:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.OTHER


class FactOrigin(StrEnum):
    """Who made the claim.  Never collapse these: the persona's reading of the
    user is not the user's statement."""

    USER_ASSERTED = "user_asserted"
    PERSONA_ASSERTED = "persona_asserted"
    SYSTEM_OBSERVED = "system_observed"
    INFERRED = "inferred"

    @classmethod
    def from_raw(cls, value: Any) -> FactOrigin:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.INFERRED


class FactStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    CANDIDATE = "candidate"
    RETRACTED = "retracted"
    EXPIRED = "expired"


class FactRelation(StrEnum):
    """How a candidate relates to an existing fact in the same slot."""

    SAME = "same"
    SUPPORTS = "supports"
    COMPATIBLE = "compatible"
    SUPERSEDES = "supersedes"
    CONTRADICTS = "contradicts"
    UNRELATED = "unrelated"

    @classmethod
    def from_raw(cls, value: Any) -> FactRelation:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.UNRELATED


class PlanStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @classmethod
    def from_raw(cls, value: Any) -> PlanStatus:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.NOT_APPLICABLE


class FactDurability(StrEnum):
    """Whether the claim describes something stable or a passing state."""

    PERMANENT = "permanent"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"

    @classmethod
    def from_raw(cls, value: Any) -> FactDurability:
        raw = str(value or "").strip().casefold()
        for candidate in cls:
            if candidate.value == raw:
                return candidate
        return cls.UNKNOWN


class FactEvidenceRole(StrEnum):
    PRIMARY = "primary"
    SUPPORTING = "supporting"
    RECONFIRMATION = "reconfirmation"


def normalize_key_part(value: Any) -> str:
    """Canonical form for identity parts (subject/predicate/value)."""

    return " ".join(str(value or "").strip().casefold().split())


def canonical_fact_key(category: FactCategory | str, subject: Any, predicate: Any) -> str:
    """Stable identity of a fact SLOT (one value per slot when exclusive)."""

    category_value = category.value if isinstance(category, FactCategory) else str(category)
    return "|".join(
        [category_value, normalize_key_part(subject), normalize_key_part(predicate)]
    )


def canonical_value_key(value_json: dict[str, Any] | None) -> str:
    """Canonical form of the fact's VALUE, used for dedup and comparison."""

    payload = dict(value_json or {})
    text = payload.get("text")
    if text is not None and len(payload) == 1:
        return normalize_key_part(text)
    return normalize_key_part(
        "|".join(f"{key}={payload[key]}" for key in sorted(payload))
    )


class FactCandidate(BaseModel):
    """One extracted claim, exactly as the model must present it.

    Nothing here is trusted: every field is normalised, bounded, and defaulted,
    so a malformed payload degrades into a weaker candidate instead of a crash
    or a poisoned row.
    """

    model_config = ConfigDict(extra="ignore")

    category: FactCategory = FactCategory.OTHER
    subject: str = ""
    predicate: str = ""
    value: str = ""
    display_text: str = ""
    origin: FactOrigin = FactOrigin.INFERRED
    durability: FactDurability = FactDurability.UNKNOWN
    confidence: float = 0.5
    exclusive: bool = False
    relation: FactRelation = FactRelation.UNRELATED
    related_fact_id: str | None = None
    evidence_turn_id: str | None = None
    evidence_role: FactEvidenceRole = FactEvidenceRole.PRIMARY
    temporal_expression: str | None = None
    temporal_normalized: str | None = None
    temporal_confidence: float = 0.0
    plan_status: PlanStatus = PlanStatus.NOT_APPLICABLE
    notes: str | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _clean_category(cls, value: Any) -> FactCategory:
        return FactCategory.from_raw(value)

    @field_validator("origin", mode="before")
    @classmethod
    def _clean_origin(cls, value: Any) -> FactOrigin:
        return FactOrigin.from_raw(value)

    @field_validator("durability", mode="before")
    @classmethod
    def _clean_durability(cls, value: Any) -> FactDurability:
        return FactDurability.from_raw(value)

    @field_validator("relation", mode="before")
    @classmethod
    def _clean_relation(cls, value: Any) -> FactRelation:
        return FactRelation.from_raw(value)

    @field_validator("plan_status", mode="before")
    @classmethod
    def _clean_plan_status(cls, value: Any) -> PlanStatus:
        return PlanStatus.from_raw(value)

    @field_validator("evidence_role", mode="before")
    @classmethod
    def _clean_evidence_role(cls, value: Any) -> FactEvidenceRole:
        raw = str(value or "").strip().casefold()
        for candidate in FactEvidenceRole:
            if candidate.value == raw:
                return candidate
        return FactEvidenceRole.PRIMARY

    @field_validator(
        "subject",
        "predicate",
        "value",
        "display_text",
        "temporal_expression",
        "notes",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return str(value or "").strip()[:_MAX_TEXT_CHARS]

    @field_validator("temporal_normalized", mode="before")
    @classmethod
    def _clean_temporal(cls, value: Any) -> str | None:
        text = str(value or "").strip()[:_MAX_TEXT_CHARS]
        return text or None

    @field_validator("related_fact_id", "evidence_turn_id", mode="before")
    @classmethod
    def _clean_id(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("confidence", "temporal_confidence", mode="before")
    @classmethod
    def _clean_probability(cls, value: Any) -> float:
        normalized = safe_float(value, default=None, minimum=0.0, maximum=1.0)
        return 0.5 if normalized is None else float(normalized)

    @property
    def value_json(self) -> dict[str, Any]:
        return {"text": self.value} if self.value else {}

    @property
    def fact_key(self) -> str:
        return canonical_fact_key(self.category, self.subject, self.predicate)

    @property
    def value_key(self) -> str:
        return canonical_value_key(self.value_json)

    @property
    def is_usable(self) -> bool:
        """A candidate with no subject/predicate/value carries no information."""

        return bool(self.subject and self.predicate and self.value)

    def display(self) -> str:
        if self.display_text:
            return self.display_text[:_MAX_DISPLAY_CHARS]
        return f"{self.subject} {self.predicate} = {self.value}"[:_MAX_DISPLAY_CHARS]


class FactExtractionPayload(BaseModel):
    """Schema-validated extractor output."""

    model_config = ConfigDict(extra="ignore")

    facts: list[FactCandidate] = Field(default_factory=list)

    @field_validator("facts", mode="before")
    @classmethod
    def _bound_facts(cls, value: Any) -> list[Any]:
        """Drop junk entries instead of failing the whole extraction.

        One malformed entry must not cost the caller the twenty well-formed ones
        next to it; the service separately rejects a payload that yields nothing
        usable at all.
        """

        if not isinstance(value, list):
            return []
        usable = [
            item for item in value if isinstance(item, (dict, FactCandidate))
        ]
        return usable[:_MAX_FACTS_PER_EXTRACTION]

    def usable(self) -> list[FactCandidate]:
        return [candidate for candidate in self.facts if candidate.is_usable]


class SemanticFact(BaseModel):
    """A persisted Semantic Fact."""

    model_config = ConfigDict(extra="ignore")

    id: str
    persona_id: str
    counterpart_id: str
    branch_id: str = "main"
    category: FactCategory = FactCategory.OTHER
    fact_key: str = ""
    value_key: str = ""
    subject: str = ""
    predicate: str = ""
    value_json: dict[str, Any] = Field(default_factory=dict)
    display_text: str = ""
    status: FactStatus = FactStatus.ACTIVE
    origin: FactOrigin = FactOrigin.INFERRED
    durability: FactDurability = FactDurability.UNKNOWN
    plan_status: PlanStatus = PlanStatus.NOT_APPLICABLE
    confidence: float = 0.5
    evidence_count: int = 0
    last_confirmed_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    observed_at: datetime | None = None
    temporal_expression: str | None = None
    temporal_normalized: str | None = None
    temporal_confidence: float = 0.0
    superseded_by_fact_id: str | None = None
    supersedes_fact_id: str | None = None
    extraction_version: int = FACT_EXTRACTION_VERSION
    visibility: str = "private_session"
    material_scope: str = "character_visible"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status is FactStatus.ACTIVE

    @property
    def is_valid_now(self) -> bool:
        if not self.is_active:
            return False
        now = datetime.now(UTC)
        if self.valid_from is not None and self.valid_from > now:
            return False
        return not (self.valid_until is not None and self.valid_until <= now)


__all__ = [
    "FACT_EXTRACTION_VERSION",
    "FactCandidate",
    "FactCategory",
    "FactDurability",
    "FactEvidenceRole",
    "FactExtractionPayload",
    "FactOrigin",
    "FactRelation",
    "FactStatus",
    "PlanStatus",
    "SemanticFact",
    "canonical_fact_key",
    "canonical_value_key",
    "normalize_key_part",
]
