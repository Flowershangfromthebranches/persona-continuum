"""Executable video production guide renderer (guide layer, Task B).

A pure, deterministic export layer stacked on top of an existing
:class:`~persona_continuum.domain.narrative.ModelPromptPackage`. It turns the
compiled clips into a self-contained, human-operable shooting handbook:
permanent reference assets with complete copy-ready image prompts, one
copy-ready video prompt per clip, a tail-frame chaining workflow, and
subtitle / dialogue / SFX / BGM / editing / checklist plans.

Rules honoured here:
* zero I/O, zero LLM calls, deterministic (identical inputs -> identical
  markdown);
* never emit a placeholder phrase (see :data:`PLACEHOLDER_PATTERNS`);
* never fabricate files — reference assets are prompts/instructions, their
  status is ``PROMPT_READY`` / ``NEEDED`` and never ``BOUND``;
* a clip whose location resolves to no bible/list entry fails closed via
  :func:`compile_copy_ready_prompt` (SHOOTING_LOCATION_CONTEXT_MISSING).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from persona_continuum.domain.narrative import (
    ExecutableVideoProductionGuide,
    GenerationClip,
    ModelPromptPackage,
    ProductionAsset,
    ProductionGuideAsset,
    ProductionPackage,
    Shot,
    VideoModelProfile,
)
from persona_continuum.narrative.video_prompt_compiler import compile_copy_ready_prompt

# Banned in every emitted prompt / instruction / markdown body. English
# patterns are matched case-insensitively; CJK patterns match verbatim.
PLACEHOLDER_PATTERNS: tuple[str, ...] = (
    "the established location",
    "per shot",
    "natural subject motion",
    "TBD",
    "TODO",
    "待定",
)

_NEAR_SHOT_SIZES = {"close", "medium", "close-up", "closeup", "medium close"}
_SCREEN_DIEGETIC = ("手机", "邮件", "屏幕", "短信", "letter", "screen", "phone", "email", "message")
_SCREEN_OVERLAY = (
    "字幕",
    "通知",
    "时间",
    "标题",
    "notification",
    "caption",
    "subtitle",
    "timestamp",
)
_SKIP_KEYS = {
    "id",
    "name",
    "character_id",
    "location_id",
    "title",
    "visual_description",
    "description",
    "visual",
    "summary",
    "color_palette",
    "forbidden_variations",
}
_STATUS_LABELS = {
    "NEEDED": "需要生成",
    "PROMPT_READY": "Prompt 就绪",
    "EXISTING": "已有素材",
    "GENERATED": "已生成",
    "BOUND": "已绑定文件",
}
_NECESSITY_LABELS = {"required": "必需", "recommended": "建议", "optional": "可选"}
_SCOPE_LABELS = {"project": "全项目复用", "episode": "本集复用", "scene": "单场景复用"}
_MODE_LABELS = {
    "text_to_video": "文生视频",
    "image_to_video": "图生视频",
    "first_frame": "首帧驱动",
    "first_last_frame": "首尾帧驱动",
    "reference_conditioned": "参考图约束",
    "auto": "自动",
}
_NUMERALS = (
    "一",
    "二",
    "三",
    "四",
    "五",
    "六",
    "七",
    "八",
    "九",
    "十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
)


# ----------------------------------------------------------------------
# Small deterministic helpers
# ----------------------------------------------------------------------
def _upper_snake(name: str) -> str:
    """Stable UPPER_SNAKE token; keeps CJK glyphs, drops punctuation."""
    text = re.sub(r"[\s\-–—/\.]+", "_", name.strip())
    text = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text.upper() or "UNNAMED"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_clock(seconds: float) -> str:
    total = max(0.0, seconds)
    minutes = int(total // 60)
    secs = int(round(total - minutes * 60))
    if secs == 60:
        minutes += 1
        secs = 0
    return f"{minutes:02d}:{secs:02d}"


def _contains_placeholder(text: str) -> bool:
    folded = text.casefold()
    return any(pattern.casefold() in folded for pattern in PLACEHOLDER_PATTERNS)


def _ensure_no_placeholders(text: str) -> None:
    """Fail closed when a banned placeholder phrase leaks into any output."""
    folded = text.casefold()
    for pattern in PLACEHOLDER_PATTERNS:
        if pattern.casefold() in folded:
            raise ValueError(
                f"GUIDE_PLACEHOLDER_DETECTED: forbidden placeholder '{pattern}' "
                "present in generated guide text"
            )


def _entry_name(entry: dict[str, Any]) -> str:
    for key in ("name", "character_id", "location_id", "title", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _entry_description_block(entry: dict[str, Any]) -> str:
    """Every descriptive field of one bible entry, flattened to readable text.

    Pulls the full visual identity (not a one-liner): the primary description
    plus colour palette, forbidden variations, and any other descriptive
    string/list fields the bible carries.
    """
    parts: list[str] = []
    for key in ("visual_description", "description", "visual", "summary"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, str) and sub_value.strip():
                    parts.append(f"{str(sub_key).replace('_', ' ')}: {sub_value.strip()}")
    palette = entry.get("color_palette")
    if isinstance(palette, list) and palette:
        joined = ", ".join(str(item) for item in palette if item)
        if joined:
            parts.append(f"Color palette: {joined}")
    forbidden = entry.get("forbidden_variations")
    if isinstance(forbidden, list) and forbidden:
        joined = ", ".join(str(item) for item in forbidden if item)
        if joined:
            parts.append(f"Never vary into: {joined}")
    for key, value in entry.items():
        if key in _SKIP_KEYS:
            continue
        label = str(key).replace("_", " ")
        if isinstance(value, str) and value.strip():
            parts.append(f"{label}: {value.strip()}")
        elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            parts.append(f"{label}: " + ", ".join(str(v) for v in value if v))
    return "\n".join(part for part in parts if part.strip())


def _character_bible_entry(key: str, package: ProductionPackage) -> dict[str, Any] | None:
    if not key:
        return None
    for entry in package.character_visual_bible:
        if isinstance(entry, dict):
            keys = {str(entry.get(field, "")) for field in ("character_id", "id", "name")}
            if key in keys:
                return entry
    for entry in package.character_list:
        if isinstance(entry, dict):
            keys = {str(entry.get(field, "")) for field in ("id", "name")}
            if key in keys:
                return entry
    return None


def _location_bible_entry(key: str, package: ProductionPackage) -> dict[str, Any] | None:
    if not key:
        return None
    for entries in (package.location_visual_bible, package.location_list):
        for entry in entries:
            if isinstance(entry, dict):
                keys = {str(entry.get(field, "")) for field in ("location_id", "id", "name")}
                if key in keys:
                    return entry
    return None


def _prop_bible_entry(key: str, package: ProductionPackage) -> dict[str, Any] | None:
    if not key:
        return None
    for entry in package.prop_visual_bible:
        if isinstance(entry, dict):
            keys = {str(entry.get(field, "")) for field in ("name", "id", "prop")}
            if key in keys or key.casefold() in {k.casefold() for k in keys}:
                return entry
    return None


def _sorted_clips(clips: list[GenerationClip]) -> list[GenerationClip]:
    return sorted(clips, key=lambda clip: clip.clip_number)


def _clip_time_ranges(clips: list[GenerationClip]) -> tuple[list[tuple[int, float, float]], float]:
    ranges: list[tuple[int, float, float]] = []
    cursor = 0.0
    for clip in _sorted_clips(clips):
        start = cursor
        end = cursor + max(0.0, clip.duration_seconds)
        ranges.append((clip.clip_number, start, end))
        cursor = end
    return ranges, cursor


def _shots_by_number(package: ProductionPackage) -> dict[int, Shot]:
    return {shot.shot_number: shot for shot in package.shot_list}


# ----------------------------------------------------------------------
# Frame chain
# ----------------------------------------------------------------------
def build_frame_chain(clips: list[GenerationClip]) -> dict[str, Any]:
    """Deterministic tail-frame chaining plan.

    Clip 1 starts from no frame; clip N+1 starts from ``FRAME_N`` (the saved
    tail frame of clip N); the last clip saves no continuation frame and ends
    clean for subtitles / fade. Also returns per-clip ``frame_plan`` dicts
    compatible with :func:`compile_copy_ready_prompt`.
    """
    ordered = _sorted_clips(clips)
    total = len(ordered)
    per_clip: dict[str, Any] = {}
    frame_plans: dict[str, Any] = {}
    diagram: list[str] = []
    for index, clip in enumerate(ordered):
        number = clip.clip_number
        produces_next = index < total - 1
        start_frame = None if index == 0 else f"FRAME_{index:02d}"
        saved = f"FRAME_{index + 1:02d}" if produces_next else "无需保存接续帧"
        duration = max(0.0, clip.duration_seconds)
        window_start = max(0.0, duration - 1.0)
        window_end = max(0.0, duration - 0.5)
        per_clip[str(number)] = {
            "clip_number": number,
            "start_frame": start_frame,
            "produces_next_start_frame": produces_next,
            "end_frame_saved": saved,
            "selection_window": (
                f"最后 0.5–1.0 秒（按时长适配：{window_start:.1f}s–{window_end:.1f}s 具体区间）"
            ),
            "criteria": [
                "主体完整、无变形、无多余肢体或残影",
                "动作已基本停止，画面处于稳定可接续的姿态",
                "构图干净、光线均匀，适合作为下一镜首帧",
                "没有字幕、水印、乱码文本叠在画面上",
            ],
            "frame_plan": {
                "carry_in_start_frame": start_frame,
                "produces_next_start_frame": produces_next,
            },
        }
        frame_plans[str(number)] = {
            "carry_in_start_frame": start_frame,
            "produces_next_start_frame": produces_next,
        }
        label = f"Clip {number:02d}"
        diagram.append(f"{label} ──尾帧 {saved if produces_next else '（收尾）'}──▶")
    if diagram:
        diagram[-1] = f"Clip {ordered[-1].clip_number:02d} ──收尾（不产接续帧）"
    return {
        "clips": per_clip,
        "frame_plans": frame_plans,
        "diagram": diagram,
        "total_clips": total,
    }


# ----------------------------------------------------------------------
# Asset necessity analysis
# ----------------------------------------------------------------------
def _clip_context_lines(clip: GenerationClip, package: ProductionPackage) -> list[str]:
    lines: list[str] = []
    if clip.purpose.strip():
        lines.append(clip.purpose.strip())
    if clip.visual_intent.strip():
        lines.append(clip.visual_intent.strip())
    shots = _shots_by_number(package)
    for number in clip.source_shot_numbers:
        shot = shots.get(number)
        if shot is not None and shot.action.strip():
            lines.append(shot.action.strip())
    return lines


def _make_asset(
    package: ProductionPackage,
    prompt_package: ModelPromptPackage,
    *,
    asset_key: str,
    asset_type: str,
    name: str,
    purpose: str,
    necessity: str,
    reuse_scope: str,
    notes: list[str],
    trace: dict[str, Any],
    character_id: str | None = None,
    location_id: str | None = None,
    source_bible_refs: list[str] | None = None,
    status: str = "NEEDED",
    generation_prompt: str = "",
) -> ProductionGuideAsset:
    return ProductionGuideAsset(
        project_id=package.project_id,
        episode_number=package.episode_number,
        asset_key=asset_key,
        asset_type=asset_type,
        name=name,
        purpose=purpose,
        necessity=necessity,
        reuse_scope=reuse_scope,
        generation_prompt=generation_prompt,
        recommended_aspect_ratio=prompt_package.aspect_ratio,
        status=status,
        notes=notes,
        character_id=character_id,
        location_id=location_id,
        source_bible_refs=source_bible_refs or [],
        compiler_trace=trace,
    )


def analyze_asset_necessity(
    production_package: ProductionPackage,
    prompt_package: ModelPromptPackage,
    profile: VideoModelProfile,
) -> list[ProductionGuideAsset]:
    """Classify every reference asset the episode needs (deterministic).

    No hardcoded story names: characters come from ``clip.character_ids``
    frequency plus shot sizes, locations from distinct ``clip.location``,
    props from ``clip.prop_ids`` frequency, one optional style asset from the
    package-level visual direction, and ``FRAME_NN`` reference frames for every
    chained clip. Reference-asset prompts are filled before return so the
    checkpoint baseline is already complete.
    """
    clips = _sorted_clips(prompt_package.clips)
    shots = _shots_by_number(production_package)
    assets: list[ProductionGuideAsset] = []

    # --- Characters -------------------------------------------------
    char_clips: dict[str, list[GenerationClip]] = {}
    for clip in clips:
        for character_id in clip.character_ids:
            if character_id:
                char_clips.setdefault(character_id, []).append(clip)
    for character_id, referencing in char_clips.items():
        entry = _character_bible_entry(character_id, production_package)
        name = _entry_name(entry) if entry else character_id
        has_near = False
        for clip in referencing:
            for number in clip.source_shot_numbers:
                shot = shots.get(number)
                if shot is not None and shot.shot_size.strip().lower() in _NEAR_SHOT_SIZES:
                    has_near = True
                    break
            if has_near:
                break
        count = len(referencing)
        necessity = "required" if (count >= 3 or has_near) else "recommended"
        context: list[str] = []
        for clip in referencing:
            context.extend(_clip_context_lines(clip, production_package))
        reason = (
            f"出现在 {count} 个 Clip"
            + ("，且含近景/中景" if has_near else "")
            + f" → {necessity}"
        )
        assets.append(
            _make_asset(
                production_package,
                prompt_package,
                asset_key=f"@CHAR_{_upper_snake(name)}_MASTER",
                asset_type="character",
                name=name,
                purpose=f"锁定角色「{name}」的长相、身形与服装，跨镜复用同一张身份设定图。",
                necessity=necessity,
                reuse_scope="project",
                notes=[reason, "作为身份锚点，每次该角色出现都要挂上这张参考图。"],
                trace={
                    "necessity": necessity,
                    "clip_count": count,
                    "has_near_shot": has_near,
                    "reason": reason,
                    "fallback_context": list(dict.fromkeys(context))[:6],
                },
                character_id=character_id,
                source_bible_refs=[name] if entry else [],
            )
        )

    # --- Locations --------------------------------------------------
    loc_clips: dict[str, list[GenerationClip]] = {}
    for clip in clips:
        if clip.location:
            loc_clips.setdefault(clip.location, []).append(clip)
    for location, referencing in loc_clips.items():
        entry = _location_bible_entry(location, production_package)
        name = _entry_name(entry) if entry else location
        count = len(referencing)
        necessity = "required" if count >= 2 else "recommended"
        context = []
        for clip in referencing:
            context.extend(_clip_context_lines(clip, production_package))
        reason = f"出现在 {count} 个 Clip → {necessity}"
        assets.append(
            _make_asset(
                production_package,
                prompt_package,
                asset_key=f"@LOC_{_upper_snake(name)}_MASTER",
                asset_type="location",
                name=name,
                purpose=f"锁定场景「{name}」的空间布局、光线与材质，作为环境身份锚点复用。",
                necessity=necessity,
                reuse_scope="episode",
                notes=[reason, "作为空间锚点，凡在此场景发生的镜头都引用它。"],
                trace={
                    "necessity": necessity,
                    "clip_count": count,
                    "reason": reason,
                    "fallback_context": list(dict.fromkeys(context))[:6],
                },
                location_id=location,
                source_bible_refs=[name] if entry else [],
            )
        )

    # --- Props ------------------------------------------------------
    prop_clips: dict[str, list[GenerationClip]] = {}
    for clip in clips:
        for prop_id in clip.prop_ids:
            if prop_id:
                prop_clips.setdefault(prop_id, []).append(clip)
    for prop_name, referencing in prop_clips.items():
        count = len(referencing)
        if count <= 1 and len(prop_name.strip()) < 2:
            continue  # skip trivial single-use fragments
        necessity = "required" if count >= 3 else ("recommended" if count == 2 else "optional")
        entry = _prop_bible_entry(prop_name, production_package)
        context = []
        for clip in referencing:
            context.extend(_clip_context_lines(clip, production_package))
        reason = f"被 {count} 个 Clip 引用 → {necessity}"
        assets.append(
            _make_asset(
                production_package,
                prompt_package,
                asset_key=f"@PROP_{_upper_snake(prop_name)}",
                asset_type="prop",
                name=prop_name,
                purpose=f"锁定关键道具「{prop_name}」的形状、材质与颜色，避免跨镜漂移。",
                necessity=necessity,
                reuse_scope="episode",
                notes=[reason],
                trace={
                    "necessity": necessity,
                    "clip_count": count,
                    "reason": reason,
                    "fallback_context": list(dict.fromkeys(context))[:6],
                },
                source_bible_refs=[prop_name] if entry else [],
            )
        )

    # --- Style (optional, package-level visual direction) -----------
    style_sources = list(prompt_package.global_visual_contract) + list(
        production_package.continuity_notes
    )
    if style_sources:
        assets.append(
            _make_asset(
                production_package,
                prompt_package,
                asset_key="@STYLE_MASTER",
                asset_type="style",
                name="全集视觉风格基准",
                purpose="统一整集的色调、材质、光线气质与渲染风格，作为所有素材与镜头的风格锚点。",
                necessity="optional",
                reuse_scope="project",
                notes=["从 package 级视觉方向（global_visual_contract / continuity_notes）提炼。"],
                trace={
                    "necessity": "optional",
                    "reason": "package-level visual direction identifiable",
                    "fallback_context": [line for line in style_sources if line][:6],
                },
                source_bible_refs=[],
            )
        )

    # --- Reference frames (chained clips, all but last) -------------
    chain = build_frame_chain(clips)
    for number_str, info in chain["clips"].items():
        if not info["produces_next_start_frame"]:
            continue
        frame_name = str(info["end_frame_saved"])
        number = int(number_str)
        capture = (
            f"在 Clip {number:02d} 生成完成后，从片尾挑选一帧保存为下一镜的 Start Frame。\n"
            f"选取区间：{info['selection_window']}\n"
            f"选取标准：{'；'.join(info['criteria'])}。\n"
            f"保存命名：{frame_name}，用作 Clip {number + 1:02d} 的 Start Frame（物理连续性锚点）。"
        )
        assets.append(
            _make_asset(
                production_package,
                prompt_package,
                asset_key=f"@{frame_name}",
                asset_type="reference_frame",
                name=frame_name,
                purpose=f"承接 Clip {number:02d} 的收尾画面，作为 Clip {number + 1:02d} 的首帧。",
                necessity="required",
                reuse_scope="episode",
                notes=["这不是需要出图的素材，而是一条尾帧捕获指令。"],
                trace={
                    "necessity": "required",
                    "reason": f"chained clip {number} produces next start frame",
                    "source_clip": number,
                    "target_clip": number + 1,
                },
                status="NEEDED",
                generation_prompt=capture,
            )
        )

    return build_reference_asset_prompts(assets, production_package, profile)


# ----------------------------------------------------------------------
# Reference asset image prompts
# ----------------------------------------------------------------------
def _global_style_line(package: ProductionPackage) -> str:
    hints = [note.strip() for note in package.continuity_notes if note.strip()]
    hints += [line.strip() for line in package.generic_video_guidance if line.strip()]
    if hints:
        return (
            "Render in the unified visual style of this production: "
            + hints[0]
            + " Keep this exact styling, colour grade, and material texture identical across "
            "every reference sheet and every clip."
        )
    return (
        "Render in the unified visual style of this production: consistent colour grade, "
        "realistic material texture, and cinematic but natural detail. Keep this styling "
        "identical across every reference sheet and every clip."
    )


def _identity_or_context(asset: ProductionGuideAsset, entry: dict[str, Any] | None) -> str:
    if entry is not None:
        block = _entry_description_block(entry)
        if block:
            return block
    fallback = [str(line) for line in asset.compiler_trace.get("fallback_context") or []]
    if fallback:
        return "\n".join(fallback)
    return asset.purpose or asset.name


def _tail(asset: ProductionGuideAsset, purpose_line: str) -> list[str]:
    aspect = asset.recommended_aspect_ratio or "16:9"
    return [
        "",
        "REFERENCE PURPOSE:",
        purpose_line,
        "",
        "STRICT:",
        "No text. No caption. No watermark. No extra subjects. Keep this reference visually "
        "identical every time it is reused.",
        "",
        f"Reference token: {asset.asset_key}",
        f"Aspect ratio: {aspect}.",
    ]


def _character_image_prompt(asset: ProductionGuideAsset, package: ProductionPackage) -> str:
    entry = _character_bible_entry(asset.character_id or asset.name, package)
    identity = _identity_or_context(asset, entry)
    style_line = _global_style_line(package)
    head = [
        "Create a clean, single-subject CHARACTER REFERENCE SHEET image. This one image is the "
        "absolute identity anchor reused across every clip of this episode, so anatomical clarity "
        "and a memorable, consistent silhouette matter more than dramatic composition.",
        "",
        "CHARACTER IDENTITY:",
        identity,
        "",
        "FACE:",
        "Render the facial features exactly as described in the identity block above; keep bone "
        "structure, eye shape, brow, nose, mouth, and skin tone identical in every reuse so the "
        "character is instantly recognizable.",
        "",
        "HAIR:",
        "Reproduce the hairstyle, hair length, hair colour, and hair texture from the identity "
        "block; keep the hair silhouette unchanged from reference to reference.",
        "",
        "BODY:",
        "Match body proportions, build, posture, and any distinguishing physical traits from the "
        "identity block; keep height and build consistent in every clip.",
        "",
        "COSTUME:",
        "Dress the character in the wardrobe described in the identity block; keep fabrics, "
        "colours, layering, and accessories identical throughout the episode unless the story "
        "explicitly changes them.",
        "",
        "VISUAL STYLE:",
        style_line,
        "",
        "LIGHTING:",
        "Use clean, even, neutral lighting on a simple background so the character reads clearly; "
        "avoid strong colour gels that would shift the character's true colours.",
        "",
        "COMPOSITION:",
        "Show the character in a clean three-quarter view with the full body visible from head to "
        "feet; simple uncluttered background; no landscape, no props, no other characters.",
    ]
    purpose = (
        f"This image is the character identity reference token {asset.asset_key}; attach it "
        f"whenever 「{asset.name}」 appears so the model locks onto one consistent person."
    )
    return "\n".join(head + _tail(asset, purpose))


def _location_image_prompt(asset: ProductionGuideAsset, package: ProductionPackage) -> str:
    entry = _location_bible_entry(asset.location_id or asset.name, package)
    identity = _identity_or_context(asset, entry)
    style_line = _global_style_line(package)
    head = [
        "Create a clean ENVIRONMENT REFERENCE image with no characters and no animals. This one "
        "image locks the spatial layout, lighting, weather, and material texture of one recurring "
        "location for this episode.",
        "",
        "ENVIRONMENT IDENTITY:",
        identity,
        "",
        "SPATIAL LAYOUT:",
        "Fix the geography, depth, key landmarks, and camera-side relationships exactly as "
        "described above; keep this layout stable whenever the location reappears.",
        "",
        "LIGHTING AND WEATHER:",
        "Render the time of day, light direction, ambient mood, and weather described in the "
        "environment identity; keep them consistent across every clip set in this location.",
        "",
        "VISUAL STYLE:",
        style_line,
        "",
        "COMPOSITION:",
        "Frame the location as a wide, readable establishing view that shows the full space; "
        "leave clean negative space where action will later occur.",
    ]
    purpose = (
        f"This image is the environment identity reference token {asset.asset_key}; attach it for "
        f"every clip set in 「{asset.name}」 so the space stays physically consistent."
    )
    return "\n".join(head + _tail(asset, purpose))


def _prop_image_prompt(asset: ProductionGuideAsset, package: ProductionPackage) -> str:
    entry = _prop_bible_entry(asset.name, package)
    identity = _identity_or_context(asset, entry)
    style_line = _global_style_line(package)
    head = [
        "Create a clean single-object PROP REFERENCE image on a simple neutral background. This "
        "image locks the exact appearance of one recurring prop so it never drifts between clips.",
        "",
        "PROP IDENTITY:",
        identity,
        "",
        "MATERIAL AND DETAIL:",
        "Render the shape, surface material, wear, and fine detail described above; keep them "
        "identical in every reuse.",
        "",
        "SCALE:",
        "Present the prop at a clear, readable real-world scale against the neutral background.",
        "",
        "VISUAL STYLE:",
        style_line,
        "",
        "COMPOSITION:",
        "Centre the prop with even lighting and no clutter; the object must read instantly.",
    ]
    purpose = (
        f"This image is the prop reference token {asset.asset_key}; attach it whenever "
        f"「{asset.name}」 appears on screen so its look stays identical."
    )
    return "\n".join(head + _tail(asset, purpose))


def _style_image_prompt(asset: ProductionGuideAsset, package: ProductionPackage) -> str:
    contract = [str(line) for line in asset.compiler_trace.get("fallback_context") or []]
    body = "\n".join(contract) if contract else _global_style_line(package)
    head = [
        "Create a single STYLE REFERENCE frame that establishes the global visual language shared "
        "by every clip of this episode. It carries no specific character and no specific plot "
        "moment; it only fixes colour, texture, light mood, and rendering style.",
        "",
        "GLOBAL VISUAL LANGUAGE:",
        body,
        "",
        "COLOUR GRADE:",
        "Define one dominant palette and contrast curve; apply the same grade to every clip.",
        "",
        "TEXTURE AND RENDERING:",
        "Fix the surface/material rendering and the overall finish so no clip looks like it came "
        "from a different pipeline.",
        "",
        "LIGHTING MOOD:",
        "Establish the default lighting mood that every scene inherits unless the story overrides "
        "it.",
    ]
    purpose = (
        f"This image is the global style reference token {asset.asset_key}; use it to keep the "
        "whole episode visually unified."
    )
    return "\n".join(head + _tail(asset, purpose))


def _image_prompt_for(asset: ProductionGuideAsset, package: ProductionPackage) -> str:
    if asset.asset_type == "character":
        return _character_image_prompt(asset, package)
    if asset.asset_type == "location":
        return _location_image_prompt(asset, package)
    if asset.asset_type == "prop":
        return _prop_image_prompt(asset, package)
    return _style_image_prompt(asset, package)


def build_reference_asset_prompts(
    assets: list[ProductionGuideAsset],
    production_package: ProductionPackage,
    profile: VideoModelProfile,
) -> list[ProductionGuideAsset]:
    """Fill a COMPLETE copy-ready image prompt for every character/location/
    prop/style asset and mark it ``PROMPT_READY``.

    ``reference_frame`` assets keep their capture instruction (they are not
    image prompts). Status never becomes ``BOUND`` because no real file
    exists at this layer. Assets lacking a bible entry still get a complete
    prompt built from the best available context — never a placeholder.
    """
    result: list[ProductionGuideAsset] = []
    for asset in assets:
        if asset.asset_type == "reference_frame":
            result.append(asset)
            continue
        prompt = _image_prompt_for(asset, production_package)
        _ensure_no_placeholders(prompt)
        result.append(
            asset.model_copy(
                update={"generation_prompt": prompt, "status": "PROMPT_READY"}
            )
        )
    return result


# ----------------------------------------------------------------------
# Section builders (pure)
# ----------------------------------------------------------------------
def build_subtitle_plan(production_package: ProductionPackage) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for entry in production_package.subtitle_track:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or entry.get("content") or "").strip()
        time_label = str(entry.get("time") or entry.get("time_range") or "").strip()
        if not time_label:
            start = entry.get("start_time", entry.get("start"))
            end = entry.get("end_time", entry.get("end"))
            if start is not None and end is not None:
                time_label = f"{_fmt_clock(_as_float(start))}–{_fmt_clock(_as_float(end))}"
        speaker = str(entry.get("speaker") or "").strip()
        position = str(entry.get("position") or "底部居中").strip()
        if not text and not time_label:
            continue
        plan.append(
            {"time": time_label, "text": text, "speaker": speaker, "position": position}
        )
    return plan


def build_screen_composite_plan(
    clips: list[GenerationClip], production_package: ProductionPackage
) -> list[dict[str, Any]]:
    """Flag clips whose action/dialogue implies on-screen text.

    Distinguishes diegetic text that must be legible inside the generated
    frame (phone/email/screen/SMS) from non-diegetic UI overlays that should
    be composited in post (subtitles/notifications/timestamps) to avoid the
    model producing garbled glyphs.
    """
    shots = _shots_by_number(production_package)
    plan: list[dict[str, Any]] = []
    for clip in _sorted_clips(clips):
        haystack = " ".join(
            [clip.purpose, clip.visual_intent]
            + [str(entry.get("line", "")) for entry in clip.dialogue]
            + [
                f"{shots[number].action} {shots[number].dialogue}"
                for number in clip.source_shot_numbers
                if number in shots
            ]
        ).lower()
        diegetic = sorted({kw for kw in _SCREEN_DIEGETIC if kw.lower() in haystack})
        overlay = sorted({kw for kw in _SCREEN_OVERLAY if kw.lower() in haystack})
        if not diegetic and not overlay:
            continue
        if diegetic:
            category = "画面真实可读文本"
            recommendation = (
                "该镜头包含需在画面内出现的可读文本。优先在生成时保证清晰可读；若模型无法稳定"
                "生成文字，则让画面留出干净区域，改由后期贴图/替换为真实可读文本，避免出现乱码。"
            )
        else:
            category = "后期 UI 叠加"
            recommendation = (
                "该镜头涉及非叙事内的界面元素（字幕/通知/时间戳等）。不要依赖视频模型直接生成，"
                "统一在剪辑软件中以后期 UI 叠加，保证字体、位置、时长可控且无乱码。"
            )
        plan.append(
            {
                "clip_number": clip.clip_number,
                "matched": diegetic + overlay,
                "category": category,
                "recommendation": recommendation,
            }
        )
    return plan


def build_dialogue_plan(
    clips: list[GenerationClip], profile: VideoModelProfile
) -> list[dict[str, Any]]:
    native = profile.supports_dialogue is True
    strategy = "native" if native else "post_dub"
    note = (
        "目标模型支持原生对白，可直接在视频 Prompt 内驱动口型与语音。"
        if native
        else "目标模型未确认原生对白能力，按后期配音处理：视频只保留口型/环境声，台词在后期对齐。"
    )
    plan: list[dict[str, Any]] = []
    for clip in _sorted_clips(clips):
        for entry in clip.dialogue:
            speaker = str(entry.get("speaker") or "").strip()
            line = str(entry.get("line") or "").strip()
            if not line:
                continue
            plan.append(
                {
                    "clip_number": clip.clip_number,
                    "speaker": speaker,
                    "line": line,
                    "strategy": strategy,
                    "note": note,
                }
            )
    return plan


def build_sound_plan(
    production_package: ProductionPackage, clips: list[GenerationClip]
) -> list[dict[str, Any]]:
    sfx_by_clip: dict[int, list[str]] = {}
    for entry in production_package.sound_effect_plan:
        if not isinstance(entry, dict):
            continue
        clip_number = entry.get("clip_number", entry.get("clip"))
        description = str(
            entry.get("effect") or entry.get("description") or entry.get("name") or ""
        ).strip()
        if clip_number is None or not description:
            continue
        sfx_by_clip.setdefault(int(_as_float(clip_number, -1)), []).append(description)
    plan: list[dict[str, Any]] = []
    for clip in _sorted_clips(clips):
        environment = [item.strip() for item in clip.audio_intent if item.strip()]
        key_sfx = sfx_by_clip.get(clip.clip_number, [])
        plan.append(
            {
                "clip_number": clip.clip_number,
                "environment": environment,
                "key_sfx": key_sfx,
                "transition": "与相邻 Clip 的环境声做 0.3–0.8 秒 audio crossfade，声音不要硬切。",
            }
        )
    return plan


def build_bgm_plan(
    production_package: ProductionPackage, clips: list[GenerationClip]
) -> dict[str, Any]:
    ranges, total = _clip_time_ranges(clips)
    direction = production_package.bgm_direction.strip()
    segments: list[dict[str, Any]] = []
    emotion_arc: list[str] = []
    lines = [
        f"Create ONE continuous {total:.0f}-second instrumental background music track for this "
        "episode. It is a single unified piece, not six separate cues.",
        "",
    ]
    if direction:
        lines += [f"Overall music direction: {direction}", ""]
    lines += [
        "Keep one consistent instrumentation and one continuous emotional arc from the first "
        "frame to the last; the music must never restart or change character at a clip boundary.",
        "",
        "STRUCTURE:",
    ]
    for number, start, end in ranges:
        clip = next((c for c in clips if c.clip_number == number), None)
        beat = (clip.purpose.strip() if clip and clip.purpose.strip() else "on-screen action")
        lines.append(
            f"{_fmt_clock(start)}–{_fmt_clock(end)} (Clip {number:02d}): underscore {beat}."
        )
        segments.append(
            {"clip_number": number, "time": f"{_fmt_clock(start)}–{_fmt_clock(end)}", "beat": beat}
        )
        emotion_arc.append(beat)
    lines += [
        "",
        "End naturally and quietly, leaving room for the closing image and any final subtitle.",
        "No vocals. No choir. No abrupt trailer-style percussion hits at clip boundaries.",
    ]
    return {
        "total_duration_seconds": total,
        "prompt": "\n".join(lines),
        "segments": segments,
        "emotion_arc": emotion_arc,
        "note": "每 Clip 不单独生成 BGM，整集统一铺一条，从 00:00 连续播放到片尾。",
    }


def build_editing_plan(
    production_package: ProductionPackage, clips: list[GenerationClip]
) -> dict[str, Any]:
    ordered = _sorted_clips(clips)
    chain = build_frame_chain(ordered)
    transitions: list[dict[str, Any]] = []
    for index in range(len(ordered) - 1):
        current = ordered[index]
        nxt = ordered[index + 1]
        info = chain["clips"].get(str(current.clip_number), {})
        carries = bool(info.get("produces_next_start_frame"))
        same_scene = (
            current.scene_number is not None and current.scene_number == nxt.scene_number
        )
        if carries and same_scene:
            cut_type = "硬切 CUT"
            reason = "尾帧即下一镜首帧，视觉状态本就连续，硬切最干净。"
        elif carries:
            cut_type = "硬切 CUT（如有轻微跳动，做 3–6 帧短叠化）"
            reason = "画面物理连续，但跨场景时允许极短叠化缓冲；不要做 1 秒叠化。"
        else:
            cut_type = "短叠化 3–6 帧（约 0.10–0.20 秒）"
            reason = "无尾帧承接，用极短叠化过渡，避免生硬跳变。"
        transitions.append(
            {
                "from_clip": current.clip_number,
                "to_clip": nxt.clip_number,
                "type": cut_type,
                "reason": reason,
            }
        )
    return {
        "order": [clip.clip_number for clip in ordered],
        "transitions": transitions,
        "audio_crossfade": "每段环境声之间做 0.3–0.8 秒 audio crossfade。",
        "bgm": "整条 BGM 从 00:00 连续铺到片尾，不跟着 Clip 切开。",
        "rules": [
            "画面优先硬切；只有轻微跳动才补 3–6 帧短叠化，绝不默认 1 秒叠化。",
            "声音不硬切：环境声之间统一 0.3–0.8 秒交叉淡入淡出。",
            "BGM 保持一条连续音轨，最大程度削弱“多段 AI 视频拼接”的观感。",
        ],
    }


def build_final_checklist(
    production_package: ProductionPackage,
    prompt_package: ModelPromptPackage,
    required_assets: list[ProductionGuideAsset],
    clips: list[GenerationClip],
    subtitle_plan: list[dict[str, Any]],
    dialogue_plan: list[dict[str, Any]],
    bgm_plan: dict[str, Any],
) -> list[str]:
    char_count = sum(1 for a in required_assets if a.asset_type == "character")
    loc_count = sum(1 for a in required_assets if a.asset_type == "location")
    prop_count = sum(1 for a in required_assets if a.asset_type == "prop")
    frame_count = sum(1 for a in required_assets if a.asset_type == "reference_frame")
    items = [
        f"□ 人物身份一致：{char_count} 个角色母图在所有 Clip 中长相/身形/服装保持同一人。",
        f"□ 空间一致：{loc_count} 个场景母图的空间布局、光线、材质跨镜不漂移。",
        f"□ 道具一致：{prop_count} 个关键道具的形状/材质/颜色每次出现都相同。",
        f"□ 尾帧链完整：{frame_count} 个接续帧按 FRAME_NN 命名，上一镜尾帧=下一镜首帧。",
        "□ 屏幕文本无乱码：所有画面内可读文本清晰，或已改为后期贴图/UI 叠加。",
    ]
    if subtitle_plan:
        items.append(f"□ 字幕时间轴：{len(subtitle_plan)} 条字幕对齐台词/画面，保留必要留白区。")
    else:
        items.append("□ 字幕时间轴：确认本集是否需要字幕，如需要则补齐并对齐。")
    if dialogue_plan:
        native = dialogue_plan[0]["strategy"] == "native"
        mode_text = "模型原生对白口型对齐" if native else "后期配音与口型对齐"
        items.append(f"□ 对白音频：{len(dialogue_plan)} 条台词按{mode_text}完成。")
    else:
        items.append("□ 对白音频：本集无台词，确认环境声与表演足够叙事。")
    items.append(
        f"□ BGM 连续：整集约 {bgm_plan.get('total_duration_seconds', 0):.0f} 秒铺一条统一配乐，"
        "不在 Clip 边界断开。"
    )
    items.append(
        f"□ 成片交付：{len(clips)} 个 Clip 按顺序硬切/短叠化拼接，环境声 0.3–0.8 秒交叉淡化，"
        "目标模型 "
        f"{prompt_package.target_video_model_display_name or prompt_package.target_profile_id}"
        f"，画幅 {prompt_package.aspect_ratio}。"
    )
    return items


# ----------------------------------------------------------------------
# Model capability -> operational instructions
# ----------------------------------------------------------------------
def _model_operation_lines(
    profile: VideoModelProfile, prompt_package: ModelPromptPackage
) -> list[str]:
    """Translate profile capabilities into OPERATIONAL shooting instructions
    (never capability JSON / raw flags)."""
    lines: list[str] = []
    modes = [_MODE_LABELS.get(mode, mode) for mode in profile.supported_modes] or ["文生视频"]
    lines.append(f"推荐生成模式：优先使用 {'、'.join(modes)}。")
    if profile.supports_image_to_video is True:
        lines.append("图生视频可用：把角色/场景母图与上一镜尾帧一并挂入参考图，一致性最稳。")
    else:
        lines.append("图生视频未确认：以文字描述为主锚点，母图仅作风格与身份的主观参照。")
    syntax = profile.reference_prompt_syntax
    if syntax == "named_reference":
        lines.append(
            "参考素材调用：在 Prompt 内用 @命名 引用参考图，本手册的 asset_key 即命名 token。"
        )
    elif syntax == "external_reference_only":
        lines.append(
            "参考素材调用：该平台不支持 Prompt 内命名引用，"
            "需先经平台上传入口挂载参考图，再提交 Prompt。"
        )
    else:
        lines.append("参考素材调用：以文字锚点描述身份与场景，同时把对应母图作为参考图一并挂入。")
    if profile.supports_first_frame is True:
        lines.append("首帧驱动：用上一镜尾帧作为本镜首帧，实现物理连续。")
    elif profile.supports_first_frame is False:
        lines.append("首帧驱动未确认：改用文字承接上一镜收尾状态，并把尾帧作为主观参照图带上。")
    lines.append(
        "连续性方法：上一镜尾帧 = 下一镜首帧（FRAME_NN 链），并全程复用同一批角色/场景母图。"
    )
    if profile.supports_dialogue is True:
        lines.append("对白策略：模型支持原生对白，可在 Prompt 内直接驱动台词与口型。")
    elif profile.supports_dialogue is False:
        lines.append("对白策略：模型不支持原生对白，台词按后期配音处理，视频只保留口型与环境声。")
    else:
        lines.append("对白策略：模型对白能力未确认，保守按后期配音处理。")
    if profile.supports_audio is False:
        lines.append("音频策略：模型不产音频，环境声/音效/BGM 全部在后期铺设。")
    if profile.duration_min_seconds is not None or profile.duration_max_seconds is not None:
        low = profile.duration_min_seconds
        high = profile.duration_max_seconds
        span = f"{low:g}–{high:g} 秒" if low is not None and high is not None else (
            f"最长 {high:g} 秒" if high is not None else f"最短 {low:g} 秒"
        )
        lines.append(f"单镜时长限制：{span}；超时请拆成多个 Clip 再用尾帧链承接。")
    lines.append(f"目标画幅：{prompt_package.aspect_ratio}；整集统一，不要逐镜改画幅。")
    return lines


def _base_params(profile: VideoModelProfile, prompt_package: ModelPromptPackage) -> dict[str, Any]:
    durations: str
    if profile.supported_durations_seconds:
        durations = "、".join(f"{value:g}s" for value in profile.supported_durations_seconds)
    elif profile.duration_min_seconds is not None and profile.duration_max_seconds is not None:
        durations = f"{profile.duration_min_seconds:g}–{profile.duration_max_seconds:g} 秒"
    else:
        durations = "按平台默认"
    return {
        "target_model": profile.display_name,
        "vendor": profile.vendor,
        "aspect_ratio": prompt_package.aspect_ratio,
        "supported_aspect_ratios": "、".join(profile.supported_aspect_ratios) or "按平台默认",
        "duration": durations,
        "prompt_language": prompt_package.prompt_language,
    }


# ----------------------------------------------------------------------
# Enrichment merge (LLM-refined rows -> guide assets / clips)
# ----------------------------------------------------------------------
def apply_asset_prompt_enrichment(
    guide_assets: list[ProductionGuideAsset], enriched_rows: list[dict[str, Any]]
) -> list[ProductionGuideAsset]:
    """Merge refined ``generation_prompt`` rows by asset_key (and/or id).

    Rows with an empty prompt or any placeholder phrase are rejected (the
    deterministic baseline stays). Merged assets are tagged
    ``provenance='llm_refined'``. Inputs are never mutated.
    """
    by_key: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for row in enriched_rows:
        if not isinstance(row, dict):
            continue
        prompt = str(row.get("generation_prompt") or "").strip()
        if not prompt or _contains_placeholder(prompt):
            continue
        key = str(row.get("asset_key") or "").strip()
        asset_id = str(row.get("id") or "").strip()
        if key:
            by_key[key] = prompt
        if asset_id:
            by_id[asset_id] = prompt
    result: list[ProductionGuideAsset] = []
    for asset in guide_assets:
        refined = by_key.get(asset.asset_key) or by_id.get(asset.id)
        if refined and refined != asset.generation_prompt:
            result.append(
                asset.model_copy(
                    update={"generation_prompt": refined, "provenance": "llm_refined"}
                )
            )
        else:
            result.append(asset.model_copy())
    return result


def apply_clip_prompt_enrichment(
    clips: list[GenerationClip], enriched_rows: list[dict[str, Any]]
) -> list[GenerationClip]:
    """Merge refined ``copy_ready_prompt`` rows by clip_id.

    Same placeholder/empty rejection as the asset variant. Returns a NEW list
    of copied clips; the inputs are never mutated.
    """
    by_id: dict[str, str] = {}
    for row in enriched_rows:
        if not isinstance(row, dict):
            continue
        prompt = str(row.get("copy_ready_prompt") or "").strip()
        clip_id = str(row.get("clip_id") or "").strip()
        if not prompt or not clip_id or _contains_placeholder(prompt):
            continue
        by_id[clip_id] = prompt
    result: list[GenerationClip] = []
    for clip in clips:
        refined = by_id.get(clip.id)
        if refined and refined != clip.copy_ready_prompt:
            result.append(clip.model_copy(update={"copy_ready_prompt": refined}))
        else:
            result.append(clip.model_copy())
    return result


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------
def build_executable_video_production_guide(
    production_package: ProductionPackage,
    prompt_package: ModelPromptPackage,
    profile: VideoModelProfile,
    assets: list[ProductionAsset],
    project_id: str,
    options: dict[str, Any] | None = None,
) -> ExecutableVideoProductionGuide:
    """Assemble the complete, deterministic production handbook.

    analyze -> reference prompts -> frame chain -> per-clip copy-ready prompts
    (via :func:`compile_copy_ready_prompt`, propagating
    SHOOTING_LOCATION_CONTEXT_MISSING) -> section builders -> overview ->
    markdown. Identical inputs (with a fixed ``options['created_at']``) yield
    byte-identical markdown.
    """
    opts = dict(options or {})
    required_assets = build_reference_asset_prompts(
        analyze_asset_necessity(production_package, prompt_package, profile),
        production_package,
        profile,
    )
    clips = [clip.model_copy(deep=True) for clip in _sorted_clips(prompt_package.clips)]
    assets_by_id = {asset.id: asset for asset in assets}
    chain = build_frame_chain(clips)
    frame_plans = chain["frame_plans"]

    compiled_clips: list[GenerationClip] = []
    for clip in clips:
        frame_plan = dict(frame_plans.get(str(clip.clip_number), {}))
        trace: dict[str, Any] = {}
        copy_ready = compile_copy_ready_prompt(
            clip, profile, production_package, assets_by_id, frame_plan, trace
        )
        _ensure_no_placeholders(copy_ready)
        merged_trace = {**clip.compiler_trace, "copy_ready": trace}
        compiled_clips.append(
            clip.model_copy(
                update={"copy_ready_prompt": copy_ready, "compiler_trace": merged_trace}
            )
        )

    subtitle_plan = build_subtitle_plan(production_package)
    screen_composite_plan = build_screen_composite_plan(compiled_clips, production_package)
    dialogue_plan = build_dialogue_plan(compiled_clips, profile)
    sound_plan = build_sound_plan(production_package, compiled_clips)
    bgm_plan = build_bgm_plan(production_package, compiled_clips)
    editing_plan = build_editing_plan(production_package, compiled_clips)
    final_checklist = build_final_checklist(
        production_package,
        prompt_package,
        required_assets,
        compiled_clips,
        subtitle_plan,
        dialogue_plan,
        bgm_plan,
    )

    _, total_duration = _clip_time_ranges(compiled_clips)
    asset_counts: dict[str, int] = {}
    for asset in required_assets:
        asset_counts[asset.asset_type] = asset_counts.get(asset.asset_type, 0) + 1
    episode_number = prompt_package.episode_number
    episode_title = str(opts.get("episode_title") or f"EP{episode_number:02d}")
    shots_map = _shots_by_number(production_package)
    shot_narrative: dict[str, str] = {}
    for clip in compiled_clips:
        bits: list[str] = []
        for number in clip.source_shot_numbers:
            shot = shots_map.get(number)
            if shot is None:
                continue
            action = shot.action.strip() or shot.visual_intent.strip()
            bits.append(f"分镜{number}：{action}" if action else f"分镜{number}")
        shot_narrative[str(clip.clip_number)] = "；".join(bits) or "（本镜无额外分镜文本）"
    overview: dict[str, Any] = {
        "episode_title": episode_title,
        "episode_number": episode_number,
        "target_model": profile.display_name,
        "target_profile_id": profile.id,
        "aspect_ratio": prompt_package.aspect_ratio,
        "total_duration_seconds": total_duration,
        "clip_count": len(compiled_clips),
        "asset_counts": asset_counts,
        "prompt_language": prompt_package.prompt_language,
        "shot_narrative": shot_narrative,
        "operation_notes": _model_operation_lines(profile, prompt_package),
        "base_params": _base_params(profile, prompt_package),
    }
    continuity_workflow = {
        "anchors": [
            {
                "name": "Character Master",
                "role": "身份锚点",
                "desc": "锁定角色的长相、身形与服装，跨镜复用同一张设定图。",
            },
            {
                "name": "Environment Master",
                "role": "空间锚点",
                "desc": "锁定场景的空间布局、光线与材质，跨镜复用同一张环境图。",
            },
            {
                "name": "Previous End Frame",
                "role": "物理连续性锚点",
                "desc": "上一镜尾帧作为下一镜首帧，保证动作、机位与光影的物理连续。",
            },
        ],
        "diagram": list(chain["diagram"]),
    }

    created_value = opts.get("created_at")
    created_at = created_value if isinstance(created_value, datetime) else datetime.now(UTC)

    guide = ExecutableVideoProductionGuide(
        project_id=project_id,
        episode_number=episode_number,
        production_package_id=prompt_package.production_package_id,
        prompt_package_id=prompt_package.id,
        episode_version_id=prompt_package.episode_version_id,
        target_profile_id=profile.id,
        target_profile_version=profile.profile_version,
        target_video_model_display_name=profile.display_name,
        aspect_ratio=prompt_package.aspect_ratio,
        prompt_language=prompt_package.prompt_language,
        status="ready",
        title=f"EP{episode_number:02d} AI视频完整制作手册",
        overview=overview,
        required_assets=required_assets,
        clip_workflows=compiled_clips,
        continuity_workflow=continuity_workflow,
        frame_chain=chain,
        subtitle_plan=subtitle_plan,
        screen_composite_plan=screen_composite_plan,
        dialogue_plan=dialogue_plan,
        sound_plan=sound_plan,
        bgm_plan=bgm_plan,
        editing_plan=editing_plan,
        final_checklist=final_checklist,
        created_at=created_at,
    )
    markdown = render_guide_markdown(guide)
    _ensure_no_placeholders(markdown)
    guide.markdown_document = markdown
    guide.runtime_trace = {
        "asset_count": len(required_assets),
        "clip_count": len(compiled_clips),
        "total_duration_seconds": total_duration,
        "sections": list(_SECTION_TITLES),
        "deterministic": True,
    }
    return guide


# ----------------------------------------------------------------------
# Markdown handbook renderer
# ----------------------------------------------------------------------
_SECTION_TITLES: dict[str, str] = {
    "goal": "一、制作目标",
    "model": "二、目标视频模型与基础参数",
    "permanent_assets": "三、需要提前准备的永久参考素材",
    "episode_assets": "四、本集专用场景/道具素材",
    "overview": "五、本集视频结构总览",
    "clip_work_orders": "六、逐 Clip 工单",
    "continuity": "七、尾帧接续流程",
    "subtitles": "八、字幕时间轴",
    "dialogue_sound": "九、对白与音效时间轴（含画面合成建议）",
    "bgm": "十、BGM",
    "editing": "十一、剪辑与转场",
    "final_order": "十二、最终成片顺序",
    "checklist": "十三、最终检查清单",
}

_ASSET_TYPE_LABELS: dict[str, str] = {
    "character": "角色母图（身份锚点）",
    "location": "场景母图（空间锚点）",
    "prop": "道具参考图",
    "style": "全集风格基准图",
    "reference_frame": "尾帧捕获指令（不是图片）",
}


def _cell(value: Any) -> str:
    """Markdown-table-safe single-line cell text (never empty)."""
    text = str(value or "").replace("|", "／").replace("\r", " ").replace("\n", "；")
    return text.strip() or "—"


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _fence(body: str) -> list[str]:
    """Fenced ```text block; widen the fence when the body itself fences."""
    opener, closer = ("````text", "````") if "```" in body else ("```text", "```")
    return [opener, body.rstrip(), closer]


def _asset_key_index(assets: list[ProductionGuideAsset]) -> dict[str, str]:
    """Deterministic asset_key lookup by character/location/prop identity.

    Only user-facing ``asset_key`` tokens are indexed — internal ids never
    reach the markdown.
    """
    index: dict[str, str] = {}
    for asset in assets:
        if asset.asset_type == "character" and asset.character_id:
            index[f"character:{asset.character_id}"] = asset.asset_key
        elif asset.asset_type == "location" and asset.location_id:
            index[f"location:{asset.location_id}"] = asset.asset_key
        elif asset.asset_type == "prop":
            index[f"prop:{asset.name.casefold()}"] = asset.asset_key
    return index


def _clip_asset_keys(
    clip: GenerationClip, index: dict[str, str], start_frame: str | None
) -> list[str]:
    """User-facing asset_key tokens one clip consumes (start frame first)."""
    keys: list[str] = []
    if start_frame:
        keys.append(f"@{start_frame}")
    for character_id in clip.character_ids:
        key = index.get(f"character:{character_id}")
        if key and key not in keys:
            keys.append(key)
    if clip.location:
        location_key = index.get(f"location:{clip.location}")
        if location_key and location_key not in keys:
            keys.append(location_key)
    for prop_id in clip.prop_ids:
        key = index.get(f"prop:{prop_id.casefold()}")
        if key and key not in keys:
            keys.append(key)
    return keys


def _clip_mode_label(start_frame: str | None) -> str:
    """Operational generation mode for one clip (derived from the chain)."""
    return "图生视频（Start Frame 承接）" if start_frame else "文生视频（母图参考）"


def _asset_block(section_number: int, position: int, asset: ProductionGuideAsset) -> list[str]:
    """One reference-asset block: 用途/必需/范围/状态/fenced prompt/注意事项."""
    type_label = _ASSET_TYPE_LABELS.get(asset.asset_type, asset.asset_type)
    prompt_label = (
        "尾帧捕获指令（Clip 生成完成后执行，不是图片生成）："
        if asset.asset_type == "reference_frame"
        else "完整图片生成 Prompt（直接复制使用）："
    )
    lines = [
        f"### {section_number}.{position} {asset.asset_key} · {type_label}",
        "",
        f"- 名称：{asset.name}",
        f"- 用途：{_cell(asset.purpose)}",
        f"- 是否必需：{_NECESSITY_LABELS.get(asset.necessity, asset.necessity)}",
        f"- 使用范围：{_SCOPE_LABELS.get(asset.reuse_scope, asset.reuse_scope)}",
        f"- 状态：{_STATUS_LABELS.get(asset.status, asset.status)}",
        f"- 保存命名：{asset.asset_key}",
        "",
        prompt_label,
        "",
    ]
    lines.extend(_fence(asset.generation_prompt or asset.purpose or asset.name))
    lines.append("")
    if asset.notes:
        for note in asset.notes:
            lines.append(f"- 注意事项：{_cell(note)}")
        lines.append("")
    return lines


def _clip_work_order_block(
    clip: GenerationClip,
    info: dict[str, Any],
    narrative: str,
    index: dict[str, str],
    display_name: str,
    start: str,
    end: str,
) -> list[str]:
    """One per-clip work order: 使用素材, ONE fenced copy-ready prompt, then
    the tail-frame follow-up actions (window / criteria / save name / usage)."""
    raw_start = info.get("start_frame")
    start_frame = str(raw_start) if raw_start else None
    produces = bool(info.get("produces_next_start_frame"))
    keys = _clip_asset_keys(clip, index, start_frame)
    char_keys = [key for key in keys if key.startswith("@CHAR_")]
    loc_keys = [key for key in keys if key.startswith("@LOC_")]
    prop_keys = [key for key in keys if key.startswith("@PROP_")]
    criteria = [str(item) for item in list(info.get("criteria") or [])]
    saved = str(info.get("end_frame_saved") or "")
    if produces and saved.startswith("FRAME_"):
        save_line = f"{saved}，用作下一镜（Clip {clip.clip_number + 1:02d}）首帧"
    else:
        save_line = "无需保存接续帧（全集收尾，直接进入后期）"
    lines = [
        f"### Clip {clip.clip_number:02d} · {start}–{end}",
        "",
        f"- 来源分镜：{_cell(narrative)}",
        f"- 核心内容：{_cell(clip.purpose or clip.visual_intent)}",
        f"- 时长：{max(0.0, clip.duration_seconds):g} 秒",
        f"- 生成模式：{_clip_mode_label(start_frame)}",
        "- Start Frame："
        + (
            f"@{start_frame}（上一镜尾帧，物理连续性锚点）"
            if start_frame
            else "无（首镜自由开场）"
        ),
        "- 身份锚点（Character Master）："
        + ("、".join(char_keys) if char_keys else "（本镜无角色参考）"),
        "- 空间锚点（Environment Master）："
        + ("、".join(loc_keys) if loc_keys else "（本镜无场景参考）"),
        "- 道具参考（Prop）：" + ("、".join(prop_keys) if prop_keys else "（无）"),
        "",
        (
            f"操作：挂好上列参考素材，把下方 Prompt 原样复制到「{display_name}」生成本镜"
            "（STRICT 部分不要改动）："
        ),
        "",
    ]
    lines.extend(_fence(clip.copy_ready_prompt))
    lines += [
        "",
        "生成后操作（尾帧）：",
        f"- 挑选区间：{_cell(info.get('selection_window'))}",
        f"- 挑选标准：{'；'.join(criteria)}",
        f"- 保存命名：{save_line}",
        "",
    ]
    return lines


def render_guide_markdown(guide: ExecutableVideoProductionGuide) -> str:
    """Render the complete continuous handbook (deterministic, idempotent).

    Chinese narration + English fenced prompt blocks; the model's capabilities
    appear as operational instructions only (never capability JSON/flags);
    source shots appear as narrative text; the only tokens that leak through
    are user-facing ``asset_key`` strings and ``FRAME_NN`` labels. Built as a
    single parts list joined once — no timestamps, no json.dumps, so identical
    guide inputs yield byte-identical markdown.
    """
    overview = dict(guide.overview)
    assets = guide.required_assets
    clips = _sorted_clips(guide.clip_workflows)
    index = _asset_key_index(assets)
    ranges, total_duration = _clip_time_ranges(clips)
    range_by_clip = {number: (start, end) for number, start, end in ranges}
    chain_clips: dict[str, Any] = dict(guide.frame_chain.get("clips") or {})
    shot_narrative = {
        str(key): str(value)
        for key, value in dict(overview.get("shot_narrative") or {}).items()
    }
    anchors = [
        dict(anchor)
        for anchor in list(guide.continuity_workflow.get("anchors") or [])
        if isinstance(anchor, dict)
    ]
    display_name = guide.target_video_model_display_name or str(
        overview.get("target_model") or ""
    )
    asset_counts = dict(overview.get("asset_counts") or {})
    char_count = int(asset_counts.get("character", 0))
    loc_count = int(asset_counts.get("location", 0))
    prop_count = int(asset_counts.get("prop", 0))
    style_count = int(asset_counts.get("style", 0))
    frame_count = int(asset_counts.get("reference_frame", 0))
    base_params = dict(overview.get("base_params") or {})
    operation_notes = [
        str(line)
        for line in list(overview.get("operation_notes") or [])
        if str(line).strip()
    ]
    editing = dict(guide.editing_plan)

    parts: list[str] = [f"# {guide.title}", ""]
    parts.append(
        "> 本手册是可直接执行的拍摄工单：先备齐参考素材，再逐 Clip 复制 Prompt 生成视频，"
        "最后按后期计划合成成片。"
    )
    parts.append("")
    parts.append(
        f"**目标模型**：{display_name} ｜ **画幅**：{guide.aspect_ratio}"
        f" ｜ **总时长**：{_fmt_clock(total_duration)} ｜ **Clip 数**：{len(clips)}"
    )
    parts.append("")

    # 一、制作目标 ------------------------------------------------------
    episode_title = str(overview.get("episode_title") or f"EP{guide.episode_number:02d}")
    parts += [
        f"## {_SECTION_TITLES['goal']}",
        "",
        (
            f"本集目标：用「{display_name}」生成 EP{guide.episode_number:02d}"
            f"「{episode_title}」整集视频——共 {len(clips)} 个 Clip、约 "
            f"{_fmt_clock(total_duration)}，画幅 {guide.aspect_ratio}。"
        ),
        "",
        "执行顺序是「先素材、后视频、再后期」：",
        "",
        (
            f"1. 备料：生成第三节、第四节列出的全部参考素材并按 asset_key 命名归档"
            f"（角色母图 {char_count} 张、场景母图 {loc_count} 张、道具参考 {prop_count} 张、"
            f"风格基准 {style_count} 张、尾帧捕获指令 {frame_count} 条）。"
        ),
        "2. 逐镜生成：按第六节工单顺序逐 Clip 复制 Prompt 生成，上一镜尾帧 = 下一镜首帧。",
        "3. 后期：按第八～十一节完成字幕、对白配音、音效铺设与 BGM。",
        "4. 合成：按第十二节顺序拼接成片，导出前对照第十三节检查清单逐项验收。",
        "",
    ]

    # 二、目标视频模型与基础参数 ----------------------------------------
    param_rows = [
        ["目标模型", _cell(base_params.get("target_model") or display_name)],
        ["厂商", _cell(base_params.get("vendor"))],
        ["目标画幅", _cell(base_params.get("aspect_ratio") or guide.aspect_ratio)],
        ["支持画幅", _cell(base_params.get("supported_aspect_ratios"))],
        ["单镜时长", _cell(base_params.get("duration"))],
        ["Prompt 语言", _cell(base_params.get("prompt_language") or guide.prompt_language)],
    ]
    parts += [
        f"## {_SECTION_TITLES['model']}",
        "",
        "基础参数（整集固定，不要逐镜更改）：",
        "",
    ]
    parts += _md_table(["参数", "取值"], param_rows)
    parts += ["", "操作要点（把模型能力翻译成拍摄动作，逐条执行）：", ""]
    if operation_notes:
        parts += [f"- {line}" for line in operation_notes]
    else:
        parts.append("- 按平台默认方式生成，Prompt 内不引用本平台不支持的能力。")
    parts.append("")

    # 三、永久参考素材 --------------------------------------------------
    permanent = [asset for asset in assets if asset.asset_type in ("character", "style")]
    parts += [
        f"## {_SECTION_TITLES['permanent_assets']}",
        "",
        (
            "本节是「永久身份锚点」素材：角色母图锁定人物长相，风格基准图锁定整集画风，"
            "全集乃至后续各集复用同一张图。请先全部生成并按 asset_key 命名归档，"
            "再开始生成视频。"
        ),
        "",
    ]
    if permanent:
        for position, asset in enumerate(permanent, start=1):
            parts += _asset_block(3, position, asset)
    else:
        parts += ["本集未识别到需预生成的永久参考素材。", ""]

    # 四、本集专用素材 --------------------------------------------------
    episode_assets = [
        asset
        for asset in assets
        if asset.asset_type in ("location", "prop", "reference_frame")
    ]
    parts += [
        f"## {_SECTION_TITLES['episode_assets']}",
        "",
        (
            "本节素材只服务本集：场景母图锁定空间（Environment Master），"
            "道具参考图锁定关键物件；reference_frame 条目不是图片，"
            "而是每条 Clip 生成完成后的尾帧捕获指令。"
        ),
        "",
    ]
    if episode_assets:
        for position, asset in enumerate(episode_assets, start=1):
            parts += _asset_block(4, position, asset)
    else:
        parts += ["本集未识别到专用场景/道具素材。", ""]

    # 五、结构总览 -------------------------------------------------------
    overview_rows: list[list[str]] = []
    for clip in clips:
        start, end = range_by_clip.get(clip.clip_number, (0.0, 0.0))
        info = dict(chain_clips.get(str(clip.clip_number)) or {})
        raw_start = info.get("start_frame")
        start_frame = str(raw_start) if raw_start else None
        keys = _clip_asset_keys(clip, index, start_frame)
        join_label = "承接 " + str(start_frame) if start_frame else "首镜开场"
        tail_label = (
            f"；收尾存 {info.get('end_frame_saved')}"
            if info.get("produces_next_start_frame")
            else "；收尾不存帧"
        )
        overview_rows.append(
            [
                f"Clip {clip.clip_number:02d}",
                f"{_fmt_clock(start)}–{_fmt_clock(end)}",
                _cell(shot_narrative.get(str(clip.clip_number), "—")),
                _cell(clip.purpose or clip.visual_intent),
                _clip_mode_label(start_frame),
                "、".join(keys) if keys else "—",
                join_label + tail_label,
            ]
        )
    parts += [
        f"## {_SECTION_TITLES['overview']}",
        "",
        f"全集共 {len(clips)} 个 Clip，总时长约 {_fmt_clock(total_duration)}，"
        "按以下顺序生成与拼接：",
        "",
    ]
    parts += _md_table(
        ["Clip", "时间", "来源分镜", "核心内容", "模式", "使用素材", "接续方式"],
        overview_rows,
    )
    parts.append("")

    # 六、逐 Clip 工单 ---------------------------------------------------
    parts += [
        f"## {_SECTION_TITLES['clip_work_orders']}",
        "",
        (
            "按顺序逐镜生成。每个工单动作固定：挂参考图 → 原样复制 Prompt → 生成 → "
            "按「生成后操作」挑选尾帧并保存 → 进入下一镜。"
        ),
        "",
    ]
    for clip in clips:
        start, end = range_by_clip.get(clip.clip_number, (0.0, 0.0))
        info = dict(chain_clips.get(str(clip.clip_number)) or {})
        parts += _clip_work_order_block(
            clip,
            info,
            shot_narrative.get(str(clip.clip_number), "—"),
            index,
            display_name,
            _fmt_clock(start),
            _fmt_clock(end),
        )

    # 七、尾帧接续流程 ---------------------------------------------------
    parts += [
        f"## {_SECTION_TITLES['continuity']}",
        "",
        "整个连续性体系由三种锚点组成，各管一件事，不能混为一谈：",
        "",
    ]
    if anchors:
        anchor_rows = [
            [_cell(anchor.get("name")), _cell(anchor.get("role")), _cell(anchor.get("desc"))]
            for anchor in anchors
        ]
        parts += _md_table(["锚点", "作用", "说明"], anchor_rows)
    else:
        parts.append("（按角色母图 / 场景母图 / 上一镜尾帧三类锚点管理连续性。）")
    parts += ["", "尾帧链全景：", ""]
    parts += _fence("\n".join(str(line) for line in list(guide.frame_chain.get("diagram") or [])))
    parts += ["", "逐镜衔接表：", ""]
    chain_rows: list[list[str]] = []
    for clip in clips:
        info = dict(chain_clips.get(str(clip.clip_number)) or {})
        raw_start = info.get("start_frame")
        start_frame = str(raw_start) if raw_start else None
        chain_rows.append(
            [
                f"Clip {clip.clip_number:02d}",
                f"@{start_frame}" if start_frame else "无（首镜）",
                str(info.get("end_frame_saved") or "—"),
                _cell(info.get("selection_window")),
            ]
        )
    parts += _md_table(["Clip", "首帧", "收尾保存", "挑选区间"], chain_rows)
    parts.append("")

    # 八、字幕时间轴 -----------------------------------------------------
    parts += [f"## {_SECTION_TITLES['subtitles']}", ""]
    if guide.subtitle_plan:
        parts += ["字幕全部在后期叠加，不要让视频模型把字幕烧进画面。对齐如下：", ""]
        subtitle_rows = [
            [
                _cell(entry.get("time")),
                _cell(entry.get("speaker")),
                _cell(entry.get("text")),
                _cell(entry.get("position")),
            ]
            for entry in guide.subtitle_plan
            if isinstance(entry, dict)
        ]
        parts += _md_table(["时间", "说话人", "字幕文本", "位置建议"], subtitle_rows)
    else:
        parts.append(
            "本集未规划字幕轨。若成片需要字幕，请在剪辑软件中后期叠加，"
            "保证位置与时长可控、无乱码。"
        )
    parts.append("")

    # 九、对白与音效时间轴 ------------------------------------------------
    parts += [f"## {_SECTION_TITLES['dialogue_sound']}", ""]
    if guide.dialogue_plan:
        parts += [f"对白策略：{_cell(guide.dialogue_plan[0].get('note'))}", ""]
        dialogue_rows: list[list[str]] = []
        for entry in guide.dialogue_plan:
            if not isinstance(entry, dict):
                continue
            number = int(entry.get("clip_number", 0) or 0)
            start, end = range_by_clip.get(number, (0.0, 0.0))
            strategy = str(entry.get("strategy") or "")
            dialogue_rows.append(
                [
                    f"Clip {number:02d}",
                    f"{_fmt_clock(start)}–{_fmt_clock(end)}",
                    _cell(entry.get("speaker")),
                    _cell(entry.get("line")),
                    "原生对白（Prompt 驱动口型与语音）"
                    if strategy == "native"
                    else "后期配音对齐口型",
                ]
            )
        parts += _md_table(["Clip", "台词时间", "说话人", "台词", "处理方式"], dialogue_rows)
        parts.append("")
    else:
        parts += ["本集无台词对白，确认环境声与表演足以支撑叙事。", ""]
    parts += ["音效与环境声（逐镜铺设，声音不硬切）：", ""]
    sound_rows: list[list[str]] = []
    for entry in guide.sound_plan:
        if not isinstance(entry, dict):
            continue
        number = int(entry.get("clip_number", 0) or 0)
        start, end = range_by_clip.get(number, (0.0, 0.0))
        environment = "、".join(str(item) for item in list(entry.get("environment") or []))
        key_sfx = "、".join(str(item) for item in list(entry.get("key_sfx") or []))
        sound_rows.append(
            [
                f"Clip {number:02d}",
                f"{_fmt_clock(start)}–{_fmt_clock(end)}",
                environment or "（无）",
                key_sfx or "（无）",
                _cell(entry.get("transition")),
            ]
        )
    parts += _md_table(["Clip", "时间", "环境声", "关键音效", "过渡处理"], sound_rows)
    parts += ["", "画面合成建议（画面内可读文本 / 后期 UI）：", ""]
    if guide.screen_composite_plan:
        for entry in guide.screen_composite_plan:
            if not isinstance(entry, dict):
                continue
            number = int(entry.get("clip_number", 0) or 0)
            matched = "、".join(str(item) for item in list(entry.get("matched") or []))
            suffix = f"｜命中词：{matched}" if matched else ""
            parts.append(
                f"- **Clip {number:02d}**（{_cell(entry.get('category'))}{suffix}）"
                f"：{_cell(entry.get('recommendation'))}"
            )
        parts.append("")
    else:
        parts += ["本集无画面内文本风险，无需特殊合成处理。", ""]

    # 十、BGM ------------------------------------------------------------
    bgm_note = str(guide.bgm_plan.get("note") or "")
    bgm_prompt = str(guide.bgm_plan.get("prompt") or "")
    bgm_total = _as_float(guide.bgm_plan.get("total_duration_seconds"), total_duration)
    parts += [
        f"## {_SECTION_TITLES['bgm']}",
        "",
        bgm_note or "整集铺一条统一配乐，不在 Clip 边界断开。",
        "",
        (
            f"用单独的音乐生成工具生成一整条约 {bgm_total:.0f} 秒的配乐，"
            "从 00:00 连续铺到片尾，不要按 Clip 切开分段生成："
        ),
        "",
    ]
    if bgm_prompt:
        parts += _fence(bgm_prompt)
    else:
        parts.append("本集未提供 BGM 生成 Prompt，请在音乐工具中按上方方向生成整条配乐。")
    parts.append("")

    # 十一、剪辑与转场 ----------------------------------------------------
    rules = [str(rule) for rule in list(editing.get("rules") or [])]
    parts += [f"## {_SECTION_TITLES['editing']}", "", "剪辑三原则：", ""]
    if rules:
        parts += [f"- {rule}" for rule in rules]
    else:
        parts.append("- 画面优先硬切；声音不硬切；BGM 一条到底。")
    parts += ["", "转场逐点确认：", ""]
    transition_rows: list[list[str]] = []
    for transition in list(editing.get("transitions") or []):
        if not isinstance(transition, dict):
            continue
        from_clip = int(transition.get("from_clip", 0) or 0)
        to_clip = int(transition.get("to_clip", 0) or 0)
        transition_rows.append(
            [
                f"Clip {from_clip:02d} → Clip {to_clip:02d}",
                _cell(transition.get("type")),
                _cell(transition.get("reason")),
            ]
        )
    if transition_rows:
        parts += _md_table(["剪辑点", "方式", "原因"], transition_rows)
    else:
        parts.append("单 Clip 成片，无剪辑点。")
    parts.append("")
    if str(editing.get("audio_crossfade") or ""):
        parts.append(f"- 声音过渡：{editing.get('audio_crossfade')}")
    if str(editing.get("bgm") or ""):
        parts.append(f"- BGM 铺设：{editing.get('bgm')}")
    parts.append("")

    # 十二、最终成片顺序 --------------------------------------------------
    order = [int(number) for number in list(editing.get("order") or [])]
    if not order:
        order = [clip.clip_number for clip in clips]
    parts += [
        f"## {_SECTION_TITLES['final_order']}",
        "",
        "按以下顺序直接拼接成片（不再调整镜头次序）：",
        "",
    ]
    parts += _fence("\n".join(f"Clip {number:02d}" for number in order))
    parts += [
        "",
        (
            f"拼接完成后总时长约 {_fmt_clock(total_duration)}：画面硬切/短叠化，"
            "声音不硬切（环境声 0.3–0.8 秒交叉淡化），整条 BGM 从 00:00 连续铺到片尾。"
        ),
        "",
    ]

    # 十三、最终检查清单 --------------------------------------------------
    parts += [f"## {_SECTION_TITLES['checklist']}", "", "成片导出前逐项打勾：", ""]
    parts += [str(item) for item in guide.final_checklist if str(item).strip()]
    parts.append("")
    return "\n".join(parts).rstrip() + "\n"
