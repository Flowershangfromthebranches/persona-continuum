"""Deterministic voice firewall and multi-channel action/speech normalizer."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from persona_continuum.domain.scene import ActionEvent, TurnChannels

PAREN = re.compile(r"（[^（）]*）|\([^()]*\)|(?m:^\s*\*[^*\n]+\*\s*$)")
POLICY = re.compile(
    r"(?:作为(?:一个)?(?:AI|人工智能)|As an AI|AI policy|OpenAI policy|根据.*政策)", re.I
)
NEXT_MORNING = re.compile(
    r"(?:现在(?:是|已经是)?|到了)?(?:第二天|翌日|次日)(?:的)?(?:早上|早晨|清晨)"
)
SLEEP = re.compile(r"^(?:我)?(?:要|准备|先)?(?:去)?(?:睡觉|睡了|睡眠)(?:了|啦|吧)?[。！!\s]*$")
WAKE = re.compile(r"^(?:我)?(?:已经)?(?:醒了|起床了|醒来)[。！!\s]*$")
NEGATED = re.compile(r"不|没|别|如果|假如|要是|明天|昨晚|昨天|说过|说：|说:|[\?\uFF1F\"“]")

STAND_UP = re.compile(r"^(?:我)?(?:站起来|站起身|起立|站立)(?:伸了个懒腰)?[。！!\s]*$")
SIT_DOWN = re.compile(r"^(?:我)?(?:坐下|坐好|坐着|坐到(?:椅子|沙发|床上)?)[。！!\s]*$")
LIE_DOWN = re.compile(r"^(?:我)?(?:躺下|躺好|躺在床上|躺着)(?:睡觉)?[。！!\s]*$")
WALK_OR_STEP = re.compile(r"^(?:我)?(?:走几步|踱步|散步|往前走|退后一步|后退)[。！!\s]*$")

APPROACH = re.compile(
    r"^(?:我)?(?:向|朝|走到|走近|来到)(.+?)(?:走过去|走近|身边|身旁|面前|旁边)[。！!\s]*$"
)
SIT_BESIDE = re.compile(r"^(?:我)?(?:坐到|坐在)(.+?)(?:身边|身旁|旁边)[。！!\s]*$")
HOLD_HANDS = re.compile(r"^(?:我)?(?:牵住|握住|拉住)(.+?)(?:的手|手)[。！!\s]*$")
HUG_ACTION = re.compile(r"^(?:我)?(?:抱住|拥抱|抱了|紧紧抱住)(.+?)[。！!\s]*$")
KISS_ACTION = re.compile(r"^(?:我)?(?:亲吻|亲了|吻了)(.+?)[。！!\s]*$")


def _classify_salience(action_type: str) -> str:
    if action_type in {"stand_up", "sit_down", "lie_down", "walk", "pick_up", "look_at"}:
        return "low"
    if action_type in {"approach", "step_back", "sit_beside", "wave", "nod", "smile"}:
        return "medium"
    if action_type in {"hug", "kiss", "hold_hands", "pull", "push", "conflict", "confession"}:
        return "high"
    return "low"


def _intents(text: str, *, stage: bool = False, actor: str = "user") -> list[dict[str, Any]]:
    text = text.strip()
    result: list[dict[str, Any]] = []
    sentences = [part.strip() for part in re.split(r"[。！!\n，,；;]", text) if part.strip()]
    if len(sentences) > 1:
        return [
            event
            for sentence in sentences
            for event in _intents(sentence, stage=stage, actor=actor)
        ]
    temporal = NEXT_MORNING.search(text)
    if temporal and not NEGATED.search(text):
        result.append({"type": "time_advance", "payload": {"intent": "next_morning"}})
        text = NEXT_MORNING.sub("", text).strip(" ，,。")
    advance = re.fullmatch(r"(?:时间)?(?:推进|快进|过去)(\d+(?:\.\d+)?)\s*(小时|分钟)", text)
    if advance:
        seconds = float(advance[1]) * (3600 if advance[2] == "小时" else 60)
        result.append({"type": "time_advance", "payload": {"seconds": seconds}})
    if SLEEP.fullmatch(text):
        result.append({"type": "activity_start", "payload": {"activity": "sleeping"}})
    if WAKE.fullmatch(text):
        result.extend(
            [
                {"type": "activity_end", "payload": {"activity": "sleeping"}},
                {"type": "activity_start", "payload": {"activity": "awake"}},
            ]
        )
    if STAND_UP.fullmatch(text):
        result.append(
            {
                "type": "action_event",
                "payload": {
                    "action_type": "stand_up",
                    "category": "self",
                    "salience": "low",
                    "parameters": {"posture": "standing"},
                },
            }
        )
    elif SIT_DOWN.fullmatch(text):
        result.append(
            {
                "type": "action_event",
                "payload": {
                    "action_type": "sit_down",
                    "category": "self",
                    "salience": "low",
                    "parameters": {"posture": "sitting"},
                },
            }
        )
    elif LIE_DOWN.fullmatch(text):
        result.append(
            {
                "type": "action_event",
                "payload": {
                    "action_type": "lie_down",
                    "category": "self",
                    "salience": "low",
                    "parameters": {"posture": "lying"},
                },
            }
        )
    elif WALK_OR_STEP.fullmatch(text):
        result.append(
            {
                "type": "action_event",
                "payload": {
                    "action_type": "walk",
                    "category": "self",
                    "salience": "low",
                    "parameters": {"manner": "step"},
                },
            }
        )
    m_sit_beside = SIT_BESIDE.fullmatch(text)
    if m_sit_beside:
        target = m_sit_beside[1].strip()
        result.append(
            {
                "type": "action_event",
                "payload": {
                    "action_type": "sit_beside",
                    "target_id": target,
                    "category": "interactive",
                    "salience": "medium",
                    "parameters": {"posture": "sitting", "proximity": "adjacent"},
                },
            }
        )
    else:
        m_appr = APPROACH.fullmatch(text)
        if m_appr:
            target = m_appr[1].strip()
            result.append(
                {
                    "type": "action_event",
                    "payload": {
                        "action_type": "approach",
                        "target_id": target,
                        "category": "interactive",
                        "salience": "medium",
                        "parameters": {"proximity": "near"},
                    },
                }
            )
    m_hug = HUG_ACTION.fullmatch(text)
    if m_hug:
        target = m_hug[1].strip()
        result.append(
            {
                "type": "action_event",
                "payload": {
                    "action_type": "hug",
                    "target_id": target,
                    "category": "interactive",
                    "salience": "high",
                    "parameters": {"contact": "hugging", "proximity": "adjacent"},
                },
            }
        )
    m_hold = HOLD_HANDS.fullmatch(text)
    if m_hold:
        target = m_hold[1].strip()
        result.append(
            {
                "type": "action_event",
                "payload": {
                    "action_type": "hold_hands",
                    "target_id": target,
                    "category": "interactive",
                    "salience": "high",
                    "parameters": {"contact": "holding_hands", "proximity": "near"},
                },
            }
        )
    m_kiss = KISS_ACTION.fullmatch(text)
    if m_kiss:
        target = m_kiss[1].strip()
        result.append(
            {
                "type": "action_event",
                "payload": {
                    "action_type": "kiss",
                    "target_id": target,
                    "category": "interactive",
                    "salience": "high",
                    "parameters": {"contact": "kissing", "proximity": "adjacent"},
                },
            }
        )
    if stage and not NEGATED.search(text):
        location = re.fullmatch(r"(?:我)?(?:回到|回了|来到|走进)([^，。！\n]{1,30})[。！]?", text)
        if location:
            result.append({"type": "location_change", "payload": {"location": location[1]}})
        action = re.fullmatch(
            r"(?:我)?(抱住|拥抱|抱了|挥手|点头|微笑)([^，。！\n]{0,30})[。！]?", text
        )
        if action:
            kind = {
                "抱住": "hug",
                "拥抱": "hug",
                "抱了": "hug",
                "挥手": "wave",
                "点头": "nod",
                "微笑": "smile",
            }[action[1]]
            category = "interactive" if action[2] or kind in {"hug"} else "self"
            salience = _classify_salience(kind)
            result.append(
                {
                    "type": "action_event",
                    "payload": {
                        "action_type": kind,
                        "target_id": action[2] or "unspecified",
                        "category": category,
                        "salience": salience,
                        "parameters": (
                            {"contact": "hugging", "proximity": "adjacent"} if kind == "hug" else {}
                        ),
                    },
                }
            )
        if text in {"离开", "离线", "回来了", "上线"}:
            result.append(
                {
                    "type": "presence_change",
                    "payload": {"presence": "absent" if text in {"离开", "离线"} else "present"},
                }
            )
    return result


def _try_split_mixed_turn(raw: str) -> tuple[list[dict[str, Any]], str] | None:
    pattern = re.compile(
        r"^(我.+?)(?:，|,)?(?:\s*)(?:笑着说|微笑着说|轻声说|认真地说|转头说|对她说|对他说|说道|说|道)[:：]\s*[“\"「](.+?)[”\"」][。！!]*$",
        re.DOTALL,
    )
    m = pattern.fullmatch(raw.strip())
    if m:
        action_part = m[1].strip()
        speech_part = m[2].strip()
        actions = _intents(action_part, stage=True)
        if actions:
            return actions, speech_part
    return None


def normalize_turn(raw: str, *, actor: str = "user", input_mode: str = "speech") -> TurnChannels:
    channels = TurnChannels(raw_content=raw, input_mode=input_mode)  # type: ignore[arg-type]
    value = raw.strip()
    if input_mode == "action" and actor == "user":
        parsed = _intents(value, stage=True, actor=actor)
        channels.spoken_text = ""
        channels.input_mode = "action"
        for item in parsed:
            if item["type"] == "action_event":
                p = dict(item["payload"])
                p["payload"] = dict(p)
                channels.actions.append(p)
                channels.action_events.append(item["payload"])
            else:
                channels.scene_events.append(item)
        if not channels.actions and not channels.scene_events:
            act = {
                "action_type": "physical_action",
                "parameters": {"description": value},
                "category": "self",
                "salience": "low",
            }
            channels.actions.append(act)
            channels.action_events.append(act)
        return channels
    structured: Any = None
    with contextlib.suppress(ValueError, TypeError):
        structured = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", value))
    prefix = ""
    if not isinstance(structured, dict) and actor != "user":
        opening = value.find("{")
        if opening >= 0:
            with contextlib.suppress(ValueError, TypeError):
                candidate, end = json.JSONDecoder().raw_decode(value[opening:])
                tail = value[opening + end :].strip().strip("`")
                if (
                    isinstance(candidate, dict)
                    and isinstance(candidate.get("speech"), str)
                    and not tail
                ):
                    structured = candidate
                    prefix = value[:opening].strip()
    if isinstance(structured, dict) and isinstance(structured.get("speech"), str):
        channels = normalize_turn(structured["speech"], actor=actor)
        channels.raw_content = raw
        if prefix:
            channels.non_voice_context.append(prefix)
        for act in structured.get("actions", []):
            if isinstance(act, dict):
                atype = str(act.get("action_type") or act.get("action") or "physical_action")
                target = str(act.get("target_id") or act.get("target") or "") or None
                salience = str(act.get("salience") or _classify_salience(atype))
                category = str(act.get("category") or ("interactive" if target else "self"))
                payload_dict = {"action": atype, "target": target}
                norm_act = {
                    "action_type": atype,
                    "action": atype,
                    "target_id": target,
                    "target": target,
                    "parameters": act.get("parameters") or {},
                    "salience": salience,
                    "category": category,
                    "payload": payload_dict,
                }
                channels.actions.append(norm_act)
                channels.action_events.append(norm_act)
        for update in structured.get("scene_updates", []):
            if isinstance(update, dict):
                channels.scene_events.append(update)
        return channels
    mixed = _try_split_mixed_turn(value)
    if mixed:
        actions, speech = mixed
        channels.spoken_text = speech
        channels.input_mode = "mixed"
        for act in actions:
            if act["type"] == "action_event":
                payload_val = act.get("payload")
                payload_dict = dict(payload_val) if isinstance(payload_val, dict) else {}
                channels.actions.append(payload_dict)
                channels.action_events.append(payload_dict)
            else:
                channels.scene_events.append(act)
        return channels

    def remove(match: re.Match[str]) -> str:
        interior = match[0].strip().strip("（）()* ")
        events = _intents(interior, stage=True, actor=actor)
        if actor != "user":
            events = [e for e in events if e["type"] != "time_advance"]
        if not events:
            channels.non_voice_context.append(interior)
        for event in events:
            if event["type"] == "action_event":
                payload_dict = dict(event["payload"])
                payload_dict["action"] = payload_dict.get("action_type") or payload_dict.get(
                    "action"
                )
                payload_dict["payload"] = {
                    "action": payload_dict["action"],
                    "target": payload_dict.get("target_id") or payload_dict.get("target"),
                }
                channels.actions.append(payload_dict)
                channels.action_events.append(event["payload"])
            else:
                channels.scene_events.append(event)
        return ""

    speech = PAREN.sub(remove, raw)
    speech = "\n".join(line for line in speech.splitlines() if not POLICY.search(line))
    if actor == "user":
        user_intents = _intents(speech, actor=actor)
        for item in user_intents:
            if item["type"] == "action_event":
                p = dict(item["payload"])
                p["payload"] = dict(p)
                channels.actions.append(p)
                channels.action_events.append(item["payload"])
            else:
                channels.scene_events.append(item)
        if any(e["type"] == "time_advance" for e in channels.scene_events):
            speech = NEXT_MORNING.sub("", speech).lstrip(" ，,。")
    channels.spoken_text = speech.strip()
    channels.scene_events = list(
        {json.dumps(e, sort_keys=True): e for e in channels.scene_events}.values()
    )
    return channels


def normalize_turn_for_prompt(turn: dict[str, Any]) -> dict[str, Any]:
    metadata = turn.get("metadata") or {}
    stored = metadata.get("channels") or {}
    actor = str(turn.get("participant_id") or turn.get("persona_id") or "user")
    raw = str(turn.get("raw_content", turn.get("content", turn.get("persona_response", ""))))
    channels = normalize_turn(raw, actor=actor)
    if turn.get("normalization_version") in (1, 2) or stored.get("normalization_version") in (1, 2):
        source = turn if turn.get("normalization_version") in (1, 2) else stored
        channels = TurnChannels.model_validate({"raw_content": raw, **source})
    res = {
        **turn,
        "content": channels.spoken_text,
        "speaker_name": turn.get("speaker_name") or actor,
        "participant_id": actor,
        "scene_time": turn.get("scene_time") or stored.get("scene_time"),
        "spoken_text": normalize_turn(channels.spoken_text, actor=actor).spoken_text,
        "actions": channels.actions,
        "action_events": channels.action_events,
        "scene_events": channels.scene_events,
        "input_mode": channels.input_mode,
    }
    if "persona_response" in turn:
        res["persona_response"] = channels.spoken_text
    res.pop("raw_content", None)
    return res


def semantic_experience(
    user: str,
    response: str,
    *,
    counterpart: str = "user",
    events: list[dict[str, Any]] | None = None,
    action_events: list[ActionEvent] | None = None,
) -> str:
    """Conservative semantic envelope; only high-salience actions enter long-term memory."""
    u = normalize_turn(user)
    p = normalize_turn(response, actor="persona")
    facts = []
    salient_actions = []
    for ae in action_events or []:
        if ae.salience == "high":
            salient_actions.append(
                f"{ae.actor_id} 与 {ae.target_id or '环境'} 发生重要互动: {ae.action_type}"
            )
    all_events = list(events or [])
    for owner, parsed in ((counterpart, u), ("Persona", p)):
        for e in parsed.scene_events:
            all_events.append({"actor": owner, **e})
        for act in parsed.actions:
            if act.get("salience") == "high":
                all_events.append({"actor": owner, "type": "physical_action", "payload": act})
    for event in all_events:
        facts.append(
            json.dumps(
                {
                    "actor": event.get("actor", counterpart),
                    "type": event.get("type"),
                    "payload": event.get("payload", {}),
                },
                ensure_ascii=False,
            )
        )
    facts.extend(salient_actions)
    for who, speech in ((counterpart, u.spoken_text), ("Persona", p.spoken_text)):
        text = re.sub(r"[\n\r\t#*`]+", " ", speech)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            facts.append(f"{who}表述的内容：{text}")
    return "本次交流事件资料：" + "；".join(dict.fromkeys(facts))
