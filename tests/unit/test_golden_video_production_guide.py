"""Golden acceptance test for the complete AI video production guide.

Task #51–#57: acceptance must check the FINAL exported markdown, not the
data model. The golden file ``tests/golden/expected_video_production_guide_
structure.md`` abstracts the structure of the user's reference document
《视频生成模型.md》; every positive marker must appear in the rendered
handbook and every ``!``-prefixed marker (developer-facing leakage like
snake_case settings or job-stage jargon) must never appear.

The fixture mirrors the reference case (EP01《来自2036年的裁员预警》): two
characters, two locations, one prop, chained clips with a scene change.
"""

from __future__ import annotations

from pathlib import Path

from persona_continuum.domain.narrative import (
    GenerationClip,
    ModelPromptPackage,
    ProductionPackage,
    Shot,
)
from persona_continuum.narrative.video_production_guide import (
    build_executable_video_production_guide,
)
from persona_continuum.narrative.video_profile_registry import get_profile

GOLDEN_PATH = (
    Path(__file__).resolve().parents[1]
    / "golden"
    / "expected_video_production_guide_structure.md"
)


def _golden_markers() -> tuple[list[str], list[str]]:
    """(must_appear, must_not_appear) markers parsed from the golden file."""
    must: list[str] = []
    must_not: list[str] = []
    for raw in GOLDEN_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        if line.startswith("!"):
            must_not.append(line[1:])
        else:
            must.append(line)
    return must, must_not


def _build_ep01_guide():
    """Deterministic EP01-like guide built straight from domain fixtures."""
    profile = get_profile("veo_3_1")
    shots = [
        Shot(
            shot_number=1,
            duration_seconds=8.0,
            shot_size="close-up",
            camera="eye-level",
            movement="slow push",
            characters=["fang_ning"],
            action="方宁盯着手里的手机，屏幕亮起一封来自2036年的邮件",
            dialogue="这封邮件……不可能。",
            location="operations_office",
            visual_intent="方宁的侧脸被手机屏幕照亮",
            sfx=["手机提示音"],
        ),
        Shot(
            shot_number=2,
            duration_seconds=8.0,
            shot_size="medium",
            camera="eye-level",
            movement="static",
            characters=["fang_ning"],
            action="方宁握着手机的手开始颤抖，抬头望向办公室窗外",
            dialogue="",
            location="operations_office",
            visual_intent="方宁缓缓抬头，脸色发白",
            sfx=["窗外雨声渐强"],
        ),
        Shot(
            shot_number=3,
            duration_seconds=8.0,
            shot_size="wide",
            camera="low-angle",
            movement="slow pull",
            characters=["fang_ning"],
            action="暴雨中的十字路口，方宁站在斑马线前，手机屏幕再次亮起",
            dialogue="19:17。",
            location="rain_crossroad",
            visual_intent="雨水冲刷着路口的红绿灯，方宁停在斑马线前",
            sfx=["暴雨与雷声"],
        ),
    ]
    production = ProductionPackage(
        project_id="proj_ep01",
        episode_number=1,
        episode_version_id="ver_canon",
        shot_list=shots,
        character_visual_bible=[
            {
                "character_id": "fang_ning",
                "name": "方宁",
                "visual_description": (
                    "28岁，鹅蛋脸，眉眼平静但总带着倦意，黑色齐肩短发别在耳后，"
                    "身穿深灰色针织开衫与白衬衫，深色直筒裤"
                ),
                "color_palette": ["深灰", "冷白"],
                "forbidden_variations": ["短发", "亮色服装"],
            }
        ],
        location_visual_bible=[
            {
                "location_id": "operations_office",
                "name": "运营办公室",
                "visual_description": (
                    "开放式办公区，白炽灯管中一支闪烁，桌面堆满文件，"
                    "落地窗外是雨夜城市"
                ),
                "color_palette": ["冷白", "青灰"],
            },
            {
                "location_id": "rain_crossroad",
                "name": "暴雨路口",
                "visual_description": (
                    "暴雨中的城市十字路口，红绿灯在雨幕中晕开光斑，"
                    "湿漉漉的斑马线反射着霓虹"
                ),
                "color_palette": ["暗红", "灰蓝"],
            },
        ],
        prop_visual_bible=[
            {
                "name": "2036手机",
                "visual_description": "黑色直板手机，屏幕边缘有细小磨损",
            }
        ],
        prop_list=["2036手机"],
        subtitle_track=[
            {"start_time": 0.0, "end_time": 8.0, "text": "这封邮件……不可能。"},
            {"start_time": 16.0, "end_time": 24.0, "text": "19:17。"},
        ],
        sound_effect_plan=[
            {"clip_number": 1, "effect": "手机提示音"},
            {"clip_number": 3, "effect": "暴雨与雷声"},
        ],
        bgm_direction="悬疑、压抑、逐渐逼近的倒计时感",
        continuity_notes=["全集保持冷色调青灰光影", "手机屏幕始终是画面强光源"],
        generic_video_guidance=["Keep faces stable and identity consistent."],
    )
    clips = [
        GenerationClip(
            id="clip_ep01_1",
            clip_number=1,
            source_shot_numbers=[1],
            duration_seconds=8.0,
            location="operations_office",
            character_ids=["fang_ning"],
            prop_ids=["2036手机"],
            visual_intent="方宁在工位前盯着手中的手机屏幕",
            camera_intent="缓慢推近至特写",
            subject_motion="方宁的手指悬停在屏幕上方，微微颤抖",
            environment_motion="窗外雨滴沿玻璃滑落",
            dialogue=[{"speaker": "方宁", "line": "这封邮件……不可能。"}],
            audio_intent=["手机提示音"],
            continuity_constraints=["手机屏幕上的时间显示为 18:30"],
            purpose="开场悬念：2036邮件抵达",
        ),
        GenerationClip(
            id="clip_ep01_2",
            clip_number=2,
            source_shot_numbers=[2],
            duration_seconds=8.0,
            location="operations_office",
            character_ids=["fang_ning"],
            prop_ids=["2036手机"],
            visual_intent="方宁握着手机缓缓抬头，脸色发白",
            camera_intent="固定机位",
            subject_motion="方宁抬头望向窗外，手指收紧",
            environment_motion="窗外雨势渐大",
            dialogue=[],
            audio_intent=["雨声渐强"],
            continuity_constraints=["previous_clip_end_frame"],
            purpose="反应与不安加深",
        ),
        GenerationClip(
            id="clip_ep01_3",
            clip_number=3,
            source_shot_numbers=[3],
            duration_seconds=8.0,
            location="rain_crossroad",
            character_ids=["fang_ning"],
            prop_ids=["2036手机"],
            visual_intent="暴雨路口，方宁停在斑马线前，手机再次亮起",
            camera_intent="低角度缓慢拉远",
            subject_motion="方宁停在斑马线前抬头看向红绿灯",
            environment_motion="暴雨倾泻，雨刷般的车流驶过",
            dialogue=[{"speaker": "方宁", "line": "19:17。"}],
            audio_intent=["暴雨与雷声"],
            continuity_constraints=[],
            purpose="死亡倒计时：19:17 的预兆",
        ),
    ]
    prompt_package = ModelPromptPackage(
        project_id="proj_ep01",
        episode_number=1,
        production_package_id=production.id,
        episode_version_id="ver_canon",
        target_profile_id=profile.id,
        target_profile_version=profile.profile_version,
        target_video_model_display_name=profile.display_name,
        status="ready",
        clips=clips,
    )
    return build_executable_video_production_guide(
        production,
        prompt_package,
        profile,
        [],
        "proj_ep01",
        {"episode_title": "来自2036年的裁员预警"},
    )


def test_golden_guide_structure_matches_reference_document() -> None:
    guide = _build_ep01_guide()
    markdown = guide.markdown_document
    assert markdown, "guide markdown must not be empty"

    substitution = {
        "{EP}": "EP01",
        "{TITLE}": "来自2036年的裁员预警",
        "{MODEL}": guide.target_video_model_display_name,
        "{CHAR_KEY}": next(
            asset.asset_key
            for asset in guide.required_assets
            if asset.asset_type == "character"
        ),
        "{LOC_KEY}": next(
            asset.asset_key
            for asset in guide.required_assets
            if asset.asset_type == "location"
        ),
    }
    must, must_not = _golden_markers()
    assert must and must_not, "golden file must define both marker sets"

    missing = []
    for marker in must:
        expected = marker
        for key, value in substitution.items():
            expected = expected.replace(key, value)
        if expected not in markdown:
            missing.append(expected)
    assert not missing, f"golden markers missing from guide markdown: {missing}"

    leaked = [marker for marker in must_not if marker in markdown]
    assert not leaked, f"developer-facing leakage in guide markdown: {leaked}"


def test_golden_guide_per_clip_work_orders_are_complete() -> None:
    """Task #31/#53: every video section carries the full work-order anatomy:
    使用方式 / Start Frame / Ingredients / ONE fenced prompt / 生成完成后."""
    guide = _build_ep01_guide()
    markdown = guide.markdown_document
    for clip in sorted(guide.clip_workflows, key=lambda c: c.clip_number):
        number = clip.clip_number
        assert f"## {_cn_num(5 + number)}、视频 {number}：" in markdown
        assert f"### Prompt {number}" in markdown
        assert f"### 视频{number}生成完成以后" in markdown
        # ONE complete copy-ready prompt per video (task #14/#24).
        assert clip.copy_ready_prompt in markdown
        # Concrete generation mode, never "自动" (task #22).
        assert "生成模式：图生视频" in markdown or "生成模式：文生视频" in markdown
    # Permanent character assets precede every video section (task #10).
    assert markdown.index("## 二、先建立永久角色参考素材") < markdown.index(
        "## 六、视频 1"
    )
    # FRAME chain: clip 2 chains from clip 1's tail (same location), clip 3
    # switches scene and gets a dedicated Scene Start Frame (task #19/#62).
    assert "保存为：" in markdown
    assert "作为视频 2 的 Start Frame。" in markdown
    assert "@EP01_CLIP03_START" in markdown


def _cn_num(n: int) -> str:
    digits = ("一", "二", "三", "四", "五", "六", "七", "八", "九")
    if n < 10:
        return digits[n - 1]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + digits[n - 11]
    tens, ones = divmod(n, 10)
    return digits[tens - 1] + "十" + (digits[ones - 1] if ones else "")
