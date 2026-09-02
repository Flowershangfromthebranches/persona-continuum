"""Requirement §32: prompt transport stress tests.

An ARGV-declared fake adapter (explicit ``prompt_transport_mode`` attribute --
the most reliable declaration path in ``agent/prompt_transport.py``) bounds
every prompt to the 64KB single-argument ceiling with its 10% margin
(``safe_prompt_bytes == 57344``).  Neither the packed protocol path nor the
legacy free_discussion context path may ever exceed it, and neither may fall
back to shipping the whole transcript.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.prompt_transport import (
    PromptTransportCapability,
    resolve_prompt_transport_capability,
)
from persona_continuum.room.context_manager import RoomContextManager
from persona_continuum.room.context_packer import budget_for
from persona_continuum.room.models import ParticipantSlot, RoomProtocolConfig, RoomProtocolType
from persona_continuum.room.orchestrator import MultiAgentOrchestrator

ARGV_SAFE_BYTES = 57344
CASE_MARKER = "CASE_ANCHOR_9F3K"
CONSTRAINT_MARKER = "CONSTRAINT_Q7W2"
FIRST_TURN = (
    f"{CASE_MARKER}：我想咨询明年的投资方向。"
    f"{CONSTRAINT_MARKER}：本金计划持有一年，最大承受10万亏损。"
)


class ArgvFakeAdapter(FakeAgentAdapter):
    """Fake adapter that declares (and enforces) an ARGV-only transport."""

    prompt_transport_mode = "argv"

    def __init__(self) -> None:
        super().__init__(adapter_id="argv_fake", chunk_delay_sec=0.0)
        # Measured at the actual send boundary, one entry per invoke.
        self.prompt_sizes: list[int] = []

    async def send(self, session: Any, turn: Any) -> Any:  # type: ignore[override]
        prompt = AgentPromptRenderer.render_for_single_prompt(turn)
        self.prompt_sizes.append(len(prompt.encode("utf-8")))
        async for event in super().send(session, turn):
            yield event

    def _generate_mock_response(self, session: Any, turn: Any) -> str:
        text = super()._generate_mock_response(session, turn)
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return text
        # The synthesis is the run's final answer: echo whatever case markers
        # are still visible in the packed prompt, so the test can prove the
        # original goal and confirmed constraints were never forgotten.
        if "participant_positions" in payload or "final_judgment" in payload:
            prompt = AgentPromptRenderer.render_for_single_prompt(turn)
            markers = [m for m in (CASE_MARKER, CONSTRAINT_MARKER) if m in prompt]
            if markers:
                payload["summary"] = (
                    "FINAL_ECHO " + " ".join(markers) + " " + str(payload.get("summary") or "")
                )
            return json.dumps(payload, ensure_ascii=False)
        return text


def _argv_fake(app: Any) -> ArgvFakeAdapter:
    adapter = app.agent_registry.get_adapter("argv_fake")
    assert isinstance(adapter, ArgvFakeAdapter)
    return adapter


def _slot(participant_id: str, persona_id: str, role: str) -> ParticipantSlot:
    return ParticipantSlot(
        participant_id=participant_id,
        persona_id=persona_id,
        display_name=participant_id,
        role=role,
        runtime_selection="argv_fake",
        model_selection="fake-gpt-5",
    )


async def _expert_room(app: Any) -> Any:
    for persona_id in ("ts_host", "ts_a", "ts_b"):
        app.personas.create_from_manifest(
            {
                "id": persona_id,
                "display_name": persona_id,
                "persona_type": "fictional",
                "run_mode": "continuation",
            }
        )
    room = app.orchestrator.create_room(
        title="transport-stress",
        topic="long consultation",
        participants=[
            _slot("slot_host", "ts_host", "host"),
            _slot("slot_a", "ts_a", "expert"),
            _slot("slot_b", "ts_b", "expert"),
        ],
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(min_experts=2, max_experts=2),
    )
    await app.orchestrator.start_room(room.id)
    return room


# --- expert consultation follow-up stress ------------------------------------


@pytest.mark.anyio
async def test_expert_consultation_30_followups_stay_transport_safe(app: Any) -> None:
    app.agent_registry.register_adapter(ArgvFakeAdapter())
    fake = _argv_fake(app)
    room = await _expert_room(app)
    room_id = room.id

    capability = resolve_prompt_transport_capability(fake)
    assert isinstance(capability, PromptTransportCapability)
    assert capability.transport_mode == "argv"
    assert capability.safe_prompt_bytes == ARGV_SAFE_BYTES
    pack_budget = budget_for(capability)
    assert pack_budget <= ARGV_SAFE_BYTES

    seen_sizes = 0
    for turn_index in range(31):  # 1 case-seeding turn + 30 follow-ups
        # Pre-invoke invariant: the planned budget respects the transport.
        assert budget_for(capability) <= capability.safe_prompt_bytes
        if turn_index == 0:
            message = FIRST_TURN  # the case's original goal + confirmed constraint
        else:
            message = f"FOLLOWUP_{turn_index}：请继续跟踪第{turn_index}轮进展，约束不变。"
        settled = await app.orchestrator.run_protocol(room_id, message)
        # No turn may fail, and none may hit PROMPT_TRANSPORT_LIMIT_EXCEEDED.
        assert settled.status.value == "ready", settled.last_error
        assert settled.protocol_state.status.value == "success"

        # Post-invoke evidence: every packed report stayed inside budget.
        # Synthesis packs against the full budget minus the repair-retry
        # reserve (its self-contained repair message re-sends the packed
        # prompt with the draft appended); every other stage packs against
        # the full budget.  Either way nothing may exceed the transport.
        for task in settled.protocol_state.tasks:
            execution = (task.output or {}).get("_execution") or {}
            packed = execution.get("packed") or {}
            if not packed:
                continue
            expected_budget = (
                pack_budget - MultiAgentOrchestrator._SYNTHESIS_REPAIR_RESERVE_BYTES
                if task.task_type == "synthesis"
                else pack_budget
            )
            assert packed["budget_bytes"] == expected_budget
            assert packed["budget_bytes"] <= pack_budget
            assert packed["used_bytes"] <= packed["budget_bytes"]
            assert packed["fits"] is True

        # Every actual invoke payload stayed inside the ARGV ceiling.
        new_sizes = fake.prompt_sizes[seen_sizes:]
        seen_sizes = len(fake.prompt_sizes)
        assert new_sizes, "expected at least one model invoke this turn"
        assert max(new_sizes) <= ARGV_SAFE_BYTES

    # Memory check: the final answer still carries the original goal and the
    # user-confirmed constraint -- 30 turns later nothing was forgotten.
    final = app.orchestrator.get_room(room_id)
    assert final is not None
    summary = str((final.protocol_state.final_result or {}).get("summary") or "")
    assert CASE_MARKER in summary
    assert CONSTRAINT_MARKER in summary
    # All ~200 invokes across the stress stayed transport-safe.
    assert seen_sizes >= 31 * 5
    assert max(fake.prompt_sizes) <= ARGV_SAFE_BYTES


# --- legacy free_discussion room stress ---------------------------------------


def _legacy_transcript(messages: int) -> list[dict[str, Any]]:
    transcript: list[dict[str, Any]] = []
    for index in range(messages):
        if index == 0:
            content = f"OLDEST_USER_FACT_{index}：我最开始的需求是长期稳健增值，不能接受本金亏损。"
            speaker, participant = "用户", "user"
        elif index % 2 == 0:
            content = f"用户第{index}轮的补充说明，包含一些细节与约束。" * 6
            speaker, participant = "用户", "user"
        else:
            content = f"主持人第{index}轮的回应，包含讨论内容与展开的分析细节。" * 6
            speaker, participant = "主持人", "slot_host"
        transcript.append(
            {
                "participant_id": participant,
                "speaker_name": speaker,
                "message_kind": "user_message" if participant == "user" else "host_message",
                "content": content,
            }
        )
    return transcript


def _transcript_bytes(turns: list[dict[str, Any]]) -> int:
    return sum(
        len(str(item.get("content") or "").encode("utf-8"))
        + len(str(item.get("speaker_name") or "").encode("utf-8"))
        for item in turns
    )


async def _prepare_truncated_context(app: Any) -> tuple[Any, int, list[dict[str, Any]]]:
    """Seed a 120-message legacy free room and pack it under the ARGV budget."""

    app.agent_registry.register_adapter(ArgvFakeAdapter())
    fake = _argv_fake(app)

    app.personas.create_from_manifest(
        {
            "id": "ts_free_host",
            "display_name": "h",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    app.personas.create_from_manifest(
        {
            "id": "ts_free_m1",
            "display_name": "m",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    room = app.orchestrator.create_room(
        title="legacy-free",
        topic="long lived room",
        participants=[
            _slot("slot_host", "ts_free_host", "host"),
            _slot("slot_m1", "ts_free_m1", "member"),
        ],
        protocol=RoomProtocolType.FREE_DISCUSSION,
    )
    state = app.orchestrator.get_room(room.id)
    assert state is not None
    state.transcript = _legacy_transcript(120)
    app.orchestrator._save_room_state(state, force=True)

    capability = resolve_prompt_transport_capability(fake)
    budget = budget_for(capability)
    transcript = list(state.transcript)
    # The raw transcript genuinely exceeds the ARGV budget.
    assert _transcript_bytes(transcript) > budget

    manager = RoomContextManager()
    ctx = manager.prepare(
        room_id=room.id,
        participant_id="slot_m1",
        transcript=transcript,
        persistent=False,
        # rolling summary unavailable for this legacy room.
        summary_block=None,
        transport_budget_bytes=budget,
    )
    return ctx, budget, transcript


@pytest.mark.anyio
async def test_legacy_free_room_truncates_transcript_instead_of_full_fallback(app: Any) -> None:
    ctx, budget, transcript = await _prepare_truncated_context(app)

    # Deterministic truncation, never a silent full-transcript fallback.
    assert ctx.mode == "transport_truncated"
    assert len(ctx.turns) < len(transcript)
    # The trim accounting itself (content bytes) stays within budget.
    content_bytes = sum(len(str(item.get("content") or "").encode("utf-8")) for item in ctx.turns)
    assert content_bytes <= budget
    # What the user said survives: user turns are prioritised by the trim,
    # so the oldest goal stays inside the transported window.
    assert any("OLDEST_USER_FACT_0" in str(item.get("content") or "") for item in ctx.turns)
    # Salvage carries a bounded set of user statements (clipped to 300 chars).
    assert ctx.salvaged_facts
    user_contents = {
        str(item.get("content") or "")
        for item in transcript
        if item.get("participant_id") == "user"
    }
    assert all(
        any(fact == content[:300] for content in user_contents) for fact in ctx.salvaged_facts
    )


@pytest.mark.anyio
async def test_legacy_truncated_window_bytes_measured_like_the_budget_decision(app: Any) -> None:
    ctx, budget, _ = await _prepare_truncated_context(app)
    # Same measurement the budget decision uses (content + speaker_name).
    assert _transcript_bytes(ctx.turns) <= budget
