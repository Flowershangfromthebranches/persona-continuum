"""Robust deterministic temporal parser for Room Scene Runtime."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel

RELATIVE_PATTERN = re.compile(
    r"^\+?\s*(?:(?:(\d+(?:\.\d+)?)\s*(?:天|d|days?))\s*)?"
    r"(?:(?:(\d+(?:\.\d+)?)\s*(?:小时|个半小时|小时半|h|hours?))\s*)?"
    r"(?:(?:(\d+(?:\.\d+)?)\s*(?:分钟|分|m|mins?|minutes?))\s*)?"
    r"(?:(?:(\d+(?:\.\d+)?)\s*(?:秒|s|secs?|seconds?))\s*)?$",
    re.I,
)

KEYWORD_DURATIONS: dict[str, float] = {
    "半小时": 1800,
    "半天": 43200,
    "一天": 86400,
    "两天": 172800,
    "三天": 259200,
    "一小时": 3600,
    "两小时": 7200,
    "三小时": 10800,
    "四小时": 14400,
    "五小时": 18000,
    "六小时": 21600,
    "八小时": 28800,
    "十小时": 36000,
    "十二小时": 43200,
    "整夜": 28800,
    "一整天": 86400,
}

SEMANTIC_PATTERN = re.compile(
    r"^(?:到了|现在是|时间来到)?\s*"
    r"(今天|当天|明天|第二天|次日|翌日|后天|三天后)?"
    r"\s*(早上|早晨|清晨|上午|中午|下午|傍晚|晚上|深夜|午夜)?"
    r"(?:\s*(\d{1,2})[点:：时](\d{1,2})?分?)?$",
    re.I,
)

ANCHOR_HOURS: dict[str, int] = {
    "早上": 8,
    "早晨": 8,
    "清晨": 7,
    "上午": 10,
    "中午": 12,
    "下午": 15,
    "傍晚": 18,
    "晚上": 20,
    "深夜": 23,
    "午夜": 0,
}

DAY_OFFSETS: dict[str, int] = {
    "今天": 0,
    "当天": 0,
    "明天": 1,
    "第二天": 1,
    "次日": 1,
    "翌日": 1,
    "后天": 2,
    "三天后": 3,
}

ISO_PATTERN = re.compile(
    r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?$"
)


class ParsedTemporalIntent(BaseModel):
    kind: str
    seconds: float = 0.0
    target_scene_time: datetime | None = None
    semantic_rule: str = ""
    description: str = ""
    raw_input: str = ""


def parse_temporal_input(
    raw: str,
    reference_time: datetime,
    timezone_str: str = "Asia/Shanghai",
) -> ParsedTemporalIntent:
    text = raw.strip()
    if not text:
        raise ValueError("time_input_empty: 时间输入不能为空。")
    tz = ZoneInfo(timezone_str)
    local_ref = reference_time.astimezone(tz)
    clean_kw = text.lstrip("+ ＋").strip()
    if clean_kw in KEYWORD_DURATIONS:
        secs = KEYWORD_DURATIONS[clean_kw]
        target = local_ref + timedelta(seconds=secs)
        return ParsedTemporalIntent(
            kind="relative",
            seconds=secs,
            target_scene_time=target,
            description=f"时间推进 {clean_kw} ({int(secs // 3600)}小时)",
            raw_input=text,
        )
    m_rel = RELATIVE_PATTERN.fullmatch(clean_kw)
    if m_rel:
        d_str, h_str, m_str, s_str = m_rel.groups()
        if any((d_str, h_str, m_str, s_str)):
            days = float(d_str or 0)
            hours = float(h_str or 0)
            mins = float(m_str or 0)
            secs_val = float(s_str or 0)
            total_seconds = days * 86400 + hours * 3600 + mins * 60 + secs_val
            if total_seconds <= 0:
                raise ValueError("time_input_zero_or_negative: 推进时间必须大于0。")
            if total_seconds > 86400 * 366:
                raise ValueError("time_input_too_large: 单次时间推进不能超过 366 天。")
            target = local_ref + timedelta(seconds=total_seconds)
            return ParsedTemporalIntent(
                kind="relative",
                seconds=total_seconds,
                target_scene_time=target,
                description=f"时间推进 {text}",
                raw_input=text,
            )
    m_iso = ISO_PATTERN.fullmatch(text)
    if m_iso:
        y, m, d, hh, mm, ss = m_iso.groups()
        target = datetime(
            year=int(y),
            month=int(m),
            day=int(d),
            hour=int(hh or 0),
            minute=int(mm or 0),
            second=int(ss or 0),
            tzinfo=tz,
        )
        if target < local_ref:
            raise ValueError("time_cannot_rewind: 目标时间早于当前场景时间。")
        elapsed = (target - local_ref).total_seconds()
        return ParsedTemporalIntent(
            kind="absolute",
            seconds=elapsed,
            target_scene_time=target,
            description=f"时间跳转到 {target.strftime('%Y-%m-%d %H:%M')}",
            raw_input=text,
        )
    m_sem = SEMANTIC_PATTERN.fullmatch(text)
    if m_sem:
        day_part, tod_part, spec_h, spec_m = m_sem.groups()
        if day_part or tod_part or spec_h:
            day_offset = DAY_OFFSETS.get(day_part, 0)
            hour = int(spec_h) if spec_h is not None else ANCHOR_HOURS.get(tod_part or "", 8)
            minute = int(spec_m) if spec_m is not None else 0
            target = (local_ref + timedelta(days=day_offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if not day_part and target <= local_ref:
                target += timedelta(days=1)
            if target < local_ref:
                raise ValueError("time_cannot_rewind: 目标时间早于当前场景时间。")
            elapsed = (target - local_ref).total_seconds()
            return ParsedTemporalIntent(
                kind="semantic",
                seconds=elapsed,
                target_scene_time=target,
                semantic_rule=f"{day_part or ''}{tod_part or ''}",
                description=f"时间推进到 {text} ({target.strftime('%m-%d %H:%M')})",
                raw_input=text,
            )
    raise ValueError(
        f"unrecognized_time_expression: 无法解析时间表达「{text}」，"
        "支持「8小时」「30分钟」「第二天早上」「明天 09:00」等。"
    )
