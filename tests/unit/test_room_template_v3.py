from __future__ import annotations

import json
from typing import Any

from persona_continuum.room.models import (
    ParticipantSlot,
    RoomProtocolType,
    RoomSessionState,
)
from persona_continuum.room.orchestrator import MultiAgentOrchestrator
from persona_continuum.room.repository import DIVINATION_TEMPLATE_ID

# The 15 tool names that are no longer granted by any built-in provider.
_REMOVED_TOOL_NAMES = (
    "almanac",
    "bazi",
    "bazi_dayun",
    "bazi_pillars_resolve",
    "ziwei",
    "ziwei_horoscope",
    "ziwei_flying_star",
    "liuyao",
    "meihua",
    "xiaoliuren",
    "qimen",
    "daliuren",
    "taiyi",
    "astrology",
    "tarot",
)


def _builtin_template(app: Any) -> Any:
    app.orchestrator.protocol_repository.ensure_builtin_templates()
    template = app.orchestrator.protocol_repository.get_template(DIVINATION_TEMPLATE_ID)
    assert template is not None
    return template


def test_builtin_divination_template_v3_shape(app: Any) -> None:
    template = _builtin_template(app)
    assert template.id == DIVINATION_TEMPLATE_ID
    assert template.protocol == RoomProtocolType.EXPERT_CONSULTATION

    participants = template.participants
    assert len(participants) == 6
    host = participants[0]
    assert host.role == "host"
    assert host.authority == 100
    for expert in participants[1:]:
        assert expert.role == "expert"
        assert expert.authority == 70


def test_builtin_divination_template_v3_has_no_tool_permissions(app: Any) -> None:
    template = _builtin_template(app)
    # Slot-level permissions are gone.
    for participant in template.participants:
        assert participant.tool_permissions == []
    # Template-level permissions are gone too.
    assert template.tool_permissions == {}
    dumped = json.dumps(template.model_dump(mode="json"), ensure_ascii=False)
    for tool in _REMOVED_TOOL_NAMES:
        # Quoted form: participant ids like "slot_expert_bazi" are not tool
        # grants; only a full JSON string value counts.
        assert f'"{tool}"' not in dumped


def test_builtin_divination_template_v3_shared_context(app: Any) -> None:
    template = _builtin_template(app)
    background = template.shared_context.background
    assert "Taibu" not in background
    assert "MCP" not in background
    assert "工具" not in background
    # The V3 principle: experts own their own computation skills.
    assert "每位专家本身掌握所属体系的理论、计算、排盘、推演和判读方法" in background


def test_builtin_divination_template_v3_specialties_are_real_tags(app: Any) -> None:
    template = _builtin_template(app)
    experts = {item.display_name: item for item in template.participants[1:]}
    assert set(experts) == {"子平先生", "紫薇先生", "易卦先生", "三式先生", "西学占测师"}
    expected_head_tags = {
        "子平先生": ["四柱八字", "子平命理", "十神", "大运", "流年"],
        "紫薇先生": ["紫微斗数", "四化", "宫位", "大限"],
        "易卦先生": ["六爻", "梅花易数", "卦象", "应期"],
        "三式先生": ["奇门遁甲", "大六壬", "太乙神数"],
        "西学占测师": ["西方占星", "塔罗", "行运", "推运"],
    }
    for display_name, head_tags in expected_head_tags.items():
        expert = experts[display_name]
        assert expert.specialties, f"{display_name} must declare real specialty tags"
        for tag in head_tags:
            assert tag in expert.specialties
        for tag in expert.specialties:
            assert tag.strip()
            assert tag not in _REMOVED_TOOL_NAMES


def test_builtin_divination_template_v3_declares_topic(app: Any) -> None:
    template = _builtin_template(app)
    dumped = template.model_dump(mode="json")
    # RoomTemplate is extra="allow", so the topic survives the round-trip.
    assert "多体系联合会诊" in dumped.get("topic", "")


def test_builtin_divination_template_names_taibuge_roles_and_discloses_risk(app: Any) -> None:
    template = _builtin_template(app)
    assert template.name == "太卜阁 · 术数综合会诊"
    assert [item.display_name for item in template.participants] == [
        "玄衡先生",
        "子平先生",
        "紫薇先生",
        "易卦先生",
        "三式先生",
        "西学占测师",
    ]
    rules = "\n".join(template.shared_context.rules)
    assert "仅供娱乐" in rules
    assert "请勿过度迷信" in rules
    assert "不构成投资" in rules


def _persona(app: Any, persona_id: str) -> None:
    app.personas.create_from_manifest(
        {
            "id": persona_id,
            "display_name": persona_id,
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )


def _slot(persona_id: str, role: str) -> ParticipantSlot:
    return ParticipantSlot(
        participant_id=f"slot_{persona_id}",
        persona_id=persona_id,
        display_name=persona_id,
        role=role,
    )


def test_normalize_legacy_room_tool_permissions_only_strips_removed_tools(app: Any) -> None:
    for persona_id in ("legacy_host", "legacy_expert", "other_host", "other_expert"):
        _persona(app, persona_id)

    legacy = app.orchestrator.create_room(
        title="legacy divination",
        topic="q",
        participants=[_slot("legacy_host", "host"), _slot("legacy_expert", "expert")],
        template_id=DIVINATION_TEMPLATE_ID,
    )
    other = app.orchestrator.create_room(
        title="other template",
        topic="q",
        participants=[_slot("other_host", "host"), _slot("other_expert", "expert")],
        template_id="some_other_template",
    )

    legacy_state = app.orchestrator.get_room(legacy.id)
    assert legacy_state is not None
    legacy_state.participants[0].tool_permissions = ["almanac", "custom_search"]
    legacy_state.participants[1].tool_permissions = list(_REMOVED_TOOL_NAMES)
    app.orchestrator._save_room_state(legacy_state, force=True)

    other_state = app.orchestrator.get_room(other.id)
    assert other_state is not None
    other_state.participants[0].tool_permissions = ["bazi", "kept_tool"]
    app.orchestrator._save_room_state(other_state, force=True)

    normalized = app.orchestrator.normalize_legacy_room_tool_permissions()
    assert normalized == 1

    refreshed = app.orchestrator.get_room(legacy.id)
    assert refreshed is not None
    assert refreshed.participants[0].tool_permissions == ["custom_search"]
    assert refreshed.participants[1].tool_permissions == []

    untouched = app.orchestrator.get_room(other.id)
    assert untouched is not None
    assert untouched.participants[0].tool_permissions == ["bazi", "kept_tool"]

    # Legacy rooms must remain openable after normalization.
    assert isinstance(refreshed, RoomSessionState)
    assert isinstance(app.orchestrator, MultiAgentOrchestrator)
