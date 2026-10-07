"""Runtime remaining freshness: no double-decrement, scope, model switch."""

from __future__ import annotations

import pytest

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.context_budget import ContextBudget
from persona_continuum.agent.context_fields import (
    ContextScope,
    apply_runtime_context_to_session,
    invalidate_runtime_context,
)
from persona_continuum.agent.models import AgentSessionConfig, RuntimeBindingSnapshot
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor, RuntimeSessionBinding


def _budget(prompt: int = 50_000) -> ContextBudget:
    return ContextBudget(
        context_window_tokens=500_000,
        max_prompt_tokens=400_000,
        system_schema_reserve_tokens=1_000,
        expected_output_reserve_tokens=4_000,
        reasoning_reserve_tokens=2_000,
        evidence_token_budget=300_000,
        estimated_prompt_tokens=prompt,
    )


class PersistentFake(FakeAgentAdapter):
    context_scope = ContextScope.PERSISTENT

    def supports_persistent_conversation(self, session: AgentSession) -> bool:
        return True


class PerRequestFake(FakeAgentAdapter):
    context_scope = ContextScope.PER_REQUEST


def _binding(adapter: FakeAgentAdapter, session_data: dict) -> RuntimeSessionBinding:
    session = AgentSession(
        config=AgentSessionConfig(
            room_id="r",
            participant_id="p",
            persona_id="x",
            model_id="grok-4.5",
        ),
        session_data=session_data,
    )
    return RuntimeSessionBinding(
        adapter=adapter,
        session=session,
        snapshot=RuntimeBindingSnapshot(
            agent_id=adapter.adapter_id,
            protocol="fake",
            effective_model="grok-4.5",
        ),
    )


def test_runtime_remaining_not_double_decremented() -> None:
    adapter = PersistentFake(context_window=500_000)
    binding = _binding(
        adapter,
        {
            "effective_context_window": 500_000,
            "remaining_context_tokens": 400_000,
            "remaining_context_verified": True,
            "remaining_context_source": "runtime_reported",
            "context_remaining_revision": 5,
            "effective_model": "grok-4.5",
            "context_scope": ContextScope.PERSISTENT.value,
        },
    )
    apply_runtime_context_to_session(
        binding.session.session_data,
        {"remaining_context_tokens": 330_000, "remaining_source": "runtime_reported"},
    )
    assert binding.session.session_data["context_remaining_revision"] == 6
    AgentRuntimeExecutor()._note_session_usage(
        binding, _budget(60_000), remaining_revision_start=5, output_tokens=0
    )
    assert binding.session.session_data["remaining_context_tokens"] == 330_000
    assert binding.session.session_data["remaining_context_verified"] is True


def test_estimated_decrement_when_runtime_silent() -> None:
    adapter = PersistentFake(context_window=500_000)
    binding = _binding(
        adapter,
        {
            "effective_context_window": 500_000,
            "remaining_context_tokens": 400_000,
            "remaining_context_verified": True,
            "remaining_context_source": "runtime_reported",
            "context_usage_revision": 5,
            "effective_model": "grok-4.5",
        },
    )
    AgentRuntimeExecutor()._note_session_usage(
        binding, _budget(50_000), remaining_revision_start=5, output_tokens=0
    )
    assert binding.session.session_data["remaining_context_tokens"] == 350_000
    assert binding.session.session_data["remaining_context_verified"] is False
    assert binding.session.session_data["remaining_context_source"] == "estimated_remaining"


def test_revision_bump_during_completion_is_not_racy() -> None:
    adapter = PersistentFake(context_window=500_000)
    binding = _binding(
        adapter,
        {
            "remaining_context_tokens": 400_000,
            "remaining_context_verified": True,
            "remaining_context_source": "runtime_reported",
            "context_usage_revision": 5,
            "effective_model": "grok-4.5",
        },
    )
    apply_runtime_context_to_session(
        binding.session.session_data,
        {"remaining_context_tokens": 310_000, "remaining_source": "runtime_reported"},
    )
    AgentRuntimeExecutor()._note_session_usage(
        binding, _budget(80_000), remaining_revision_start=0, output_tokens=10_000
    )
    assert binding.session.session_data["remaining_context_tokens"] == 310_000


def test_model_switch_invalidates_stale_remaining() -> None:
    data = {
        "remaining_context_tokens": 200_000,
        "remaining_context_verified": True,
        "remaining_context_source": "runtime_reported",
        "context_usage_model_id": "grok-4.5",
        "effective_model": "gpt-5.6-luna",
        "effective_context_window": 500_000,
    }
    invalidate_runtime_context(data, reason="model_change")
    assert data["remaining_context_tokens"] is None
    assert data["remaining_context_verified"] is False
    assert data["remaining_context_source"] == "unknown"

    adapter = PersistentFake(context_window=500_000)
    binding = _binding(
        adapter,
        {
            "effective_context_window": 500_000,
            "remaining_context_tokens": 200_000,
            "remaining_context_verified": True,
            "remaining_context_source": "runtime_reported",
            "context_usage_model_id": "grok-4.5",
            "effective_model": "gpt-5.6-luna",
        },
    )
    caps = AgentRuntimeExecutor()._planning_capability(
        binding, phase="material_classification"
    )
    assert caps.remaining_context_tokens is None or (
        caps.remaining_context_source != "runtime_reported"
    )


def test_per_request_scope_does_not_accumulate_remaining() -> None:
    adapter = PerRequestFake(context_window=500_000)
    binding = _binding(
        adapter,
        {
            "effective_context_window": 500_000,
            "remaining_context_tokens": 400_000,
            "remaining_context_verified": True,
            "remaining_context_source": "runtime_reported",
            "context_usage_revision": 1,
            "effective_model": "grok-4.5",
            "context_scope": ContextScope.PER_REQUEST.value,
            "workload_context_scope": "per_request",
        },
    )
    AgentRuntimeExecutor()._note_session_usage(
        binding, _budget(300_000), remaining_revision_start=1, output_tokens=0
    )
    assert binding.session.session_data.get("session_used_tokens") == 0
    caps = AgentRuntimeExecutor()._planning_capability(binding, phase="research")
    assert caps.remaining_context_source == "inferred_fresh_session"
    assert caps.remaining_context_verified is False


@pytest.mark.anyio
async def test_persistent_runtime_remaining_followed_across_turns() -> None:
    class ReportingFake(PersistentFake):
        def __init__(self) -> None:
            super().__init__(context_window=500_000, chunk_delay_sec=0.0)
            self.reported: list[int] = []

        async def send(self, session, turn):
            previous = int(session.session_data.get("remaining_context_tokens") or 500_000)
            nxt = previous - 10_000
            apply_runtime_context_to_session(
                session.session_data,
                {"remaining_context_tokens": nxt, "remaining_source": "runtime_reported"},
            )
            self.reported.append(nxt)
            async for event in super().send(session, turn):
                yield event

    adapter = ReportingFake()
    executor = AgentRuntimeExecutor()
    binding = await executor.open_session(
        adapter,
        AgentSessionConfig(
            room_id="r", participant_id="p", persona_id="x", model_id="fake-gpt-5"
        ),
    )
    try:
        binding.session.session_data.update(
            {
                "effective_context_window": 500_000,
                "remaining_context_tokens": 400_000,
                "remaining_context_verified": True,
                "remaining_context_source": "runtime_reported",
                "context_usage_revision": 1,
                "context_scope": ContextScope.PERSISTENT.value,
                "effective_model": "fake-gpt-5",
            }
        )
        for _ in range(10):
            await executor.execute_text(
                binding, system_prompt="s", user_message="hello", phase="room"
            )
        assert adapter.reported[-1] == 400_000 - 10_000 * 10
        assert binding.session.session_data["remaining_context_tokens"] == adapter.reported[-1]
        assert binding.session.session_data["remaining_context_source"] == "runtime_reported"
    finally:
        await executor.close(binding)


def test_context_window_update_does_not_skip_remaining_estimate() -> None:
    adapter = PersistentFake(context_window=500_000)
    binding = _binding(
        adapter,
        {
            "effective_context_window": 500_000,
            "remaining_context_tokens": 400_000,
            "remaining_context_verified": True,
            "remaining_context_source": "runtime_reported",
            "context_remaining_revision": 5,
            "context_capability_revision": 2,
            "effective_model": "grok-4.5",
            "context_scope": ContextScope.PERSISTENT.value,
        },
    )
    apply_runtime_context_to_session(
        binding.session.session_data, {"context_window": 500_000}
    )
    assert binding.session.session_data["context_capability_revision"] == 3
    assert binding.session.session_data["context_remaining_revision"] == 5
    AgentRuntimeExecutor()._note_session_usage(
        binding, _budget(60_000), remaining_revision_start=5, output_tokens=0
    )
    assert binding.session.session_data["remaining_context_tokens"] == 340_000
    assert binding.session.session_data["remaining_context_source"] == "estimated_remaining"


def test_billing_input_tokens_do_not_advance_remaining_revision() -> None:
    from persona_continuum.agent.context_fields import (
        ACP_USAGE_SEMANTICS,
        extract_runtime_context_facts,
    )

    facts = extract_runtime_context_facts(
        {"modelUsage": {"grok-4.6": {"contextWindow": 500000, "inputTokens": 130000}}},
        semantics=ACP_USAGE_SEMANTICS,
    )
    data: dict = {"context_remaining_revision": 5}
    apply_runtime_context_to_session(data, facts, semantics=ACP_USAGE_SEMANTICS)
    assert data["effective_context_window"] == 500_000
    assert data.get("remaining_context_tokens") is None
    assert data["context_remaining_revision"] == 5
    assert data["context_capability_revision"] == 1


def test_cumulative_used_context_advances_remaining_revision() -> None:
    from persona_continuum.agent.context_fields import (
        OCCUPANCY_CONTEXT_KEYS,
        ContextUsageKind,
        ContextUsageSemantics,
    )

    semantics = ContextUsageSemantics(
        occupancy_keys=OCCUPANCY_CONTEXT_KEYS,
        derive_remaining_from_occupancy=True,
        usage_kind=ContextUsageKind.CUMULATIVE_CONTEXT_USAGE.value,
    )
    data: dict = {
        "effective_context_window": 500_000,
        "context_remaining_revision": 3,
    }
    apply_runtime_context_to_session(
        data, {"used_context_tokens": 150_000}, semantics=semantics
    )
    assert data["remaining_context_tokens"] == 350_000
    assert data["context_remaining_revision"] == 4
    assert data["remaining_context_verified"] is False


@pytest.mark.anyio
async def test_stream_and_execute_share_remaining_accounting() -> None:
    adapter = PersistentFake(context_window=500_000, chunk_delay_sec=0.0)
    executor = AgentRuntimeExecutor()
    binding = await executor.open_session(
        adapter,
        AgentSessionConfig(room_id="r", participant_id="p", persona_id="x", model_id="fake-gpt-5"),
    )
    try:
        binding.session.session_data.update(
            {
                "effective_context_window": 500_000,
                "remaining_context_tokens": 400_000,
                "remaining_context_verified": True,
                "remaining_context_source": "runtime_reported",
                "context_remaining_revision": 1,
                "context_scope": ContextScope.PERSISTENT.value,
                "workload_context_scope": ContextScope.PERSISTENT.value,
                "effective_model": "fake-gpt-5",
            }
        )
        await executor.execute_text(
            binding, system_prompt="s", user_message="hello", phase="room"
        )
        after_text = int(binding.session.session_data["remaining_context_tokens"])
        assert after_text < 400_000
        binding.session.session_data["remaining_context_tokens"] = 400_000
        binding.session.session_data["context_remaining_revision"] = 1
        events = [
            event
            async for event in executor.stream_events(
                binding, system_prompt="s", user_message="hello", phase="room"
            )
        ]
        assert events
        after_stream = int(binding.session.session_data["remaining_context_tokens"])
        assert after_stream < 400_000
        assert after_stream == after_text
    finally:
        await executor.close(binding)


@pytest.mark.anyio
async def test_persistent_stream_without_runtime_remaining_estimates() -> None:
    adapter = PersistentFake(context_window=500_000, chunk_delay_sec=0.0)
    executor = AgentRuntimeExecutor()
    binding = await executor.open_session(
        adapter,
        AgentSessionConfig(room_id="r", participant_id="p", persona_id="x", model_id="fake-gpt-5"),
    )
    try:
        binding.session.session_data.update(
            {
                "effective_context_window": 500_000,
                "remaining_context_tokens": 400_000,
                "remaining_context_verified": True,
                "remaining_context_source": "runtime_reported",
                "context_remaining_revision": 2,
                "context_scope": ContextScope.PERSISTENT.value,
                "workload_context_scope": "persistent",
                "effective_model": "fake-gpt-5",
            }
        )
        async for _event in executor.stream_events(
            binding, system_prompt="s", user_message="hello", phase="room"
        ):
            pass
        assert binding.session.session_data["remaining_context_tokens"] < 400_000
        assert binding.session.session_data["remaining_context_source"] == "estimated_remaining"
    finally:
        await executor.close(binding)


@pytest.mark.anyio
async def test_alternating_runtime_and_estimated_remaining() -> None:
    class AlternatingFake(PersistentFake):
        def __init__(self) -> None:
            super().__init__(context_window=500_000, chunk_delay_sec=0.0)
            self.turn = 0

        async def send(self, session, turn):
            self.turn += 1
            if self.turn % 2 == 1:
                nxt = int(session.session_data.get("remaining_context_tokens") or 0) - 8_000
                apply_runtime_context_to_session(
                    session.session_data,
                    {"remaining_context_tokens": nxt, "remaining_source": "runtime_reported"},
                )
            async for event in super().send(session, turn):
                yield event

    adapter = AlternatingFake()
    executor = AgentRuntimeExecutor()
    binding = await executor.open_session(
        adapter,
        AgentSessionConfig(room_id="r", participant_id="p", persona_id="x", model_id="fake-gpt-5"),
    )
    try:
        binding.session.session_data.update(
            {
                "effective_context_window": 500_000,
                "remaining_context_tokens": 400_000,
                "remaining_context_verified": True,
                "remaining_context_source": "runtime_reported",
                "context_scope": ContextScope.PERSISTENT.value,
                "workload_context_scope": "persistent",
                "effective_model": "fake-gpt-5",
            }
        )
        history: list[int] = []
        for _ in range(10):
            await executor.execute_text(
                binding, system_prompt="s", user_message="hello", phase="room"
            )
            history.append(int(binding.session.session_data["remaining_context_tokens"]))
        assert history == sorted(history, reverse=True)
        assert history[-1] < history[0]
        assert len(set(history)) == 10
    finally:
        await executor.close(binding)


def test_pending_remaining_flush_skips_estimate() -> None:
    adapter = PersistentFake(context_window=500_000)
    binding = _binding(
        adapter,
        {
            "effective_context_window": 500_000,
            "remaining_context_tokens": 400_000,
            "remaining_context_verified": True,
            "remaining_context_source": "runtime_reported",
            "context_remaining_revision": 5,
            "effective_model": "grok-4.5",
            "context_scope": ContextScope.PERSISTENT.value,
            "_pending_runtime_context": {
                "remaining_context_tokens": 330_000,
                "remaining_source": "runtime_reported",
            },
        },
    )
    AgentRuntimeExecutor()._note_session_usage(
        binding, _budget(60_000), remaining_revision_start=5, output_tokens=0
    )
    assert binding.session.session_data["remaining_context_tokens"] == 330_000
    assert binding.session.session_data["context_remaining_revision"] == 6


def test_per_window_accumulates_only_inside_session() -> None:
    adapter = PersistentFake(context_window=500_000)
    binding = _binding(
        adapter,
        {
            "effective_context_window": 500_000,
            "remaining_context_tokens": 400_000,
            "remaining_context_verified": True,
            "remaining_context_source": "runtime_reported",
            "context_remaining_revision": 1,
            "effective_model": "grok-4.5",
            "context_scope": ContextScope.PER_WINDOW.value,
            "workload_context_scope": "per_window",
        },
    )
    AgentRuntimeExecutor()._note_session_usage(
        binding, _budget(60_000), remaining_revision_start=1, output_tokens=0
    )
    assert binding.session.session_data["remaining_context_tokens"] == 340_000
    invalidate_runtime_context(binding.session.session_data, reason="window_complete")
    assert binding.session.session_data["remaining_context_tokens"] is None


@pytest.mark.anyio
async def test_per_request_stream_does_not_accumulate() -> None:
    adapter = PerRequestFake(context_window=500_000, chunk_delay_sec=0.0)
    executor = AgentRuntimeExecutor()
    binding = await executor.open_session(
        adapter,
        AgentSessionConfig(room_id="r", participant_id="p", persona_id="x", model_id="fake-gpt-5"),
    )
    try:
        binding.session.session_data.update(
            {
                "effective_context_window": 500_000,
                "remaining_context_tokens": 400_000,
                "remaining_context_verified": True,
                "remaining_context_source": "runtime_reported",
                "context_remaining_revision": 1,
                "context_scope": ContextScope.PER_REQUEST.value,
                "workload_context_scope": "per_request",
                "effective_model": "fake-gpt-5",
            }
        )
        async for _event in executor.stream_events(
            binding, system_prompt="s", user_message="hello", phase="research"
        ):
            pass
        assert binding.session.session_data.get("session_used_tokens") == 0
        assert binding.session.session_data["remaining_context_tokens"] == 400_000
    finally:
        await executor.close(binding)


@pytest.mark.anyio
async def test_streaming_cancel_estimates_when_session_alive() -> None:
    adapter = PersistentFake(context_window=500_000, chunk_delay_sec=0.02)
    executor = AgentRuntimeExecutor()
    binding = await executor.open_session(
        adapter,
        AgentSessionConfig(room_id="r", participant_id="p", persona_id="x", model_id="fake-gpt-5"),
    )
    try:
        binding.session.session_data.update(
            {
                "effective_context_window": 500_000,
                "remaining_context_tokens": 400_000,
                "remaining_context_verified": True,
                "remaining_context_source": "runtime_reported",
                "context_remaining_revision": 2,
                "context_scope": ContextScope.PERSISTENT.value,
                "workload_context_scope": "persistent",
                "effective_model": "fake-gpt-5",
            }
        )
        agen = executor.stream_events(
            binding, system_prompt="s", user_message="hello " * 40, phase="room"
        )
        await agen.__anext__()
        await agen.aclose()
        assert binding.session.session_data["remaining_context_tokens"] < 400_000
        assert binding.session.session_data["remaining_context_source"] == "estimated_remaining"
    finally:
        await executor.close(binding)


@pytest.mark.anyio
async def test_streaming_cancel_after_close_does_not_keep_estimate() -> None:
    adapter = PersistentFake(context_window=500_000, chunk_delay_sec=0.02)
    executor = AgentRuntimeExecutor()
    binding = await executor.open_session(
        adapter,
        AgentSessionConfig(room_id="r", participant_id="p", persona_id="x", model_id="fake-gpt-5"),
    )
    binding.session.session_data.update(
        {
            "effective_context_window": 500_000,
            "remaining_context_tokens": 400_000,
            "remaining_context_verified": True,
            "remaining_context_source": "runtime_reported",
            "context_remaining_revision": 2,
            "context_scope": ContextScope.PERSISTENT.value,
            "workload_context_scope": "persistent",
            "effective_model": "fake-gpt-5",
        }
    )
    agen = executor.stream_events(
        binding, system_prompt="s", user_message="hello " * 40, phase="room"
    )
    await agen.__anext__()
    await executor.close(binding)
    await agen.aclose()
    assert binding.session.session_data["remaining_context_tokens"] == 400_000
