"""Registry of built-in video model profiles.

Profiles are packaged YAML files under
``persona_continuum.narrative.video_profiles``. Only facts stated by official
sources are recorded; unknown capabilities stay null and are treated as hard
requirement gaps by downstream planners and compilers.
"""

from __future__ import annotations

from importlib import resources
from typing import Any, cast

import yaml

from persona_continuum.domain.narrative import VideoModelProfile

_CACHE: dict[str, VideoModelProfile] | None = None


def load_builtin_profiles() -> dict[str, VideoModelProfile]:
    """Load (and cache) the bundled video model profiles from YAML."""
    global _CACHE
    cached = _CACHE
    if cached is not None:
        return cached
    profiles: dict[str, VideoModelProfile] = {}
    package = resources.files("persona_continuum.narrative.video_profiles")
    for entry in sorted(package.iterdir(), key=lambda item: item.name):
        if entry.name.startswith("_") or not entry.name.endswith(".yaml"):
            continue
        raw = yaml.safe_load(entry.read_text(encoding="utf-8"))
        data = cast(dict[str, Any], raw)
        profile = VideoModelProfile.model_validate(data)
        profiles[profile.id] = profile
    _CACHE = profiles
    return profiles


def list_profiles() -> list[VideoModelProfile]:
    """All built-in profiles, ordered by profile id for determinism."""
    return [load_builtin_profiles()[key] for key in sorted(load_builtin_profiles())]


def get_profile(profile_id: str) -> VideoModelProfile:
    """Fetch one built-in profile; raise ValueError with a stable code."""
    profiles = load_builtin_profiles()
    if profile_id not in profiles:
        raise ValueError(f"VIDEO_PROFILE_NOT_FOUND: no video model profile '{profile_id}'")
    return profiles[profile_id]


def validate_profile(data: dict[str, Any]) -> VideoModelProfile:
    """Validate untrusted profile data (e.g. a custom user-supplied profile)."""
    return VideoModelProfile.model_validate(data)


_CAPABILITY_FLAGS = (
    "supports_audio",
    "supports_dialogue",
    "supports_sfx",
    "supports_image_to_video",
    "supports_text_to_video",
    "supports_first_frame",
    "supports_last_frame",
    "supports_first_last_frame",
    "supports_reference_images",
    "supports_timestamp_prompting",
    "supports_negative_prompt",
)


def profile_capabilities_digest(profile: VideoModelProfile) -> dict[str, Any]:
    """Compact JSON-safe digest for UI display and permission prompts.

    Only non-null capability flags are included; null (UNKNOWN) flags stay
    absent so callers can distinguish facts from gaps.
    """
    capabilities: dict[str, Any] = {}
    for flag in _CAPABILITY_FLAGS:
        value = getattr(profile, flag)
        if value is not None:
            capabilities[flag] = value
    if profile.max_reference_images is not None:
        capabilities["max_reference_images"] = profile.max_reference_images
    capabilities["negative_prompt_strategy"] = profile.negative_prompt_strategy
    capabilities["reference_prompt_syntax"] = profile.reference_prompt_syntax
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "vendor": profile.vendor,
        "verification_status": profile.verification_status,
        "modes": list(profile.supported_modes),
        "durations": {
            "supported": (
                list(profile.supported_durations_seconds)
                if profile.supported_durations_seconds is not None
                else None
            ),
            "min": profile.duration_min_seconds,
            "max": profile.duration_max_seconds,
        },
        "aspect_ratios": list(profile.supported_aspect_ratios),
        "capabilities": capabilities,
        "clip_planning": dict(profile.clip_planning),
        "prompt_language_preferences": list(profile.prompt_language_preferences),
    }
