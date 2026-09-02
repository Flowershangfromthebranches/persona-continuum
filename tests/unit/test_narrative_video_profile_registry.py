"""Unit tests for the built-in video model profile registry."""

from __future__ import annotations

import pytest

from persona_continuum.narrative.video_profile_registry import (
    get_profile,
    list_profiles,
    load_builtin_profiles,
    profile_capabilities_digest,
)

BUILTIN_PROFILE_IDS = (
    "generic",
    "veo_3_1",
    "runway_gen_4_5",
    "hailuo",
    "seedance",
    "kling",
)


def test_all_six_builtin_profiles_load() -> None:
    profiles = load_builtin_profiles()
    assert set(profiles) == set(BUILTIN_PROFILE_IDS)
    listed = list_profiles()
    assert [profile.id for profile in listed] == sorted(BUILTIN_PROFILE_IDS)
    assert listed[0].id == "generic"


def test_verified_profiles_carry_official_sources_draft_stays_kling() -> None:
    for profile_id in ("veo_3_1", "runway_gen_4_5"):
        profile = get_profile(profile_id)
        assert profile.verification_status == "verified"
        assert profile.official_sources, f"{profile_id} needs official sources"
        assert all(str(source) for source in profile.official_sources)
    kling = get_profile("kling")
    assert kling.verification_status == "draft"


def test_unverifiable_capabilities_stay_unknown() -> None:
    # None means UNKNOWN: a fact not stated by an official source, treated as
    # a hard requirement gap downstream. It must never be guessed.
    runway = get_profile("runway_gen_4_5")
    assert runway.supports_audio is None
    veo = get_profile("veo_3_1")
    assert veo.supports_timestamp_prompting is None
    kling = get_profile("kling")
    # kling's aspect ratios are not documented by an official source: empty.
    assert kling.supported_aspect_ratios == []


def test_get_profile_unknown_id_raises_stable_code() -> None:
    with pytest.raises(ValueError, match="VIDEO_PROFILE_NOT_FOUND"):
        get_profile("definitely_not_a_model")
    with pytest.raises(ValueError, match="definitely_not_a_model"):
        get_profile("definitely_not_a_model")


def test_capabilities_digest_includes_planning_and_language_prefs() -> None:
    for profile_id in BUILTIN_PROFILE_IDS:
        profile = get_profile(profile_id)
        digest = profile_capabilities_digest(profile)
        assert digest["id"] == profile_id
        assert digest["verification_status"] == profile.verification_status
        assert isinstance(digest["clip_planning"], dict)
        assert digest["clip_planning"] == profile.clip_planning
        assert isinstance(digest["prompt_language_preferences"], list)
        assert digest["prompt_language_preferences"] == list(
            profile.prompt_language_preferences
        )
    # Null capability flags stay absent from the digest (facts vs gaps).
    generic_digest = profile_capabilities_digest(get_profile("generic"))
    assert "supports_audio" not in generic_digest["capabilities"]
    veo_digest = profile_capabilities_digest(get_profile("veo_3_1"))
    assert veo_digest["capabilities"]["supports_audio"] is True


def test_reference_prompt_syntax_defaults_to_textual_anchor_for_all_profiles(
) -> None:
    """Guide-layer honesty rule: unless an official source documents a named
    reference-input syntax, every profile falls back to textual_anchor, and
    the fact is carried in the capabilities digest."""
    for profile_id in BUILTIN_PROFILE_IDS:
        profile = get_profile(profile_id)
        assert profile.reference_prompt_syntax == "textual_anchor", profile_id
        digest = profile_capabilities_digest(profile)
        assert digest["capabilities"]["reference_prompt_syntax"] == "textual_anchor"
