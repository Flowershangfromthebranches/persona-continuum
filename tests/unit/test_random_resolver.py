from __future__ import annotations

import pytest

from persona_continuum.agent.registry import AgentRegistry
from persona_continuum.room.models import ParticipantSlot
from persona_continuum.room.random_resolver import RandomBindingResolver


@pytest.mark.anyio
async def test_random_resolver_explicit() -> None:
    registry = AgentRegistry(include_builtins=False, include_fake=True)
    resolver = RandomBindingResolver(registry, seed=42)
    probes = await registry.probe_all()

    slot = ParticipantSlot(
        participant_id="slot_1",
        persona_id="persona_steve",
        display_name="Steve",
        runtime_selection="fake_agent",
        model_selection="fake-gpt-5",
        reasoning_selection="high",
    )

    snapshot = await resolver.resolve_slot(slot, probes)
    assert snapshot.participant_id == "slot_1"
    assert snapshot.persona_id == "persona_steve"
    assert snapshot.agent_runtime_id == "fake_agent"
    assert snapshot.model_id == "fake-gpt-5"
    assert snapshot.reasoning_effort == "high"


@pytest.mark.anyio
async def test_random_resolver_random() -> None:
    registry = AgentRegistry(include_builtins=False, include_fake=True)
    resolver = RandomBindingResolver(registry, seed=12345)
    probes = await registry.probe_all()

    slot = ParticipantSlot(
        participant_id="slot_random",
        persona_id="persona_elon",
        runtime_selection="random",
        model_selection="random",
        reasoning_selection="random",
    )

    snapshot = await resolver.resolve_slot(slot, probes)
    assert snapshot.agent_runtime_id != ""
    assert snapshot.model_id != ""
    assert snapshot.reasoning_effort in {"none", "low", "medium", "high", "xhigh", "max"}


@pytest.mark.anyio
async def test_random_resolver_candidate_pools() -> None:
    registry = AgentRegistry(include_builtins=False, include_fake=True)
    resolver = RandomBindingResolver(registry, seed=99)
    probes = await registry.probe_all()

    slot = ParticipantSlot(
        participant_id="slot_pool",
        persona_id="persona_friedrich",
        runtime_selection="random",
        runtime_candidate_pool=[
            {"agent_id": "fake_agent", "weight": 10.0},
        ],
        model_selection="random",
        model_candidate_pool=[
            {"model_id": "fake-claude-4", "weight": 5.0},
        ],
    )

    snapshot = await resolver.resolve_slot(slot, probes)
    assert snapshot.agent_runtime_id == "fake_agent"
    assert snapshot.model_id == "fake-claude-4"


@pytest.mark.anyio
async def test_random_rejects_no_ready_agents() -> None:
    from persona_continuum.agent.models import AgentCapabilityFlags, AgentProbeResult, AgentStatus
    from persona_continuum.room.random_resolver import NoReadyAgentError

    registry = AgentRegistry(include_builtins=False, include_fake=False)
    resolver = RandomBindingResolver(registry)

    # All probes are DISABLED or BROKEN
    probes = [
        AgentProbeResult(
            id="broken_agent",
            name="Broken",
            status=AgentStatus.BROKEN,
            protocols=[],
            capabilities=AgentCapabilityFlags(),
        )
    ]
    slot = ParticipantSlot(
        participant_id="slot_1",
        persona_id="p1",
        runtime_selection="random",
    )

    with pytest.raises(NoReadyAgentError):
        await resolver.resolve_participant(slot, probes)


@pytest.mark.anyio
async def test_random_rejects_auth_required_agents() -> None:
    from persona_continuum.agent.models import AgentCapabilityFlags, AgentProbeResult, AgentStatus
    from persona_continuum.room.random_resolver import NoReadyAgentError

    registry = AgentRegistry(include_builtins=False, include_fake=False)
    resolver = RandomBindingResolver(registry)

    # Probes in AUTH_REQUIRED status
    probes = [
        AgentProbeResult(
            id="claude_cli",
            name="Claude CLI",
            status=AgentStatus.AUTH_REQUIRED,
            protocols=[],
            capabilities=AgentCapabilityFlags(),
        )
    ]
    slot = ParticipantSlot(
        participant_id="slot_1",
        persona_id="p1",
        runtime_selection="random",
    )

    with pytest.raises(NoReadyAgentError):
        await resolver.resolve_participant(slot, probes)


@pytest.mark.anyio
async def test_explicit_unknown_model_rejected() -> None:
    from persona_continuum.room.random_resolver import UnknownModelError

    registry = AgentRegistry(include_builtins=False, include_fake=True)
    resolver = RandomBindingResolver(registry)
    probes = await registry.probe_all()

    slot = ParticipantSlot(
        participant_id="slot_1",
        persona_id="p1",
        runtime_selection="fake_agent",
        model_selection="nonexistent-model-xyz",
        allow_manual_model_id=False,
    )

    with pytest.raises(UnknownModelError):
        await resolver.resolve_participant(slot, probes)

    # When allow_manual_model_id=True, it should succeed
    slot_manual = ParticipantSlot(
        participant_id="slot_1",
        persona_id="p1",
        runtime_selection="fake_agent",
        model_selection="nonexistent-model-xyz",
        allow_manual_model_id=True,
    )
    snap = await resolver.resolve_participant(slot_manual, probes)
    assert snap.model_id == "nonexistent-model-xyz"


@pytest.mark.anyio
async def test_explicit_unsupported_reasoning_rejected() -> None:
    from persona_continuum.room.random_resolver import UnsupportedReasoningEffortError

    registry = AgentRegistry(include_builtins=False, include_fake=True)
    resolver = RandomBindingResolver(registry)
    probes = await registry.probe_all()

    # fake-gpt-5 has supported_reasoning_efforts=["none", "low", "medium", "high", "xhigh"]
    slot = ParticipantSlot(
        participant_id="slot_1",
        persona_id="p1",
        runtime_selection="fake_agent",
        model_selection="fake-gpt-5",
        reasoning_selection="ultra_max_impossible",
    )

    with pytest.raises(UnsupportedReasoningEffortError):
        await resolver.resolve_participant(slot, probes)


@pytest.mark.anyio
async def test_dynamic_model_accepts_reasoning_effort() -> None:
    from persona_continuum.agent.models import AgentProbeResult, AgentStatus, ModelCapability

    registry = AgentRegistry(include_builtins=False, include_fake=False)

    class MockApiAdapter:
        adapter_id = "api_openrouter"
        name = "OpenRouter API"

    registry.register_adapter(MockApiAdapter())  # type: ignore[arg-type]
    resolver = RandomBindingResolver(registry)

    mock_probe = AgentProbeResult(
        id="api_openrouter",
        name="OpenRouter API",
        status=AgentStatus.READY,
        models=[
            ModelCapability(
                id="stealth/ox-alpha",
                display_name="Stealth OX Alpha",
                supported_reasoning_efforts=[],
            )
        ],
    )

    slot = ParticipantSlot(
        participant_id="slot_1",
        persona_id="p1",
        runtime_selection="api_openrouter",
        model_selection="stealth/ox-alpha",
        reasoning_selection="xhigh",
    )

    snap = await resolver.resolve_participant(slot, [mock_probe])
    assert snap.model_id == "stealth/ox-alpha"
    assert snap.reasoning_effort == "xhigh"
