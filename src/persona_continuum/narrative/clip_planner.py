"""Deterministic clip planning from a production package shot list.

Pure functions, zero I/O: given the shot list and a :class:`VideoModelProfile`,
produce :class:`GenerationClip` entries that respect the model's duration /
capability envelope. Merging and splitting are deterministic so the same
inputs always yield the same clips.

Planning rules
--------------
* Merge: consecutive shots with the same location, no dialogue on either side
  of the boundary, a ``cut`` (or empty) transition, and combined duration
  within the effective max become ONE clip (``source_shot_numbers`` spans
  shots). There is no 1-shot = 1-clip assumption.
* Split: a single shot longer than the effective max is split into ceil
  parts; each part's duration is snapped upward (parts * snapped >= the
  original duration) so no screen time is silently dropped.
* Dialogue shots never merge across a dialogue boundary; a non-cut
  transition always forces a clip boundary.
"""

from __future__ import annotations

import math
from typing import Any

from persona_continuum.domain.narrative import GenerationClip, Shot, VideoModelProfile

_EPS = 1e-6

_CUT_TRANSITIONS = {"cut", ""}

_NEAR_SHOT_SIZES = {"close", "medium"}


def _clip_planning_value(profile: VideoModelProfile, key: str, default: Any) -> Any:
    value = profile.clip_planning.get(key, default)
    return default if value is None else value


def _effective_durations(
    profile: VideoModelProfile, quality_priority: str
) -> tuple[float, float, float]:
    """Return (target, effective_max, snap_floor) clip durations in seconds.

    ``target`` is the preferred clip length; ``effective_max`` is the hard
    upper bound for a merged clip. Both respect the profile's absolute
    duration envelope when one is documented.
    """
    prefer = float(_clip_planning_value(profile, "prefer_duration_seconds", 8.0))
    hard_max = (
        float(profile.duration_max_seconds)
        if profile.duration_max_seconds is not None
        else prefer
    )
    hard_min = (
        float(profile.duration_min_seconds)
        if profile.duration_min_seconds is not None
        else 0.0
    )
    effective_max = min(max(prefer, hard_min), hard_max)
    if quality_priority == "quality":
        target = effective_max
    elif quality_priority == "fast":
        target = max(min(prefer / 2.0, effective_max), hard_min)
    else:  # balanced and unknown priorities keep the documented preference.
        target = min(prefer, effective_max)
    return target, effective_max, hard_min


def _snap_duration(seconds: float, supported: list[float] | None) -> float:
    if not supported:
        return round(seconds, 2)
    nearest = float(min(supported, key=lambda value: abs(float(value) - seconds)))
    return round(nearest, 2)


def _snap_split_part_duration(
    original: float, parts: int, supported: list[float] | None
) -> float:
    """Snap a split part duration upward: ``parts * snapped >= original``.

    A downward snap would silently drop the shot's remaining screen time,
    so the smallest documented duration that still covers the ideal part
    length wins; the part count grows when none does. Without a documented
    duration list the value is ceiled to centiseconds (continuous envelope).
    """
    if not supported:
        return math.ceil((original / parts) * 100.0) / 100.0
    values = [float(value) for value in supported]
    ideal = original / parts
    covering = [value for value in values if value + _EPS >= ideal]
    while not covering:
        parts += 1
        ideal = original / parts
        covering = [value for value in values if value + _EPS >= ideal]
    return round(min(covering), 2)


def _resolve_generation_mode(shot: Shot, profile: VideoModelProfile) -> str:
    """Characters in a close/medium framing prefer image_to_video when the
    model documents support; everything else falls back to text_to_video."""
    if (
        shot.characters
        and shot.shot_size in _NEAR_SHOT_SIZES
        and profile.supports_image_to_video is True
    ):
        return "image_to_video"
    return "text_to_video"


def _clip_dialogue(shot: Shot) -> list[dict[str, Any]]:
    if not shot.dialogue:
        return []
    speaker = shot.characters[0] if shot.characters else ""
    return [{"speaker": speaker, "line": shot.dialogue}]


def _purpose(shot: Shot) -> str:
    return shot.action.strip() or shot.visual_intent.strip() or f"Shot {shot.shot_number}"


def _mergeable(
    prev: Shot, nxt: Shot, combined: float, effective_max: float, count: int, max_shots: int
) -> bool:
    if count >= max_shots:
        return False
    if prev.location != nxt.location:
        return False
    # A dialogue line must never be cut by a clip boundary.
    if prev.dialogue or nxt.dialogue:
        return False
    if prev.transition not in _CUT_TRANSITIONS:
        return False
    return combined + nxt.duration_seconds <= effective_max + _EPS


def plan_clips(
    shots: list[Shot],
    profile: VideoModelProfile,
    aspect_ratio: str = "16:9",
    quality_priority: str = "balanced",
) -> list[GenerationClip]:
    """Plan generation clips from the ordered shot list (deterministic)."""
    target, effective_max, hard_min = _effective_durations(profile, quality_priority)
    # Default 2: profiles that do not declare max_major_actions still merge
    # adjacent mergeable shots ("no 1-shot = 1-clip assumption"), instead of
    # silently degrading to one clip per shot.
    max_shots = max(1, int(_clip_planning_value(profile, "max_major_actions", 2)))
    supported = profile.supported_durations_seconds
    # Snapping must never push a clip past the planning envelope: only
    # documented durations the envelope can actually hold are eligible.
    snappable = [
        float(value)
        for value in (supported or [])
        if float(value) <= effective_max + _EPS
    ] or None

    # Pass 1: build raw clips (merge and split), keeping shot references.
    raw: list[tuple[list[Shot], str, float]] = []  # (shots, rationale, duration)
    index = 0
    while index < len(shots):
        shot = shots[index]
        if shot.duration_seconds > effective_max + _EPS:
            # Split oversized shots into ceil parts snapped to the envelope.
            # Upward snap: parts * snapped >= original keeps the shot's full
            # screen time (a downward snap would drop seconds).
            parts = max(2, math.ceil(shot.duration_seconds / effective_max - _EPS))
            part_duration = _snap_split_part_duration(
                shot.duration_seconds, parts, snappable
            )
            if part_duration < hard_min:
                part_duration = hard_min
            # The split must materialize every part as its own clip sharing
            # the source shot; emitting a single shortened clip would drop
            # the rest of the shot's screen time.
            for part_index in range(1, parts + 1):
                rationale = (
                    f"Split shot {shot.shot_number} ({shot.duration_seconds:.1f}s) "
                    f"into {parts} parts of ~{part_duration:.1f}s to fit the model "
                    f"max of {effective_max:.1f}s (part {part_index}/{parts})."
                )
                raw.append(([shot], rationale, part_duration))
            index += 1
            continue
        group = [shot]
        combined = shot.duration_seconds
        rationale = f"Shot {shot.shot_number} kept standalone."
        while index + 1 < len(shots) and _mergeable(
            group[-1], shots[index + 1], combined, effective_max, len(group), max_shots
        ):
            nxt = shots[index + 1]
            group.append(nxt)
            combined += nxt.duration_seconds
            rationale = (
                f"Merged shots {[s.shot_number for s in group]} into one clip "
                f"(~{combined:.1f}s <= {effective_max:.1f}s max; same location "
                f"'{group[0].location}', cut transitions, no dialogue boundary)."
            )
            index += 1
        raw.append((group, rationale, _snap_duration(combined, snappable)))
        index += 1

    # Pass 2: materialize GenerationClip entries with continuity hints.
    clips: list[GenerationClip] = []
    for number, (group, rationale, duration) in enumerate(raw, start=1):
        head = group[0]
        visual_intent = head.visual_intent or head.visual_prompt
        camera_intent = head.camera_intent or head.camera
        subject_motion = head.subject_motion_intent or head.movement
        environment_motion = head.environment_motion_intent
        # Character attribution across merged/split shots: deduplicated,
        # preserving first-seen order. prop_ids stay empty here (the planner
        # has no prop context; prop attribution happens in the compiler).
        character_ids = list(
            dict.fromkeys(
                character_id
                for shot in group
                for character_id in shot.characters
                if character_id
            )
        )
        dialogue: list[dict[str, Any]] = []
        audio_intent: list[str] = []
        continuity_constraints: list[str] = []
        for shot in group:
            dialogue.extend(_clip_dialogue(shot))
            audio_intent.extend(shot.audio_intent)
            audio_intent.extend(shot.sfx)
            continuity_constraints.extend(shot.continuity_constraints)
        generation_mode = _resolve_generation_mode(head, profile)
        # Consecutive same-scene shots chain via the previous clip end frame
        # when the model documents last-frame support. raw[number - 2] is the
        # previous clip (number starts at 1).
        if (
            number > 1
            and head.location == raw[number - 2][0][0].location
            and profile.supports_last_frame is not False
        ):
            continuity_constraints.append("previous_clip_end_frame")
        clips.append(
            GenerationClip(
                clip_number=number,
                source_shot_numbers=[shot.shot_number for shot in group],
                character_ids=character_ids,
                scene_number=None,
                purpose=_purpose(head),
                duration_seconds=duration,
                # Merging only happens within one location, so the head
                # shot's location is the whole clip's location.
                location=head.location,
                generation_mode=generation_mode,
                target_profile_id=profile.id,
                aspect_ratio=aspect_ratio,
                visual_intent=visual_intent,
                camera_intent=camera_intent,
                subject_motion=subject_motion,
                environment_motion=environment_motion,
                dialogue=dialogue,
                audio_intent=audio_intent,
                continuity_constraints=continuity_constraints,
                planning_rationale=rationale,
                recommended_settings={
                    # Single duration source of truth (task #29): the value
                    # this clip is actually generated with. The old
                    # target_duration_seconds key is intentionally gone — it
                    # contradicted duration_seconds in the UI.
                    "actual_generation_duration": duration,
                    "aspect_ratio": aspect_ratio,
                    "quality_priority": quality_priority,
                },
            )
        )
    return clips
