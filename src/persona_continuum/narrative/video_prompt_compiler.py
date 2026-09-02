"""Compile profile-specific video prompts from generation clips.

Pure functions over domain models: the compiler reads the target model's
``prompt_strategy`` and capability flags and produces a final prompt per clip.
It never fabricates files — missing reference assets become explicit
``asset_requirements`` entries with ``prepared: false``.
"""

from __future__ import annotations

from typing import Any

from persona_continuum.application._utils import new_id
from persona_continuum.domain.narrative import (
    GenerationClip,
    ModelPromptPackage,
    ProductionAsset,
    ProductionPackage,
    Shot,
    VideoModelProfile,
)
from persona_continuum.narrative.runtime import (
    SHOOTING_LOCATION_CONTEXT_MISSING,
    NarrativeAgentError,
)
from persona_continuum.narrative.video_profile_registry import profile_capabilities_digest

_EPS = 1e-6

# Deterministic positive-phrasing rewrites for models that reject negative
# prompts (negative_prompt_strategy == "positive_phrasing_only").
_POSITIVE_REWRITES: dict[str, str] = {
    "no camera movement": "locked camera",
    "no camera shake": "steady camera",
    "no zoom": "fixed focal length",
    "no cuts": "single continuous take",
    "no text": "clean frame",
    "no subtitles": "clean frame",
    "no watermark": "clean frame",
    "no fast motion": "slow deliberate motion",
    "no flicker": "even lighting",
}


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def validate_clip_against_profile(
    clip: GenerationClip, profile: VideoModelProfile
) -> list[str]:
    """Return violation codes for capability gaps (empty list = compliant)."""
    violations: list[str] = []
    duration = clip.duration_seconds
    if profile.supported_durations_seconds:
        if not any(abs(value - duration) < _EPS for value in profile.supported_durations_seconds):
            violations.append("VIDEO_PROFILE_DURATION_UNSUPPORTED")
    elif profile.duration_min_seconds is not None and duration + _EPS < (
        profile.duration_min_seconds
    ) or profile.duration_max_seconds is not None and duration - _EPS > (
        profile.duration_max_seconds
    ):
        violations.append("VIDEO_PROFILE_DURATION_UNSUPPORTED")
    if profile.supported_aspect_ratios and clip.aspect_ratio not in (
        profile.supported_aspect_ratios
    ):
        violations.append("VIDEO_PROFILE_ASPECT_RATIO_UNSUPPORTED")
    if (
        clip.generation_mode not in ("", "auto")
        and profile.supported_modes
        and clip.generation_mode not in profile.supported_modes
    ):
        violations.append("VIDEO_PROFILE_MODE_UNSUPPORTED")
    return violations


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _strategy_for(profile: VideoModelProfile, mode: str) -> dict[str, Any]:
    strategy = profile.prompt_strategy.get(mode)
    return dict(strategy) if isinstance(strategy, dict) else {}


def _bible_entry_text(entry: dict[str, Any]) -> str:
    for key in ("visual_description", "description", "visual", "summary"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _bible_entry_name(entry: dict[str, Any]) -> str:
    for key in ("name", "character_id", "location_id", "title", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _character_anchor(
    character_id: str, production_package: ProductionPackage
) -> str:
    if not character_id:
        return ""
    for entry in production_package.character_visual_bible:
        if not isinstance(entry, dict):
            continue
        keys = {str(entry.get(key, "")) for key in ("character_id", "id", "name")}
        if character_id in keys:
            name = _bible_entry_name(entry)
            text = _bible_entry_text(entry)
            return f"{name}: {text}" if name and text else (name or text)
    return character_id


# 仅限旧编译器路径；新手册层（video_production_guide/copy_ready）禁止使用占位锚点
_GENERIC_LOCATION_ANCHOR = "the established location"


def _location_anchor(location: str, production_package: ProductionPackage) -> str:
    """Bible anchor for one clip's location.

    The anchor is always derived from the clip's OWN location: an empty or
    unmatched location must never inherit the first bible entry, which may
    describe a completely different scene — it falls back to a generalized
    phrase instead.
    """
    if location:
        for entry in production_package.location_visual_bible:
            if not isinstance(entry, dict):
                continue
            keys = {str(entry.get(key, "")) for key in ("location_id", "id", "name")}
            if location in keys:
                name = _bible_entry_name(entry)
                text = _bible_entry_text(entry)
                return f"{name}: {text}" if name and text else (name or text)
    return _GENERIC_LOCATION_ANCHOR


def _shot_negative_constraints(
    clip: GenerationClip, production_package: ProductionPackage
) -> list[str]:
    by_number = {shot.shot_number: shot for shot in production_package.shot_list}
    constraints: list[str] = []
    for number in clip.source_shot_numbers:
        shot: Shot | None = by_number.get(number)
        if shot is None:
            continue
        for constraint in shot.negative_constraints:
            if constraint and constraint not in constraints:
                constraints.append(constraint)
    return constraints


def _positive_phrasing(constraints: list[str]) -> list[str]:
    """Deterministic rewrite of negative constraints into positive phrasing."""
    rewrites: list[str] = []
    for constraint in constraints:
        positive = _POSITIVE_REWRITES.get(constraint.strip().lower())
        if positive and positive not in rewrites:
            rewrites.append(positive)
    return rewrites


def _clip_prop_ids(
    clip: GenerationClip, production_package: ProductionPackage
) -> list[str]:
    """Deterministic prop attribution (guide-layer input).

    A prop bible entry is attributed to a clip when its name appears as a
    case-insensitive substring in any source shot's action / visual_intent /
    visual_prompt text. Order follows the bible; names are deduplicated.
    """
    if not production_package.prop_visual_bible:
        return []
    shots_by_number = {shot.shot_number: shot for shot in production_package.shot_list}
    haystacks: list[str] = []
    for number in clip.source_shot_numbers:
        shot = shots_by_number.get(number)
        if shot is None:
            continue
        haystacks.append(
            f"{shot.action} {shot.visual_intent} {shot.visual_prompt}".lower()
        )
    if not haystacks:
        return []
    matched: list[str] = []
    for prop in production_package.prop_visual_bible:
        if not isinstance(prop, dict):
            continue
        name = _bible_entry_name(prop)
        if not name:
            continue
        lowered = name.lower()
        if any(lowered in haystack for haystack in haystacks) and name not in matched:
            matched.append(name)
    return matched


def _resolve_mode(clip: GenerationClip, profile: VideoModelProfile) -> str:
    if clip.generation_mode not in ("", "auto"):
        return clip.generation_mode
    has_frame_inputs = bool(clip.start_frame_asset_id or clip.reference_asset_ids)
    if has_frame_inputs and profile.supports_image_to_video is not False:
        return "image_to_video"
    return "text_to_video"


def _resolve_language(prompt_language: str, profile: VideoModelProfile) -> str:
    if prompt_language and prompt_language != "auto":
        return prompt_language
    if profile.prompt_language_preferences:
        return profile.prompt_language_preferences[0]
    return "en"


# ----------------------------------------------------------------------
# Prompt compilation
# ----------------------------------------------------------------------
def compile_clip_prompt(
    clip: GenerationClip,
    profile: VideoModelProfile,
    production_package: ProductionPackage,
    assets_by_id: dict[str, ProductionAsset],
    prompt_language: str = "auto",
) -> GenerationClip:
    """Return an updated copy of the clip with prompt / audio fields filled."""
    mode = _resolve_mode(clip, profile)
    strategy = _strategy_for(profile, mode)
    language = _resolve_language(prompt_language, profile)

    missing_reference = bool(clip.reference_asset_ids) and not all(
        asset_id in assets_by_id for asset_id in clip.reference_asset_ids
    )
    missing_start_frame = bool(clip.start_frame_asset_id) and (
        clip.start_frame_asset_id not in assets_by_id
    )
    reference_ready = not (missing_reference or missing_start_frame)

    segments: list[str] = []
    if mode == "image_to_video":
        # Motion-first prompting: never restate full static visuals; bible
        # entries serve only as short identity anchors when reference assets
        # are missing.
        if strategy.get("focus_on_motion", True):
            segments.append(f"Motion: {clip.subject_motion or 'natural subject motion'}.")
        if clip.environment_motion:
            segments.append(f"Environment motion: {clip.environment_motion}.")
        if clip.camera_intent:
            segments.append(f"Camera: {clip.camera_intent}.")
        if clip.visual_intent and strategy.get("include_subject_visuals") is True:
            segments.append(f"Subject: {clip.visual_intent}.")
        if not reference_ready:
            anchor = _character_anchor(
                clip.dialogue[0]["speaker"] if clip.dialogue else "", production_package
            )
            if anchor:
                segments.append(f"Subject anchor: {anchor}.")
    else:
        # Text-to-video carries the full static description plus motion.
        if strategy.get("include_subject_visuals", True):
            speaker = clip.dialogue[0]["speaker"] if clip.dialogue else ""
            anchor = _character_anchor(speaker, production_package) if speaker else ""
            segments.append(
                f"Subject: {clip.visual_intent or anchor or 'the scene subject'}."
            )
        if strategy.get("include_environment_visuals", True):
            environment_anchor = _location_anchor(clip.location, production_package)
            if environment_anchor:
                segments.append(f"Environment: {environment_anchor}.")
        segments.append(f"Composition and lighting per shot {clip.clip_number} style.")
        if strategy.get("include_camera", True) and clip.camera_intent:
            segments.append(f"Camera: {clip.camera_intent}.")
        if strategy.get("include_subject_motion", True) and clip.subject_motion:
            segments.append(f"Subject motion: {clip.subject_motion}.")
        if strategy.get("include_environment_motion", True) and clip.environment_motion:
            segments.append(f"Environment motion: {clip.environment_motion}.")
    if clip.purpose:
        segments.append(f"Beat: {clip.purpose}.")

    negative_constraints = _shot_negative_constraints(clip, production_package)
    negative_decision: dict[str, Any]
    negative_prompt: str | None = None
    if profile.supports_negative_prompt is True:
        negative_prompt = ", ".join(negative_constraints) or None
        negative_decision = {
            "decision": "separate_field",
            "strategy": profile.negative_prompt_strategy,
            "constraints": negative_constraints,
        }
    elif profile.supports_negative_prompt is False:
        positives = _positive_phrasing(negative_constraints)
        segments.extend(f"Ensure {positive}." for positive in positives)
        negative_decision = {
            "decision": "positive_phrasing_only",
            "strategy": profile.negative_prompt_strategy,
            "rewritten": positives,
            "dropped": [
                constraint
                for constraint in negative_constraints
                if constraint.strip().lower() not in _POSITIVE_REWRITES
            ],
        }
    else:
        negative_decision = {
            "decision": "unknown_capability_constraints_omitted",
            "strategy": profile.negative_prompt_strategy,
            "constraints": negative_constraints,
        }

    dialogue_lines = [
        f"{entry.get('speaker', '')}: {entry.get('line', '')}".strip(": ")
        for entry in clip.dialogue
    ]
    postproduction_audio_plan: list[str] = []
    audio_decision: dict[str, Any]
    audio_prompt: str | None = None
    if profile.supports_audio is True:
        parts: list[str] = []
        if dialogue_lines and profile.supports_dialogue is not False:
            parts.append(f"Speech: {'; '.join(dialogue_lines)}")
        elif dialogue_lines:
            parts.append(f"Voice to match: {'; '.join(dialogue_lines)}")
        for item in clip.audio_intent:
            parts.append(f"Audio: {item}.")
        audio_prompt = " ".join(parts) or None
        audio_decision = {"decision": "inline_audio_prompt", "dialogue_embedded": bool(
            dialogue_lines
        )}
    else:
        if dialogue_lines:
            postproduction_audio_plan.append(
                f"Add dialogue in post: {'; '.join(dialogue_lines)}"
            )
        for item in clip.audio_intent:
            postproduction_audio_plan.append(item)
        audio_decision = {"decision": "postproduction_audio_plan"}

    # Screen dialogue only when the model documents support; otherwise it is
    # a postproduction note handled above.
    if profile.supports_dialogue is True and dialogue_lines:
        segments.append(f"Dialogue: {' | '.join(dialogue_lines)}.")

    prompt = " ".join(segment.strip() for segment in segments if segment.strip())
    if profile.recommended_defaults.get("max_prompt_chars"):
        limit = int(profile.recommended_defaults["max_prompt_chars"])
        prompt = prompt[:limit]

    trace = {
        "profile_id": profile.id,
        "profile_version": profile.profile_version,
        "generation_mode": mode,
        "strategy_applied": {key: value for key, value in strategy.items()},
        "negative_prompt_decision": negative_decision,
        "audio_decision": audio_decision,
        "language": language,
        "reference_assets_ready": reference_ready,
    }
    return clip.model_copy(
        update={
            "generation_mode": mode,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "audio_prompt": audio_prompt,
            "postproduction_audio_plan": postproduction_audio_plan,
            "target_profile_id": profile.id,
            "compiler_trace": trace,
        }
    )


# ----------------------------------------------------------------------
# Copy-ready guide layer (video production guide)
# ----------------------------------------------------------------------
_ORDINALS: dict[int, str] = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
    13: "thirteenth",
    14: "fourteenth",
    15: "fifteenth",
    16: "sixteenth",
    17: "seventeenth",
    18: "eighteenth",
    19: "nineteenth",
    20: "twentieth",
}


def _ordinal(clip_number: int) -> str:
    """Sequence word for the goal line; falls back to ``#N`` past twentieth."""
    return _ORDINALS.get(clip_number, f"#{clip_number}")


def _reference_token(asset: ProductionAsset) -> str:
    """Named-reference token for one asset (guide-layer convention)."""
    explicit = str(asset.metadata.get("asset_key") or "").strip()
    if explicit:
        return explicit if explicit.startswith("@") else f"@{explicit}"
    return f"@{asset.name.strip().upper().replace(' ', '_')}"


def _character_identities_for_guide(
    character_ids: list[str], production_package: ProductionPackage
) -> list[tuple[str, str]]:
    """(character_id, full bible text) pairs; text stays empty when the id
    has no character_visual_bible entry (the caller owns the fallback)."""
    identities: list[tuple[str, str]] = []
    for character_id in character_ids:
        if not character_id:
            continue
        text = ""
        for entry in production_package.character_visual_bible:
            if not isinstance(entry, dict):
                continue
            keys = {str(entry.get(key, "")) for key in ("character_id", "id", "name")}
            if character_id in keys:
                text = _bible_entry_text(entry)
                break
        identities.append((character_id, text))
    return identities


def _location_identity_for_guide(
    location: str, production_package: ProductionPackage
) -> str:
    """Full environment identity for one clip's location (guide layer).

    Raises ``NarrativeAgentError`` with ``SHOOTING_LOCATION_CONTEXT_MISSING``
    when the location resolves to no location bible entry and no location
    list entry: the guide layer never falls back to a generic anchor.
    """
    if location:
        for entries in (
            production_package.location_visual_bible,
            production_package.location_list,
        ):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                keys = {
                    str(entry.get(key, "")) for key in ("location_id", "id", "name")
                }
                if location in keys:
                    name = _bible_entry_name(entry)
                    text = _bible_entry_text(entry)
                    if name or text:
                        return f"{name}: {text}" if name and text else (name or text)
    raise NarrativeAgentError(
        SHOOTING_LOCATION_CONTEXT_MISSING,
        f"Clip location '{location}' resolves to no location bible entry and "
        "no location list entry; add the environment to the episode's location "
        "visual bible before compiling the copy-ready guide.",
        stage="video_production_guide",
    )


def compile_copy_ready_prompt(
    clip: GenerationClip,
    profile: VideoModelProfile,
    production_package: ProductionPackage,
    assets_by_id: dict[str, ProductionAsset],
    frame_plan: dict[str, Any] | None = None,
    trace_out: dict[str, Any] | None = None,
) -> str:
    """Build ONE self-contained, copy-paste-ready prompt for one clip.

    The old :func:`compile_clip_prompt` path stays byte-identical: this guide
    layer calls it only to reuse its deterministic decisions (generation
    mode, audio/postproduction split) and then composes a multi-section
    prompt that never relies on placeholder phrases ("the established
    location", "per shot", "natural subject motion", "TBD", "TODO").

    ``frame_plan`` (built by the guide compiler) carries
    ``carry_in_start_frame`` (this clip starts from the previous clip's
    final state) and ``produces_next_start_frame`` (this clip's final frame
    becomes the next clip's start frame). ``trace_out``, when a dict is
    passed, is populated with ``{profile_id, syntax, sections_included,
    character_count}``.

    The old ``max_prompt_chars`` truncation is intentionally NOT applied:
    the copy-ready prompt owns its own section budget.
    """
    # Baseline compile (untouched old logic) provides the audio decision.
    compiled = compile_clip_prompt(clip, profile, production_package, assets_by_id)

    plan = frame_plan or {}
    carry_in = bool(plan.get("carry_in_start_frame"))
    produces_next = bool(plan.get("produces_next_start_frame"))
    syntax = profile.reference_prompt_syntax
    sections: list[str] = []
    sections_included: list[str] = []

    def add_section(header: str, lines: list[str]) -> None:
        body = [line.strip() for line in lines if line and line.strip()]
        if not body:
            return
        sections.append("\n".join([header, *body]))
        sections_included.append(header)

    duration_text = f"{clip.duration_seconds:g}"

    goal_lines = [
        f"Create the {_ordinal(clip.clip_number)} {duration_text}-second clip "
        "of the SAME continuous episode."
    ]
    if clip.purpose:
        goal_lines.append(f"Story beat: {clip.purpose}.")
    if carry_in and clip.clip_number > 1:
        goal_lines.append(
            "Continue exactly from the provided START FRAME (the final state "
            "of the previous clip); do not restart the scene."
        )
    goal_lines.append(f"Frame the shot in {clip.aspect_ratio}.")
    add_section("GOAL", goal_lines)

    start_frame_asset = (
        assets_by_id.get(clip.start_frame_asset_id)
        if clip.start_frame_asset_id
        else None
    )
    reference_assets = [
        assets_by_id[asset_id]
        for asset_id in clip.reference_asset_ids
        if asset_id in assets_by_id
    ]
    reference_lines: list[str] = []
    if syntax == "named_reference":
        if start_frame_asset is not None:
            token = _reference_token(start_frame_asset)
            reference_lines.append(
                f"START FRAME: attach it as named reference {token} and use "
                "it as the first frame of this clip."
            )
        for asset in reference_assets:
            reference_lines.append(
                "REFERENCE IMAGE: attach it as named reference "
                f"{_reference_token(asset)} ({asset.asset_type.replace('_', ' ')})."
            )
    elif syntax == "external_reference_only":
        reference_lines.append(
            "REFERENCE INPUTS: this platform has no named in-prompt references; "
            "upload the files below through the provider's own upload mechanism "
            "before submitting the prompt."
        )
        if start_frame_asset is not None:
            reference_lines.append(
                f"- Start frame: {start_frame_asset.name}; use the uploaded file "
                "as the first frame."
            )
        for asset in reference_assets:
            reference_lines.append(
                f"- Reference image: {asset.name} "
                f"({asset.asset_type.replace('_', ' ')})."
            )
    else:  # textual_anchor (default)
        if start_frame_asset is not None or carry_in:
            reference_lines.append(
                "START FRAME: use the provided START FRAME image as the first "
                "frame of this clip."
            )
        if reference_assets:
            names = ", ".join(asset.name for asset in reference_assets)
            reference_lines.append(
                "REFERENCE IMAGES: use the provided reference images "
                f"({names}) to lock identity and style."
            )
    if not reference_lines:
        reference_lines.append(
            "REFERENCE INPUTS: none for this clip; the written description "
            "below is the only visual anchor."
        )
    add_section("REFERENCE INPUTS", reference_lines)

    identity_lines: list[str] = []
    if clip.character_ids:
        for character_id, text in _character_identities_for_guide(
            clip.character_ids, production_package
        ):
            if text:
                identity_lines.append(f"- {character_id}: {text}")
            else:
                identity_lines.append(
                    f"- {character_id}: no character visual bible entry; match "
                    "this character to the provided reference images exactly."
                )
    else:
        identity_lines.append(
            "On-screen characters: none; this is an environment-only shot."
        )
    add_section("CHARACTER IDENTITY", identity_lines)

    environment_text = _location_identity_for_guide(clip.location, production_package)
    add_section("ENVIRONMENT IDENTITY", [f"- {environment_text}"])

    action_lines: list[str] = []
    if clip.visual_intent:
        action_lines.append(f"Action: {clip.visual_intent}.")
    if clip.subject_motion:
        action_lines.append(f"Performance and motion: {clip.subject_motion}.")
    add_section("ACTION AND PERFORMANCE", action_lines)

    if clip.camera_intent:
        add_section("CAMERA", [f"Camera: {clip.camera_intent}."])

    add_section(
        "TEMPORAL PROGRESSION",
        [
            f"The clip plays for {duration_text} seconds as one continuous "
            "take: the action starts at the beginning of the clip and "
            "progresses without internal cuts."
        ],
    )

    if clip.environment_motion:
        add_section("ENVIRONMENT MOTION", [f"{clip.environment_motion}."])

    dialogue_lines = [
        f"{entry.get('speaker', '')}: {entry.get('line', '')}".strip(": ")
        for entry in clip.dialogue
    ]
    if profile.supports_audio is True:
        audio_lines: list[str] = []
        if profile.supports_dialogue is True:
            audio_lines.extend(f"Speech: {line}" for line in dialogue_lines)
        for item in clip.audio_intent:
            audio_lines.append(f"Sound: {item}.")
        if clip.audio_prompt and clip.audio_prompt.strip():
            audio_lines.append(f"Audio direction: {clip.audio_prompt.strip()}")
        add_section("AUDIO", audio_lines)

    continuity_lines: list[str] = []
    if carry_in or "previous_clip_end_frame" in clip.continuity_constraints:
        continuity_lines.append(
            "Continue exactly from the provided START FRAME, which is the "
            "final state of the previous clip; match its framing, lighting, "
            "and character positions."
        )
    continuity_lines.extend(
        constraint
        for constraint in clip.continuity_constraints
        if constraint and constraint != "previous_clip_end_frame"
    )
    add_section("CONTINUITY", continuity_lines)

    if produces_next:
        ending_line = (
            "ENDING: design the final frame to be stable and well-composed, "
            "because it will become the start frame of the next clip."
        )
    else:
        ending_line = (
            "ENDING: leave the final frame clean and stable so subtitles or a "
            "fade-out can be applied in post-production."
        )
    add_section("ENDING", [ending_line])

    strict_lines: list[str] = [
        "Identity lock: characters, wardrobe, props, and the environment must "
        "match the descriptions in this prompt exactly; do not introduce any "
        "person, prop, or location that is not described here."
    ]
    if profile.supports_negative_prompt is False:
        strict_lines.extend(
            f"Ensure {positive}."
            for positive in _positive_phrasing(
                _shot_negative_constraints(clip, production_package)
            )
        )
    strict_lines.extend(
        guidance.strip()
        for guidance in production_package.generic_video_guidance
        if guidance and guidance.strip()
    )
    add_section("STRICT", strict_lines)

    postproduction: list[str] = list(compiled.postproduction_audio_plan)
    if dialogue_lines and not (
        profile.supports_audio is True and profile.supports_dialogue is True
    ):
        note = f"Add dialogue in post: {'; '.join(dialogue_lines)}"
        if note not in postproduction:
            postproduction.insert(0, note)
    add_section("POST-PRODUCTION", postproduction)

    if trace_out is not None:
        trace_out.clear()
        trace_out.update(
            {
                "profile_id": profile.id,
                "syntax": syntax,
                "sections_included": sections_included,
                "character_count": len(clip.character_ids),
            }
        )
    return "\n\n".join(sections)


# ----------------------------------------------------------------------
# Package compilation
# ----------------------------------------------------------------------
def compile_prompt_package(
    production_package: ProductionPackage,
    profile: VideoModelProfile,
    clips: list[GenerationClip],
    assets: list[ProductionAsset],
    project_id: str,
    options: dict[str, Any],
    package_id: str | None = None,
) -> ModelPromptPackage:
    """Compile a full :class:`ModelPromptPackage` for one target profile."""
    assets_by_id = {asset.id: asset for asset in assets}
    prompt_language = str(options.get("prompt_language", "auto"))
    compiled_clips = [
        compile_clip_prompt(clip, profile, production_package, assets_by_id, prompt_language)
        for clip in clips
    ]
    # Deterministic prop attribution (guide-layer input): matched prop bible
    # names land on each compiled clip's prop_ids, bible order preserved.
    attributed_clips: list[GenerationClip] = []
    for compiled in compiled_clips:
        prop_ids = _clip_prop_ids(compiled, production_package)
        attributed_clips.append(
            compiled.model_copy(update={"prop_ids": prop_ids}) if prop_ids else compiled
        )
    compiled_clips = attributed_clips

    global_visual_contract: list[str] = []
    for entry in production_package.character_visual_bible:
        if isinstance(entry, dict):
            name = _bible_entry_name(entry)
            text = _bible_entry_text(entry)
            if name or text:
                global_visual_contract.append(f"Character {name}: {text}".strip(": "))
    for entry in production_package.location_visual_bible:
        if isinstance(entry, dict):
            name = _bible_entry_name(entry)
            text = _bible_entry_text(entry)
            if name or text:
                global_visual_contract.append(f"Location {name}: {text}".strip(": "))
    for prop in production_package.prop_visual_bible:
        if isinstance(prop, dict):
            name = _bible_entry_name(prop)
            text = _bible_entry_text(prop)
            if name or text:
                global_visual_contract.append(f"Prop {name}: {text}".strip(": "))

    global_continuity_contract = list(production_package.continuity_notes)
    for compiled in compiled_clips:
        for constraint in compiled.continuity_constraints:
            if constraint not in global_continuity_contract:
                global_continuity_contract.append(constraint)

    asset_requirements: list[dict[str, Any]] = []
    for compiled in compiled_clips:
        if compiled.start_frame_asset_id and compiled.start_frame_asset_id not in assets_by_id:
            asset_requirements.append(
                {
                    "asset_id": compiled.start_frame_asset_id,
                    "clip_number": compiled.clip_number,
                    "purpose": "start_frame",
                    "prepared": False,
                }
            )
        if compiled.end_frame_asset_id and compiled.end_frame_asset_id not in assets_by_id:
            asset_requirements.append(
                {
                    "asset_id": compiled.end_frame_asset_id,
                    "clip_number": compiled.clip_number,
                    "purpose": "end_frame",
                    "prepared": False,
                }
            )
        for asset_id in compiled.reference_asset_ids:
            if asset_id not in assets_by_id:
                asset_requirements.append(
                    {
                        "asset_id": asset_id,
                        "clip_number": compiled.clip_number,
                        "purpose": "reference",
                        "prepared": False,
                    }
                )

    violations: dict[str, list[str]] = {}
    for compiled in compiled_clips:
        codes = validate_clip_against_profile(compiled, profile)
        if codes:
            violations[str(compiled.clip_number)] = codes

    runtime_trace = {
        "compiler": "video_prompt_compiler",
        "profile_digest": profile_capabilities_digest(profile),
        "clip_count": len(compiled_clips),
        "violations": violations,
        "asset_requirement_count": len(asset_requirements),
    }
    return ModelPromptPackage(
        id=package_id or new_id("pkgs"),
        project_id=project_id,
        episode_number=production_package.episode_number,
        production_package_id=production_package.id,
        episode_version_id=production_package.episode_version_id or "",
        target_profile_id=profile.id,
        target_profile_version=profile.profile_version,
        target_video_model_display_name=profile.display_name,
        aspect_ratio=str(options.get("aspect_ratio", "16:9")),
        generation_strategy=str(options.get("generation_strategy", "auto")),
        quality_priority=str(options.get("quality_priority", "balanced")),
        continuity_strategy=str(options.get("continuity_strategy", "auto")),
        audio_strategy=str(options.get("audio_strategy", "auto")),
        prompt_language=prompt_language,
        status="ready",
        clips=compiled_clips,
        global_visual_contract=global_visual_contract,
        global_continuity_contract=global_continuity_contract,
        asset_requirements=asset_requirements,
        runtime_trace=runtime_trace,
    )
