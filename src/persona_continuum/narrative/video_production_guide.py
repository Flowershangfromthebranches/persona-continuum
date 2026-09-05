"""Executable video production guide renderer (guide layer).

A pure, deterministic export layer stacked on top of an existing
:class:`~persona_continuum.domain.narrative.ModelPromptPackage`. It compiles
ONE continuous, human-operable production handbook structured after the
user's reference document 《视频生成模型.md》:

* 永久角色/场景/道具素材 (素材N with a COMPLETE image-generation prompt each)
  come BEFORE every video section — masters are generated once and reused;
* per-clip work orders named 视频 N with 使用方式 / Start Frame /
  Ingredients / ONE complete copy-ready Prompt / 生成完成后 tail-frame action;
* tail-frame chaining workflow, subtitle / screen-composite / dialogue /
  SFX / BGM / editing / final checklist sections.

Rules honoured here:
* zero I/O, zero LLM calls, deterministic (identical inputs -> identical
  markdown);
* never emit a placeholder phrase (see :data:`PLACEHOLDER_PATTERNS`);
* never fabricate files — reference assets are prompts/instructions, their
  status is ``PROMPT_READY`` / ``NEEDED`` and never ``BOUND``;
* a clip whose location resolves to no bible/list entry fails closed via
  :func:`compile_copy_ready_prompt` (SHOOTING_LOCATION_CONTEXT_MISSING);
* thin visual bibles trigger deterministic CharacterVisualIdentity synthesis
  (fixed age/face/hair/body/costume) instead of a generic person;
* continuity constraints / reference lists / strict rules are stable-ordered
  deduplicated before they reach the final guide;
* one duration source of truth (``actual_generation_duration``) — never a
  ``duration_seconds`` vs ``target_duration_seconds`` contradiction.
"""

from __future__ import annotations

import hashlib
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
from persona_continuum.narrative.video_prompt_compiler import (
    compile_copy_ready_prompt,
    resolve_location_entry,
)

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
_QUALITY_LABELS = {
    "quality": "质量优先",
    "balanced": "均衡",
    "fast": "速度优先",
}
_DIGITS = ("一", "二", "三", "四", "五", "六", "七", "八", "九")

# Visual-identity synthesis option pools (task #13). Selection is seeded by a
# stable hash of (project_id, character_id) so every episode reuses the SAME
# synthesized identity instead of drifting back to a generic person.
_FACE_OPTIONS = (
    "oval face with soft cheekbones and a small straight nose",
    "round face with full cheeks and a gentle jawline",
    "long face with defined cheekbones and a straight brow line",
    "square face with a strong, calm jawline",
    "heart-shaped face with a narrow chin and wide-set eyes",
)
_EYE_OPTIONS = (
    "dark brown almond eyes with a calm, steady gaze",
    "deep black eyes with slightly downturned lids, reading as tired but focused",
    "dark eyes with short, straight brows and an alert expression",
    "brown eyes with a mild epicanthic fold and quiet intensity",
)
_HAIR_OPTIONS = (
    "shoulder-length straight black hair, centre-parted, tucked behind the left ear",
    "short black hair cropped just above the collar, neatly brushed",
    "black hair tied in a low ponytail with a clean side part",
    "collar-length black hair with a light natural wave and no parting",
    "very short black hair with a faint fade along the sides",
)
_HEIGHT_OPTIONS = {
    "female": ("162 cm", "165 cm", "168 cm", "158 cm"),
    "male": ("174 cm", "177 cm", "180 cm", "171 cm"),
    "neutral": ("168 cm", "173 cm", "176 cm"),
}
_BODY_OPTIONS = {
    "female": (
        "slim build with upright, composed posture",
        "medium build with a slightly forward, work-worn posture",
        "lean build with quick, light movements",
    ),
    "male": (
        "lean build with squared shoulders and a settled stance",
        "medium build with a slightly rounded, desk-bound posture",
        "slim build with tense, energy-saving movements",
    ),
    "neutral": ("medium build with a quietly alert posture",),
}
_COSTUME_OPTIONS = {
    "female": (
        "charcoal-grey knitted top with sleeves pushed to the forearms, dark "
        "tailored trousers, plain black leather sneakers",
        "off-white cotton shirt over a light grey cardigan, dark straight "
        "trousers, low practical shoes",
        "dark navy blazer over a plain grey tee, black slim trousers, quiet "
        "leather flats",
    ),
    "male": (
        "heather-grey polo shirt, dark chino trousers, plain black trainers",
        "navy cotton overshirt over a white tee, dark slim jeans, black "
        "sneakers",
        "light blue oxford shirt with rolled sleeves, charcoal trousers, "
        "leather loafers",
    ),
    "neutral": (
        "muted grey layered top, dark trousers, plain dark shoes",
    ),
}
_PALETTE_OPTIONS: tuple[tuple[str, ...], ...] = (
    ("charcoal grey", "off white", "muted steel blue"),
    ("ink black", "warm grey", "faded denim blue"),
    ("deep navy", "stone grey", "soft ivory"),
    ("graphite", "cool grey", "pale blue-white"),
)
_TEMPERAMENT_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("焦虑", "紧张", "不安", "anxious", "nervous"),
        "wound-up and watchful, always half a step from leaving",
    ),
    (
        ("冷静", "沉稳", "克制", "calm", "restrained"),
        "quiet and level, economising every movement",
    ),
    (
        ("疲惫", "倦", "tired", "weary"),
        "visibly worn down, moving on habit rather than will",
    ),
    (
        ("愤怒", "愤", "angry", "furious"),
        "coiled and sharp-edged, anger held just under the surface",
    ),
    (
        ("温暖", "善良", "温柔", "warm", "kind"),
        "gentle and open, an easy presence in the frame",
    ),
    (
        ("坚毅", "决然", "determined", "resolute"),
        "still and resolved, eyes doing more than the body",
    ),
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


def _cn_num(n: int) -> str:
    """Chinese numeral for section numbers (1–99)."""
    if n < 1:
        return "零"
    if n < 10:
        return _DIGITS[n - 1]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _DIGITS[n - 11]
    tens, ones = divmod(n, 10)
    return _DIGITS[tens - 1] + "十" + (_DIGITS[ones - 1] if ones else "")


def _dedupe_stable(items: list[str] | tuple[str, ...] | None) -> list[str]:
    """Stable ordered dedupe (task #30): keeps first-seen order, drops repeats."""
    return list(dict.fromkeys(str(item) for item in (items or []) if str(item).strip()))


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


def _platform_term(profile: VideoModelProfile | None, key: str, default: str) -> str:
    """Vendor UI wording for one internal concept (task #63/#64)."""
    if profile is None:
        return default
    terms = profile.platform_terms or {}
    return str(terms.get(key) or default)


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
    entry, _ = resolve_location_entry(key, package)
    return entry


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
# CharacterVisualIdentity synthesis (task #11–#14)
# ----------------------------------------------------------------------
_AGE_RE = re.compile(r"(\d{1,2})\s*(?:岁|years?\s*old|yo\b)")
_FEMALE_RE = re.compile(r"女|她|female|woman|lady|母亲|姐姐|妹妹|阿姨|妻子")
_MALE_RE = re.compile(r"男|他(?![们A-Za-z])|male|man|父亲|哥哥|弟弟|叔叔|丈夫|先生")


def _identity_cues(text: str) -> dict[str, bool]:
    """Which concrete visual cue families a bible text already covers."""
    lowered = text.casefold()
    return {
        "age": bool(_AGE_RE.search(text)) or bool(
            re.search(r"少女|中年|老年|年轻|young|middle-aged|elderly", lowered)
        ),
        "face": bool(re.search(r"脸|面容|眉|眼|鼻|唇|酒窝|face|brow|eye|nose|lip", lowered)),
        "hair": bool(re.search(r"发|hair|ponytail|bob|刘海", lowered)),
        "costume": bool(
            re.search(
                r"衣|衫|裙|裤|西装|毛衣|外套|制服|鞋|帽|wear|shirt|suit|dress|coat|jacket|shoe",
                lowered,
            )
        ),
        "body": bool(
            re.search(r"身高|体型|身形|瘦|胖|高|矮|build|tall|short|slim|slender", lowered)
        ),
    }


def _seeded_pick[PickT](options: tuple[PickT, ...], seed_key: str, salt: str) -> PickT:
    digest = hashlib.sha256(f"{salt}:{seed_key}".encode()).digest()
    return options[int.from_bytes(digest[:4], "big") % len(options)]


def synthesize_character_visual_identity(
    project_id: str,
    character_id: str,
    name: str,
    context_text: str,
    color_palette: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministic CharacterVisualIdentity for thin bibles (task #13).

    Extracts every concrete cue the story text already states (age, gender,
    wardrobe) and fills the gaps from stable hash-seeded option pools, so the
    SAME character gets the SAME fixed visual identity in every episode —
    never a silent fallback to a generic person.
    """
    seed_key = f"{project_id}:{character_id or name}"
    text = context_text or ""
    gender = "female" if _FEMALE_RE.search(text) else (
        "male" if _MALE_RE.search(text) else "neutral"
    )
    if gender == "neutral":
        gender = _seeded_pick(("female", "male"), seed_key, "gender")

    age_match = _AGE_RE.search(text)
    if age_match:
        age = f"{int(age_match.group(1))} years old"
    else:
        age_options = tuple(str(value) for value in range(24, 39))
        age = f"{_seeded_pick(age_options, seed_key, 'age')} years old"

    palette_input = [str(item) for item in (color_palette or []) if str(item).strip()]
    if palette_input:
        picked_palette: tuple[str, ...] = tuple(palette_input)
    else:
        picked_palette = _seeded_pick(_PALETTE_OPTIONS, seed_key, "palette")

    temperament = ""
    lowered = text.casefold()
    for keywords, value in _TEMPERAMENT_MAP:
        if any(keyword in lowered for keyword in keywords):
            temperament = value
            break
    if not temperament:
        temperament = _seeded_pick(
            tuple(value for _k, value in _TEMPERAMENT_MAP), seed_key, "temperament"
        )

    wardrobe_hint = ""
    wardrobe_match = re.search(
        r"(穿着|身着|戴着|wearing|wears)\s*([^。；;,.]{2,40})", text
    )
    if wardrobe_match:
        wardrobe_hint = wardrobe_match.group(2).strip()
    if wardrobe_hint:
        costume = f"{wardrobe_hint} (keep this exact wardrobe every reuse)"
    else:
        costume = _seeded_pick(_COSTUME_OPTIONS[gender], seed_key, "costume")

    return {
        "age": age,
        "gender": gender,
        "face_shape": _seeded_pick(_FACE_OPTIONS, seed_key, "face"),
        "eyes": _seeded_pick(_EYE_OPTIONS, seed_key, "eyes"),
        "hair": _seeded_pick(_HAIR_OPTIONS, seed_key, "hair"),
        "height": _seeded_pick(_HEIGHT_OPTIONS[gender], seed_key, "height"),
        "body": _seeded_pick(_BODY_OPTIONS[gender], seed_key, "body"),
        "costume": costume,
        "color_palette": list(picked_palette),
        "temperament": temperament,
        "source": "synthesized",
    }


def _render_identity_lines(identity: dict[str, Any]) -> list[str]:
    """Identity dict -> ordered English descriptor lines for prompt blocks."""
    lines = [
        f"Age: {identity.get('age', 'adult')}",
        f"Face: {identity.get('face_shape', '')}".rstrip(": "),
        f"Eyes: {identity.get('eyes', '')}".rstrip(": "),
        f"Hair: {identity.get('hair', '')}".rstrip(": "),
        f"Height: {identity.get('height', '')}".rstrip(": "),
        f"Body: {identity.get('body', '')}".rstrip(": "),
        f"Costume: {identity.get('costume', '')}".rstrip(": "),
    ]
    palette = identity.get("color_palette")
    if isinstance(palette, list) and palette:
        lines.append("Colour anchors: " + ", ".join(str(item) for item in palette))
    if identity.get("temperament"):
        lines.append(f"Temperament cues: {identity['temperament']}")
    return [line for line in lines if line.split(": ", 1)[-1].strip()]


def _identity_block_for_character(
    asset: ProductionGuideAsset,
    entry: dict[str, Any] | None,
    package: ProductionPackage,
) -> tuple[str, dict[str, Any]]:
    """Merged identity text + the visual identity dict for one character.

    Rich bibles keep their full text (source ``bible``). Thin bibles get a
    synthesized fixed visual identity (task #13) recorded on the asset and
    rendered into the prompt; extracted story cues always win over synthesized
    defaults.
    """
    seed_text = _entry_description_block(entry) if entry else ""
    fallback = [
        str(line) for line in (asset.compiler_trace.get("fallback_context") or [])
    ]
    context = "\n".join([seed_text, *fallback])
    cues = _identity_cues(context)
    if sum(bool(value) for value in cues.values()) >= 3 and seed_text.strip():
        return seed_text, {"source": "bible"}
    identity = synthesize_character_visual_identity(
        package.project_id,
        asset.character_id or asset.name,
        asset.name,
        context,
        color_palette=(entry or {}).get("color_palette")
        if isinstance((entry or {}).get("color_palette"), list)
        else None,
    )
    rendered = (
        "Synthesized fixed visual identity for this character (reuse the SAME "
        "identity in every clip and every episode):\n"
        + "\n".join(f"- {line}" for line in _render_identity_lines(identity))
    )
    if seed_text.strip():
        rendered = f"{seed_text}\n{rendered}"
    return rendered, identity


# ----------------------------------------------------------------------
# Frame chain (location-aware, with scene start frames)
# ----------------------------------------------------------------------
def build_frame_chain(
    clips: list[GenerationClip],
    profile: VideoModelProfile | None = None,
    episode_number: int = 1,
) -> dict[str, Any]:
    """Deterministic tail-frame chaining plan (task #19/#62).

    Clips inside ONE location chain physically: clip N saves ``FRAME_NN``
    which becomes clip N+1's start frame. A location change breaks the chain;
    the new clip then either opens from a dedicated Scene Start Frame asset
    (``@EPxx_CLIPnn_START``, when the model documents image-to-video support
    and characters make identity lock worthwhile) or opens free as text-to-video.
    """
    ordered = _sorted_clips(clips)
    total = len(ordered)
    # Scene start frames are only planned when the target model documents
    # image-to-video (or first-frame) support; UNKNOWN means a hard gap.
    supports_start_frames = (
        profile is None
        or profile.supports_image_to_video is True
        or profile.supports_first_frame is True
    )
    per_clip: dict[str, Any] = {}
    frame_plans: dict[str, Any] = {}
    diagram: list[str] = []
    for index, clip in enumerate(ordered):
        number = clip.clip_number
        previous = ordered[index - 1] if index > 0 else None
        same_location = (
            previous is not None
            and bool(previous.location.strip())
            and previous.location == clip.location
            and bool(clip.location.strip())
        )
        # The previous clip only saves a frame when THIS clip continues it.
        continues_chain = same_location and index > 0
        produces_next = False
        nxt = ordered[index + 1] if index + 1 < total else None
        if nxt is not None and clip.location == nxt.location and bool(clip.location.strip()):
            produces_next = True
        start_frame = f"FRAME_{index:02d}" if continues_chain else None
        scene_start_frame = None
        if start_frame is None and supports_start_frames and clip.character_ids:
            scene_start_frame = f"@EP{episode_number:02d}_CLIP{number:02d}_START"
        saved = f"FRAME_{number:02d}" if produces_next else "无需保存接续帧"
        duration = max(0.0, clip.duration_seconds)
        per_clip[str(number)] = {
            "clip_number": number,
            "start_frame": start_frame,
            "scene_start_frame": scene_start_frame,
            "produces_next_start_frame": produces_next,
            "end_frame_saved": saved,
            "selection_window": (
                f"最后 0.8–1.0 秒（按时长适配：{max(0.0, duration - 1.0):.1f}s–"
                f"{max(0.0, duration - 0.2):.1f}s 具体区间）"
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
        if start_frame:
            link = f"Clip {number:02d} ──尾帧 {saved}──▶"
        elif scene_start_frame:
            link = f"Clip {number:02d} ──独立开镜 {scene_start_frame}──▶"
        elif produces_next:
            link = f"Clip {number:02d} ──尾帧 {saved}──▶"
        else:
            link = f"Clip {number:02d} ──场景切换（不产接续帧）──▶"
        diagram.append(link)
    if diagram and ordered:
        last = ordered[-1]
        if not per_clip[str(last.clip_number)]["produces_next_start_frame"]:
            diagram[-1] = f"Clip {last.clip_number:02d} ──收尾（不产接续帧）"
    return {
        "clips": per_clip,
        "frame_plans": frame_plans,
        "diagram": diagram,
        "total_clips": total,
    }


def _clip_operational_mode(info: dict[str, Any]) -> str:
    """Concrete per-clip generation mode (task #22): never '自动'."""
    if info.get("start_frame"):
        return "图生视频（上一段尾帧承接）"
    if info.get("scene_start_frame"):
        return "图生视频（独立开镜画面）"
    return "文生视频"


def _clip_short_mode(info: dict[str, Any]) -> str:
    if info.get("start_frame") or info.get("scene_start_frame"):
        return "图生视频"
    return "文生视频"


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
    visual_identity: dict[str, Any] | None = None,
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
        visual_identity=visual_identity or {},
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
    package-level visual direction, Scene Start Frames for clips that open a
    scene via image-to-video, and ``FRAME_NN`` capture instructions for every
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
            f"出现在 {count} 个视频段"
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
        entry, sub = resolve_location_entry(location, production_package)
        if entry and not sub:
            name = _entry_name(entry) or location
        else:
            name = location if location else ((_entry_name(entry) if entry else "") or "location")
        count = len(referencing)
        necessity = "required" if count >= 2 else "recommended"
        context = []
        for clip in referencing:
            context.extend(_clip_context_lines(clip, production_package))
        reason = f"出现在 {count} 个视频段 → {necessity}"
        source_refs: list[str] = []
        if entry:
            entry_n = _entry_name(entry)
            if entry_n:
                source_refs.append(entry_n)
            if name and name not in source_refs:
                source_refs.append(name)
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
                source_bible_refs=source_refs,
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
        reason = f"被 {count} 个视频段引用 → {necessity}"
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

    # --- Scene Start Frames (task #19/#20) ---------------------------
    chain = build_frame_chain(clips, profile, prompt_package.episode_number)
    for clip in clips:
        info = chain["clips"].get(str(clip.clip_number), {})
        scene_start = info.get("scene_start_frame")
        if not scene_start:
            continue
        entry = _location_bible_entry(clip.location, production_package)
        loc_name = _entry_name(entry) if entry else clip.location
        context = _clip_context_lines(clip, production_package)
        assets.append(
            _make_asset(
                production_package,
                prompt_package,
                asset_key=str(scene_start),
                asset_type="start_frame",
                name=f"视频{clip.clip_number:02d} 开镜画面",
                purpose=(
                    f"锁定视频 {clip.clip_number:02d} 的第一帧构图：角色、场景与动作起始状态"
                    "合成一张可直接驱动图生视频的开镜图。"
                ),
                necessity="required",
                reuse_scope="scene",
                notes=[
                    "这不是身份母图，而是本视频段专用的开场画面；"
                    "生成后作为该段的 Start Frame 上传。",
                    f"场景：{loc_name or '（见场景母图）'}。",
                ],
                trace={
                    "necessity": "required",
                    "reason": f"clip {clip.clip_number} opens a scene via image-to-video",
                    "clip_number": clip.clip_number,
                    "fallback_context": list(dict.fromkeys(context))[:6],
                },
                location_id=clip.location or None,
            )
        )

    # --- Reference frames (chained clips, capture instructions) ------
    for number_str, info in chain["clips"].items():
        if not info["produces_next_start_frame"]:
            continue
        frame_name = str(info["end_frame_saved"])
        number = int(number_str)
        capture = (
            f"在视频 {number:02d} 生成完成后，从片尾挑选一帧保存为下一镜的 Start Frame。\n"
            f"选取区间：{info['selection_window']}\n"
            f"选取标准：{'；'.join(info['criteria'])}。\n"
            f"保存命名：{frame_name}，用作视频 {number + 1:02d} 的 Start Frame（物理连续性锚点）。"
        )
        assets.append(
            _make_asset(
                production_package,
                prompt_package,
                asset_key=f"@{frame_name}",
                asset_type="reference_frame",
                name=frame_name,
                purpose=f"承接视频 {number:02d} 的收尾画面，作为视频 {number + 1:02d} 的首帧。",
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


def _character_image_prompt(
    asset: ProductionGuideAsset, package: ProductionPackage
) -> tuple[str, dict[str, Any]]:
    """(prompt, visual_identity) for one character master; the identity dict
    is persisted on the asset copy by the caller (task #13/#14)."""
    entry = _character_bible_entry(asset.character_id or asset.name, package)
    identity, visual_identity = _identity_block_for_character(asset, entry, package)
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
        "EXPRESSION:",
        "Neutral, relaxed, camera-aware but not posing; the face must stay readable for later "
        "close-up clips.",
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
    return "\n".join(head + _tail(asset, purpose)), visual_identity


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


def _image_prompt_for(
    asset: ProductionGuideAsset,
    package: ProductionPackage,
    profile: VideoModelProfile,
) -> tuple[str, dict[str, Any]]:
    """(complete image prompt, extra asset-field updates) for one asset."""
    if asset.asset_type == "character":
        prompt, identity = _character_image_prompt(asset, package)
        return prompt, ({"visual_identity": identity} if identity else {})
    if asset.asset_type == "location":
        return _location_image_prompt(asset, package), {}
    if asset.asset_type == "prop":
        return _prop_image_prompt(asset, package), {}
    if asset.asset_type == "start_frame":
        return _start_frame_prompt_for(asset, package, profile), {}
    return _style_image_prompt(asset, package), {}


def _start_frame_prompt_for(
    asset: ProductionGuideAsset, package: ProductionPackage, profile: VideoModelProfile
) -> str:
    """Complete Scene Start Frame prompt built from this clip's own data."""
    trace = asset.compiler_trace or {}
    clip_number = int(trace.get("clip_number") or 0)
    # The clip context is re-derived from the trace + package shot list.
    fallback = [str(line) for line in (trace.get("fallback_context") or [])]
    entry = _location_bible_entry(asset.location_id or "", package)
    location_identity = _identity_or_context(asset, entry)
    style_line = _global_style_line(package)
    aspect = asset.recommended_aspect_ratio or "16:9"
    action_text = fallback[0] if fallback else "the opening action of this clip"
    head = [
        f"Create the OPENING STILL FRAME for video clip {clip_number:02d} of this episode. "
        "This image is not a standalone illustration: it will be uploaded as the Start Frame "
        "that the video model animates forward from, so it must capture the exact starting "
        "state of the action.",
        "",
        "SUBJECT AND OPENING ACTION:",
        action_text
        + (
            ". The subject is caught in the instant right before the main movement begins: "
            "weight settled, gaze set, hands in their starting positions."
        ),
        "",
        "ENVIRONMENT:",
        location_identity,
        "",
        "CAMERA:",
        "Framing, angle, and lens height must read as the first frame of a continuous shot; "
        "compose so there is room for the action to unfold inside the frame.",
        "",
        "LIGHTING:",
        "Match the location reference's time of day and light direction exactly; the first "
        "video frame must not jump in exposure or colour from this still.",
        "",
        "COMPOSITION:",
        f"Compose in {aspect} exactly as the clip will render; keep every key subject fully "
        "inside the safe area.",
        "",
        "VISUAL STYLE:",
        style_line,
    ]
    purpose = (
        f"This image is the start-frame token {asset.asset_key}; upload it as the Start Frame "
        f"of video {clip_number:02d} together with the character/location master references."
    )
    return "\n".join(head + _tail(asset, purpose))


def build_reference_asset_prompts(
    assets: list[ProductionGuideAsset],
    production_package: ProductionPackage,
    profile: VideoModelProfile,
) -> list[ProductionGuideAsset]:
    """Fill a COMPLETE copy-ready image prompt for every character/location/
    prop/style/start-frame asset and mark it ``PROMPT_READY``.

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
        prompt, extra_updates = _image_prompt_for(asset, production_package, profile)
        _ensure_no_placeholders(prompt)
        updates: dict[str, Any] = {
            "generation_prompt": prompt,
            "status": "PROMPT_READY",
            **extra_updates,
        }
        result.append(asset.model_copy(update=updates))
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
    clips: list[GenerationClip],
    production_package: ProductionPackage,
    profile: VideoModelProfile | None = None,
) -> list[dict[str, Any]]:
    """Flag clips whose action/dialogue implies on-screen text.

    Distinguishes diegetic text that must be legible inside the generated
    frame (phone/email/screen/SMS) from non-diegetic UI overlays that should
    be composited in post (subtitles/notifications/timestamps) to avoid the
    model producing garbled glyphs. Profiles without a documented stable
    text capability default to post-compositing (task #35/#36).
    """
    shots = _shots_by_number(production_package)
    native = (profile.screen_text_strategy == "native_screen_text") if profile else False
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
        if diegetic and native:
            category = "画面真实可读文本（模型原生文字）"
            recommendation = (
                "该镜头包含需在画面内出现的可读文本，目标模型具备稳定文字生成能力："
                "在 Prompt 中明确描述文字内容与位置；若生成结果出现乱码，仍回退为后期贴图。"
            )
        elif diegetic:
            category = "画面真实可读文本（后期合成）"
            recommendation = (
                "该镜头包含需在画面内出现的可读文本。不要依赖视频模型生成中文文字："
                "让画面只保留手机/屏幕的亮屏与手部动作，屏幕内容留出干净区域，"
                "在剪辑软件中做屏幕跟踪 + UI 贴图，把邮件正文、时间戳等真实文字贴上去，"
                "避免出现乱码。"
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
        environment = _dedupe_stable(clip.audio_intent)
        key_sfx = _dedupe_stable(sfx_by_clip.get(clip.clip_number, []))
        plan.append(
            {
                "clip_number": clip.clip_number,
                "environment": environment,
                "key_sfx": key_sfx,
                "transition": "与相邻视频段的环境声做 0.3–0.8 秒 audio crossfade，声音不要硬切。",
            }
        )
    return plan


# Musical Director Agent (task #38–#40): one complete, copy-ready music prompt.
_TEMPERAMENT_INSTRUMENTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("悬疑", "suspense", "紧张", "thriller"),
        "sustained low strings, sparse felt piano, sub-bass pulses, and a "
        "single high sustained violin tone",
    ),
    (
        ("温暖", "warm", "治愈", "healing"),
        "felt piano, nylon guitar, warm pad, soft clarinet",
    ),
    (
        ("悲伤", "sad", "忧伤", "melancholy"),
        "solo cello, slow piano arpeggios, airy strings pad",
    ),
    (
        ("愤怒", "angry", "激烈", "intense"),
        "low brass swells, marcato strings, tight percussion",
    ),
    ((), "piano, warm strings pad, light percussion"),
)


def _instrumentation_for(direction: str) -> str:
    lowered = direction.casefold()
    for keywords, value in _TEMPERAMENT_INSTRUMENTS:
        if keywords and any(keyword in lowered for keyword in keywords):
            return value
    return _TEMPERAMENT_INSTRUMENTS[-1][1]


def build_bgm_plan(
    production_package: ProductionPackage, clips: list[GenerationClip]
) -> dict[str, Any]:
    """One COMPLETE music-generation prompt (task #38/#39).

    Structure: OVERALL STYLE / INSTRUMENTATION / EMOTIONAL ARC (per clip with
    timecodes) / FINAL 10 SECONDS / MIX / STRICT — never a bare
    "underscore Clip 7" line.
    """
    ranges, total = _clip_time_ranges(clips)
    direction = production_package.bgm_direction.strip()
    segments: list[dict[str, Any]] = []
    arc_lines: list[str] = []
    for number, start, end in ranges:
        clip = next((c for c in clips if c.clip_number == number), None)
        beat = (clip.purpose.strip() if clip and clip.purpose.strip() else "on-screen action")
        arc_lines.append(
            f"{_fmt_clock(start)}–{_fmt_clock(end)} (视频{number:02d}): {beat}"
        )
        segments.append(
            {"clip_number": number, "time": f"{_fmt_clock(start)}–{_fmt_clock(end)}", "beat": beat}
        )
    final_start = max(0.0, total - 10.0)
    style = direction or "restrained, cinematic underscore that follows the story's emotion"
    final_header = (
        f"FINAL 10 SECONDS ({_fmt_clock(final_start)}–{_fmt_clock(total)}):"
        if total >= 12.0
        else "ENDING:"
    )
    lines = [
        f"Create a {total:.0f}-second instrumental score for this episode — ONE continuous "
        "piece of music from the first frame to the last, never a collection of per-clip cues.",
        "",
        "OVERALL STYLE:",
        style
        + ". The music must feel like a single composed work with one identity.",
        "",
        "INSTRUMENTATION:",
        _instrumentation_for(direction),
        "",
        "EMOTIONAL ARC:",
        *arc_lines,
        "",
        final_header,
        "Land the ending: resolve or deliberately withhold resolution to match the final "
        "image, then fade naturally so the closing frame and any final subtitle can breathe.",
        "",
        "MIX:",
        "Background-music level: the score sits under dialogue and ambience, never covering "
        "them; keep dynamics controlled so no clip boundary needs an abrupt level jump.",
        "",
        "STRICT:",
        "No vocals. No choir. No lyrics. No spoken word. No sudden trailer-style percussion "
        "hits at clip boundaries. The music never restarts or changes character mid-episode.",
    ]
    return {
        "total_duration_seconds": total,
        "prompt": "\n".join(lines),
        "segments": segments,
        "emotion_arc": [segment["beat"] for segment in segments],
        "note": (
            "每段视频不单独生成 BGM：整集统一用下面这一条 Prompt 生成一整条配乐，"
            "从 00:00 连续播放到片尾。"
        ),
    }


def build_editing_plan(
    production_package: ProductionPackage,
    clips: list[GenerationClip],
    profile: VideoModelProfile | None = None,
) -> dict[str, Any]:
    ordered = _sorted_clips(clips)
    chain = build_frame_chain(ordered, profile, production_package.episode_number)
    transitions: list[dict[str, Any]] = []
    for index in range(len(ordered) - 1):
        current = ordered[index]
        nxt = ordered[index + 1]
        info = chain["clips"].get(str(current.clip_number), {})
        carries = bool(info.get("produces_next_start_frame"))
        same_scene = (
            bool(current.location.strip()) and current.location == nxt.location
        )
        if carries and same_scene:
            cut_type = "硬切 CUT"
            reason = "尾帧即下一镜首帧，视觉状态本就连续，硬切最干净。"
        elif carries:
            cut_type = "硬切 CUT（如有轻微跳动，做 3–6 帧短叠化）"
            reason = "画面物理连续，但跨场景时允许极短叠化缓冲；不要做 1 秒叠化。"
        else:
            cut_type = "短叠化 3–6 帧（约 0.10–0.20 秒）"
            reason = "无尾帧承接（场景切换重新开镜），用极短叠化过渡，避免生硬跳变。"
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
        "bgm": "整条 BGM 从 00:00 连续铺到片尾，不跟着视频段切开。",
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
    start_frame_count = sum(1 for a in required_assets if a.asset_type == "start_frame")
    items = [
        f"□ 人物身份一致：{char_count} 个角色母图在所有视频段中长相/身形/服装保持同一人。",
        f"□ 空间一致：{loc_count} 个场景母图的空间布局、光线、材质跨镜不漂移。",
        f"□ 道具一致：{prop_count} 个关键道具的形状/材质/颜色每次出现都相同。",
        f"□ 开镜画面就绪：{start_frame_count} 张 Scene Start Frame 已生成并命名归档。",
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
        "不在视频段边界断开。"
    )
    items.append(
        f"□ 成片交付：{len(clips)} 个视频段按顺序硬切/短叠化拼接，环境声 0.3–0.8 秒交叉淡化，"
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
        lines.append(f"单镜时长限制：{span}；超时请拆成多个视频段再用尾帧链承接。")
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

    analyze -> reference prompts (incl. CharacterVisualIdentity + Scene Start
    Frames) -> frame chain -> per-clip copy-ready prompts (via
    :func:`compile_copy_ready_prompt`, propagating
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
    chain = build_frame_chain(clips, profile, prompt_package.episode_number)
    frame_plans = chain["frame_plans"]

    # CharacterVisualIdentity overrides for clips whose bible text is thin:
    # the synthesized fixed identity is embedded into the copy-ready prompt's
    # CHARACTER IDENTITY section instead of "match the reference images" only.
    identity_by_character: dict[str, str] = {}
    for asset in required_assets:
        if asset.asset_type != "character" or not asset.character_id:
            continue
        entry = _character_bible_entry(asset.character_id, production_package)
        merged, identity = _identity_block_for_character(asset, entry, production_package)
        if identity.get("source") == "synthesized":
            identity_by_character[asset.character_id] = merged

    compiled_clips: list[GenerationClip] = []
    # User-facing master tokens per clip so the copy-ready prompt's REFERENCE
    # INPUTS section stays consistent with the work order's upload list.
    planned_key_index = _asset_key_index(required_assets)
    for clip in clips:
        frame_plan = dict(frame_plans.get(str(clip.clip_number), {}))
        info = chain["clips"].get(str(clip.clip_number), {})
        scene_start = str(info.get("scene_start_frame") or "").strip()
        reference_tokens: list[str] = []
        if scene_start:
            frame_plan["scene_start_frame"] = scene_start
            reference_tokens.append(scene_start)
        for character_id in clip.character_ids:
            key = planned_key_index.get(f"character:{character_id}")
            if key and key not in reference_tokens:
                reference_tokens.append(key)
        if clip.location:
            key = planned_key_index.get(f"location:{clip.location}")
            if key and key not in reference_tokens:
                reference_tokens.append(key)
        frame_plan["reference_tokens"] = reference_tokens
        trace: dict[str, Any] = {}
        copy_ready = compile_copy_ready_prompt(
            clip,
            profile,
            production_package,
            assets_by_id,
            frame_plan,
            trace,
            identity_overrides=identity_by_character,
        )
        _ensure_no_placeholders(copy_ready)
        # Stable ordered dedupe (task #30): continuity lists never repeat rows.
        merged_trace = {**clip.compiler_trace, "copy_ready": trace}
        start_key = scene_start or str(info.get("start_frame") or "")
        compiled_clips.append(
            clip.model_copy(
                update={
                    "copy_ready_prompt": copy_ready,
                    "compiler_trace": merged_trace,
                    "continuity_constraints": _dedupe_stable(clip.continuity_constraints),
                    "start_frame_asset_key": start_key.lstrip("@"),
                }
            )
        )

    subtitle_plan = build_subtitle_plan(production_package)
    screen_composite_plan = build_screen_composite_plan(
        compiled_clips, production_package, profile
    )
    dialogue_plan = build_dialogue_plan(compiled_clips, profile)
    sound_plan = build_sound_plan(production_package, compiled_clips)
    bgm_plan = build_bgm_plan(production_package, compiled_clips)
    editing_plan = build_editing_plan(production_package, compiled_clips, profile)
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
    episode_title = str(opts.get("episode_title") or f"第{episode_number}集")
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
        # Platform terminology + capability summary the renderer needs AFTER
        # persistence (the guide stores no profile object).
        "platform_terms": dict(profile.platform_terms),
        "negative_prompt_supported": profile.supports_negative_prompt is True,
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
                "name": "Scene Start Frame",
                "role": "开镜画面锚点",
                "desc": "锁定本段视频第一帧的构图与起始动作，用图片生成后再驱动视频。",
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
        title=f"EP{episode_number:02d}《{episode_title}》AI视频完整制作方案",
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
        "sections": list(_SECTION_KEYS),
        "deterministic": True,
    }
    return guide


# ----------------------------------------------------------------------
# Markdown handbook renderer (structure aligned with 《视频生成模型.md》)
# ----------------------------------------------------------------------
_SECTION_KEYS = (
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
)

_ASSET_TYPE_LABELS: dict[str, str] = {
    "character": "角色母图（身份锚点）",
    "location": "场景母图（空间锚点）",
    "prop": "道具参考图",
    "style": "全集风格基准图",
    "start_frame": "Scene Start Frame（开镜画面）",
    "reference_frame": "尾帧捕获指令（不是图片）",
}

_ASSET_GENERATION_NOTE = {
    "character": (
        "用任意文生图工具（如即梦、Midjourney、Nano Banana）生成；"
        "生成后按 asset_key 命名保存，全集及后续各集固定复用这一张。"
    ),
    "location": (
        "用任意文生图工具生成；生成后按 asset_key 命名保存，"
        "本集内该场景的所有镜头复用这一张。"
    ),
    "prop": (
        "用任意文生图工具生成；生成后按 asset_key 命名保存，"
        "出现该道具的镜头复用这一张。"
    ),
    "style": "用任意文生图工具生成；作为全集风格基准，可选拍摄。",
    "start_frame": (
        "用任意文生图工具生成；生成后作为该视频段的 Start Frame 上传到视频平台。"
    ),
    "reference_frame": "不需要生成图片：这是该视频段生成完成后的尾帧捕获指令。",
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
        elif asset.asset_type == "location":
            if asset.location_id:
                index[f"location:{asset.location_id}"] = asset.asset_key
            if asset.name:
                index[f"location:{asset.name}"] = asset.asset_key
        elif asset.asset_type == "prop":
            index[f"prop:{asset.name.casefold()}"] = asset.asset_key
    return index


def _clip_asset_keys(
    clip: GenerationClip,
    index: dict[str, str],
    start_frame: str | None,
    scene_start_frame: str | None,
) -> list[str]:
    """User-facing asset_key tokens one clip consumes (start frame first)."""
    keys: list[str] = []
    if scene_start_frame:
        keys.append(str(scene_start_frame))
    elif start_frame:
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


def _settings_line(clip: GenerationClip, info: dict[str, Any], keys: list[str]) -> str:
    """Human-readable recommended settings (task #28): no snake_case, ONE
    generation duration, platform-neutral labels."""
    duration = max(0.0, clip.duration_seconds)
    mode = _clip_short_mode(info)
    quality = str(
        (clip.recommended_settings or {}).get("quality_priority") or "balanced"
    )
    parts = [
        f"时长 {duration:g} 秒",
        f"比例 {clip.aspect_ratio}",
        f"模式 {mode}",
        f"质量 {_QUALITY_LABELS.get(quality, quality)}",
        f"参考图 {len(keys)} 张",
    ]
    start = info.get("start_frame") or ""
    if start:
        parts.append(f"首帧 {start}")
    return " ｜ ".join(parts)


def _asset_block(position: int, asset: ProductionGuideAsset) -> list[str]:
    """One reference-asset block in 《视频生成模型.md》 style: 素材N、定位说明、
    用途、生成方式、fenced 完整图片生成 Prompt."""
    if asset.asset_type == "character":
        positioning = (
            f"这张图不是一个视频分镜。它是「{asset.name}」在整部作品中的永久身份参考，"
            "后续所有出现该角色的镜头都应复用同一张图。"
        )
    elif asset.asset_type == "location":
        positioning = (
            f"这张图锁定「{asset.name}」的空间与视觉世界；本集所有发生在此的场景镜头"
            "都引用这一张作为环境锚点。"
        )
    elif asset.asset_type == "start_frame":
        positioning = (
            "这张图是某一段视频专用的第一帧画面（不是身份母图）：生成后作为该段的"
            "开镜画面（Start Frame）上传。"
        )
    elif asset.asset_type == "reference_frame":
        positioning = "这不是需要生成的图片，而是一条尾帧捕获指令：在对应视频段生成完成后执行。"
    else:
        positioning = "这一素材用于锁定跨镜头一致性。"
    prompt_label = (
        "尾帧捕获指令（视频生成完成后执行，不是图片生成）："
        if asset.asset_type == "reference_frame"
        else "完整图片生成 Prompt（直接复制到图片生成工具）："
    )
    lines = [
        f"### 素材{position}：{asset.asset_key}",
        "",
        positioning,
        "",
        f"- 名称：{asset.name}（{_ASSET_TYPE_LABELS.get(asset.asset_type, asset.asset_type)}）",
        f"- 用途：{_cell(asset.purpose)}",
        f"- 是否必需：{_NECESSITY_LABELS.get(asset.necessity, asset.necessity)}",
        f"- 使用范围：{_SCOPE_LABELS.get(asset.reuse_scope, asset.reuse_scope)}",
        f"- 保存命名：{asset.asset_key}",
        f"- 生成方式：{_ASSET_GENERATION_NOTE.get(asset.asset_type, '用任意文生图工具生成。')}",
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
    index: dict[str, str],
    character_names: dict[str, str],
    display_name: str,
    platform_terms: dict[str, str],
    negative_supported: bool,
) -> list[str]:
    """One per-video work order (task #31): 使用方式 → Start Frame →
    Ingredients → ONE fenced complete Prompt → 生成完成后 tail-frame action."""
    start_frame = str(info.get("start_frame")) if info.get("start_frame") else None
    scene_start = str(info.get("scene_start_frame")) if info.get("scene_start_frame") else None
    produces = bool(info.get("produces_next_start_frame"))
    keys = _clip_asset_keys(clip, index, start_frame, scene_start)
    char_keys = [key for key in keys if key.startswith("@CHAR_")]
    loc_keys = [key for key in keys if key.startswith("@LOC_")]
    prop_keys = [key for key in keys if key.startswith("@PROP_")]
    criteria = [
        *(
            f"{name} 脸部正确（与角色母图一致）"
            for name in _clip_character_names(clip, character_names)
        ),
        *(f"{prop} 位置与形状正确" for prop in (clip.prop_ids or [])),
        "手部没有变形、没有多余肢体或残影",
        "动作已基本停止，画面处于稳定可接续的姿态",
        "没有字幕、水印、乱码文本叠在画面上",
    ]
    saved = str(info.get("end_frame_saved") or "")
    mode = _clip_operational_mode(info)
    lines: list[str] = [
        "## 使用方式",
        "",
        f"生成模式：{mode}",
        "",
        f"平台操作（{display_name}）：",
    ]
    sf_term = platform_terms.get("start_frame") or "Start Frame"
    ref_term = platform_terms.get("reference_images") or "参考图"
    prompt_term = platform_terms.get("prompt_field") or "Prompt"
    if scene_start:
        lines.append(f"- {sf_term}：上传 {scene_start}（开镜画面，先用第四节素材 Prompt 生成）")
    elif start_frame:
        lines.append(f"- {sf_term}：上传 @{start_frame}（上一段保存的尾帧）")
    if char_keys:
        lines.append(f"- {ref_term}：上传 {'、'.join(char_keys)}（角色母图）")
    if loc_keys:
        lines.append(f"- {ref_term}：上传 {'、'.join(loc_keys)}（场景母图）")
    if prop_keys:
        lines.append(f"- {ref_term}：上传 {'、'.join(prop_keys)}（道具参考图）")
    lines.extend(
        [
            f"- {prompt_term}：复制下方完整 Prompt（整段一起复制，包括 STRICT 部分）",
            "",
            f"建议设置：{_settings_line(clip, info, keys)}",
            "",
            "### Start Frame",
            "",
        ]
    )
    if scene_start:
        lines.append(f"{scene_start}（独立开镜画面：先用素材区的图片 Prompt 生成这张图，再上传）")
    elif start_frame:
        lines.append(f"@{start_frame}（上一段视频保存的尾帧，物理连续性锚点）")
    else:
        lines.append("无（本段为文生视频自由开场，不依赖首帧）")
    lines += [
        "",
        "### Ingredients / References",
        "",
    ]
    if keys:
        lines.extend(keys)
    else:
        lines.append("（本段无参考素材，仅凭 Prompt 描述生成）")
    lines += [
        "",
        f"### Prompt {clip.clip_number}",
        "",
        (
            f"操作：挂好上列参考素材后，把下面整段 Prompt 原样复制到「{display_name}」"
            "生成本段视频："
        ),
        "",
    ]
    lines.extend(_fence(clip.copy_ready_prompt))
    lines.append("")
    # 可选 Negative Prompt（task #26）：只在目标模型适合独立负面词时给出，
    # 且完整 Prompt 不依赖它成立。
    if negative_supported and clip.negative_prompt:
        lines += ["可选 Negative Prompt（平台的负面词输入框，可留空）：", ""]
        lines.extend(_fence(clip.negative_prompt))
        lines.append("")
    lines += [
        f"### 视频{clip.clip_number}生成完成以后",
        "",
        f"在本段视频的第 {max(0.0, clip.duration_seconds - 1.0):.1f}～"
        f"{max(0.0, clip.duration_seconds - 0.2):.1f} 秒之间选择：",
        "",
    ]
    lines.extend(f"- {criterion}" for criterion in _dedupe_stable(criteria))
    if produces and saved.startswith("FRAME_"):
        lines += [
            "",
            "保存为：",
            "",
            f"{saved}",
            "",
            f"作为视频 {clip.clip_number + 1} 的 Start Frame。",
        ]
    else:
        lines += [
            "",
            "无需保存接续帧（下一段切换场景或全集收尾，直接进入下一段/后期）。",
        ]
    lines.append("")
    return lines


def _clip_character_names(clip: GenerationClip, character_names: dict[str, str]) -> list[str]:
    """Human names for the tail-frame criteria lines (deduplicated)."""
    names: list[str] = []
    for character_id in clip.character_ids:
        name = str(character_names.get(character_id) or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def render_guide_markdown(guide: ExecutableVideoProductionGuide) -> str:
    """Render the complete continuous handbook (deterministic, idempotent).

    Structure follows the user's reference 《视频生成模型.md》: permanent
    character/location/prop assets FIRST with complete image prompts, then the
    episode structure table, then one work order per 视频 N (使用方式 / Start
    Frame / Ingredients / 完整 Prompt / 生成完成后), then the tail-frame
    workflow, subtitle / UI composite / dialogue / SFX / BGM / editing /
    checklist sections. Chinese narration + English fenced prompt blocks; the
    model's capabilities appear as operational instructions only; the only
    tokens that leak through are user-facing ``asset_key`` strings and
    ``FRAME_NN`` labels. Identical guide inputs yield byte-identical markdown.
    """
    overview = dict(guide.overview)
    assets = guide.required_assets
    clips = _sorted_clips(guide.clip_workflows)
    index = _asset_key_index(assets)
    character_names = {
        asset.character_id: asset.name
        for asset in assets
        if asset.asset_type == "character" and asset.character_id
    }
    ranges, total_duration = _clip_time_ranges(clips)
    range_by_clip = {number: (start, end) for number, start, end in ranges}
    chain_clips: dict[str, Any] = dict(guide.frame_chain.get("clips") or {})
    shot_narrative = {
        str(key): str(value)
        for key, value in dict(overview.get("shot_narrative") or {}).items()
    }
    display_name = guide.target_video_model_display_name or str(
        overview.get("target_model") or ""
    )
    platform_terms = {
        str(key): str(value)
        for key, value in dict(overview.get("platform_terms") or {}).items()
    }
    negative_supported = bool(overview.get("negative_prompt_supported"))
    base_params = dict(overview.get("base_params") or {})
    operation_notes = [
        str(line)
        for line in list(overview.get("operation_notes") or [])
        if str(line).strip()
    ]
    editing = dict(guide.editing_plan)
    episode_title = str(overview.get("episode_title") or f"第{guide.episode_number}集")

    # Section numbering: fixed pre-video sections 1–5, then one section per
    # video (6..5+K), then the tail workflow + post sections.
    clip_count = len(clips)

    parts: list[str] = []
    parts.append(f"# EP{guide.episode_number:02d}《{episode_title}》")
    parts.append(f"# {display_name} AI视频完整制作方案")
    parts.append("")
    parts.append(
        "> 本手册是可直接执行的完整制作方案：先备齐素材区的角色/场景/道具参考图，"
        "再按「视频 N」的顺序逐段生成（每段复制完整 Prompt），"
        "最后按后期章节合成成片。全程不需要回到系统查其它卡片。"
    )
    parts.append("")
    parts.append(
        f"**目标模型**：{display_name} ｜ **画幅**：{guide.aspect_ratio}"
        f" ｜ **总时长**：{_fmt_clock(total_duration)} ｜ **视频段数**：{clip_count}"
    )
    parts.append("")

    # 一、制作目标与基础设置 --------------------------------------------
    parts += [
        "## 一、制作目标与基础设置",
        "",
        (
            f"本集目标：用「{display_name}」生成 EP{guide.episode_number:02d}"
            f"「{episode_title}」整集视频——共 {clip_count} 个视频段、约 "
            f"{_fmt_clock(total_duration)}，画幅 {guide.aspect_ratio}。"
        ),
        "",
        "制作顺序固定为「先素材、后视频、再后期」：",
        "",
        (
            "1. 备料：先生成第二、三、四节列出的全部参考素材并按 asset_key 命名归档"
            "（角色母图优先，它们在全集乃至后续各集固定复用）。"
        ),
        "2. 逐段生成：按「视频 N」章节顺序逐段复制 Prompt 生成，"
        "同一场景内上一段尾帧 = 下一段首帧。",
        "3. 后期：按字幕 / UI 合成 / 对白 / SFX / BGM 各节完成声音与画面后期。",
        "4. 合成：按剪辑顺序拼接成片，导出前对照最终检查清单逐项验收。",
        "",
        "基础参数（整集固定，不要逐段更改）：",
        "",
    ]
    param_rows = [
        ["目标模型", _cell(base_params.get("target_model") or display_name)],
        ["厂商", _cell(base_params.get("vendor"))],
        ["目标画幅", _cell(base_params.get("aspect_ratio") or guide.aspect_ratio)],
        ["支持画幅", _cell(base_params.get("supported_aspect_ratios"))],
        ["单段时长", _cell(base_params.get("duration"))],
        ["Prompt 语言", _cell(base_params.get("prompt_language") or guide.prompt_language)],
    ]
    parts += _md_table(["参数", "取值"], param_rows)
    parts += ["", "操作要点（把模型能力翻译成拍摄动作，逐条执行）：", ""]
    if operation_notes:
        parts += [f"- {line}" for line in operation_notes]
    else:
        parts.append("- 按平台默认方式生成，Prompt 内不引用本平台不支持的能力。")
    parts.append("")

    # 素材全局编号（素材1、素材2…贯穿二三四节） ---------------------------
    asset_position = 0

    # 二、先建立永久角色参考素材 ------------------------------------------
    characters = [asset for asset in assets if asset.asset_type == "character"]
    styles = [asset for asset in assets if asset.asset_type == "style"]
    permanent = characters + styles
    parts += [
        "## 二、先建立永久角色参考素材",
        "",
        (
            "本节是「永久身份锚点」素材：角色母图锁定人物长相，风格基准图锁定整集画风，"
            "全集乃至后续各集复用同一张图。请先全部生成并按 asset_key 命名归档，"
            "再开始生成任何视频段。"
        ),
        "",
    ]
    for asset in permanent:
        asset_position += 1
        parts += _asset_block(asset_position, asset)
    if not permanent:
        parts += ["本集未识别到需预生成的永久参考素材。", ""]

    # 三、本集需要建立的场景参考素材 --------------------------------------
    locations = [asset for asset in assets if asset.asset_type == "location"]
    parts += [
        "## 三、本集需要建立的场景参考素材",
        "",
        (
            "本节素材锁定本集的空间（Environment Master）：场景母图确定后，"
            "凡在此场景发生的视频段都引用同一张。"
        ),
        "",
    ]
    for asset in locations:
        asset_position += 1
        parts += _asset_block(asset_position, asset)
    if not locations:
        parts += ["本集未识别到专用场景素材。", ""]

    # 四、关键道具 / UI / 屏幕与开镜画面素材 ------------------------------
    others = [
        asset
        for asset in assets
        if asset.asset_type in ("prop", "start_frame", "reference_frame")
    ]
    parts += [
        "## 四、关键道具 / UI / 屏幕与开镜画面素材",
        "",
        (
            "本节包含：关键道具参考图、各视频段的 Scene Start Frame（独立开镜画面），"
            "以及「FRAME_NN 尾帧捕获指令」（不是图片，是每段生成完成后的动作）。"
        ),
        "",
    ]
    for asset in others:
        asset_position += 1
        parts += _asset_block(asset_position, asset)
    if not others:
        parts += ["本集未识别到道具/开镜画面素材。", ""]

    # 五、整集视频结构 -----------------------------------------------------
    structure_rows: list[list[str]] = []
    for clip in clips:
        start, end = range_by_clip.get(clip.clip_number, (0.0, 0.0))
        info = dict(chain_clips.get(str(clip.clip_number)) or {})
        start_frame = str(info.get("start_frame")) if info.get("start_frame") else None
        scene_start = str(info.get("scene_start_frame")) if info.get("scene_start_frame") else None
        keys = _clip_asset_keys(clip, index, start_frame, scene_start)
        structure_rows.append(
            [
                f"{_fmt_clock(start)}–{_fmt_clock(end)}",
                f"视频 {clip.clip_number}",
                _cell(clip.purpose or clip.visual_intent),
                _clip_short_mode(info),
                "、".join(keys) if keys else "—",
            ]
        )
    parts += [
        "## 五、整集视频结构",
        "",
        f"全集共 {clip_count} 个视频段，总时长约 {_fmt_clock(total_duration)}，"
        "按以下顺序生成与拼接：",
        "",
    ]
    parts += _md_table(
        ["时间", "视频段", "作用", "模式", "使用素材"],
        structure_rows,
    )
    parts.append("")

    # 六..、视频 N 工单 ----------------------------------------------------
    for clip in clips:
        start, end = range_by_clip.get(clip.clip_number, (0.0, 0.0))
        info = dict(chain_clips.get(str(clip.clip_number)) or {})
        parts.append(
            f"## {_cn_num(5 + clip.clip_number)}、视频 {clip.clip_number}："
            f"{_fmt_clock(start)}–{_fmt_clock(end)}"
        )
        parts.append("")
        if shot_narrative.get(str(clip.clip_number)):
            parts.append(f"来源分镜：{_cell(shot_narrative[str(clip.clip_number)])}")
            parts.append("")
        parts += _clip_work_order_block(
            clip,
            info,
            index,
            character_names,
            display_name,
            platform_terms,
            negative_supported,
        )

    # 尾帧接首帧完整流程 ----------------------------------------------------
    tail_number = _cn_num(6 + clip_count)
    parts += [
        f"## {tail_number}、尾帧接首帧完整流程",
        "",
        "整个连续性体系由四种锚点组成，各管一件事，不能混为一谈：",
        "",
    ]
    anchors = [
        dict(anchor)
        for anchor in list(guide.continuity_workflow.get("anchors") or [])
        if isinstance(anchor, dict)
    ]
    if anchors:
        anchor_rows = [
            [_cell(anchor.get("name")), _cell(anchor.get("role")), _cell(anchor.get("desc"))]
            for anchor in anchors
        ]
        parts += _md_table(["锚点", "作用", "说明"], anchor_rows)
    else:
        parts.append("（按角色母图 / 场景母图 / 开镜画面 / 上一镜尾帧四类锚点管理连续性。）")
    parts += ["", "尾帧链全景：", ""]
    parts += _fence("\n".join(str(line) for line in list(guide.frame_chain.get("diagram") or [])))
    parts += ["", "逐段衔接表：", ""]
    chain_rows: list[list[str]] = []
    for clip in clips:
        info = dict(chain_clips.get(str(clip.clip_number)) or {})
        start_frame = str(info.get("start_frame")) if info.get("start_frame") else None
        scene_start = str(info.get("scene_start_frame")) if info.get("scene_start_frame") else None
        first = scene_start or (f"@{start_frame}" if start_frame else "无（文生视频开场）")
        chain_rows.append(
            [
                f"视频 {clip.clip_number}",
                first,
                str(info.get("end_frame_saved") or "—"),
                _cell(info.get("selection_window")),
            ]
        )
    parts += _md_table(["视频段", "首帧", "收尾保存", "挑选区间"], chain_rows)
    parts.append("")

    # 字幕时间轴 ------------------------------------------------------------
    parts += [f"## {_cn_num(7 + clip_count)}、字幕时间轴", ""]
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

    # 手机 / 邮件 / UI 后期合成方案 -----------------------------------------
    parts += [f"## {_cn_num(8 + clip_count)}、手机 / 邮件 / UI 后期合成方案", ""]
    if guide.screen_composite_plan:
        parts += ["逐段确认（画面内可读文本 / 后期 UI）：", ""]
        for entry in guide.screen_composite_plan:
            if not isinstance(entry, dict):
                continue
            number = int(entry.get("clip_number", 0) or 0)
            matched = "、".join(str(item) for item in list(entry.get("matched") or []))
            suffix = f"｜命中词：{matched}" if matched else ""
            parts.append(
                f"- **视频 {number:02d}**（{_cell(entry.get('category'))}{suffix}）"
                f"：{_cell(entry.get('recommendation'))}"
            )
        parts.append("")
    else:
        parts += ["本集无画面内文本风险，无需特殊合成处理。", ""]

    # 对白与环境音 ----------------------------------------------------------
    parts += [f"## {_cn_num(9 + clip_count)}、对白与环境音", ""]
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
                    f"视频 {number}",
                    f"{_fmt_clock(start)}–{_fmt_clock(end)}",
                    _cell(entry.get("speaker")),
                    _cell(entry.get("line")),
                    "原生对白（Prompt 驱动口型与语音）"
                    if strategy == "native"
                    else "后期配音对齐口型",
                ]
            )
        parts += _md_table(["视频段", "台词时间", "说话人", "台词", "处理方式"], dialogue_rows)
        parts.append("")
    else:
        parts += ["本集无台词对白，确认环境声与表演足以支撑叙事。", ""]

    # SFX -------------------------------------------------------------------
    parts += [f"## {_cn_num(10 + clip_count)}、SFX", "", "逐段铺设，声音不硬切：", ""]
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
                f"视频 {number}",
                f"{_fmt_clock(start)}–{_fmt_clock(end)}",
                environment or "（无）",
                key_sfx or "（无）",
                _cell(entry.get("transition")),
            ]
        )
    parts += _md_table(["视频段", "时间", "环境声", "关键音效", "过渡处理"], sound_rows)
    parts.append("")

    # BGM -------------------------------------------------------------------
    bgm_note = str(guide.bgm_plan.get("note") or "")
    bgm_prompt = str(guide.bgm_plan.get("prompt") or "")
    bgm_total = _as_float(guide.bgm_plan.get("total_duration_seconds"), total_duration)
    parts += [
        f"## {_cn_num(11 + clip_count)}、BGM 完整生成 Prompt",
        "",
        bgm_note or "整集铺一条统一配乐，不在视频段边界断开。",
        "",
        (
            f"用单独的音乐生成工具（如 Suno / Udio / Stable Audio），"
            f"把下面整段 Prompt 复制进去，生成一整条约 {bgm_total:.0f} 秒的配乐，"
            "从 00:00 连续铺到片尾，不要按视频段切开分段生成："
        ),
        "",
    ]
    if bgm_prompt:
        parts += _fence(bgm_prompt)
    else:
        parts.append("本集未提供 BGM 生成 Prompt，请在音乐工具中按上方方向生成整条配乐。")
    parts.append("")

    # 剪辑顺序与转场 --------------------------------------------------------
    rules = [str(rule) for rule in list(editing.get("rules") or [])]
    parts += [
        f"## {_cn_num(12 + clip_count)}、剪辑顺序与转场",
        "",
        "剪辑三原则：",
        "",
    ]
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
                f"视频 {from_clip} → 视频 {to_clip}",
                _cell(transition.get("type")),
                _cell(transition.get("reason")),
            ]
        )
    if transition_rows:
        parts += _md_table(["剪辑点", "方式", "原因"], transition_rows)
    else:
        parts.append("单段成片，无剪辑点。")
    parts.append("")
    if str(editing.get("audio_crossfade") or ""):
        parts.append(f"- 声音过渡：{editing.get('audio_crossfade')}")
    if str(editing.get("bgm") or ""):
        parts.append(f"- BGM 铺设：{editing.get('bgm')}")
    parts += ["", "最终成片顺序（直接拼接，不再调整镜头次序）：", ""]
    order = [int(number) for number in list(editing.get("order") or [])]
    if not order:
        order = [clip.clip_number for clip in clips]
    parts += _fence("\n".join(f"视频 {number}" for number in order))
    parts += [
        "",
        (
            f"拼接完成后总时长约 {_fmt_clock(total_duration)}：画面硬切/短叠化，"
            "声音不硬切（环境声 0.3–0.8 秒交叉淡化），整条 BGM 从 00:00 连续铺到片尾。"
        ),
        "",
    ]

    # 最终检查清单 ----------------------------------------------------------
    parts += [
        f"## {_cn_num(13 + clip_count)}、最终检查清单",
        "",
        "成片导出前逐项打勾：",
        "",
    ]
    parts += [str(item) for item in guide.final_checklist if str(item).strip()]
    parts.append("")
    return "\n".join(parts).rstrip() + "\n"
