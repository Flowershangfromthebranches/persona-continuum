"""Unit tests for deterministic clip planning (merge/split rules)."""

from __future__ import annotations

from persona_continuum.domain.narrative import Shot
from persona_continuum.narrative.clip_planner import plan_clips
from persona_continuum.narrative.video_profile_registry import get_profile


def _shot(
    number: int,
    *,
    duration: float,
    location: str = "Office",
    dialogue: str = "",
    transition: str = "cut",
) -> Shot:
    return Shot(
        shot_number=number,
        duration_seconds=duration,
        location=location,
        dialogue=dialogue,
        transition=transition,
    )


def _sixteen_shot_fixture() -> list[Shot]:
    """Synthetic 16-shot list exercising every planner rule.

    Shots 1-7: seven 4s same-location no-dialogue shots (mergeable pairs).
    Shots 8-9: two 12s shots (each exceeds every documented max, forcing
    splits). Shots 10/12/16 carry dialogue. Shot 13 has transition "dissolve"
    which forces a clip boundary. Shots 14-15 are a second location
    (Rooftop) pair.
    """
    shots: list[Shot] = []
    for number in range(1, 8):
        shots.append(_shot(number, duration=4.0))
    shots.append(_shot(8, duration=12.0))
    shots.append(_shot(9, duration=12.0))
    shots.append(_shot(10, duration=4.0, dialogue="你来了。"))
    shots.append(_shot(11, duration=4.0))
    shots.append(_shot(12, duration=4.0, dialogue="我知道了。"))
    shots.append(_shot(13, duration=4.0, transition="dissolve"))
    shots.append(_shot(14, duration=4.0, location="Rooftop"))
    shots.append(_shot(15, duration=4.0, location="Rooftop"))
    shots.append(_shot(16, duration=4.0, location="Rooftop", dialogue="再见。"))
    return shots


def test_generic_profile_merges_and_splits() -> None:
    clips = plan_clips(_sixteen_shot_fixture(), get_profile("generic"))
    # Merging + splitting means the clip count differs from the shot count.
    assert len(clips) != 16
    # Merged clips: at least one clip spans >= 2 source shots.
    merged = [clip for clip in clips if len(clip.source_shot_numbers) >= 2]
    assert merged, "expected at least one merged clip"
    assert any(clip.source_shot_numbers == [1, 2] for clip in merged)
    # Splitting: each 12s shot (8 and 9, above the 8s effective max) becomes
    # exactly two clips that share the same source shot.
    for oversized in (8, 9):
        parts = [
            clip for clip in clips if clip.source_shot_numbers == [oversized]
        ]
        assert len(parts) == 2
    # Dialogue shots are never merged across a dialogue boundary.
    dialogue_shot_numbers = {10, 12, 16}
    for clip in clips:
        if dialogue_shot_numbers & set(clip.source_shot_numbers):
            assert len(clip.source_shot_numbers) == 1
    # A non-cut transition always forces a clip boundary: no clip may span
    # shots 13 and 14 (shot 13 ends with a dissolve).
    for clip in clips:
        numbers = clip.source_shot_numbers
        if 13 in numbers:
            assert 14 not in numbers
    # Generic clips never exceed the effective max (prefer 8s, max 10s).
    for clip in clips:
        assert clip.duration_seconds <= 8.0 + 1e-6


def test_generic_vs_runway_produce_different_plans() -> None:
    shots = _sixteen_shot_fixture()
    generic = plan_clips(shots, get_profile("generic"))
    runway = plan_clips(shots, get_profile("runway_gen_4_5"))
    assert len(generic) != len(runway)
    # Runway prefers 5s clips: 4s pairs no longer fit into one clip.
    assert not [
        clip for clip in runway if len(clip.source_shot_numbers) >= 2
    ], "runway (prefer 5s) must not merge the 4s pairs"
    # ... and each 12s shot now splits into ceil(12/5) = 3 parts.
    for oversized in (8, 9):
        parts = [
            clip for clip in runway if clip.source_shot_numbers == [oversized]
        ]
        assert len(parts) == 3
    for clip in runway:
        assert clip.duration_seconds <= 5.0 + 1e-6


def test_each_clip_carries_settings_and_rationale() -> None:
    clips = plan_clips(_sixteen_shot_fixture(), get_profile("generic"))
    assert clips
    for clip in clips:
        # Single duration source of truth (task #29): no target_duration_seconds.
        assert clip.recommended_settings["actual_generation_duration"] == (
            clip.duration_seconds
        )
        assert "target_duration_seconds" not in clip.recommended_settings
        assert clip.recommended_settings["aspect_ratio"] == clip.aspect_ratio
        assert clip.planning_rationale
        assert clip.target_profile_id == "generic"
        assert clip.generation_mode in {"text_to_video", "image_to_video"}


def test_plan_clips_propagates_head_shot_location() -> None:
    """F2: every clip carries its head shot's location so the T2V compiler
    can anchor the Environment to the right bible entry."""
    clips = plan_clips(_sixteen_shot_fixture(), get_profile("generic"))
    for clip in clips:
        numbers = set(clip.source_shot_numbers)
        if numbers & {14, 15, 16}:
            assert clip.location == "Rooftop"
        else:
            assert clip.location == "Office"
    # A location change always forces a clip boundary: no clip may span
    # shots 13 (Office) and 14 (Rooftop).
    for clip in clips:
        numbers = set(clip.source_shot_numbers)
        if 13 in numbers:
            assert 14 not in numbers


def test_split_duration_snaps_upward_no_screen_time_drop() -> None:
    """F7: split parts snap UP to the smallest documented duration that still
    covers the ideal part length — a nearest-downward snap would silently
    drop the shot's remaining screen time (10s → 2×4s = 8s on veo)."""
    veo = get_profile("veo_3_1")
    clips = plan_clips([_shot(1, duration=10.0)], veo)
    assert len(clips) == 2
    total = sum(clip.duration_seconds for clip in clips)
    assert total >= 10.0
    for clip in clips:
        assert clip.duration_seconds in {4.0, 6.0, 8.0}
        assert clip.duration_seconds <= 8.0 + 1e-6


def test_merge_snap_only_uses_supported_within_envelope() -> None:
    """F7: supported durations above the effective max are filtered out
    before snapping — an over-envelope snap would violate the model's
    duration cap; with no eligible value left, the raw combined duration
    is kept (continuous envelope fallback)."""
    from persona_continuum.domain.narrative import VideoModelProfile

    profile = VideoModelProfile(
        id="snap_filter_test",
        display_name="Snap Filter Test",
        vendor="test",
        supported_modes=["text_to_video"],
        supported_durations_seconds=[12.0],
        duration_min_seconds=2.0,
        duration_max_seconds=10.0,
        clip_planning={"prefer_duration_seconds": 8},
    )
    clips = plan_clips([_shot(1, duration=4.0), _shot(2, duration=4.0)], profile)
    assert len(clips) == 1
    # 8.0s combined: 12.0 (above the 8s effective max) is not a snap target,
    # so no documented duration is eligible and the raw 8.0s is kept.
    assert clips[0].duration_seconds == 8.0
