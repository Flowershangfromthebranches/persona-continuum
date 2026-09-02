from __future__ import annotations

import pytest

from persona_continuum.application.persona_creation_service import (
    PersonaCreationError,
    PersonaCreationOrchestrator,
)


def test_normalize_research_plan_standard() -> None:
    raw = {
        "queries": ["Jensen Huang Nvidia history", "Jensen Huang early life"],
        "life_stages": [{"id": "stage_1", "title": "Early Years", "start": "1963", "end": "1984"}],
        "contradiction_search_queries": ["Jensen Huang controversies"],
    }
    normalized = PersonaCreationOrchestrator._normalize_research_plan(raw)
    assert normalized["queries"] == ["Jensen Huang Nvidia history", "Jensen Huang early life"]
    assert len(normalized["life_stages"]) == 1
    assert normalized["contradiction_search_queries"] == ["Jensen Huang controversies"]
    assert normalized["negative_evidence_queries"] == []


def test_normalize_research_plan_alternative_keys() -> None:
    raw = {
        "search_queries": ["Jensen Huang key milestones", "Nvidia founding story"],
        "life_stages": [],
    }
    normalized = PersonaCreationOrchestrator._normalize_research_plan(raw)
    assert normalized["queries"] == ["Jensen Huang key milestones", "Nvidia founding story"]


def test_normalize_research_plan_nested_plan() -> None:
    raw = {
        "plan": {
            "queries": ["Jensen Huang management style"],
            "life_stages": [{"id": "s1", "title": "CEO era"}],
        }
    }
    normalized = PersonaCreationOrchestrator._normalize_research_plan(raw)
    assert normalized["queries"] == ["Jensen Huang management style"]


def test_normalize_research_plan_query_objects() -> None:
    raw = {
        "queries": [
            {"query": "Jensen Huang Stanford education", "focus": "academic"},
            {"text": "Jensen Huang Denny's meeting", "focus": "founding"},
        ]
    }
    normalized = PersonaCreationOrchestrator._normalize_research_plan(raw)
    assert normalized["queries"] == [
        "Jensen Huang Stanford education",
        "Jensen Huang Denny's meeting",
    ]


def test_normalize_research_plan_embedded_in_stages() -> None:
    raw = {
        "life_stages": [
            {"id": "s1", "title": "Early", "queries": ["Jensen Huang childhood Oregon"]},
            {"id": "s2", "title": "Nvidia", "search_queries": ["Nvidia GPU evolution"]},
        ]
    }
    normalized = PersonaCreationOrchestrator._normalize_research_plan(raw)
    assert normalized["queries"] == [
        "Jensen Huang childhood Oregon",
        "Nvidia GPU evolution",
    ]


def test_normalize_research_plan_invalid_raises() -> None:
    with pytest.raises(PersonaCreationError):
        PersonaCreationOrchestrator._normalize_research_plan({"empty": "data"})
