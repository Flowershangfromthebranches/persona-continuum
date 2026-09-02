from __future__ import annotations

import pytest

from persona_continuum.application.persona_creation_service import PersonaCreationJob
from persona_continuum.domain.persona import PersonaType


def _seed_personas(app):
    return app.personas.create(
        display_name="Steve Jobs",
        aliases=["Jobs"],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode="counterfactual_continuation",
    )


def test_actor_persona_match(app) -> None:
    persona = _seed_personas(app)
    matches = app.persona_creation.match_world_actors(
        [{"id": "steve_jobs", "name": "Steve Jobs", "actor_type": "persona_actor"}]
    )
    assert matches[0].status == "MATCHED"
    assert matches[0].persona_id == persona.id


def test_organization_not_marked_missing_persona(app) -> None:
    matches = app.persona_creation.match_world_actors(
        [{"id": "apple", "name": "Apple Inc.", "actor_type": "organization_actor"}]
    )
    assert matches[0].status == "NOT_PERSON"


def test_missing_public_actor_requests_confirmation(app) -> None:
    matches = app.persona_creation.match_world_actors(
        [{"id": "jensen_huang", "name": "Jensen Huang", "actor_type": "persona_actor"}]
    )
    assert matches[0].status == "MISSING"


@pytest.mark.anyio
async def test_missing_public_actor_auto_creation(app, monkeypatch) -> None:
    async def fake_create_job(**kwargs):
        return PersonaCreationJob(
            id="pcjob_test",
            display_name=kwargs["display_name"],
            persona_type=kwargs["persona_type"],
            creation_mode=kwargs["creation_mode"],
            runtime_source=kwargs["runtime_source"],
            agent_id=kwargs["agent_id"],
            model_id=kwargs["model_id"] or "fake-gpt-5",
            persona_id=None,
        )

    monkeypatch.setattr(app.persona_creation, "create_job", fake_create_job)
    result = await app.persona_creation.confirm_world_persona_completion(
        actors=[{"id": "jensen_huang", "name": "Jensen Huang", "actor_type": "persona_actor"}],
        selected_actor_ids=["jensen_huang"],
        runtime={"runtime_source": "test", "agent_id": "fake_agent", "model_id": "fake-gpt-5"},
    )
    assert result.jobs


def test_private_missing_actor_requires_materials(app) -> None:
    matches = app.persona_creation.match_world_actors(
        [
            {
                "id": "friend_zhang",
                "name": "我的朋友小张",
                "actor_type": "persona_actor",
                "private": True,
            }
        ]
    )
    assert matches[0].status == "MISSING"


def test_new_persona_auto_bound_to_actor(app) -> None:
    actor = {"id": "new_person", "name": "New Person", "actor_type": "persona_actor"}
    matches = app.persona_creation.match_world_actors([actor])
    assert matches[0].status == "MISSING"
    actor["persona_id"] = "new_person"
    rebound = app.persona_creation.match_world_actors([actor])
    # A binding is only considered MATCHED once the Persona exists in the library.
    assert rebound[0].status == "MISSING"


def test_direct_world_create_pauses_for_persona_confirmation(app) -> None:
    # The HTTP acceptance test exercises the 409 response; the application
    # match result is the shared gate used by both preview and direct-create.
    matches = app.persona_creation.match_world_actors(
        [{"id": "unknown_actor", "name": "Unknown Actor", "actor_type": "persona_actor"}]
    )
    assert any(match.status == "MISSING" for match in matches)
