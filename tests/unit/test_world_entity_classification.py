from __future__ import annotations

import pytest


def test_world_entity_person_is_agent(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="Steve Jobs decides the product strategy.",
        entities=[{"id": "jobs", "name": "Steve Jobs", "actor_type": "persona_actor"}],
    )
    assert result.classified_agents[0].subtype == "person"
    assert result.classified_agents[0].agent_capable is True


def test_world_entity_organization_is_agent(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="Apple funds a new silicon program.",
        entities=[{"id": "apple", "name": "Apple", "actor_type": "organization_actor"}],
    )
    assert result.classified_agents[0].subtype == "organization"


def test_world_entity_institution_is_agent(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="美国政府调整出口政策。",
        entities=[{"id": "us_gov", "name": "美国政府"}],
    )
    assert result.classified_agents[0].subtype == "institution"


def test_world_entity_collective_is_agent(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="开发者社区决定是否采用新的工具链。",
        entities=[{"id": "developers", "name": "开发者社区"}],
    )
    assert result.classified_agents[0].subtype == "collective"


@pytest.mark.parametrize(
    ("name", "subtype"),
    [("出口政策", "policy"), ("台积电良率下降", "event"), ("先进制程产能", "resource")],
)
def test_world_entity_non_agent_types_are_not_agents(app, name: str, subtype: str) -> None:
    result = app.entity_classifier.classify_sync(
        description=name,
        entities=[{"id": name, "name": name, "subtype": subtype}],
    )
    assert not result.classified_agents
    assert result.non_agent_entities[0].subtype == subtype
    assert result.non_agent_entities[0].agent_capable is False


def test_world_entity_policy_is_not_agent(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="政策约束",
        entities=[{"id": "policy", "name": "出口政策", "subtype": "policy"}],
    )
    assert result.non_agent_entities[0].category == "non_agent"


def test_world_entity_event_is_not_agent(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="事件",
        entities=[{"id": "event", "name": "台积电良率下降", "subtype": "event"}],
    )
    assert result.non_agent_entities[0].agent_capable is False


def test_world_entity_resource_is_not_agent(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="资源",
        entities=[{"id": "resource", "name": "先进制程产能", "subtype": "resource"}],
    )
    assert result.non_agent_entities[0].subtype == "resource"


def test_default_actor_runtime_only_applies_to_agent_capable_entities(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="Apple 与出口政策互动；先进制程产能是初始资源。",
        entities=[
            {"id": "apple", "name": "Apple", "actor_type": "organization_actor"},
            {"id": "policy", "name": "出口政策", "subtype": "policy"},
            {"id": "capacity", "name": "先进制程产能", "subtype": "resource"},
        ],
    )
    assert [item.id for item in result.classified_agents] == ["apple"]
    assert {item.id for item in result.non_agent_entities} == {"policy", "capacity"}


@pytest.mark.anyio
async def test_world_entity_classification_uses_llm_then_deterministic_validation(
    app, monkeypatch
) -> None:
    async def fake_llm(**kwargs):
        del kwargs
        return {
            "policy": {
                "id": "policy",
                "category": "agent",
                "agent_capable": True,
                "subtype": "person",
                "rationale": "model mistake",
                "confidence": 0.99,
            }
        }

    monkeypatch.setattr(app.entity_classifier, "_classify_with_llm", fake_llm)
    result = await app.entity_classifier.classify(
        description="出口政策",
        entities=[{"id": "policy", "name": "出口政策", "subtype": "policy"}],
        adapter=app.agent_registry.get_adapter("fake_agent"),
        runtime={"agent_id": "fake_agent", "model_id": "fake-gpt-5"},
        require_llm=True,
    )
    assert result.llm_used is True
    assert result.non_agent_entities[0].agent_capable is False
    assert any(warning.startswith("classification_corrected") for warning in result.warnings)
