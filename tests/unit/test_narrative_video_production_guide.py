"""Pure module tests for the executable video production guide layer.

No database, no HTTP, no LLM: everything is built from synthetic inline
fixtures (a ProductionPackage + a ModelPromptPackage + the generic profile)
and driven through the deterministic guide compiler in
``persona_continuum.narrative.video_production_guide``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from persona_continuum.domain.narrative import (
    GenerationClip,
    ModelPromptPackage,
    ProductionPackage,
    Shot,
    VideoModelProfile,
)
from persona_continuum.narrative.runtime import (
    SHOOTING_LOCATION_CONTEXT_MISSING,
    NarrativeAgentError,
)
from persona_continuum.narrative.video_production_guide import (
    PLACEHOLDER_PATTERNS,
    analyze_asset_necessity,
    apply_asset_prompt_enrichment,
    apply_clip_prompt_enrichment,
    build_executable_video_production_guide,
    build_frame_chain,
    build_reference_asset_prompts,
    render_guide_markdown,
)
from persona_continuum.narrative.video_profile_registry import get_profile

# Long, distinctive bible texts: the guide prompts must embed the full
# identity blocks, so every fixture text carries a unique fragment that the
# assertions can look for.
CHAR_TEXTS = (
    "黑色高领毛衣的袖口略有磨损，左眉上方有一道浅浅的旧疤，说话时习惯压低声音，"
    "肩背微微前倾，像随时准备从工位上站起来",
    "总是穿一件洗旧的牛仔外套，笑起来眼角有细纹，右手腕缠着一圈黑色橡皮筋，"
    "走路时习惯把双手插在口袋里",
)
LOC_TEXTS = (
    "开放式办公区的第 7 排工位，头顶白光灯管有一支总是闪烁，桌面堆着未归档的"
    "文件，隔板上贴着褪色的便利贴",
    "顶楼天台的东南角，铁丝网围栏上挂着褪色的红色横幅，地面有干涸的水洼，"
    "远处是灰蒙蒙的城市天际线",
)
PROP_TEXT = "黄铜表壳已经氧化发暗，表盖内侧刻着一朵极小的四叶草，表链缺少一节，指针停在 11:47"

_COPY_READY_MARKERS = (
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
)

_SECTION_TITLES = (
    "一、制作目标与基础设置",
    "二、先建立永久角色参考素材",
    "三、本集需要建立的场景参考素材",
    "四、关键道具 / UI / 屏幕与开镜画面素材",
    "五、整集视频结构",
    "六、视频 1",
    "七、视频 2",
    "八、视频 3",
    "九、尾帧接首帧完整流程",
    "十、字幕时间轴",
    "十一、手机 / 邮件 / UI 后期合成方案",
    "十二、对白与环境音",
    "十三、SFX",
    "十四、BGM 完整生成 Prompt",
    "十五、剪辑顺序与转场",
    "十六、最终检查清单",
)


def _build_fixtures(
    *,
    char_ids: tuple[str, str] = ("lin_wan", "a_kai"),
    char_names: tuple[str, str] = ("林晚", "阿凯"),
    loc_ids: tuple[str, str] = ("office", "rooftop"),
    loc_names: tuple[str, str] = ("办公室", "天台"),
    prop_name: str = "青铜怀表",
) -> tuple[ProductionPackage, ModelPromptPackage, VideoModelProfile]:
    """Synthetic episode: 2 characters, 2 locations, 1 prop, 3 shots, and a
    3-clip chained prompt package (character/prop/location attribution filled,
    continuity chained via previous_clip_end_frame)."""
    profile = get_profile("generic")
    shots = [
        Shot(
            shot_number=1,
            duration_seconds=5.0,
            shot_size="close-up",
            camera="eye-level",
            movement="slow push",
            characters=[char_names[0]],
            action=f"{char_names[0]}盯着手里的手机，屏幕亮起一封新邮件",
            dialogue="这封邮件不可能存在",
            location=loc_ids[0],
            visual_intent=f"{char_names[0]}的侧脸被手机屏幕照亮",
            sfx=["手机提示音"],
        ),
        Shot(
            shot_number=2,
            duration_seconds=6.0,
            shot_size="wide",
            camera="waist-level",
            movement="tracking",
            characters=[char_names[0], char_names[1]],
            action=f"{char_names[1]}从门口走进，手里握着{prop_name}",
            dialogue="把它交给我",
            location=loc_ids[1],
            visual_intent=f"{char_names[1]}穿过门框走向镜头",
            sfx=["脚步声"],
        ),
        Shot(
            shot_number=3,
            duration_seconds=8.0,
            shot_size="wide",
            camera="high-angle",
            movement="slow pull",
            characters=[char_names[1]],
            action=f"{char_names[1]}在{loc_names[1]}的围栏边停下，呼出一口气",
            dialogue="就这么定了",
            location=loc_ids[1],
            visual_intent=f"{char_names[1]}独自站在{loc_names[1]}的围栏边",
            sfx=["远处城市低鸣"],
        ),
    ]
    production = ProductionPackage(
        project_id="proj_guide",
        episode_number=1,
        episode_version_id="ver_canon",
        shot_list=shots,
        character_visual_bible=[
            {
                "character_id": char_ids[0],
                "name": char_names[0],
                "visual_description": CHAR_TEXTS[0],
                "color_palette": ["黑色", "冷灰"],
                "forbidden_variations": ["短发", "亮色服装"],
            },
            {
                "character_id": char_ids[1],
                "name": char_names[1],
                "visual_description": CHAR_TEXTS[1],
                "color_palette": ["牛仔蓝"],
                "forbidden_variations": ["正装"],
            },
        ],
        location_visual_bible=[
            {
                "location_id": loc_ids[0],
                "name": loc_names[0],
                "visual_description": LOC_TEXTS[0],
                "color_palette": ["冷白", "青灰"],
            },
            {
                "location_id": loc_ids[1],
                "name": loc_names[1],
                "visual_description": LOC_TEXTS[1],
                "color_palette": ["暗红", "灰蓝"],
            },
        ],
        prop_visual_bible=[
            {
                "name": prop_name,
                "visual_description": PROP_TEXT,
                "color_palette": ["黄铜色"],
            }
        ],
        prop_list=[prop_name],
        subtitle_track=[
            {"start_time": 0.0, "end_time": 5.0, "text": "这封邮件不可能存在"},
            {"start_time": 5.0, "end_time": 11.0, "text": "把怀表交给我"},
        ],
        sound_effect_plan=[
            {"clip_number": 1, "effect": "手机提示音与雨声"},
            {"clip_number": 3, "effect": "远处城市低鸣"},
        ],
        bgm_direction="低沉克制，弦乐与钢琴交织，随剧情逐渐升温",
        continuity_notes=["全集保持冷色调青灰光影", "手机屏幕始终是画面里的强光源之一"],
        generic_video_guidance=["Keep faces stable and identity consistent across every clip."],
    )
    clips = [
        GenerationClip(
            id="clip_guide_1",
            clip_number=1,
            source_shot_numbers=[1],
            duration_seconds=5.0,
            location=loc_ids[0],
            character_ids=[char_ids[0]],
            prop_ids=[prop_name],
            visual_intent=f"{char_names[0]}在工位前盯着手中的手机屏幕",
            camera_intent="缓慢推近至特写",
            subject_motion=f"{char_names[0]}的手指悬停在屏幕上方，微微颤抖",
            environment_motion="窗外雨滴沿玻璃滑落",
            dialogue=[{"speaker": char_names[0], "line": "你看到手机上的邮件了吗？"}],
            audio_intent=["雨声渐强"],
            continuity_constraints=[
                "手机屏幕上的时间显示为 23:47",
            ],
            purpose="开场悬念：神秘邮件抵达",
        ),
        GenerationClip(
            id="clip_guide_2",
            clip_number=2,
            source_shot_numbers=[2],
            duration_seconds=6.0,
            location=loc_ids[1],
            character_ids=[char_ids[0], char_ids[1]],
            prop_ids=[prop_name],
            visual_intent=f"{char_names[0]}与{char_names[1]}在门口相遇",
            camera_intent="跟随摇镜",
            subject_motion=f"{char_names[1]}递出手中的{prop_name}",
            environment_motion="走廊尽头的灯忽明忽暗",
            dialogue=[{"speaker": char_names[1], "line": "把它交给我"}],
            audio_intent=["脚步声接近"],
            continuity_constraints=[],
            purpose="相遇与信物交接",
        ),
        GenerationClip(
            id="clip_guide_3",
            clip_number=3,
            source_shot_numbers=[3],
            duration_seconds=8.0,
            location=loc_ids[1],
            character_ids=[char_ids[1]],
            prop_ids=[],
            visual_intent=f"{char_names[1]}独自站在{loc_names[1]}的围栏边",
            camera_intent="缓慢拉远至全景",
            subject_motion=f"{char_names[1]}呼出一口气，肩膀放松下来",
            environment_motion="云层在天窗外缓缓移动",
            dialogue=[],
            audio_intent=["远处城市低鸣"],
            continuity_constraints=["previous_clip_end_frame"],
            purpose="收束：决意与释然",
        ),
    ]
    prompt_package = ModelPromptPackage(
        project_id="proj_guide",
        episode_number=1,
        production_package_id=production.id,
        episode_version_id="ver_canon",
        target_profile_id=profile.id,
        target_profile_version=profile.profile_version,
        target_video_model_display_name=profile.display_name,
        status="ready",
        clips=clips,
    )
    return production, prompt_package, profile


def _build_guide(
    production: ProductionPackage,
    prompt_package: ModelPromptPackage,
    profile: VideoModelProfile,
    options: dict[str, Any] | None = None,
) -> Any:
    return build_executable_video_production_guide(
        production, prompt_package, profile, [], "proj_guide", options
    )


def _assert_no_placeholders(text: str) -> None:
    folded = text.casefold()
    for pattern in PLACEHOLDER_PATTERNS:
        assert pattern.casefold() not in folded, pattern


# ----------------------------------------------------------------------
# analyze_asset_necessity
# ----------------------------------------------------------------------
def test_analyze_classifies_assets_from_structure_not_names() -> None:
    production, prompt_package, profile = _build_fixtures()
    assets = analyze_asset_necessity(production, prompt_package, profile)

    characters = [asset for asset in assets if asset.asset_type == "character"]
    assert {asset.character_id for asset in characters} == {"lin_wan", "a_kai"}
    lead = next(asset for asset in characters if asset.character_id == "lin_wan")
    assert lead.necessity == "required"  # close-up source shot
    assert lead.reuse_scope == "project"
    assert lead.asset_key.startswith("@CHAR_")
    assert lead.asset_key.endswith("_MASTER")
    assert lead.compiler_trace["clip_count"] == 2
    assert lead.compiler_trace["has_near_shot"] is True
    minor = next(asset for asset in characters if asset.character_id == "a_kai")
    assert minor.necessity == "recommended"
    assert minor.compiler_trace["has_near_shot"] is False

    locations = [asset for asset in assets if asset.asset_type == "location"]
    assert {asset.location_id for asset in locations} == {"office", "rooftop"}
    office = next(asset for asset in locations if asset.location_id == "office")
    assert office.necessity == "recommended"  # referenced by 1 clip
    assert office.asset_key.startswith("@LOC_")
    assert office.asset_key.endswith("_MASTER")
    rooftop = next(asset for asset in locations if asset.location_id == "rooftop")
    assert rooftop.necessity == "required"  # referenced by 2 clips

    props = [asset for asset in assets if asset.asset_type == "prop"]
    assert len(props) == 1
    assert props[0].asset_key.startswith("@PROP_")
    assert props[0].necessity == "recommended"  # referenced by 2 clips

    styles = [asset for asset in assets if asset.asset_type == "style"]
    assert len(styles) == 1
    assert styles[0].necessity == "optional"
    assert styles[0].asset_key == "@STYLE_MASTER"

    # Scene Start Frames (task #19/#20): clips 1 and 2 open scenes via
    # image-to-video (characters present + generic profile supports i2v).
    starts = [asset for asset in assets if asset.asset_type == "start_frame"]
    assert {asset.asset_key for asset in starts} == {
        "@EP01_CLIP01_START",
        "@EP01_CLIP02_START",
    }
    for start in starts:
        assert start.necessity == "required"
        assert start.status == "PROMPT_READY"
        assert "OPENING STILL FRAME" in start.generation_prompt
        assert "SUBJECT AND OPENING ACTION" in start.generation_prompt
        assert start.asset_key in start.generation_prompt
    clip1_start = next(a for a in starts if a.asset_key == "@EP01_CLIP01_START")
    assert clip1_start.compiler_trace["clip_number"] == 1

    # Reference frames: only clip 2 chains into clip 3 (same location).
    frames = [asset for asset in assets if asset.asset_type == "reference_frame"]
    assert {asset.name for asset in frames} == {"FRAME_02"}
    for frame in frames:
        assert frame.status == "NEEDED"  # capture instruction, not an image
        assert "Start Frame" in frame.generation_prompt
        assert "保存命名：" in frame.generation_prompt
        assert "片尾" in frame.generation_prompt
    frame_02 = next(asset for asset in frames if asset.name == "FRAME_02")
    assert "视频 02" in frame_02.generation_prompt
    assert frame_02.compiler_trace["source_clip"] == 2
    assert frame_02.compiler_trace["target_clip"] == 3


def test_analyze_classification_survives_fixture_rename() -> None:
    """No hardcoded story names: renaming every fixture identity keeps the
    same structural classification."""
    production, prompt_package, profile = _build_fixtures(
        char_ids=("old_chen", "little_yu"),
        char_names=("老陈", "小雨"),
        loc_ids=("harbor", "attic"),
        loc_names=("码头仓库", "阁楼"),
        prop_name="红色雨伞",
    )
    assets = analyze_asset_necessity(production, prompt_package, profile)
    by_type: dict[str, list[str]] = {}
    for asset in assets:
        by_type.setdefault(asset.asset_type, []).append(asset.necessity)
    assert sorted(by_type["character"]) == ["recommended", "required"]
    assert sorted(by_type["location"]) == ["recommended", "required"]
    assert by_type["prop"] == ["recommended"]
    assert by_type["style"] == ["optional"]
    assert sorted(by_type["start_frame"]) == ["required", "required"]
    assert by_type["reference_frame"] == ["required"]
    keys = {asset.asset_key for asset in assets}
    assert "@CHAR_老陈_MASTER" in keys  # CJK names stay verbatim in the key
    assert "@LOC_码头仓库_MASTER" in keys
    assert "@CHAR_小雨_MASTER" in keys
    assert "@LOC_阁楼_MASTER" in keys
    assert "@PROP_红色雨伞" in keys


# ----------------------------------------------------------------------
# build_reference_asset_prompts
# ----------------------------------------------------------------------
def test_reference_asset_prompts_are_complete_and_never_bound() -> None:
    production, prompt_package, profile = _build_fixtures()
    assets = analyze_asset_necessity(production, prompt_package, profile)
    for asset in assets:
        assert asset.status != "BOUND"
        if asset.asset_type == "reference_frame":
            continue  # keeps its capture instruction, never an image prompt
        assert asset.status == "PROMPT_READY"
        assert len(asset.generation_prompt) > 400, asset.asset_key
        assert f"Reference token: {asset.asset_key}" in asset.generation_prompt
        assert "STRICT:" in asset.generation_prompt
        assert "REFERENCE PURPOSE:" in asset.generation_prompt
        if asset.asset_type != "style":
            # The style asset prompt is itself the style statement; identity
            # prompts additionally carry the shared VISUAL STYLE section.
            assert "VISUAL STYLE:" in asset.generation_prompt

    char_a = next(a for a in assets if a.character_id == "lin_wan")
    assert "CHARACTER REFERENCE SHEET" in char_a.generation_prompt
    assert CHAR_TEXTS[0] in char_a.generation_prompt
    office = next(a for a in assets if a.location_id == "office")
    assert "ENVIRONMENT REFERENCE" in office.generation_prompt
    assert LOC_TEXTS[0] in office.generation_prompt
    prop = next(a for a in assets if a.asset_type == "prop")
    assert "PROP REFERENCE" in prop.generation_prompt
    assert PROP_TEXT in prop.generation_prompt


def test_build_reference_asset_prompts_refills_blank_assets_deterministically(
) -> None:
    production, prompt_package, profile = _build_fixtures()
    original = analyze_asset_necessity(production, prompt_package, profile)
    blanked = [
        asset.model_copy(update={"generation_prompt": "", "status": "NEEDED"})
        if asset.asset_type != "reference_frame"
        else asset
        for asset in original
    ]
    refilled = build_reference_asset_prompts(blanked, production, profile)
    by_key = {asset.asset_key: asset for asset in refilled}
    for asset in original:
        if asset.asset_type == "reference_frame":
            continue
        assert by_key[asset.asset_key].generation_prompt == asset.generation_prompt
        assert by_key[asset.asset_key].status == "PROMPT_READY"


# ----------------------------------------------------------------------
# Frame chain
# ----------------------------------------------------------------------
def test_frame_chain_location_aware_with_scene_starts() -> None:
    """Chaining is location-aware (task #19/#62): same-location clips chain via
    FRAME tokens; a scene change opens a dedicated Scene Start Frame instead
    of physically continuing the previous shot."""
    _production, prompt_package, profile = _build_fixtures()
    clips = sorted(prompt_package.clips, key=lambda clip: clip.clip_number)
    chain = build_frame_chain(clips, profile, 1)
    first = chain["clips"]["1"]
    assert first["start_frame"] is None
    assert first["scene_start_frame"] == "@EP01_CLIP01_START"  # opens the episode
    assert first["produces_next_start_frame"] is False  # next clip changes scene
    second = chain["clips"]["2"]
    assert second["start_frame"] is None  # office -> rooftop scene change
    assert second["scene_start_frame"] == "@EP01_CLIP02_START"
    assert second["produces_next_start_frame"] is True
    assert second["end_frame_saved"] == "FRAME_02"
    third = chain["clips"]["3"]
    assert third["start_frame"] == "FRAME_02"  # same location: chain continues
    assert third["scene_start_frame"] is None
    assert third["produces_next_start_frame"] is False
    assert chain["frame_plans"]["3"] == {
        "carry_in_start_frame": "FRAME_02",
        "produces_next_start_frame": False,
    }
    assert chain["total_clips"] == 3
    assert chain["diagram"][-1].startswith("Clip 03")


# ----------------------------------------------------------------------
# Full guide orchestration
# ----------------------------------------------------------------------
def test_build_executable_guide_full_structure() -> None:
    production, prompt_package, profile = _build_fixtures()
    guide = _build_guide(production, prompt_package, profile)

    assert guide.status == "ready"
    assert guide.project_id == "proj_guide"
    assert guide.episode_number == 1
    assert guide.production_package_id == production.id
    assert guide.prompt_package_id == prompt_package.id
    assert guide.episode_version_id == "ver_canon"
    assert guide.target_profile_id == profile.id
    assert guide.stale is False
    assert guide.title.startswith("EP01")
    assert guide.title.endswith("AI视频完整制作方案")

    overview = guide.overview
    assert overview["clip_count"] == 3
    assert overview["target_model"] == profile.display_name
    assert overview["episode_title"]
    assert overview["total_duration_seconds"] == 19.0
    assert overview["base_params"]["target_model"] == profile.display_name
    # textual_anchor profiles translate into operational wording, not flags.
    assert any("文字锚点" in line for line in overview["operation_notes"])
    assert overview["platform_terms"]["start_frame"] == "Start Frame"
    assert overview["shot_narrative"]["1"]

    assert list(guide.runtime_trace["sections"]) == [
        "goal",
        "character_assets",
        "location_assets",
        "prop_ui_assets",
        "episode_structure",
        "clip_work_orders",
        "frame_workflow",
        "subtitles",
        "screen_composite",
        "dialogue_sound",
        "sfx",
        "bgm",
        "editing",
        "checklist",
    ]

    # Per-clip copy-ready prompts: self-contained, complete, placeholder-free.
    by_number = {clip.clip_number: clip for clip in guide.clip_workflows}
    assert set(by_number) == {1, 2, 3}
    for number, clip in by_number.items():
        assert clip.copy_ready_prompt, number
        for marker in _COPY_READY_MARKERS:
            assert marker in clip.copy_ready_prompt, (number, marker)
        _assert_no_placeholders(clip.copy_ready_prompt)
        # Stable ordered dedupe (task #30): no repeated constraint rows.
        constraints = clip.continuity_constraints
        assert len(constraints) == len(set(constraints)), constraints
        # One resolved user-facing start-frame token per clip (task #12).
        assert clip.start_frame_asset_key, number
    # Scene / character identity text comes from the bibles (self-contained).
    assert CHAR_TEXTS[0] in by_number[1].copy_ready_prompt
    assert LOC_TEXTS[0] in by_number[1].copy_ready_prompt
    assert "你看到手机上的邮件了吗" in by_number[1].copy_ready_prompt
    # Clip 1 opens via its scene start frame token.
    assert by_number[1].start_frame_asset_key == "EP01_CLIP01_START"
    assert "@EP01_CLIP01_START" in by_number[1].copy_ready_prompt
    # Clip 2 changes scene: its own start frame, not a carried frame.
    assert by_number[2].start_frame_asset_key == "EP01_CLIP02_START"
    assert CHAR_TEXTS[1] in by_number[2].copy_ready_prompt
    assert LOC_TEXTS[1] in by_number[2].copy_ready_prompt
    # Clip 3 chains physically from clip 2's tail frame.
    assert by_number[3].start_frame_asset_key == "FRAME_02"
    assert "START FRAME: use the provided START FRAME image" in (
        by_number[3].copy_ready_prompt
    )
    assert "23:47" in by_number[1].copy_ready_prompt

    frame_chain = guide.frame_chain["clips"]
    assert frame_chain["1"]["start_frame"] is None
    assert frame_chain["1"]["scene_start_frame"] == "@EP01_CLIP01_START"
    assert frame_chain["2"]["start_frame"] is None
    assert frame_chain["2"]["scene_start_frame"] == "@EP01_CLIP02_START"
    assert frame_chain["3"]["start_frame"] == "FRAME_02"
    assert frame_chain["3"]["produces_next_start_frame"] is False

    # Screen composite: clip 1 mentions 手机/邮件 → diegetic readable text.
    screen = guide.screen_composite_plan
    assert screen and screen[0]["clip_number"] == 1
    assert screen[0]["category"].startswith("画面真实可读文本")
    assert {"手机", "邮件"} & set(screen[0]["matched"])

    # Subtitle plan mirrors the subtitle track.
    assert len(guide.subtitle_plan) == len(production.subtitle_track) == 2
    assert guide.subtitle_plan[0]["text"] == "这封邮件不可能存在"
    assert guide.subtitle_plan[0]["time"] == "00:00–00:05"

    # Dialogue plan: generic profile does not document native dialogue.
    assert guide.dialogue_plan
    assert all(entry["strategy"] == "post_dub" for entry in guide.dialogue_plan)
    assert {entry["clip_number"] for entry in guide.dialogue_plan} == {1, 2}

    # Sound plan pulls the sfx plan per clip.
    sound_by_clip = {entry["clip_number"]: entry for entry in guide.sound_plan}
    assert sound_by_clip[1]["key_sfx"] == ["手机提示音与雨声"]
    assert sound_by_clip[3]["key_sfx"] == ["远处城市低鸣"]
    assert sound_by_clip[1]["environment"] == ["雨声渐强"]

    # BGM: one COMPLETE music prompt (task #38/#39).
    bgm = guide.bgm_plan
    assert len(bgm["prompt"]) > 300
    for section in ("OVERALL STYLE:", "INSTRUMENTATION:", "EMOTIONAL ARC:", "MIX:", "STRICT:"):
        assert section in bgm["prompt"], section
    assert "00:00" in bgm["prompt"]
    assert bgm["total_duration_seconds"] == 19.0
    assert [segment["clip_number"] for segment in bgm["segments"]] == [1, 2, 3]

    editing = guide.editing_plan
    assert editing["order"] == [1, 2, 3]
    assert len(editing["transitions"]) == 2
    assert editing["transitions"][0]["from_clip"] == 1

    assert guide.final_checklist
    assert any("19" in item for item in guide.final_checklist)

    _assert_no_placeholders(guide.markdown_document)


def test_guide_markdown_renders_all_sections_idempotently() -> None:
    production, prompt_package, profile = _build_fixtures()
    guide = _build_guide(production, prompt_package, profile)
    markdown = guide.markdown_document
    for title in _SECTION_TITLES:
        assert title in markdown, title
    # 《视频生成模型.md》 structure markers (task #8/#10/#31).
    assert markdown.startswith("# EP01《")
    assert "AI视频完整制作方案" in markdown.splitlines()[1]
    assert "### 素材1：@CHAR_" in markdown
    assert "这张图不是一个视频分镜" in markdown
    assert "完整图片生成 Prompt（直接复制到图片生成工具）：" in markdown
    assert "## 使用方式" in markdown
    assert "生成模式：" in markdown
    assert "### Start Frame" in markdown
    assert "### Ingredients / References" in markdown
    assert "### Prompt 1" in markdown
    assert "### 视频2生成完成以后" in markdown
    assert "保存为：" in markdown
    assert "作为视频 3 的 Start Frame。" in markdown  # clip 2 chains into clip 3
    assert "尾帧接首帧完整流程" in markdown
    assert render_guide_markdown(guide) == markdown


def test_guide_markdown_is_deterministic_across_builds() -> None:
    production, prompt_package, profile = _build_fixtures()
    options = {"created_at": datetime(2026, 9, 2, tzinfo=UTC)}
    first = _build_guide(production, prompt_package, profile, options)
    second = _build_guide(production, prompt_package, profile, options)
    assert first.markdown_document == second.markdown_document
    assert first.overview == second.overview
    assert first.frame_chain == second.frame_chain
    assert first.bgm_plan["prompt"] == second.bgm_plan["prompt"]
    keys_first = {asset.asset_key: asset.generation_prompt for asset in first.required_assets}
    keys_second = {
        asset.asset_key: asset.generation_prompt for asset in second.required_assets
    }
    assert keys_first == keys_second


def test_markdown_asset_tokens_are_closed_under_required_assets() -> None:
    production, prompt_package, profile = _build_fixtures()
    guide = _build_guide(production, prompt_package, profile)
    asset_keys = {asset.asset_key for asset in guide.required_assets}
    tokens = set(re.findall(r"@[0-9A-Za-z_\u4e00-\u9fff]{2,}", guide.markdown_document))
    assert tokens - asset_keys == set(), tokens - asset_keys
    assert asset_keys - tokens == set(), asset_keys - tokens
    # Every required asset surfaces inside the markdown asset sections.
    for asset in guide.required_assets:
        assert asset.asset_key in guide.markdown_document


def test_build_guide_fails_closed_on_unresolvable_location() -> None:
    production, prompt_package, profile = _build_fixtures()
    bad_clip = prompt_package.clips[1].model_copy(update={"location": "neverland"})
    prompt_package = prompt_package.model_copy(
        update={
            "clips": [
                prompt_package.clips[0],
                bad_clip,
                prompt_package.clips[2],
            ]
        }
    )
    with pytest.raises(NarrativeAgentError) as excinfo:
        _build_guide(production, prompt_package, profile)
    assert excinfo.value.code == SHOOTING_LOCATION_CONTEXT_MISSING
    assert excinfo.value.stage == "video_production_guide"


# ----------------------------------------------------------------------
# Enrichment merge
# ----------------------------------------------------------------------
def test_apply_asset_prompt_enrichment_merges_good_rows_only() -> None:
    production, prompt_package, profile = _build_fixtures()
    guide = _build_guide(production, prompt_package, profile)
    target = next(asset for asset in guide.required_assets if asset.asset_type == "character")
    baseline_prompt = target.generation_prompt
    refined = (
        "Refined character sheet: full-body identity anchor with wardrobe, "
        "palette and silhouette locked. " + "细节逐项展开。" * 20
    )
    merged = apply_asset_prompt_enrichment(
        guide.required_assets,
        [
            {"asset_key": target.asset_key, "generation_prompt": refined},
            {"asset_key": target.asset_key, "generation_prompt": "TBD"},
            {"asset_key": "@NOT_A_REAL_KEY", "generation_prompt": "orphan row"},
            {"asset_key": target.asset_key, "generation_prompt": "   "},
            {"generation_prompt": "missing key row"},
        ],
    )
    by_key = {asset.asset_key: asset for asset in merged}
    assert by_key[target.asset_key].generation_prompt == refined
    assert by_key[target.asset_key].provenance == "llm_refined"
    other = next(asset for asset in merged if asset.asset_type == "location")
    assert other.provenance == "deterministic"
    # Inputs are never mutated.
    assert target.generation_prompt == baseline_prompt
    assert target.provenance == "deterministic"


def test_apply_clip_prompt_enrichment_merges_by_clip_id_and_rejects_placeholders(
) -> None:
    production, prompt_package, profile = _build_fixtures()
    guide = _build_guide(production, prompt_package, profile)
    clip = guide.clip_workflows[0]
    baseline_prompt = clip.copy_ready_prompt
    refined = "Refined copy-ready prompt with full sections. " + "镜头描述。" * 20
    merged = apply_clip_prompt_enrichment(
        guide.clip_workflows,
        [
            {"clip_id": clip.id, "copy_ready_prompt": refined},
            {"clip_id": clip.id, "copy_ready_prompt": "TODO"},
            {"clip_id": "clip_missing", "copy_ready_prompt": "orphan"},
            {"clip_id": clip.id},
        ],
    )
    by_id = {item.id: item for item in merged}
    assert by_id[clip.id].copy_ready_prompt == refined
    assert by_id[guide.clip_workflows[1].id].copy_ready_prompt == (
        guide.clip_workflows[1].copy_ready_prompt
    )
    # Inputs are never mutated.
    assert clip.copy_ready_prompt == baseline_prompt


# ----------------------------------------------------------------------
# CharacterVisualIdentity synthesis (task #13/#14)
# ----------------------------------------------------------------------
def test_thin_bible_triggers_synthesized_visual_identity() -> None:
    """A character whose bible lacks concrete visual cues gets a FIXED
    synthesized identity (age/face/hair/body/costume), never a generic
    'East Asian woman' placeholder — and the same seed always yields the
    same identity (reuse across episodes)."""
    from persona_continuum.narrative.video_production_guide import (
        synthesize_character_visual_identity,
    )

    first = synthesize_character_visual_identity(
        "proj_guide", "fang", "方宁", "办公室职员，神情焦虑"
    )
    second = synthesize_character_visual_identity(
        "proj_guide", "fang", "方宁", "办公室职员，神情焦虑"
    )
    assert first == second  # deterministic per (project, character)
    assert first["source"] == "synthesized"
    for field in (
        "age",
        "gender",
        "face_shape",
        "eyes",
        "hair",
        "height",
        "body",
        "costume",
        "color_palette",
        "temperament",
    ):
        assert first[field], field
    # Extracted story cues win over seeded defaults.
    aged = synthesize_character_visual_identity(
        "proj_guide", "fang", "方宁", "29岁，穿着深灰色连帽衫"
    )
    assert aged["age"] == "29 years old"
    assert "连帽衫" in aged["costume"]
    # Different characters get different designs.
    other = synthesize_character_visual_identity(
        "proj_guide", "lin", "林薇", "办公室职员，神情焦虑"
    )
    assert other != first


def test_guide_synthesizes_identity_for_thin_bible_character() -> None:
    production, prompt_package, profile = _build_fixtures()
    # Thin out one character's bible and enrich the other: the thin entry
    # must get a fixed synthesized identity, the rich one stays untouched.
    thin_bible = [
        {"character_id": "lin_wan", "name": "林晚", "visual_description": "职员"},
        {
            **production.character_visual_bible[1],
            "visual_description": (
                CHAR_TEXTS[1] + "身高175，黑色短发，眼神坚毅，体型偏瘦"
            ),
        },
    ]
    production = production.model_copy(
        update={"character_visual_bible": thin_bible}
    )
    guide = _build_guide(production, prompt_package, profile)
    thin = next(
        asset
        for asset in guide.required_assets
        if asset.character_id == "lin_wan"
    )
    assert thin.visual_identity.get("source") == "synthesized"
    assert thin.visual_identity.get("age")
    assert "Synthesized fixed visual identity" in thin.generation_prompt
    # The copy-ready prompt embeds the synthesized identity too (task #11).
    clip1 = next(
        clip for clip in guide.clip_workflows if clip.clip_number == 1
    )
    assert "Synthesized fixed visual identity" in clip1.copy_ready_prompt
    # Rich bibles stay untouched (no synthesis).
    rich = next(
        asset
        for asset in guide.required_assets
        if asset.character_id == "a_kai"
    )
    assert rich.visual_identity.get("source") != "synthesized"
    assert CHAR_TEXTS[1] in rich.generation_prompt


def test_build_guide_with_sub_locations_succeeds() -> None:
    """Clips with hierarchical sub-locations (e.g. 智源科技大厦·运营部开放工位)
    resolve against parent bible entries and build complete guides without error."""
    production, prompt_package, profile = _build_fixtures(
        loc_ids=("loc_mindcore_hq", "loc_crossroad"),
        loc_names=("智源科技总部大楼（MindCore Tower）", "滨海大道十字路口"),
    )
    clips_with_sub_locs = [
        prompt_package.clips[0].model_copy(update={"location": "智源科技大厦·运营部开放工位"}),
        prompt_package.clips[1].model_copy(update={"location": "智源科技大厦·茶水间外走廊"}),
        prompt_package.clips[2].model_copy(update={"location": "智源科技大厦外·街角至十字路口"}),
    ]
    prompt_package = prompt_package.model_copy(update={"clips": clips_with_sub_locs})
    guide = _build_guide(production, prompt_package, profile)

    assert guide.status == "ready"
    assert len(guide.clip_workflows) == 3
    for clip in guide.clip_workflows:
        assert clip.copy_ready_prompt
        assert "ENVIRONMENT IDENTITY" in clip.copy_ready_prompt
        assert clip.location in clip.copy_ready_prompt

