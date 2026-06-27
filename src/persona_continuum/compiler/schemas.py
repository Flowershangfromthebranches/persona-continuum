from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from persona_continuum.domain.evidence import EvidenceClaim, EvidenceSource
from persona_continuum.domain.persona import PersonaManifest

Dimension = Literal[
    "identity_and_timeline",
    "works_and_views",
    "interviews_and_dialogue",
    "expression_dna",
    "decisions_and_behavior",
    "third_party_views",
    "affect_relationship_defense",
    "values_desires_contradictions",
]

ClaimType = Literal[
    "historical_self_report",
    "historical_third_party_report",
    "historical_inference",
    "counterfactual_simulated",
    "user_correction",
]


class ArtifactClaim(BaseModel):
    content: str = Field(min_length=1)
    source_id: str | None = None
    claim_type: ClaimType
    confidence: float = Field(ge=0, le=1)
    reliability: float = Field(default=0.5, ge=0, le=1)
    inference_strength: float = Field(default=0.5, ge=0, le=1)
    raw_location: str | None = None
    event_time: str | None = None
    has_counter_evidence: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactMemory(BaseModel):
    content: str = Field(min_length=1)
    type: str = "semantic"
    importance: float = Field(default=0.5, ge=0, le=1)
    source_kind: str = "historical_inference"
    source_id: str | None = None
    source_confidence: float = Field(default=0.5, ge=0, le=1)
    participants: list[str] = Field(default_factory=list)


class ResearchArtifact(BaseModel):
    artifact_id: str = Field(min_length=1)
    schema_version: str
    dimension: Dimension
    source_ids: list[str] = Field(min_length=1)
    claims: list[ArtifactClaim] = Field(default_factory=list)
    memories: list[ArtifactMemory] = Field(default_factory=list)
    extracted_components: dict[str, Any]
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    uncertainty: dict[str, Any]
    created_by: str = Field(min_length=1)
    artifact_hash: str = Field(min_length=1)

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value != "1.1":
            raise ValueError("unsupported_schema_version")
        return value

    @model_validator(mode="after")
    def _validate_references(self) -> ResearchArtifact:
        source_ids = set(self.source_ids)
        for claim in self.claims:
            if claim.source_id is not None and claim.source_id not in source_ids:
                raise ValueError("claim_source_id_not_in_artifact")
        for memory in self.memories:
            if memory.source_id is not None and memory.source_id not in source_ids:
                raise ValueError("memory_source_id_not_in_artifact")
        level = self.uncertainty.get("level", 0.5)
        if not isinstance(level, int | float) or not 0 <= float(level) <= 1:
            raise ValueError("uncertainty_level_out_of_range")
        if not self.claims and not self.memories and not self.extracted_components:
            raise ValueError("empty_artifact")
        return self


__all__ = [
    "EvidenceClaim",
    "EvidenceSource",
    "PersonaManifest",
    "ResearchArtifact",
]
