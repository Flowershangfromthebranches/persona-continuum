from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class PersonaType(StrEnum):
    PUBLIC_LIVING_PERSON = "public_living_person"
    PUBLIC_HISTORICAL_PERSON = "public_historical_person"
    PRIVATE_LIVING_PERSON = "private_living_person"
    PRIVATE_DECEASED_PERSON = "private_deceased_person"
    FICTIONAL_OR_SYNTHETIC_PERSON = "fictional_or_synthetic_person"


class RunMode(StrEnum):
    HISTORICAL_SNAPSHOT = "historical_snapshot"
    COUNTERFACTUAL_CONTINUATION = "counterfactual_continuation"
    DIGITAL_CONTINUATION = "digital_continuation"


class PersonaManifest(BaseModel):
    id: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    persona_type: PersonaType
    created_at: datetime = Field(default_factory=utc_now)
    version: str = "0.1.0"
    birth_date: str | None = None
    death_date: str | None = None
    data_cutoff_date: str | None = None
    run_mode: RunMode
    active: bool = False
    confidence: dict[str, float] = Field(default_factory=dict)
    source_count: int = 0
    compile_state: str = "draft"
    has_counterfactual_continuation: bool = False
    current_main_branch: str | None = None
    sensitivity: str = "normal"
    archived: bool = False


class PersonaRecord(BaseModel):
    id: str
    display_name: str
    manifest: PersonaManifest
    package_path: str
    metadata: dict[str, Any] = Field(default_factory=dict)
