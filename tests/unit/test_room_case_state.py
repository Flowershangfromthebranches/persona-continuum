"""Requirement §27/§28 regression tests for RoomCaseState and the clarification gate.

Covers:

- §27  anchor facts: the first user turn defines intent/problem_definition
- §27  sticky merge: follow-ups never overwrite the anchor or sticky facts
- §27  clarification budget: one blocking clarification per case at most
- §28  output-vs-input boundary: deliverables must never be demanded back
- prompt-block projection excludes bookkeeping fields
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest

from persona_continuum.room.case_state import (
    MAX_BLOCKING_CLARIFICATIONS,
    RoomCaseState,
    extract_facts,
    is_output_intent_question,
    merge_case_state,
    merge_from_host_analysis,
    violates_output_input_boundary,
)
from persona_continuum.room.models import (
    ParticipantSlot,
    RoomProtocolConfig,
    RoomProtocolType,
)
from persona_continuum.room.protocol_runtime import (
    ProtocolActionRequest,
    RoomProtocolRuntime,
)

FIRST_TURN = (
    "我是男性，1990年5月1日10点30分出生于杭州，"
    "想请教老师我明年的财运如何，适合做什么方向的投资。"
)
FOLLOW_UP = "补充一下：这笔钱计划持有一年，最大承受10万亏损。"

_LEGAL_CLARIFICATION = "请提供您的出生时间"
_OUTPUT_ASK_BACK = "您问哪天更适合买，请提供您打算买入的具体日期"


# --- §27 anchor facts ------------------------------------------------------


def test_first_user_message_establishes_anchor_facts() -> None:
    state = merge_case_state(None, FIRST_TURN)

    assert state.intent == FIRST_TURN
    assert state.problem_definition == FIRST_TURN
    assert state.last_user_message == FIRST_TURN

    facts = extract_facts(FIRST_TURN)
    assert facts["gender"] == "male"
    assert facts["birth_date"] == "1990-05-01"
    assert facts["birth_time"] == "10:30"
    assert facts["birth_datetime"] == "1990-05-01 10:30"
    assert facts["birth_place"] == "杭州"
    assert state.known_user_facts["birth_datetime"] == "1990-05-01 10:30"


def test_followup_merge_preserves_anchor_and_sticky_facts() -> None:
    state = merge_case_state(None, FIRST_TURN)
    state = merge_case_state(state, FOLLOW_UP)

    # The original goal is the case anchor: a follow-up must never replace it.
    assert state.intent == FIRST_TURN
    assert state.problem_definition == FIRST_TURN
    assert state.last_user_message == FOLLOW_UP

    facts = state.known_user_facts
    # Sticky structural facts from round one all survive round two.
    assert facts["gender"] == "male"
    assert facts["birth_date"] == "1990-05-01"
    assert facts["birth_time"] == "10:30"
    assert facts["birth_datetime"] == "1990-05-01 10:30"
    assert facts["birth_place"] == "杭州"
    # New structural facts from the follow-up merge in. The durations
    # bucket is heuristic and also picks up birth-date components, so only
    # assert membership of the follow-up constraint.
    assert "一年" in facts["mentioned_durations"]
    assert facts["mentioned_amounts"] == ["10万"]


def test_vague_later_mention_never_downgrades_sticky_fact() -> None:
    state = merge_case_state(None, FIRST_TURN)
    # A later message repeating a partial fact must not overwrite the exact one.
    state = merge_case_state(state, "我是女的，预算大概几万块")
    assert state.known_user_facts["gender"] == "male"
    assert state.known_user_facts["birth_datetime"] == "1990-05-01 10:30"


# --- §27 clarification budget ---------------------------------------------


def test_case_state_counts_clarifications_and_exhausts_budget() -> None:
    assert MAX_BLOCKING_CLARIFICATIONS == 1

    state = RoomCaseState()
    merge_from_host_analysis(state, {"clarification_question": _LEGAL_CLARIFICATION})
    assert state.clarification_round_count == 1
    assert state.clarification_asked_questions == [_LEGAL_CLARIFICATION]
    assert state.clarification_budget_exhausted is True

    merge_from_host_analysis(state, {"clarification_question": "再问一次？"})
    assert state.clarification_round_count == 2
    assert state.clarification_budget_exhausted is True
    assert state.clarification_asked_questions == [_LEGAL_CLARIFICATION, "再问一次？"]


def _room(app: Any, suffix: str) -> Any:
    for persona_id in (f"cl_{suffix}_host", f"cl_{suffix}_a", f"cl_{suffix}_b"):
        app.personas.create_from_manifest(
            {
                "id": persona_id,
                "display_name": persona_id,
                "persona_type": "fictional",
                "run_mode": "continuation",
            }
        )
    return app.orchestrator.create_room(
        title="clarification",
        topic="consultation case",
        participants=[
            ParticipantSlot(
                participant_id="slot_host",
                persona_id=f"cl_{suffix}_host",
                display_name="host",
                role="host",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
            ),
            ParticipantSlot(
                participant_id="slot_a",
                persona_id=f"cl_{suffix}_a",
                display_name="a",
                role="expert",
                specialties=["alpha"],
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
            ),
            ParticipantSlot(
                participant_id="slot_b",
                persona_id=f"cl_{suffix}_b",
                display_name="b",
                role="expert",
                specialties=["beta"],
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
            ),
        ],
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(min_experts=2, max_experts=2),
    )


def _asking_executor(
    seen: defaultdict[str, list[ProtocolActionRequest]], question: str
) -> Any:
    """Executor whose host always tries to block with one clarification."""

    async def execute(request: ProtocolActionRequest) -> dict[str, Any]:
        seen[request.action].append(request)
        if request.action == "host_analysis":
            return {
                "problem_definition": "user wants a reading",
                "known_facts_patch": {},
                "assumptions": [],
                "can_proceed": False,
                "missing_indispensable_fields": ["birth_datetime"],
                "clarification_question": question,
            }
        if request.action == "routing":
            return {
                "selected_participant_ids": ["slot_a", "slot_b"],
                "routing_reason": "both relevant",
            }
        if request.action == "analysis":
            return {
                "summary": f"analysis by {request.participant.participant_id}",
                "key_findings": [],
                "evidence": [],
                "uncertainties": [],
                "recommendation": "continue",
                "confidence": 0.8,
            }
        if request.action == "review":
            return {
                "agree": [],
                "disagree": [],
                "concerns": [],
                "changed_position": False,
                "updated_conclusion": None,
            }
        return {
            "summary": "synthesis",
            "consensus": [],
            "conflicts": [],
            "uncertainties": [],
            "recommendations": [],
        }

    return execute


@pytest.mark.anyio
async def test_first_blocking_clarification_reaches_waiting_user(app: Any) -> None:
    room = _room(app, "first")
    runtime = RoomProtocolRuntime(app.orchestrator.protocol_repository)
    seen: defaultdict[str, list[ProtocolActionRequest]] = defaultdict(list)

    state = await runtime.run(room, "case", _asking_executor(seen, _LEGAL_CLARIFICATION))

    assert state.status.value == "waiting_clarification"
    assert state.clarification is not None
    assert state.clarification["question"] == _LEGAL_CLARIFICATION
    assert room.case_state.clarification_round_count == 1
    # The run stopped before the experts were wasted on an unanswered case.
    assert seen["analysis"] == []


@pytest.mark.anyio
async def test_second_clarification_is_suppressed_and_run_completes(app: Any) -> None:
    room = _room(app, "second")
    runtime = RoomProtocolRuntime(app.orchestrator.protocol_repository)
    seen: defaultdict[str, list[ProtocolActionRequest]] = defaultdict(list)

    first = await runtime.run(room, "case", _asking_executor(seen, _LEGAL_CLARIFICATION))
    second = await runtime.run(room, "用户补充了新信息", _asking_executor(seen, "还想再问一次？"))

    # The case budget is spent: the second ask must never block the run again.
    assert second.status.value != "waiting_clarification"
    assert second.clarification is None
    # A SUPPRESSED ask never charges the budget: the counter stays at the
    # first legal round, the asked list gains nothing, and the open
    # questions recorded by the first run are left untouched.
    assert room.case_state.clarification_round_count == 1
    assert room.case_state.clarification_budget_exhausted is True
    assert room.case_state.clarification_asked_questions == [_LEGAL_CLARIFICATION]
    assert room.case_state.open_indispensable_questions == ["birth_datetime"]
    suppressed = second.artifacts.get("clarification_suppressed")
    assert suppressed is not None
    assert suppressed["reason"] == "clarification_budget_exhausted"
    # The first run is a completed outcome either way (never an error).
    assert first.status.value in {"success", "waiting_clarification"}


# --- §28 output-vs-input boundary -----------------------------------------


def test_output_intent_boundary_function_semantics() -> None:
    # "哪天更适合买" is a deliverable, never a required input.
    assert is_output_intent_question("哪天更适合买") is True
    assert is_output_intent_question("请提供您的出生时间") is False

    # Asking back for a deliverable is the infinite-clarification loop.
    assert violates_output_input_boundary(_OUTPUT_ASK_BACK) is True
    assert violates_output_input_boundary("请提供您的出生时间") is False
    assert violates_output_input_boundary("您想问适合做什么方向") is False


@pytest.mark.anyio
async def test_output_ask_back_clarification_never_blocks_run(app: Any) -> None:
    room = _room(app, "output")
    runtime = RoomProtocolRuntime(app.orchestrator.protocol_repository)
    seen: defaultdict[str, list[ProtocolActionRequest]] = defaultdict(list)

    state = await runtime.run(room, "case", _asking_executor(seen, _OUTPUT_ASK_BACK))

    # The deliverable ask-back must not become a blocking clarification.
    assert state.clarification is None
    assert state.status.value != "waiting_clarification"
    suppressed = state.artifacts.get("clarification_suppressed")
    assert suppressed is not None


# --- prompt-block projection ------------------------------------------------


def test_as_prompt_block_drops_bookkeeping_fields() -> None:
    state = merge_case_state(None, FIRST_TURN)
    state.resolved_questions = ["resolved earlier"]
    state.note_clarification("asked once")
    state.touch()

    block = state.as_prompt_block()

    assert set(block) == {
        "intent",
        "problem_definition",
        "known_user_facts",
        "constraints",
        "assumptions",
        "open_indispensable_questions",
        "optional_details",
    }
    assert "last_user_message" not in block
    assert "updated_at" not in block
    assert "clarification_round_count" not in block
    assert "clarification_asked_questions" not in block
    assert "resolved_questions" not in block
    assert block["intent"] == FIRST_TURN
    assert block["known_user_facts"]["birth_place"] == "杭州"
