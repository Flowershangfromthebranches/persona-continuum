"""Unit tests for the profile-aware video prompt compiler."""

from __future__ import annotations

from typing import Any

import pytest

from persona_continuum.domain.narrative import (
    GenerationClip,
    ProductionAsset,
    ProductionPackage,
    Shot,
)
from persona_continuum.narrative.runtime import (
    SHOOTING_LOCATION_CONTEXT_MISSING,
    NarrativeAgentError,
)
from persona_continuum.narrative.video_profile_registry import get_profile
from persona_continuum.narrative.video_prompt_compiler import (
    compile_clip_prompt,
    compile_copy_ready_prompt,
    validate_clip_against_profile,
)


def _production_package(**overrides: Any) -> ProductionPackage:
    base: dict[str, Any] = {
        "project_id": "proj_1",
        "episode_number": 1,
        "shot_list": [Shot(shot_number=1, duration_seconds=6.0)],
    }
    base.update(overrides)
    return ProductionPackage(**base)


def _clip(**overrides: Any) -> GenerationClip:
    base: dict[str, Any] = {
        "clip_number": 1,
        "source_shot_numbers": [1],
        "duration_seconds": 6.0,
        "generation_mode": "text_to_video",
        "visual_intent": "一名年轻职员站在工位前",
        "camera_intent": "缓慢推近",
        "subject_motion": "她转身走向门口",
        "environment_motion": "窗外雨滴滑落",
    }
    base.update(overrides)
    return GenerationClip(**base)


def _asset(asset_id: str = "past_ref1") -> ProductionAsset:
    return ProductionAsset(
        id=asset_id,
        project_id="proj_1",
        asset_type="character_reference",
        name="参考图",
    )


def test_i2v_prompt_is_motion_first_not_bible_dump() -> None:
    long_bible_text = (
        "方宁穿着一件洗得发白的蓝色衬衫，袖口磨出了细细的毛边，头发有些凌乱，"
        "眼下挂着淡淡的青黑，手指因为常年敲击键盘而略显粗糙，整个人透着一股"
        "被生活反复捶打后的疲惫。"
    )
    package = _production_package(
        character_visual_bible=[
            {
                "character_id": "fang_ning",
                "name": "方宁",
                "visual_description": long_bible_text,
            }
        ]
    )
    clip = _clip(
        generation_mode="image_to_video",
        reference_asset_ids=["past_ref1"],
        dialogue=[{"speaker": "fang_ning", "line": "你来了。"}],
    )
    compiled = compile_clip_prompt(
        clip, get_profile("veo_3_1"), package, {"past_ref1": _asset()}
    )
    # Motion-first: subject motion and camera movement are present ...
    assert "Motion:" in compiled.prompt
    assert "她转身走向门口" in compiled.prompt
    assert "Camera:" in compiled.prompt
    assert "缓慢推近" in compiled.prompt
    # ... but the full character bible text must never be restated: the
    # reference asset carries the identity.
    assert long_bible_text not in compiled.prompt
    assert "Subject anchor" not in compiled.prompt


def test_runway_negative_prompt_becomes_positive_phrasing() -> None:
    package = _production_package(
        shot_list=[
            Shot(
                shot_number=1,
                duration_seconds=6.0,
                negative_constraints=["no camera movement"],
            )
        ]
    )
    clip = _clip(generation_mode="text_to_video")
    compiled = compile_clip_prompt(clip, get_profile("runway_gen_4_5"), package, {})
    # Runway documents supports_negative_prompt=False: no separate field ...
    assert compiled.negative_prompt is None
    # ... the constraint is deterministically rewritten into positive phrasing.
    assert "locked camera" in compiled.prompt
    assert "no camera movement" not in compiled.prompt


def test_t2v_prompt_carries_all_static_sections() -> None:
    package = _production_package(
        location_visual_bible=[
            {
                "location_id": "office",
                "name": "办公室",
                "visual_description": "昏暗的开放式办公区",
            }
        ]
    )
    # The clip's own location selects the bible entry for the Environment
    # anchor (F2): planner-populated clips always carry their head shot's
    # location, which here matches the single bible entry.
    clip = _clip(generation_mode="text_to_video", location="office")
    compiled = compile_clip_prompt(clip, get_profile("generic"), package, {})
    prompt = compiled.prompt
    assert "Subject:" in prompt
    assert "一名年轻职员站在工位前" in prompt
    assert "Environment:" in prompt
    assert "昏暗的开放式办公区" in prompt
    assert "Composition and lighting" in prompt
    assert "Camera:" in prompt
    assert "Subject motion:" in prompt
    assert "Environment motion:" in prompt


def test_t2v_environment_anchor_uses_clips_own_location() -> None:
    """F2: two-scene fixture — each clip is anchored to ITS OWN location's
    bible text, never the first bible entry."""
    package = _production_package(
        location_visual_bible=[
            {
                "location_id": "bar",
                "name": "酒吧",
                "visual_description": "霓虹闪烁的深夜酒吧卡座",
            },
            {
                "location_id": "rooftop",
                "name": "天台",
                "visual_description": "城市灯火之上的开阔天台",
            },
        ]
    )
    bar_clip = _clip(generation_mode="text_to_video", location="bar")
    rooftop_clip = _clip(
        generation_mode="text_to_video",
        clip_number=2,
        source_shot_numbers=[2],
        location="rooftop",
    )
    bar_prompt = compile_clip_prompt(
        bar_clip, get_profile("generic"), package, {}
    ).prompt
    rooftop_prompt = compile_clip_prompt(
        rooftop_clip, get_profile("generic"), package, {}
    ).prompt
    assert "霓虹闪烁的深夜酒吧卡座" in bar_prompt
    assert "城市灯火之上的开阔天台" not in bar_prompt
    assert "城市灯火之上的开阔天台" in rooftop_prompt
    assert "霓虹闪烁的深夜酒吧卡座" not in rooftop_prompt


def test_t2v_unmatched_or_empty_location_gets_generic_anchor() -> None:
    """F2: an empty or unmatched location must never inherit the first bible
    entry — it falls back to a generalized phrase instead."""
    package = _production_package(
        location_visual_bible=[
            {
                "location_id": "bar",
                "name": "酒吧",
                "visual_description": "霓虹闪烁的深夜酒吧卡座",
            }
        ]
    )
    for location in ("", "rooftop"):
        clip = _clip(generation_mode="text_to_video", location=location)
        prompt = compile_clip_prompt(clip, get_profile("generic"), package, {}).prompt
        assert "the established location" in prompt
        assert "霓虹闪烁的深夜酒吧卡座" not in prompt


def test_audio_routing_follows_profile_support() -> None:
    package = _production_package()
    clip = _clip(audio_intent=["雨声渐强"])
    # generic does not document supports_audio (UNKNOWN): audio goes to post.
    generic_compiled = compile_clip_prompt(clip, get_profile("generic"), package, {})
    assert generic_compiled.audio_prompt is None
    assert generic_compiled.postproduction_audio_plan
    assert any("雨声渐强" in item for item in generic_compiled.postproduction_audio_plan)
    # veo documents supports_audio=True: an inline audio prompt is produced.
    veo_compiled = compile_clip_prompt(clip, get_profile("veo_3_1"), package, {})
    assert veo_compiled.audio_prompt is not None
    assert "Audio:" in veo_compiled.audio_prompt
    assert "雨声渐强" in veo_compiled.audio_prompt
    assert veo_compiled.postproduction_audio_plan == []


def test_validate_clip_against_profile_violation_codes() -> None:
    veo = get_profile("veo_3_1")
    runway = get_profile("runway_gen_4_5")
    duration = validate_clip_against_profile(_clip(duration_seconds=15.0), veo)
    assert "VIDEO_PROFILE_DURATION_UNSUPPORTED" in duration
    mode = validate_clip_against_profile(_clip(generation_mode="first_last_frame"), runway)
    assert "VIDEO_PROFILE_MODE_UNSUPPORTED" in mode
    aspect = validate_clip_against_profile(_clip(aspect_ratio="4:3"), veo)
    assert "VIDEO_PROFILE_ASPECT_RATIO_UNSUPPORTED" in aspect
    # A compliant clip yields no violations.
    assert validate_clip_against_profile(_clip(duration_seconds=8.0), veo) == []


def test_compiler_trace_records_profile_and_mode() -> None:
    package = _production_package()
    compiled = compile_clip_prompt(_clip(), get_profile("generic"), package, {})
    trace = compiled.compiler_trace
    assert trace["profile_id"] == "generic"
    assert trace["profile_version"] == get_profile("generic").profile_version
    assert trace["generation_mode"] == "text_to_video"


# ----------------------------------------------------------------------
# Copy-ready guide compiler (video production guide layer)
# ----------------------------------------------------------------------
def test_copy_ready_prompt_is_self_contained_with_all_sections() -> None:
    package = _production_package(
        character_visual_bible=[
            {
                "character_id": "fang_ning",
                "name": "方宁",
                "visual_description": "洗得发白的蓝色衬衫",
            }
        ],
        location_visual_bible=[
            {
                "location_id": "office",
                "name": "办公室",
                "visual_description": "昏暗的开放式办公区",
            }
        ],
    )
    clip = _clip(
        location="office",
        character_ids=["fang_ning"],
        dialogue=[{"speaker": "方宁", "line": "你来了。"}],
        continuity_constraints=["previous_clip_end_frame"],
    )
    trace: dict[str, Any] = {}
    prompt = compile_copy_ready_prompt(
        clip,
        get_profile("generic"),
        package,
        {},
        {"carry_in_start_frame": None, "produces_next_start_frame": False},
        trace,
    )
    for marker in (
        "GOAL",
        "REFERENCE INPUTS",
        "CHARACTER IDENTITY",
        "ENVIRONMENT IDENTITY",
        "ACTION AND PERFORMANCE",
        "CAMERA",
        "TEMPORAL PROGRESSION",
        "ENVIRONMENT MOTION",
        "CONTINUITY",
        "ENDING",
        "STRICT",
        "POST-PRODUCTION",
    ):
        assert marker in prompt, marker
    # Self-contained: identity text is embedded from the bibles verbatim.
    assert "洗得发白的蓝色衬衫" in prompt
    assert "昏暗的开放式办公区" in prompt
    assert "你来了" in prompt
    # generic does not document supports_audio: no inline AUDIO section.
    assert "AUDIO" not in prompt
    assert trace["profile_id"] == "generic"
    assert trace["syntax"] == "textual_anchor"
    assert "GOAL" in trace["sections_included"]


def test_copy_ready_prompt_textual_anchor_start_frame_phrasing() -> None:
    """textual_anchor syntax never claims named-reference attachment: a
    carried-in frame becomes a plain-text instruction to use the provided
    START FRAME image."""
    package = _production_package(
        location_visual_bible=[
            {
                "location_id": "office",
                "name": "办公室",
                "visual_description": "昏暗的开放式办公区",
            }
        ]
    )
    clip = _clip(location="office")
    prompt = compile_copy_ready_prompt(
        clip,
        get_profile("generic"),
        package,
        {},
        {"carry_in_start_frame": "FRAME_01", "produces_next_start_frame": True},
        {},
    )
    assert (
        "START FRAME: use the provided START FRAME image as the first "
        "frame of this clip." in prompt
    )
    # And the no-frame variant instructs that the description is the anchor.
    plain = compile_copy_ready_prompt(
        clip, get_profile("generic"), package, {}, {}, {}
    )
    assert (
        "REFERENCE INPUTS: none for this clip; the written description "
        "below is the only visual anchor." in plain
    )


def test_copy_ready_prompt_fails_closed_on_missing_location() -> None:
    """Unlike the legacy compiler's generic anchor fallback, the copy-ready
    guide fails closed when a clip location resolves to no bible entry."""
    package = _production_package()
    clip = _clip(location="neverland")
    with pytest.raises(NarrativeAgentError) as excinfo:
        compile_copy_ready_prompt(
            clip, get_profile("generic"), package, {}, {}, {}
        )
    assert excinfo.value.code == SHOOTING_LOCATION_CONTEXT_MISSING
    assert excinfo.value.stage == "video_production_guide"
