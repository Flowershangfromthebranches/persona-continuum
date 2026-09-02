from __future__ import annotations

import pytest

from persona_continuum.domain.persona import PersonaType
from persona_continuum.domain.profile import ProfileStatus, ProfileType


def test_existing_persona_migrates_to_persona_profile(app) -> None:
    persona = app.personas.create(
        display_name="Steve Jobs",
        aliases=["Jobs"],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode="counterfactual_continuation",
    )
    profile = app.profile_library.sync_persona(persona)
    assert profile.profile_type == ProfileType.PERSONA
    assert profile.persona_id == persona.id
    assert profile.slug


def test_profile_card_has_summary(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.ORGANIZATION,
        display_name="Apple",
        payload={"mission": "Build integrated products"},
    )
    assert profile.summary
    assert "Compiled digital continuum persona" not in profile.summary


def test_profile_list_can_filter_by_type(app) -> None:
    app.profile_library.create_profile(
        profile_type=ProfileType.ORGANIZATION,
        display_name="Apple",
    )
    app.profile_library.create_profile(
        profile_type=ProfileType.INSTITUTION,
        display_name="美国联邦政府",
    )
    organizations = app.profile_library.list_profiles(profile_type=ProfileType.ORGANIZATION)
    assert [profile.display_name for profile in organizations] == ["Apple"]


def test_duplicate_profile_detected(app) -> None:
    app.profile_library.create_profile(
        profile_type=ProfileType.ORGANIZATION,
        display_name="OpenAI",
    )
    with pytest.raises(ValueError, match="profile_already_exists"):
        app.profile_library.create_profile(
            profile_type=ProfileType.ORGANIZATION,
            display_name="OpenAI",
        )


def test_profile_detail_shows_type_specific_fields(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.INSTITUTION,
        display_name="美国联邦政府",
        payload={"policy_tools": ["regulation", "subsidy"]},
    )
    detail = app.profile_library.get_detail(profile.id)
    assert detail["profile_type"] == ProfileType.INSTITUTION.value
    assert detail["payload"]["policy_tools"] == ["regulation", "subsidy"]


def test_profile_summary_generated_after_compile(app) -> None:
    persona = app.personas.create(
        display_name="Ada Lovelace",
        aliases=[],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode="counterfactual_continuation",
    )
    profile = app.profile_library.sync_persona(persona)
    assert profile.summary
    assert profile.status in {ProfileStatus.DRAFT, ProfileStatus.COMPILED}


def test_profile_summary_generated_after_enrichment(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.COLLECTIVE,
        display_name="开发者社区",
    )
    updated = app.profile_library.update_profile(
        profile.id,
        summary="由工具采用、激励与内部差异共同驱动的开发者群体。",
        payload={"incentives": ["互操作性", "生态收益"]},
        status=ProfileStatus.COMPILED,
        compile_state="compiled",
        created_by="test_enrichment",
    )
    assert updated.version == profile.version + 1
    assert updated.summary.startswith("由工具采用")
    assert app.profile_library.list_versions(profile.id)[0].version == profile.version


def test_persona_profile_versions_survive_new_id_export_import(app, tmp_path) -> None:
    persona = app.personas.create(
        display_name="Portable Persona",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode="counterfactual_continuation",
    )
    original = app.profile_library.sync_persona(persona)
    enriched = app.profile_library.update_profile(
        original.id,
        summary="A versioned Persona profile prepared for portable export.",
        payload={"decision_style": "evidence first"},
        created_by="test_enrichment",
    )

    archive = app.personas.export_persona(persona.id, tmp_path / "portable-profile.zip")
    imported = app.personas.import_persona(archive, new_id="portable-persona-copy")

    imported_profile = app.profile_library.get_profile(imported.id)
    assert imported_profile.persona_id == imported.id
    assert imported_profile.version == enriched.version
    assert app.profile_library.list_versions(imported.id)[0].version == original.version
    assert app.profile_library.get_profile(original.id).persona_id == original.persona_id
