from __future__ import annotations

from persona_continuum.domain.narrative import (
    Beat,
    EpisodeVersion,
    NarrativeFormat,
    NarrativeScene,
    StoryBible,
    StoryBibleCharacter,
    StoryBibleLocation,
)
from persona_continuum.narrative.production import ProductionPackageBuilder
from persona_continuum.narrative.screenwriter import ScreenwriterStage


def _bible() -> StoryBible:
    return StoryBible(
        project_id="p1",
        tone=["悬疑", "克制"],
        characters=[
            StoryBibleCharacter(
                id="c1",
                name="方宁",
                visual={
                    "age": "28",
                    "gender": "female",
                    "hair": "黑色齐肩发",
                    "costume": "灰色西装",
                    "color_palette": ["灰", "蓝"],
                },
            )
        ],
        locations=[
            StoryBibleLocation(
                id="loc1",
                name="办公室会议室",
                visual={"style": "冷色调现代办公", "props": ["旧照片"]},
            )
        ],
    )


def test_production_package_structure() -> None:
    version = EpisodeVersion(
        project_id="p1",
        episode_number=1,
        screenplay="EP01\n方宁走进办公室会议室，桌上的旧照片还在原处。",
        beat_sheet=[
            Beat(order=1, title="Hook", description="开场", start_seconds=0, end_seconds=10),
            Beat(order=2, title="Reversal", description="反转", start_seconds=10, end_seconds=30),
        ],
    )
    package = ProductionPackageBuilder().build(
        "p1", 1, version, _bible(), [], fmt=NarrativeFormat.MICRO_DRAMA
    )
    assert len(package.shot_list) == 2
    assert package.shot_list[0].start_time == "00:00"
    assert all(s.visual_prompt for s in package.shot_list)
    assert package.character_visual_bible[0]["hair"] == "黑色齐肩发"
    assert package.location_visual_bible[0]["style"] == "冷色调现代办公"
    assert "旧照片" in package.prop_list
    assert package.bgm_direction
    assert package.continuity_notes
    assert package.video_generation_prompts


def test_shot_list_from_scenes() -> None:
    scene = NarrativeScene(
        project_id="p1",
        episode_number=1,
        order=1,
        location="办公室会议室",
        participants=[{"character_id": "c1", "name": "方宁", "goal": "找到线索"}],
        dialogue=[{"speaker": "方宁", "text": "这张照片是什么时候拍的？"}],
    )
    version = EpisodeVersion(
        project_id="p1",
        episode_number=1,
        screenplay="EP01",
        beat_sheet=[
            Beat(order=1, title="Hook", description="开场", start_seconds=0, end_seconds=15)
        ],
    )
    package = ProductionPackageBuilder().build("p1", 1, version, _bible(), [scene])
    assert package.shot_list[0].characters == ["方宁"]
    assert "方宁" in package.dialogue_track[0]["character"] or package.dialogue_track
    assert package.subtitle_track


def test_screenwriter_beat_timing_adapts_to_duration() -> None:
    plan_short = _plan(60)
    beats_short = ScreenwriterStage().beat_sheet_for(plan_short, 60, NarrativeFormat.MICRO_DRAMA)
    beats_long = ScreenwriterStage().beat_sheet_for(_plan(1800), 1800, NarrativeFormat.SERIES)
    assert beats_short[-1].end_seconds == 60
    assert beats_long[-1].end_seconds == 1800
    # Micro drama hook window is compressed.
    assert beats_short[0].end_seconds <= 15


def _plan(duration: int):
    from persona_continuum.domain.narrative import EpisodePlan

    return EpisodePlan(
        project_id="p1", episode_number=1, estimated_duration_seconds=duration
    )
