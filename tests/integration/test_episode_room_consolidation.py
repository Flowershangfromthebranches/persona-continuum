"""Room path: Episodes are assigned per turn and summarised off the reply path.

Two properties are load-bearing here.  First, a room turn must produce an
Episode assignment without the user waiting for anything: the reply is
committed and streamed before consolidation is even scheduled.  Second, a
consolidation failure must be recorded on the Episode and stay retryable while
chatting continues normally.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.episode import EpisodeStatus
from persona_continuum.room.models import ParticipantSlot, RoomMode

PERSONA = "su_he"


class _StructuredResult:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


def _summary_payload() -> dict[str, Any]:
    return {
        "title": "讨论考研与重庆理工",
        "summary": "用户正在考虑重庆理工大学人工智能专硕，主要担心数学成绩。",
        "important_events": ["用户表达数学焦虑"],
        "commitments": ["用户计划第二天开始复习"],
        "unresolved": ["尚未确定完整复习安排"],
        "emotional_arc": ["焦虑", "被理解", "形成计划"],
        "topics": ["考研", "重庆理工"],
        "entities": ["重庆理工大学"],
        "user_stated": ["担心数学成绩"],
        "persona_stated": ["苏禾安慰了用户"],
        "inferred_context": [],
        "importance": 0.7,
        "confidence": 0.8,
    }


@pytest.fixture()
def room_app(tmp_path) -> Any:
    config = Config(data_dir=tmp_path / "pc-room")
    app = PersonaContinuum(config, include_fake_agent=True)
    app.init()
    app.personas.create_from_manifest(
        {
            "id": PERSONA,
            "display_name": "苏禾",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    yield app
    app.close()


async def _direct_room(
    app: PersonaContinuum, *, metadata: dict[str, Any] | None = None
) -> str:
    room = app.orchestrator.create_room(
        title="苏禾",
        topic="日常",
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
        metadata={"phase8_context_assembly": False, **(metadata or {})},
    )
    await app.orchestrator.start_room(room.id)
    return room.id


async def _turn(app: PersonaContinuum, room_id: str, message: str) -> list[dict[str, Any]]:
    return [event async for event in app.orchestrator.step_turn(room_id, user_message=message)]


@pytest.mark.anyio
async def test_room_turn_assigns_an_episode_without_blocking_the_reply(
    room_app: PersonaContinuum, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def slow_structured(
        binding: Any,
        *,
        system_prompt: str,
        user_message: str,
        schema: dict[str, Any],
        phase: str,
    ) -> Any:
        assert phase == "memory_episode_consolidation"
        started.set()
        await asyncio.sleep(0.5)  # a summariser that is genuinely slow
        return _StructuredResult(_summary_payload())

    monkeypatch.setattr(
        room_app.orchestrator.runtime_executor, "execute_structured", slow_structured
    )

    room_id = await _direct_room(room_app)
    events = await _turn(room_app, room_id, "我最近在想考研的事")

    # The reply completed and was committed before any summary exists.
    assert "turn_completed" in [event.get("event") for event in events]
    commit_event = next(
        event for event in events if event.get("event") == "persona_commit_completed"
    )
    assert commit_event["episode_id"]

    episodes = room_app.episodes.list_episodes(persona_id=PERSONA, room_id=room_id)
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.turn_count == 1
    assert episode.room_id == room_id
    assert episode.status is EpisodeStatus.OPEN
    assert episode.summary_status == "pending"  # still owed

    task = room_app.orchestrator._episode_tasks.get(room_id)
    assert task is not None
    # Non-blocking evidence: the turn is long over and consolidation is still
    # running, exactly as a background pass should be.
    assert not task.done()
    await task

    refreshed = room_app.episodes.get_episode(episode.id)
    assert refreshed is not None
    assert refreshed.summary_status == "ready"
    assert refreshed.title == "讨论考研与重庆理工"
    # An OPEN episode keeps accepting turns even once it has a provisional summary.
    assert refreshed.status is EpisodeStatus.OPEN
    assert refreshed.structured_summary().commitments == ["用户计划第二天开始复习"]
    assert episode.turn_count == 1  # provenance untouched by consolidation
    assert room_app.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0


@pytest.mark.anyio
async def test_room_turn_survives_a_failing_summariser(
    room_app: PersonaContinuum, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_structured(
        binding: Any,
        *,
        system_prompt: str,
        user_message: str,
        schema: dict[str, Any],
        phase: str,
    ) -> Any:
        raise TimeoutError("summariser timed out")

    monkeypatch.setattr(
        room_app.orchestrator.runtime_executor, "execute_structured", failing_structured
    )

    room_id = await _direct_room(room_app)
    events = await _turn(room_app, room_id, "第一句")
    assert "turn_completed" in [event.get("event") for event in events]
    task = room_app.orchestrator._episode_tasks.get(room_id)
    assert task is not None
    await task

    episode = room_app.episodes.list_episodes(persona_id=PERSONA, room_id=room_id)[0]
    assert episode.status is EpisodeStatus.FAILED
    assert episode.last_error == "TimeoutError"
    assert episode.turn_count == 1
    assert room_app.episodes.coverage(persona_id=PERSONA)["pending_consolidation"] == 1

    # Chat continues normally while the Episode stays retryable.
    events = await _turn(room_app, room_id, "第二句，照常聊天")
    assert "turn_completed" in [event.get("event") for event in events]
    assert room_app.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0
    assert room_app.episodes.get_episode(episode.id) is not None


@pytest.mark.anyio
async def test_room_episode_grows_across_turns(
    room_app: PersonaContinuum, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def structured(
        binding: Any,
        *,
        system_prompt: str,
        user_message: str,
        schema: dict[str, Any],
        phase: str,
    ) -> Any:
        return _StructuredResult(_summary_payload())

    monkeypatch.setattr(room_app.orchestrator.runtime_executor, "execute_structured", structured)

    room_id = await _direct_room(room_app)
    episode_ids = []
    for index in range(3):
        events = await _turn(room_app, room_id, f"第{index}句话")
        commit_event = next(
            event for event in events if event.get("event") == "persona_commit_completed"
        )
        episode_ids.append(commit_event["episode_id"])

    assert len(set(episode_ids)) == 1  # one continuing Episode
    episode = room_app.episodes.list_episodes(persona_id=PERSONA, room_id=room_id)[0]
    assert episode.turn_count == 3
    sources = room_app.episodes.episode_turns(episode.id)
    # Phase 3.1: each turn carries the shared user message it answered as an
    # extra source, so a 3-turn Episode has 3 own turns + 3 shared references.
    assert [item.source_kind for item in sources].count("session_turn") == 3
    assert [item.source_kind for item in sources].count("shared_user") == 3
    assert [item.position for item in sources] == [0, 1, 2, 3, 4, 5]
    # Every source turn resolves back to a committed room turn.
    transcript_ids = {
        str(row["turn_id"])
        for row in room_app.database.conn.execute(
            "SELECT turn_id FROM room_transcripts WHERE room_id = ?", (room_id,)
        ).fetchall()
    }
    assert {item.turn_id for item in sources} <= transcript_ids
    assert room_app.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0


@pytest.mark.anyio
async def test_room_pass_also_extracts_semantic_facts(
    room_app: PersonaContinuum, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One background pass produces both derived layers, in order.

    The Episode summary and the Semantic Facts come from the same raw turns but
    are separate model calls; the room path drives both, sequentially, off the
    reply path.
    """

    phases: list[str] = []

    def _payload_for(user_message: str) -> dict[str, Any]:
        if "茉莉奶绿" in user_message:
            return {
                "facts": [
                    {
                        "category": "preference",
                        "subject": "用户",
                        "predicate": "最喜欢的饮品",
                        "value": "茉莉奶绿",
                        "origin": "user_asserted",
                        "confidence": 0.8,
                        "exclusive": True,
                    }
                ]
            }
        return {"facts": []}

    async def structured(
        binding: Any,
        *,
        system_prompt: str,
        user_message: str,
        schema: dict[str, Any],
        phase: str,
    ) -> Any:
        phases.append(phase)
        if phase == "memory_fact_extraction":
            from persona_continuum.application.fact_service import FACT_EXTRACTION_INTRO

            assert system_prompt == FACT_EXTRACTION_INTRO
            payload = json.loads(user_message)
            text = " ".join(str(turn.get("text") or "") for turn in payload.get("turns") or [])
            return _StructuredResult(_payload_for(text))
        if phase == "memory_thread_resolution":
            # Active Threads are a later layer; this test is about Facts.
            return _StructuredResult({"threads": []})
        if phase == "memory_hierarchy_summary":
            # Phase 6 groups the Episode into a Chapter; not this test's subject.
            payload = json.loads(user_message)
            titles = [
                str(item.get("title") or "") for item in payload.get("sources") or []
            ]
            return _StructuredResult(
                {
                    "title": "、".join(titles[:2]) or "阶段",
                    "summary": "、".join(title for title in titles if title),
                }
            )
        return _StructuredResult(_summary_payload())

    monkeypatch.setattr(room_app.orchestrator.runtime_executor, "execute_structured", structured)

    room_id = await _direct_room(room_app)
    await _turn(room_app, room_id, "我最喜欢喝茉莉奶绿。")
    task = room_app.orchestrator._episode_tasks.get(room_id)
    assert task is not None
    await task

    # Episode summary, then facts, then threads, then the Chapter: one
    # inference at a time.
    assert phases == [
        "memory_episode_consolidation",
        "memory_fact_extraction",
        "memory_thread_resolution",
        "memory_hierarchy_summary",
    ]

    episode = room_app.episodes.list_episodes(persona_id=PERSONA, room_id=room_id)[0]
    assert episode.summary_status == "ready"

    facts = room_app.facts.list_facts(persona_id=PERSONA, status="active")
    assert len(facts) == 1
    assert facts[0].value_json == {"text": "茉莉奶绿"}
    assert facts[0].evidence_count == 1
    inspected = room_app.facts.inspect_fact(facts[0].id)
    assert inspected is not None
    assert inspected["source_episode_ids"] == [episode.id]
    assert inspected["sources"][0]["resolvable"] is True

    # The Episode still owes nothing, and no turn became unaccounted for.
    assert room_app.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0
    assert room_app.facts.pending_extraction_episodes(room_id=room_id) == []

    # A second pass is a no-op: both work lists are empty.
    task = room_app.orchestrator._episode_tasks.get(room_id)
    if task is not None and not task.done():
        await task
    assert room_app.facts.stats(persona_id=PERSONA)["facts"] == 1

    # Facts are stored, not injected: the prompt still says so explicitly.
    events = await _turn(room_app, room_id, "第二句")
    report = next(event for event in events if event.get("event") == "room_context_report")
    assert report["fact_injection"] == "phase8_not_enabled"
    assert report["semantic_memory_tokens"] == 0
    assert report["semantic_facts_available"] == 1


@pytest.mark.anyio
async def test_context_report_reports_episode_storage_without_injecting_it(
    room_app: PersonaContinuum,
) -> None:
    """Phase 3 records Episodes; it does not put them in the prompt."""

    room_id = await _direct_room(room_app)
    first_events = await _turn(room_app, room_id, "第一句")
    first_report = next(
        event for event in first_events if event.get("event") == "room_context_report"
    )
    assert first_report["episodes_selected"] == 0
    assert first_report["episode_tokens"] == 0
    assert first_report["episode_injection"] == "phase8_not_enabled"
    # The report describes the state when the PROMPT was assembled, which is
    # before this turn's Episode exists -- 0 here is the honest answer.
    assert first_report["episodes_available"] == 0
    # Phase 5 layers report real storage statistics while contributing nothing
    # to the prompt: "not enabled" must stay distinguishable from "nothing
    # stored", so the selected/token numbers are 0 and the mode is explicit.
    assert first_report["threads_selected"] == 0
    assert first_report["thread_tokens"] == 0
    assert first_report["thread_injection"] == "phase8_not_enabled"
    assert first_report["threads_available"] == 0
    # Layers that do not exist yet stay None or 0 rather than injecting tokens.
    assert first_report["historical_excerpt_tokens"] in (None, 0)

    # By the next turn the recorded Episode is visible as a storage statistic,
    # while still contributing nothing to the prompt.
    second_events = await _turn(room_app, room_id, "第二句")
    second_report = next(
        event for event in second_events if event.get("event") == "room_context_report"
    )
    assert second_report["episodes_available"] == 1
    assert second_report["episodes_selected"] == 0
