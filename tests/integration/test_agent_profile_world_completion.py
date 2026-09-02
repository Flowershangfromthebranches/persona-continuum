from __future__ import annotations

from persona_continuum.domain.profile import ProfileType
from persona_continuum.world.models import ActorRuntimeConfig, WorldSeed


def test_missing_person_profile_requests_completion(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="Jensen Huang leads the response.",
        entities=[{"id": "jensen", "name": "Jensen Huang", "actor_type": "persona_actor"}],
    )
    assert result.missing_profiles[0].subtype == "person"
    assert result.missing_profiles[0].profile_match_status == "missing"


def test_missing_organization_profile_requests_completion(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="TSMC allocates advanced capacity.",
        entities=[{"id": "tsmc", "name": "TSMC", "actor_type": "organization_actor"}],
    )
    assert result.missing_profiles[0].subtype == "organization"


def test_missing_institution_profile_requests_completion(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="工信部发布新的政策。",
        entities=[{"id": "miit", "name": "工信部", "subtype": "institution"}],
    )
    assert result.missing_profiles[0].subtype == "institution"


def test_non_agent_entity_not_sent_to_completion(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="先进制程产能是初始资源。",
        entities=[{"id": "capacity", "name": "先进制程产能", "subtype": "resource"}],
    )
    assert result.non_agent_entities[0].profile_match_status == "non_agent"
    assert not result.missing_profiles


def test_default_actor_runtime_only_applies_to_agent_capable_entities(app) -> None:
    result = app.entity_classifier.classify_sync(
        description="Apple reacts to an export policy.",
        entities=[
            {"id": "apple", "name": "Apple", "actor_type": "organization_actor"},
            {"id": "policy", "name": "export policy", "subtype": "policy"},
        ],
    )
    assert result.classified_agents[0].subtype == ProfileType.ORGANIZATION.value
    assert result.non_agent_entities[0].agent_capable is False


def test_default_actor_runtime_is_not_inherited_by_environment_entities(app) -> None:
    seed = WorldSeed(
        start_date="2013-01-01",
        initial_actors=["jobs", "market"],
        metadata={
            "initial_actors": [
                {"id": "jobs", "name": "Steve Jobs", "actor_type": "persona_actor"},
                {
                    "id": "market",
                    "name": "AI market state",
                    "actor_type": "environment_actor",
                    "entity_category": "non_agent",
                },
            ],
            "entity_classification": {
                "non_agent_entities": [
                    {"id": "market", "name": "AI market state", "subtype": "market_state"}
                ]
            },
        },
    )
    actors = app.worlds.engine.actor_mgr.initialize_actors_for_world(
        seed,
        ActorRuntimeConfig(agent_id="fake_agent", model_id="fake-gpt-5"),
    )
    assert actors["jobs"].runtime_config.agent_id == "fake_agent"
    assert "market" not in actors
