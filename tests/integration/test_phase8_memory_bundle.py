"""Phase 8 integration tests: MemoryBundle, multi-tier retrieval, and context assembly.

Verifies:
1. BASE retrieval: active threads, current relationship, valid facts (no raw excerpts).
2. STANDARD retrieval: relevant episodes, temporal facts, hierarchical summaries.
3. DEEP retrieval: detail queries activate RawRecall excerpts with safety fence.
4. Implicit continuation: "票买好了" matches live thread without keyword overlap.
5. Temporal fact resolution: "现在" picks current fact; "以前" picks superseded fact.
6. Structured trimming ladder: degrades in order (summaries -> episodes -> facts -> raw).
7. Recall gate ordering: recall_started < recall_completed < agent_started.
8. Fallback resilience: subsystem exceptions do not crash turn assembly.
9. Anti-leakage hygiene: no internal IDs or retrieval scores leaked into prompt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.episode import (
    EpisodeScope,
    EpisodeSummary,
)
from persona_continuum.domain.memory_bundle import (
    FactReliability,
    RetrievalMode,
)
from persona_continuum.domain.raw_recall import ExcerptMessage, HistoricalExcerpt, SelectionReason
from persona_continuum.domain.semantic_fact import (
    FactCategory,
    FactOrigin,
    FactStatus,
    SemanticFact,
    canonical_fact_key,
)
from persona_continuum.domain.thread import MemoryActiveThread, ThreadStatus, ThreadType
from persona_continuum.room.context_policy import (
    LOCAL_CONSTRAINED_PROFILE,
    REMOTE_QUALITY_PROFILE,
)
from persona_continuum.room.models import ParticipantSlot, RoomMode
from persona_continuum.room.orchestrator import _trim_bundle_for_stage
from persona_continuum.room.prompt_composer import PromptComposer

PERSONA = "su_he"
COUNTERPART = "user"
BASE_TIME = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


@pytest.fixture()
def app(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Any:
    data_dir = tmp_path / "pc-phase8"
    monkeypatch.setenv("PERSONA_CONTINUUM_HOME", str(data_dir))
    config = Config(data_dir=data_dir, phase8_context_assembly=True)
    continuum = PersonaContinuum(config, include_fake_agent=True)
    continuum.init()
    continuum.personas.create_from_manifest(
        {
            "id": PERSONA,
            "display_name": "苏禾",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    yield continuum
    continuum.close()


def _seed_persona_data(app: PersonaContinuum) -> None:
    """Seed episodes, facts, threads, and summaries for Su He."""
    # 1. Semantic Facts: coffee (superseded) vs tea (current)
    app.facts._insert_fact(
        SemanticFact(
            id="fact_coffee",
            persona_id=PERSONA,
            counterpart_id=COUNTERPART,
            category=FactCategory.PREFERENCE,
            fact_key=canonical_fact_key(FactCategory.PREFERENCE, "苏禾", "最喜欢的饮品"),
            value_key="拿铁咖啡",
            subject="苏禾",
            predicate="最喜欢的饮品",
            value_json={"text": "热拿铁咖啡"},
            display_text="苏禾最喜欢喝热拿铁咖啡",
            status=FactStatus.SUPERSEDED,
            origin=FactOrigin.PERSONA_ASSERTED,
            confidence=0.85,
            valid_from=BASE_TIME - timedelta(days=365),
            valid_until=BASE_TIME - timedelta(days=60),
        )
    )
    app.facts._insert_fact(
        SemanticFact(
            id="fact_tea",
            persona_id=PERSONA,
            counterpart_id=COUNTERPART,
            category=FactCategory.PREFERENCE,
            fact_key=canonical_fact_key(FactCategory.PREFERENCE, "苏禾", "最喜欢的饮品"),
            value_key="茉莉奶绿",
            subject="苏禾",
            predicate="最喜欢的饮品",
            value_json={"text": "茉莉奶绿微糖"},
            display_text="苏禾平时最喜欢喝茉莉奶绿微糖",
            status=FactStatus.ACTIVE,
            origin=FactOrigin.PERSONA_ASSERTED,
            confidence=0.95,
            valid_from=BASE_TIME - timedelta(days=60),
        )
    )

    # 2. Active Thread: Chongqing trip (LIVE)
    app.threads._insert_thread(
        MemoryActiveThread(
            id="thread_chongqing",
            persona_id=PERSONA,
            counterpart_id=COUNTERPART,
            thread_key="plan|user|chongqing",
            thread_type=ThreadType.PLAN,
            title="重庆旅行",
            summary="用户和苏禾打算下个月一起去重庆旅游吃火锅。",
            status=ThreadStatus.ACTIVE,
            confidence=0.9,
            current_state={"step": "准备买票"},
        )
    )

    # 3. Episode: High school memory
    ep = app.episodes._create_episode(
        EpisodeScope(persona_id=PERSONA, counterpart_id=COUNTERPART, session_id="sess_hist"),
        started_at=BASE_TIME - timedelta(days=10),
        room_id="room_history",
        visibility="room_public",
        boundary_reason="first_turn",
    )
    summary_obj = EpisodeSummary(
        title="高中母校回忆",
        summary="苏禾回忆起高三那年夏天在附中后门的树荫下吃西瓜的场景。",
        topics=["高中", "母校", "夏天"],
        entities=["附中"],
        user_stated=["问起苏禾的高中生活"],
        persona_stated=["很怀念高中的蝉鸣"],
        importance=0.7,
        confidence=0.9,
    )
    app.database.conn.execute(
        "UPDATE memory_episodes SET title = ?, summary = ?, summary_json = ?, "
        "summary_status = 'ready', importance = 0.7, confidence = 0.9 WHERE id = ?",
        (
            summary_obj.title,
            summary_obj.summary,
            json.dumps(summary_obj.model_dump(mode="json"), ensure_ascii=False),
            ep.id,
        ),
    )
    app.database.conn.commit()


# --- 1. BASE Retrieval Mode --------------------------------------------------


def test_phase8_base_retrieval_mode(app: PersonaContinuum) -> None:
    _seed_persona_data(app)
    planner = app.retrieval_planner

    # Routine greeting - should be BASE mode
    bundle = planner.plan_and_retrieve(
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        user_message="早上好，今天天气真不错。",
        context_profile=LOCAL_CONSTRAINED_PROFILE,
    )

    assert bundle.retrieval_metadata.mode == RetrievalMode.BASE
    # Active threads should be present
    assert any(th.title == "重庆旅行" and th.is_live for th in bundle.active_threads)
    # Current active fact should be present
    assert any("茉莉奶绿" in f.display_text for f in bundle.semantic_facts)
    # No raw excerpts in BASE mode
    assert len(bundle.historical_excerpts) == 0

    # Test prompt formatting
    session = app.sessions.start_session(PERSONA)
    prepared = app.sessions.prepare_turn(PERSONA, session.id, "你好苏禾")
    composer = PromptComposer()
    sys_p, user_p, full_p = composer.compose_turn_prompt(
        slot=ParticipantSlot(participant_id="p1", persona_id=PERSONA, display_name="苏禾"),
        prepared=prepared,
        memory_bundle=bundle,
    )

    assert "### Active Threads" in user_p
    assert "重庆旅行" in user_p
    assert "### Relevant Facts" in user_p
    assert "茉莉奶绿" in user_p
    # Anti-leakage instruction must be in system prompt
    assert "Never mention internal memory system identifiers" in sys_p
    # No internal database ID leaking
    assert "fact_" not in user_p
    assert "thread_" not in user_p


# --- 2. STANDARD Retrieval Mode ----------------------------------------------


def test_phase8_standard_retrieval_mode(app: PersonaContinuum) -> None:
    _seed_persona_data(app)
    planner = app.retrieval_planner

    # Question about the past - should trigger STANDARD mode
    bundle = planner.plan_and_retrieve(
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        user_message="你以前高中母校是在哪里读的？当时有什么好玩的事？",
        context_profile=LOCAL_CONSTRAINED_PROFILE,
    )

    assert bundle.retrieval_metadata.mode in {RetrievalMode.STANDARD, RetrievalMode.DEEP}
    # Episode should be retrieved
    assert any("高中母校回忆" in ep.title for ep in bundle.relevant_episodes)

    session = app.sessions.start_session(PERSONA)
    msg = "你以前高中母校是在哪里读的？当时有什么好玩的事？"
    prepared = app.sessions.prepare_turn(PERSONA, session.id, msg)
    composer = PromptComposer()
    report: dict[str, Any] = {}
    composer.compose_turn_prompt(
        slot=ParticipantSlot(participant_id="p1", persona_id=PERSONA, display_name="苏禾"),
        prepared=prepared,
        memory_bundle=bundle,
        report=report,
    )

    assert report["episodes_rendered"] >= 1
    assert report["episode_tokens"] > 0


# --- 3. DEEP Retrieval Mode & Raw Recall ------------------------------------


def test_phase8_deep_retrieval_mode(app: PersonaContinuum) -> None:
    _seed_persona_data(app)
    planner = app.retrieval_planner

    # Specific detail query - should trigger DEEP mode
    bundle = planner.plan_and_retrieve(
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        user_message="你还记得我们当时坐火车是几点出发的吗？具体是哪个车厢哪个铺位？",
        context_profile=REMOTE_QUALITY_PROFILE,
    )

    assert bundle.retrieval_metadata.mode == RetrievalMode.DEEP
    assert "detail_request_requires_raw_evidence" in bundle.retrieval_metadata.reasons

    excerpt = HistoricalExcerpt(
        excerpt_id="exc_1",
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        selection_reason=SelectionReason.EPISODE_ANCHOR,
        messages=[
            ExcerptMessage(
                turn_id="t_1",
                speaker="user",
                raw_text="我买好了K1024次列车，晚上20:15发车，7车厢12号下铺。",
                timestamp="2026-09-01 10:00",
            )
        ],
    )
    bundle.historical_excerpts = [excerpt]

    session = app.sessions.start_session(PERSONA)
    prepared = app.sessions.prepare_turn(PERSONA, session.id, "具体是哪个车厢哪个铺位？")
    composer = PromptComposer()
    report: dict[str, Any] = {}
    _, user_p, _ = composer.compose_turn_prompt(
        slot=ParticipantSlot(participant_id="p1", persona_id=PERSONA, display_name="苏禾"),
        prepared=prepared,
        memory_bundle=bundle,
        report=report,
    )

    assert "### Historical Evidence" in user_p
    assert "dialogue_evidence" in user_p
    assert "20:15发车" in user_p
    assert "7车厢12号下铺" in user_p
    assert report["raw_excerpts_rendered"] == 1
    assert report["historical_excerpt_tokens"] > 0


# --- 4. Implicit Continuation Without Keywords -------------------------------


def test_phase8_implicit_continuation(app: PersonaContinuum) -> None:
    _seed_persona_data(app)
    planner = app.retrieval_planner

    # User says "票买好了" without mentioning "重庆" or "旅行"
    bundle = planner.plan_and_retrieve(
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        user_message="票买好了",
        context_profile=LOCAL_CONSTRAINED_PROFILE,
    )

    # Live thread "重庆旅行" should be strongly matched
    live_matched = [th for th in bundle.active_threads if th.title == "重庆旅行"]
    assert len(live_matched) == 1
    assert live_matched[0].relevance_score >= 0.55
    assert bundle.retrieval_metadata.mode == RetrievalMode.STANDARD


# --- 5. Temporal Fact Resolution ---------------------------------------------


def test_phase8_temporal_fact_resolution(app: PersonaContinuum) -> None:
    _seed_persona_data(app)
    planner = app.retrieval_planner

    # 1. Asking about current preference
    current_bundle = planner.plan_and_retrieve(
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        user_message="你现在平时想喝点什么？",
        context_profile=LOCAL_CONSTRAINED_PROFILE,
    )
    current_facts = [
        f for f in current_bundle.semantic_facts
        if "饮品" in f.predicate or "喝" in f.display_text
    ]
    assert any("茉莉奶绿" in f.display_text for f in current_facts)
    assert not any("拿铁" in f.display_text for f in current_facts)

    # 2. Asking about past preference
    past_bundle = planner.plan_and_retrieve(
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        user_message="你以前在大学那时候最喜欢喝什么来着？",
        context_profile=LOCAL_CONSTRAINED_PROFILE,
    )
    past_facts = [
        f for f in past_bundle.semantic_facts
        if "饮品" in f.predicate or "喝" in f.display_text
    ]
    assert any("拿铁" in f.display_text for f in past_facts)
    # The past fact should carry the historical superseded label
    latte_fact = next(f for f in past_facts if "拿铁" in f.display_text)
    assert latte_fact.reliability == FactReliability.HISTORICAL_SUPERSEDED


# --- 6. Structured Budget Trim Ladder ----------------------------------------


def test_phase8_structured_budget_trim_ladder(app: PersonaContinuum) -> None:
    _seed_persona_data(app)
    planner = app.retrieval_planner

    bundle = planner.plan_and_retrieve(
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        user_message="以前高中母校发生过什么？车厢号是多少？",
        context_profile=LOCAL_CONSTRAINED_PROFILE,
    )
    # Add dummy excerpt
    bundle.historical_excerpts = [
        HistoricalExcerpt(
            excerpt_id="exc_1",
            persona_id=PERSONA,
            counterpart_id=COUNTERPART,
            selection_reason=SelectionReason.EPISODE_ANCHOR,
            messages=[ExcerptMessage(turn_id="t1", speaker="user", raw_text="7车厢")],
        )
    ]

    # Stage 0: full
    s0 = _trim_bundle_for_stage(bundle, stage_index=0, mem_limit=8)
    assert s0 is bundle

    # Stage 1: trim low relevance summaries
    s1 = _trim_bundle_for_stage(bundle, stage_index=1, mem_limit=4)
    assert s1 is not None

    # Stage 3: drop summaries, trim facts to limit
    s3 = _trim_bundle_for_stage(bundle, stage_index=3, mem_limit=2)
    assert s3 is not None
    assert len(s3.hierarchical_summaries) == 0
    assert len(s3.semantic_facts) <= 2

    # Stage 4: drop raw excerpts
    s4 = _trim_bundle_for_stage(bundle, stage_index=4, mem_limit=2)
    assert s4 is not None
    assert len(s4.historical_excerpts) == 0

    # Stage 5: minimal (1 live thread, 1 fact)
    s5 = _trim_bundle_for_stage(bundle, stage_index=5, mem_limit=1)
    assert s5 is not None
    assert len(s5.active_threads) <= 1
    assert len(s5.semantic_facts) <= 1


# --- 7. Full Room Turn Execution & Event Sequence ----------------------------


@pytest.mark.anyio
async def test_phase8_room_turn_execution_and_events(app: PersonaContinuum) -> None:
    _seed_persona_data(app)

    room = app.orchestrator.create_room(
        title="苏禾会话",
        topic="重庆旅行准备",
        participants=[
            ParticipantSlot(
                participant_id="slot_su_he",
                persona_id=PERSONA,
                display_name="苏禾",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
                reasoning_selection="none",
            )
        ],
        mode=RoomMode.DIRECT_CHAT,
        metadata={"phase8_context_assembly": True},
    )
    await app.orchestrator.start_room(room.id)

    events: list[dict[str, Any]] = []
    async for ev in app.orchestrator.step_turn(room.id, user_message="票买好了，下个月出发！"):
        events.append(ev)

    event_types = [e.get("event") for e in events]
    # Enforce Recall Gate order: recall_started < recall_completed < agent_started
    assert "recall_started" in event_types
    assert "recall_completed" in event_types
    assert "agent_started" in event_types

    r_start = event_types.index("recall_started")
    r_done = event_types.index("recall_completed")
    a_start = event_types.index("agent_started")
    assert r_start < r_done < a_start

    # Verify context report carries Phase 8 fields
    report = next(e for e in events if e.get("event") == "room_context_report")
    assert report["context_capacity_ceiling"] > 0
    assert report["context_target_budget"] > 0
    assert report["context_hard_budget"] > 0
    assert report["retrieval_mode"] in {"base", "standard", "deep"}
    assert report["thread_injection"] == "phase8_active"
    assert report["fact_injection"] == "phase8_active"
    assert report["threads_available"] >= 1
    assert report["semantic_facts_available"] >= 1


# --- 8. Fallback Resilience -------------------------------------------------


def test_phase8_fallback_resilience(app: PersonaContinuum) -> None:
    planner = app.retrieval_planner
    # Break raw recall and hierarchies
    broken_raw = MagicMock()
    broken_raw.recall.side_effect = RuntimeError("Raw recall service crashed")
    planner.raw_recall = broken_raw

    broken_hierarchies = MagicMock()
    broken_hierarchies.list_summaries.side_effect = RuntimeError("Hierarchy DB locked")
    planner.hierarchies = broken_hierarchies

    # Should not raise exception
    bundle = planner.plan_and_retrieve(
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        user_message="车厢几点发车？具体原话是什么？",
        context_profile=REMOTE_QUALITY_PROFILE,
    )

    assert bundle is not None
    assert bundle.retrieval_metadata.mode == RetrievalMode.DEEP
    assert len(bundle.historical_excerpts) == 0
    assert len(bundle.hierarchical_summaries) == 0
