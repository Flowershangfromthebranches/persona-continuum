from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.domain.persona import (
    PersonaManifest,
    PersonaRecord,
    PersonaType,
    RunMode,
)


class SubjectKind(StrEnum):
    REAL_PERSON = "real_person"
    FICTIONAL_CHARACTER = "fictional_character"
    ORIGINAL_CHARACTER = "original_character"
    SYNTHETIC_PERSONA = "synthetic_persona"


class LifeStatus(StrEnum):
    UNKNOWN = "unknown"
    LIVING = "living"
    DECEASED = "deceased"
    NOT_APPLICABLE = "not_applicable"


class PrivacyScope(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    NOT_APPLICABLE = "not_applicable"


class ResearchMode(StrEnum):
    AUTO = "auto"
    WEB = "web"
    LOCAL = "local"
    HYBRID = "hybrid"
    GUIDED_INTERVIEW = "guided_interview"
    AUTHOR_DEFINED = "author_defined"


class WebResearchScope(StrEnum):
    IDENTITY_AND_BIOGRAPHY = "identity_and_biography"
    IDENTITY_AND_CANON = "identity_and_canon"
    BACKGROUND_ONLY = "background_only"


class IdentitySpec(BaseModel):
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    subject_kind: SubjectKind = SubjectKind.REAL_PERSON
    work_or_universe: str | None = None
    life_status: LifeStatus = LifeStatus.UNKNOWN
    privacy_scope: PrivacyScope = PrivacyScope.PUBLIC
    identity_context: str | None = None
    user_defined_facts: str | None = None
    research_mode: ResearchMode = ResearchMode.AUTO
    web_scope: WebResearchScope | None = None
    research_instructions: str | None = None

    def effective_web_scope(self) -> WebResearchScope:
        if self.web_scope is not None:
            return self.web_scope
        if self.subject_kind == SubjectKind.REAL_PERSON:
            return WebResearchScope.IDENTITY_AND_BIOGRAPHY
        if self.subject_kind == SubjectKind.FICTIONAL_CHARACTER:
            return WebResearchScope.IDENTITY_AND_CANON
        return WebResearchScope.BACKGROUND_ONLY

    def to_legacy_pair(self) -> tuple[PersonaType, str]:
        """Map V2 IdentitySpec to backward-compatible (PersonaType, creation_mode)."""
        if self.subject_kind == SubjectKind.REAL_PERSON:
            if self.privacy_scope == PrivacyScope.PRIVATE:
                p_type = (
                    PersonaType.PRIVATE_DECEASED_PERSON
                    if self.life_status == LifeStatus.DECEASED
                    else PersonaType.PRIVATE_LIVING_PERSON
                )
                mode = (
                    "guided_interview"
                    if self.research_mode == ResearchMode.GUIDED_INTERVIEW
                    else "private_materials"
                )
            else:
                p_type = (
                    PersonaType.PUBLIC_HISTORICAL_PERSON
                    if self.life_status == LifeStatus.DECEASED
                    else PersonaType.PUBLIC_LIVING_PERSON
                )
                mode = "public_research"
        else:
            p_type = PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON
            if self.research_mode in {ResearchMode.WEB, ResearchMode.AUTO}:
                mode = (
                    "public_research"
                    if self.subject_kind == SubjectKind.FICTIONAL_CHARACTER
                    else "fictional"
                )
            elif self.research_mode == ResearchMode.HYBRID:
                mode = "public_research"
            elif self.research_mode == ResearchMode.GUIDED_INTERVIEW:
                mode = "guided_interview"
            else:
                mode = "fictional"
        return p_type, mode

    @classmethod
    def from_legacy(
        cls,
        *,
        display_name: str,
        aliases: list[str] | None = None,
        persona_type: PersonaType | str,
        creation_mode: str = "public_research",
        extra: dict[str, Any] | None = None,
    ) -> IdentitySpec:
        extra = extra or {}
        pt_str = str(getattr(persona_type, "value", persona_type))
        mode_str = str(creation_mode).lower()

        if pt_str == "public_living_person":
            kind = SubjectKind.REAL_PERSON
            status = LifeStatus.LIVING
            privacy = PrivacyScope.PUBLIC
        elif pt_str == "public_historical_person":
            kind = SubjectKind.REAL_PERSON
            status = LifeStatus.DECEASED
            privacy = PrivacyScope.PUBLIC
        elif pt_str == "private_living_person":
            kind = SubjectKind.REAL_PERSON
            status = LifeStatus.LIVING
            privacy = PrivacyScope.PRIVATE
        elif pt_str == "private_deceased_person":
            kind = SubjectKind.REAL_PERSON
            status = LifeStatus.DECEASED
            privacy = PrivacyScope.PRIVATE
        else:
            kind = (
                SubjectKind.FICTIONAL_CHARACTER
                if extra.get("work_or_universe")
                else SubjectKind.SYNTHETIC_PERSONA
            )
            status = LifeStatus.NOT_APPLICABLE
            privacy = PrivacyScope.NOT_APPLICABLE

        res_mode = {
            "public_research": ResearchMode.WEB,
            "private_materials": ResearchMode.LOCAL,
            "guided_interview": ResearchMode.GUIDED_INTERVIEW,
            "fictional": ResearchMode.AUTHOR_DEFINED,
            "hybrid": ResearchMode.HYBRID,
        }.get(mode_str, ResearchMode.AUTO)

        return cls(
            display_name=display_name,
            aliases=list(aliases or []),
            subject_kind=kind,
            work_or_universe=extra.get("work_or_universe"),
            life_status=status,
            privacy_scope=privacy,
            identity_context=extra.get("identity_context"),
            user_defined_facts=extra.get("user_defined_facts"),
            research_mode=res_mode,
            web_scope=extra.get("web_scope"),
            research_instructions=extra.get("research_instructions"),
        )


class ResolvedIdentity(BaseModel):
    display_name: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    subject_kind: SubjectKind = SubjectKind.REAL_PERSON
    work_or_universe: str | None = None
    life_status: LifeStatus | None = None
    canonical_identifiers: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None
    confidence: float = 1.0
    positive_search_terms: list[str] = Field(default_factory=list)
    negative_search_terms: list[str] = Field(default_factory=list)
    ambiguity_notes: list[str] = Field(default_factory=list)
    candidate_entities: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "IdentitySpec",
    "LifeStatus",
    "PersonaManifest",
    "PersonaRecord",
    "PersonaType",
    "PrivacyScope",
    "ResearchMode",
    "ResolvedIdentity",
    "RunMode",
    "SubjectKind",
    "WebResearchScope",
]
