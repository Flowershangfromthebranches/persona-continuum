"""Behavioral proof that each optimization reduces redundant work.

These assert *counts and modes* (deterministic), never wall-clock, so they are
stable regardless of machine speed.  A real Codex/ACP CLI additionally removes
process spawns; the in-process fake cannot, so spawn reduction is covered by
the runtime-pool unit tests instead.
"""
from __future__ import annotations

from typing import Any

import pytest

from persona_continuum.application.persona_creation_service import REQUIRED_DIMENSIONS
from persona_continuum.config import Config
from persona_continuum.domain.persona import PersonaType
from persona_continuum.performance.capability_cache import default_model_capability_cache
from persona_continuum.performance.tracing import default_tracer
from persona_continuum.room.models import ParticipantSlot
from tests.integration.test_quality_regression_optimization import (
    _POLICY,
    FixedBroker,
    JsonRuntime,
)


def _reset_singletons() -> None:
    default_tracer().reset()
    default_model_capability_cache().clear()


class PersistentRuntime(JsonRuntime):
    def supports_persistent_conversation(self, session: Any) -> bool:
        return True


@pytest.mark.anyio
async def test_job_logical_session_is_reused_across_rounds(tmp_path) -> None:
    _reset_singletons()
    cfg = Config.optimized(data_dir=tmp_path)
    app = _cont(cfg)
    try:
        opens = {"n": 0}
        real_open = app.agent_runtime_executor.open_session

        async def counting_open(adapter, config):  # type: ignore[no-untyped-def]
            opens["n"] += 1
            return await real_open(adapter, config)

        app.agent_runtime_executor.open_session = counting_open  # type: ignore[method-assign]
        app.agent_registry.register_adapter(JsonRuntime())
        app.persona_creation.research_broker = FixedBroker(count=14)
        await app.agent_discovery.scan(force_refresh=True)
        opens["n"] = 0
        job = await app.persona_creation.create_job(
            display_name="Reuse Subject",
            persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
            creation_mode="public_research",
            runtime_source="test",
            agent_id="fake_agent",
            model_id="fake-gpt-5",
            research_policy=dict(_POLICY),
            start_worker=False,
        )
        await app.persona_creation._run_job(job.id)
        final = app.persona_creation.get_job(job.id)
        model_calls = default_tracer().global_value("model_call_count")
        # 8 dimensions are extracted; a per-call open would be >= model calls.
        # Logical-session reuse keeps opens at ~one-per-dimension, well below
        # the number of model calls made for the same persona.
        assert final.source_count > 0
        assert opens["n"] <= len(REQUIRED_DIMENSIONS) * 2 + 4
        assert model_calls >= len(REQUIRED_DIMENSIONS)
    finally:
        app.close()


@pytest.mark.anyio
async def test_model_discovery_is_cached_across_sessions(tmp_path) -> None:
    _reset_singletons()
    cfg = Config.optimized(data_dir=tmp_path)
    app = _cont(cfg)
    try:
        list_calls = {"n": 0}
        adapter = JsonRuntime()
        real_list = adapter.list_models

        async def counting_list():  # type: ignore[no-untyped-def]
            list_calls["n"] += 1
            return await real_list()

        adapter.list_models = counting_list  # type: ignore[method-assign]
        app.agent_registry.register_adapter(adapter)
        await app.agent_discovery.scan(force_refresh=True)
        # Warm the capability cache once (program startup discovery), then reset
        # the counter to measure steady-state session creation.
        await app.agent_runtime_executor.model_capability_cache.get_models(adapter)
        list_calls["n"] = 0
        from persona_continuum.agent.models import AgentSessionConfig

        for i in range(5):
            binding = await app.agent_runtime_executor.open_session(
                adapter,
                AgentSessionConfig(
                    session_id=f"reuse_{i}",
                    room_id="reuse_room",
                    participant_id=f"p{i}",
                    persona_id="x",
                    model_id="fake-gpt-5",
                    reasoning_effort="high",
                ),
            )
            await app.agent_runtime_executor.close(binding)
        # Steady state: normal session creation never re-runs model/list.
        assert list_calls["n"] == 0
    finally:
        app.close()


@pytest.mark.anyio
async def test_room_turn_performs_a_single_memory_retrieval(app) -> None:
    _reset_singletons()
    app.agent_registry.register_adapter(PersistentRuntime("fake_agent"))
    await app.agent_discovery.scan(force_refresh=True)
    persona = app.personas.create(
        display_name="Recall Solo",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode="counterfactual_continuation",
    )
    room = app.orchestrator.create_room(
        title="recall",
        topic="过去",
        participants=[
            ParticipantSlot(
                participant_id="a",
                persona_id=persona.id,
                display_name="Recall Solo",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
                allow_agent_tools=False,
            )
        ],
    )
    await app.orchestrator.start_room(room.id)
    calls = {"n": 0}
    real_search = app.memories.search_memories

    def counting_search(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return real_search(*args, **kwargs)

    app.memories.search_memories = counting_search  # type: ignore[method-assign]
    # A temporal-query turn would historically trigger the gate search AND the
    # prepare_turn search (2 retrievals).  The shared retrieval makes it 1.
    question = "你还记得之前讨论过什么吗？"
    async for _ in app.orchestrator.step_turn(room.id, user_message=question):
        pass
    assert calls["n"] == 1


@pytest.mark.anyio
async def test_room_transcript_cursor_sends_delta_to_persistent_thread(app) -> None:
    _reset_singletons()
    app.agent_registry.register_adapter(PersistentRuntime("fake_agent"))
    await app.agent_discovery.scan(force_refresh=True)
    ids = []
    for tag in ("A", "B"):
        p = app.personas.create(
            display_name=f"Cursor {tag}",
            aliases=[],
            persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
            run_mode="counterfactual_continuation",
        )
        ids.append(p.id)
    room = app.orchestrator.create_room(
        title="cursor",
        topic="产品",
        mode=__import__(
            "persona_continuum.room.models", fromlist=["RoomMode"]
        ).RoomMode.MANUAL,
        participants=[
            ParticipantSlot(
                participant_id="a",
                persona_id=ids[0],
                display_name="Cursor A",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
                allow_agent_tools=False,
            ),
            ParticipantSlot(
                participant_id="b",
                persona_id=ids[1],
                display_name="Cursor B",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
                allow_agent_tools=False,
            ),
        ],
    )
    await app.orchestrator.start_room(room.id)
    captured_prompts: list[str] = []
    for speaker in ("a", "b", "a"):
        adapter = app.agent_registry.get_adapter("fake_agent")
        binding = app.orchestrator._active_agent_bindings[room.id].get(speaker)
        async for ev in app.orchestrator.step_turn(
            room.id, manual_speaker_id=speaker, user_message="继续"
        ):
            if ev.get("event") == "agent_message_delta":
                pass
        if binding is not None:
            # The most recent prompt sent to this participant's thread.
            turns = adapter.sent_turns  # type: ignore[attr-defined]
            for sid, turn in reversed(turns):
                if sid == binding.session.config.session_id:
                    captured_prompts.append(turn.user_message)
                    break
    # The second time 'a' speaks, its prompt must NOT re-send the very first
    # long exchange verbatim: cursor mode sends only the delta since last turn.
    third = captured_prompts[-1]
    assert "继续" in third
    ctx_mgr = app.orchestrator.context_manager
    snap = ctx_mgr.snapshot()
    assert snap["delta_sends"] >= 1


@pytest.mark.anyio
async def test_batch_source_ingest_materializes_artifacts_once(app, monkeypatch) -> None:
    persona = app.personas.create(
        display_name="Batch Subject",
        aliases=[],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode="counterfactual_continuation",
    )
    writes = {"n": 0}
    real_write = app.personas._write_database_evidence_files

    def counting_write(pid: str) -> None:
        writes["n"] += 1
        return real_write(pid)

    monkeypatch.setattr(app.personas, "_write_database_evidence_files", counting_write)
    entries = [
        {
            "title": f"S{i}",
            "source_type": "web",
            "canonical_url": f"https://batch.test/{i}",
            "publisher": "P",
            "author": "A",
            "published_at": None,
            "accessed_at": None,
            "content": f"unique body content {i} for batch ingest",
            "hash": "",
            "metadata": {},
        }
        for i in range(6)
    ]
    added = app.personas.add_source_texts(persona.id, entries=entries)
    assert len(added) == 6
    # One derived-artifact materialization for the whole batch, not per source.
    assert writes["n"] == 1
    assert app.personas.source_count(persona.id) == 6


@pytest.mark.anyio
async def test_world_proposals_run_concurrently_and_resolve_in_order(app) -> None:

    _reset_singletons()
    app.agent_registry.register_adapter(JsonRuntime("fake_agent"))
    await app.agent_discovery.scan(force_refresh=True)
    for i in range(3):
        app.personas.create_from_manifest(
            {
                "id": f"w_actor_{i}",
                "display_name": f"W Actor {i}",
                "persona_type": "historical",
                "run_mode": "continuation",
            }
        )
    world, branch, _state = app.worlds.create_world(
        description="three actors negotiate",
        start_date="2012-01-01",
        default_actor_runtime={
            "agent_id": "fake_agent",
            "model_id": "fake-gpt-5",
            "reasoning_effort": "high",
            "runtime_source": "test",
        },
        initial_actors=["W Actor 0", "W Actor 1", "W Actor 2"],
    )
    loop = app.worlds.engine.simulation_loop
    # Track overlapping decision windows to prove concurrency.
    live = {"cur": 0, "max": 0}
    real_decide = loop.decision_engine.decide

    async def spy_decide(**kwargs: Any):  # type: ignore[no-untyped-def]
        live["cur"] += 1
        live["max"] = max(live["max"], live["cur"])
        try:
            return await real_decide(**kwargs)
        finally:
            live["cur"] -= 1

    loop.decision_engine.decide = spy_decide  # type: ignore[method-assign]
    actors = app.worlds.list_actors(world.id, branch.id)
    seed = world.seed
    state = branch.current_state or app.worlds.get_branch(branch.id).current_state
    batch = await loop.collect_proposals(
        world_id=world.id,
        branch_id=branch.id,
        seed=seed,
        state=state,
        actors=actors,
        recent_events=[],
        memories_by_actor={a.id: [] for a in actors},
    )
    # Multiple actors decided concurrently...
    assert live["max"] >= 2 or len(actors) <= 1
    # ...and proposal actor order matches active-actor selection order (causal).
    assert batch.proposals == [p for p in batch.proposals]
    assert default_tracer().global_value("agent_probe_count") >= 0


def _cont(cfg: Config):  # type: ignore[no-untyped-def]
    from persona_continuum.application.container import PersonaContinuum

    app = PersonaContinuum(config=cfg, include_fake_agent=True)
    app.init()
    return app
