"""Requirement §27/§28: protocol run memory commit policy, synthesis repair
self-containment and the waiting_clarification room settlement.

Uses the ``include_fake_agent`` app fixture and the real orchestrator path;
only the deterministic fake model is invoked (no real model calls).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.room.models import ParticipantSlot, RoomProtocolConfig, RoomProtocolType

CASE_ANCHOR = "REPAIR_ANCHOR：我想咨询明年的投资方向，预算五十万以内，追求稳健。"

ROUTING_MARKER = "Fake adapter selected declared routing candidates."
REVIEW_MARKER = "The submission follows the requested structure."
ANALYSIS_MARKER = "Independent protocol analysis by"
SYNTHESIS_MARKER = "Protocol synthesis completed by the fake adapter."
REPAIRED_SUMMARY = "REPAIRED_DENSE_SYNTHESIS：立即推进，五百万内分两批建仓。"


def _manifest(persona_id: str) -> dict[str, str]:
    return {
        "id": persona_id,
        "display_name": persona_id,
        "persona_type": "fictional",
        "run_mode": "continuation",
    }


def _slot(participant_id: str, persona_id: str, role: str) -> ParticipantSlot:
    return ParticipantSlot(
        participant_id=participant_id,
        persona_id=persona_id,
        display_name=participant_id,
        role=role,
        runtime_selection="fake_agent",
        model_selection="fake-gpt-5",
    )


async def _expert_room(app: Any, suffix: str) -> Any:
    for persona_id in (f"cp_{suffix}_host", f"cp_{suffix}_a", f"cp_{suffix}_b"):
        app.personas.create_from_manifest(_manifest(persona_id))
    room = app.orchestrator.create_room(
        title=f"commit-{suffix}",
        topic="consultation",
        participants=[
            _slot("slot_host", f"cp_{suffix}_host", "host"),
            _slot("slot_a", f"cp_{suffix}_a", "expert"),
            _slot("slot_b", f"cp_{suffix}_b", "expert"),
        ],
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(min_experts=2, max_experts=2),
    )
    await app.orchestrator.start_room(room.id)
    return room


def _persona_turn_responses(app: Any) -> list[str]:
    rows = app.database.conn.execute("SELECT persona_response FROM session_turns").fetchall()
    return [row["persona_response"] for row in rows]


def _persona_memory_contents(app: Any, persona_ids: list[str]) -> list[str]:
    placeholders = ",".join("?" for _ in persona_ids)
    rows = app.database.conn.execute(
        f"SELECT content FROM memories WHERE persona_id IN ({placeholders})",
        persona_ids,
    ).fetchall()
    return [row["content"] for row in rows]


# --- commit policy -----------------------------------------------------------


@pytest.mark.anyio
async def test_routing_and_review_never_produce_persona_memory(app: Any) -> None:
    room = await _expert_room(app, "policy")
    persona_ids = ["cp_policy_host", "cp_policy_a", "cp_policy_b"]

    completed = await app.orchestrator.run_protocol(room.id, CASE_ANCHOR)
    assert completed.protocol_state.status.value == "success"
    assert len(completed.protocol_state.submissions) == 2
    assert len(completed.protocol_state.reviews) == 2

    responses = _persona_turn_responses(app)
    memories = _persona_memory_contents(app, persona_ids)

    # analysis (2 experts) + synthesis (host) are the only committed turns.
    assert len(responses) == 3
    joined_responses = "\n".join(responses)
    joined_memories = "\n".join(memories)
    assert ANALYSIS_MARKER in joined_responses
    assert SYNTHESIS_MARKER in joined_responses

    # Internal bookkeeping actions never leak into persona memory.
    for committed in (joined_responses, joined_memories):
        assert ROUTING_MARKER not in committed
        assert REVIEW_MARKER not in committed
        # Host analysis is an internal gate decision, not a public answer.
        assert "Fake host analysis" not in committed


# --- synthesis density repair ------------------------------------------------


@pytest.mark.anyio
async def test_synthesis_repair_prompt_is_self_contained(app: Any) -> None:
    room = await _expert_room(app, "repair")
    fake = app.agent_registry.get_adapter("fake_agent")
    assert isinstance(fake, FakeAgentAdapter)
    original = fake._generate_mock_response
    repair_prompts: list[str] = []

    def patched(session: Any, turn: Any) -> str:
        text = original(session, turn)
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return text
        required = set((turn.expected_output or {}).get("required") or [])
        if "participant_positions" not in required:
            return text
        prompt = AgentPromptRenderer.render_for_single_prompt(turn)
        if "CURRENT SYNTHESIS DRAFT" in prompt:
            # Second (stateless) call: return a dense, schema-valid rewrite.
            repair_prompts.append(prompt)
            payload["summary"] = REPAIRED_SUMMARY
            payload["participant_positions"] = [
                {
                    "participant_id": "slot_a",
                    "position": "推进",
                    "key_reason": "证据一致",
                }
            ]
            payload["final_judgment"] = "立即推进"
        else:
            # First draft: empty summary/positions/judgment trips the gate.
            payload["summary"] = ""
            payload["participant_positions"] = []
            payload["final_judgment"] = ""
        return json.dumps(payload, ensure_ascii=False)

    fake._generate_mock_response = patched  # type: ignore[method-assign]

    completed = await app.orchestrator.run_protocol(room.id, CASE_ANCHOR)

    # The density gate fired exactly one stateless repair call.
    assert len(repair_prompts) == 1
    prompt = repair_prompts[0]
    assert "CURRENT SYNTHESIS DRAFT" in prompt
    assert "REWRITE INSTRUCTION" in prompt
    # Self-contained: the packed case anchor rides along ...
    assert "REPAIR_ANCHOR" in prompt
    # ... as do the expert submissions / peer reviews summaries ...
    assert ANALYSIS_MARKER in prompt
    assert REVIEW_MARKER in prompt
    # ... and the existing synthesis draft to rewrite.
    assert '"summary": ""' in prompt

    final = completed.protocol_state.final_result or {}
    assert final.get("summary") == REPAIRED_SUMMARY


# --- waiting_clarification settlement ----------------------------------------


@pytest.mark.anyio
async def test_waiting_clarification_settles_room_ready_without_error(app: Any) -> None:
    room = await _expert_room(app, "clarify")
    fake = app.agent_registry.get_adapter("fake_agent")
    assert isinstance(fake, FakeAgentAdapter)
    original = fake._generate_mock_response

    def patched(session: Any, turn: Any) -> str:
        required = set((turn.expected_output or {}).get("required") or [])
        if "problem_definition" in required:
            return json.dumps(
                {
                    "problem_definition": "user wants a reading",
                    "known_facts_patch": {},
                    "assumptions": [],
                    "can_proceed": False,
                    "missing_indispensable_fields": ["birth_datetime"],
                    "clarification_question": "请提供您的出生时间",
                },
                ensure_ascii=False,
            )
        return original(session, turn)

    fake._generate_mock_response = patched  # type: ignore[method-assign]

    settled = await app.orchestrator.run_protocol(room.id, CASE_ANCHOR)

    assert settled.protocol_state.status.value == "waiting_clarification"
    # The room is back to a healthy waiting state -- never ERROR.
    assert settled.status.value == "ready"
    assert settled.last_error is None
    assert settled.protocol_state.clarification is not None


@pytest.mark.anyio
async def test_protocol_run_failure_records_specific_error_detail(app: Any) -> None:
    room = await _expert_room(app, "fail_detail")
    fake = app.agent_registry.get_adapter("fake_agent")
    assert isinstance(fake, FakeAgentAdapter)

    def failing_response(session: Any, turn: Any) -> str:
        raise RuntimeError("simulated_expert_crash")

    fake._generate_mock_response = failing_response  # type: ignore[method-assign]

    settled = await app.orchestrator.run_protocol(room.id, CASE_ANCHOR)
    assert settled.protocol_state.status.value == "failed"
    assert settled.status.value == "error"
    assert settled.last_error.startswith("room_protocol_run_failed: ")
    assert "Agent transport failed" in settled.last_error

