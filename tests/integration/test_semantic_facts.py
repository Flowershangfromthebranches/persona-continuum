"""Phase 4 acceptance: Semantic Facts with temporal validity and provenance.

Scenarios A-K from the Phase 4 brief.  The extractor is scripted so the tests
are about the STORE (dedup, supersession, validity, origin policy, idempotency)
rather than about model quality; ``scripts/phase4_semantic_facts.py`` is the
real-model counterpart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.semantic_fact import (
    FactDurability,
    FactOrigin,
    FactStatus,
)

PERSONA = "su_he"
BASE = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
DRINK_SLOT = "最喜欢的饮品"


def _cand(value: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "category": "preference",
        "subject": "用户",
        "predicate": DRINK_SLOT,
        "value": value,
        "origin": "user_asserted",
        "confidence": 0.8,
        "exclusive": True,
    }
    payload.update(overrides)
    return payload


def _extractor(script: dict[str, list[dict[str, Any]]]) -> Any:
    """Deterministic stand-in: emit the facts of every marker present.

    Markers are chosen to be non-overlapping so "all matches" stays
    unambiguous; accumulating (rather than first-match-wins) mirrors a real
    extractor, which returns everything it found in one pass.
    """

    async def extract(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(str(turn.get("text") or "") for turn in payload.get("turns") or [])
        facts: list[dict[str, Any]] = []
        for marker, entries in script.items():
            if marker in text:
                facts.extend(entries)
        return {"facts": facts}

    return extract


@pytest.fixture()
def app(tmp_path) -> Any:
    config = Config(data_dir=tmp_path / "pc-facts")
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


def _session(app: PersonaContinuum, counterpart: str = "user") -> str:
    return app.sessions.start_session(
        persona_id=PERSONA, title="t", counterpart_id=counterpart
    ).id


def _commit(
    app: PersonaContinuum,
    session_id: str,
    message: str,
    *,
    minutes: int = 0,
    counterpart: str = "user",
    reply: str = "苏禾回应",
) -> str:
    report = app.sessions.commit_turn(
        persona_id=PERSONA,
        session_id=session_id,
        user_message=message,
        persona_response=reply,
        occurred_at=BASE + timedelta(minutes=minutes),
        counterpart_id=counterpart,
    )
    return str(report["turn_id"])


def _episodes(app: PersonaContinuum, *, counterpart: str = "user") -> list[Any]:
    return sorted(
        app.episodes.list_episodes(persona_id=PERSONA, counterpart_id=counterpart),
        key=lambda item: item.started_at,
    )


async def _extract_all(
    app: PersonaContinuum, extract: Any, *, counterpart: str = "user"
) -> list[dict[str, Any]]:
    reports = []
    for episode in _episodes(app, counterpart=counterpart):
        reports.append(await app.facts.extract_episode_facts(episode.id, extract=extract))
    return reports


def _active(app: PersonaContinuum, *, counterpart: str = "user") -> list[Any]:
    return app.facts.list_facts(
        persona_id=PERSONA, counterpart_id=counterpart, status="active"
    )


# --- A. stable preference ---------------------------------------------------


@pytest.mark.anyio
async def test_a_stable_preference_is_stored_as_an_active_fact(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    await _extract_all(app, _extractor({"最喜欢喝茉莉奶绿": [_cand("茉莉奶绿")]}))

    facts = _active(app)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.category.value == "preference"
    assert fact.subject == "用户"
    assert fact.predicate == DRINK_SLOT
    assert fact.value_json == {"text": "茉莉奶绿"}
    assert fact.origin is FactOrigin.USER_ASSERTED
    assert fact.evidence_count == 1
    assert fact.valid_from is not None
    assert fact.valid_until is None
    assert fact.display_text


# --- B. reconfirmation ------------------------------------------------------


@pytest.mark.anyio
async def test_b_reconfirmation_strengthens_without_duplicating(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    _commit(app, session_id, "还是茉莉奶绿最好喝。", minutes=240)
    reports = await _extract_all(
        app,
        _extractor(
            {
                "最好喝": [
                    _cand("茉莉奶绿", relation="same", evidence_role="reconfirmation")
                ],
                "最喜欢喝茉莉奶绿": [_cand("茉莉奶绿")],
            }
        ),
    )
    assert reports[0]["facts_created"] == 1
    assert reports[1]["facts_reinforced"] == 1

    facts = _active(app)
    assert len(facts) == 1  # never #1 #2 #3 #4
    fact = facts[0]
    assert fact.evidence_count == 2
    assert fact.confidence > 0.8  # strengthened by the second piece of evidence
    assert all(
        item.status is FactStatus.ACTIVE
        for item in app.facts.list_facts(persona_id=PERSONA)
    )


# --- C. supersession -------------------------------------------------------


@pytest.mark.anyio
async def test_c_supersession_keeps_history(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    _commit(app, session_id, "茉莉奶绿喝腻了，现在更喜欢美式。", minutes=3 * 24 * 60)
    await _extract_all(
        app,
        _extractor(
            {
                "喝腻了": [
                    _cand("美式", relation="supersedes", confidence=0.85)
                ],
                "最喜欢喝茉莉奶绿": [_cand("茉莉奶绿")],
            }
        ),
    )

    facts = app.facts.list_facts(persona_id=PERSONA)
    assert len(facts) == 2
    old = next(item for item in facts if item.value_json["text"] == "茉莉奶绿")
    new = next(item for item in facts if item.value_json["text"] == "美式")

    assert old.status is FactStatus.SUPERSEDED
    assert old.valid_until is not None
    assert old.superseded_by_fact_id == new.id
    assert old.valid_until == new.valid_from  # closed exactly when the new value began
    assert not old.is_valid_now

    assert new.status is FactStatus.ACTIVE
    assert new.supersedes_fact_id == old.id
    assert new.valid_until is None
    assert new.is_valid_now
    # The old value is history, not garbage: it is still readable.
    assert old.value_json == {"text": "茉莉奶绿"}
    assert old.evidence_count == 1

    stats = app.facts.stats(persona_id=PERSONA)
    assert stats["active"] == 1
    assert stats["superseded"] == 1


# --- C2. cross-slot supersession -------------------------------------------


@pytest.mark.anyio
async def test_c2_explicit_citation_supersedes_across_slots(app: PersonaContinuum) -> None:
    """A stated replacement ends the old value even when the slot name differs.

    Found by the Phase 4.1 real-model run: the model stored "现在基本只喝美式"
    under a NEW predicate ("当前饮品偏好") while citing the old fact with
    relation=supersedes.  Slot identity alone would keep the persona believing
    the value the user just said they were tired of.
    """

    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    _commit(app, session_id, "茉莉奶绿喝腻了，现在基本只喝美式。", minutes=3 * 24 * 60)
    episodes = _episodes(app)
    first = await app.facts.extract_episode_facts(
        episodes[0].id, extract=_extractor({"最喜欢喝茉莉奶绿": [_cand("茉莉奶绿")]})
    )
    assert first["facts_created"] == 1
    old = _active(app)[0]

    second = await app.facts.extract_episode_facts(
        episodes[1].id,
        extract=_extractor(
            {
                "只喝美式": [
                    _cand(
                        "美式",
                        predicate="当前饮品偏好",
                        relation="supersedes",
                        related_fact_id=old.id,
                    )
                ]
            }
        ),
    )
    assert second["facts_superseded"] == 1
    facts = app.facts.list_facts(persona_id=PERSONA)
    stored_old = next(item for item in facts if item.id == old.id)
    new = next(item for item in facts if item.value_json == {"text": "美式"})
    assert new.predicate == "当前饮品偏好"
    assert stored_old.status is FactStatus.SUPERSEDED
    assert stored_old.valid_until == new.valid_from
    assert stored_old.superseded_by_fact_id == new.id

    # A citation that does NOT claim a replacement changes nothing...
    session_two = _session(app)
    _commit(app, session_two, "顺便说一句，我还是挺喜欢乌龙茶。", minutes=600)
    await _extract_all(
        app,
        _extractor(
            {
                "乌龙茶": [
                    _cand(
                        "乌龙茶",
                        predicate="喜欢的饮品",
                        relation="compatible",
                        related_fact_id=new.id,
                    )
                ]
            }
        ),
    )
    assert app.facts.get_fact(new.id).status is FactStatus.ACTIVE

    # ...and a passing state still may not end a durable fact.
    _commit(app, session_two, "今天突然特别想喝美式。", minutes=1200)
    await _extract_all(
        app,
        _extractor(
            {
                "突然特别想喝": [
                    _cand(
                        "美式",
                        predicate="今天的想喝清单",
                        durability="temporary",
                        relation="supersedes",
                        related_fact_id=new.id,
                    )
                ]
            }
        ),
    )
    assert app.facts.get_fact(new.id).status is FactStatus.ACTIVE


# --- D. compatible preferences ---------------------------------------------

@pytest.mark.anyio
async def test_d_compatible_preference_does_not_invalidate_the_old_one(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝美式。", minutes=0)
    _commit(app, session_id, "我也挺喜欢乌龙茶。", minutes=240)
    await _extract_all(
        app,
        _extractor(
            {
                "乌龙": [
                    {
                        "category": "preference",
                        "subject": "用户",
                        "predicate": "喜欢的饮品",
                        "value": "乌龙茶",
                        "origin": "user_asserted",
                        "confidence": 0.7,
                        "exclusive": False,
                        "relation": "compatible",
                    }
                ],
                "美式": [_cand("美式")],
            }
        ),
    )
    active = {fact.value_json["text"] for fact in _active(app)}
    assert active == {"美式", "乌龙茶"}  # two coexisting preferences
    assert all(fact.valid_until is None for fact in _active(app))
    assert app.facts.stats(persona_id=PERSONA)["superseded"] == 0


# --- E. persona inference --------------------------------------------------


@pytest.mark.anyio
async def test_e_persona_inference_is_not_stored_as_a_user_fact(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我跟小陈最近有点别扭。", minutes=0)
    reports = await _extract_all(
        app,
        _extractor(
            {
                "小陈": [
                    {
                        "category": "relation",
                        "subject": "用户",
                        "predicate": "对小陈的态度",
                        "value": "舍不得",
                        "origin": "inferred",
                        "confidence": 0.3,
                    }
                ]
            }
        ),
    )
    assert reports[0]["skipped_inferred"] == 1
    assert reports[0]["facts_created"] == 0
    # Nothing was written at all: the persona's guess is not the user's history.
    assert app.facts.list_facts(persona_id=PERSONA) == []
    assert reports[0]["error"] is None  # skipped, not failed


@pytest.mark.anyio
async def test_e2_inferred_can_be_kept_only_when_explicitly_enabled(tmp_path) -> None:
    config = Config(data_dir=tmp_path / "pc-inferred", memory_fact_persist_inferred=True)
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
    session_id = _session(app)
    _commit(app, session_id, "我跟小陈最近有点别扭。")
    await _extract_all(
        app,
        _extractor(
            {
                "小陈": [
                    {
                        "category": "relation",
                        "subject": "用户",
                        "predicate": "对小陈的态度",
                        "value": "舍不得",
                        "origin": "inferred",
                        "confidence": 0.3,
                    }
                ]
            }
        ),
    )
    facts = app.facts.list_facts(persona_id=PERSONA)
    assert len(facts) == 1
    # Even when kept, it is labelled as inference -- never as a user statement.
    assert facts[0].origin is FactOrigin.INFERRED
    assert facts[0].confidence <= 0.3
    app.close()


# --- F. temporary state ----------------------------------------------------


@pytest.mark.anyio
async def test_f_temporary_state_gets_bounded_validity(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我今天特别累。", minutes=0)
    await _extract_all(
        app,
        _extractor(
            {
                "特别累": [
                    {
                        "category": "other",
                        "subject": "用户",
                        "predicate": "当前状态",
                        "value": "特别累",
                        "origin": "user_asserted",
                        "durability": "temporary",
                        "confidence": 0.4,
                    }
                ]
            }
        ),
    )
    facts = app.facts.list_facts(persona_id=PERSONA)
    if facts:  # either not stored, or stored with an explicit expiry
        fact = facts[0]
        assert fact.durability is FactDurability.TEMPORARY
        assert fact.valid_until is not None
        assert fact.valid_until - (fact.valid_from or BASE) <= timedelta(hours=24)
    # And a temporary state never invalidates a durable fact in the same slot.
    session_id_two = _session(app)
    _commit(app, session_id_two, "我的日常饮品偏好是美式。", minutes=600)
    _commit(app, session_id_two, "我现在有点想喝茉莉奶绿。", minutes=610)
    await _extract_all(
        app,
        _extractor(
            {
                "有点想喝": [
                    {
                        "category": "preference",
                        "subject": "用户",
                        "predicate": DRINK_SLOT,
                        "value": "茉莉奶绿",
                        "origin": "user_asserted",
                        "durability": "temporary",
                        "relation": "supersedes",
                        "exclusive": True,
                    }
                ],
                "日常饮品偏好": [_cand("美式")],
            }
        ),
    )
    active = _active(app)
    assert any(fact.value_json["text"] == "美式" for fact in active)


# --- G. plan ---------------------------------------------------------------


@pytest.mark.anyio
async def test_g_plan_keeps_its_lifecycle_and_relative_expression(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我下个月去重庆。", minutes=0)
    await _extract_all(
        app,
        _extractor(
            {
                "去重庆": [
                    {
                        "category": "plan",
                        "subject": "用户",
                        "predicate": "下个月的计划",
                        "value": "去重庆",
                        "origin": "user_asserted",
                        "confidence": 0.7,
                        "plan_status": "planned",
                        "temporal_expression": "下个月",
                        "temporal_normalized": "2026-10",
                        "temporal_confidence": 0.6,
                    }
                ]
            }
        ),
    )
    fact = _active(app)[0]
    assert fact.category.value == "plan"
    assert fact.plan_status.value == "planned"
    assert fact.temporal_expression == "下个月"  # original wording preserved
    assert fact.temporal_normalized == "2026-10"
    assert fact.temporal_confidence == pytest.approx(0.6)


@pytest.mark.anyio
async def test_g2_plan_completion_is_a_state_change_not_a_new_value(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我下个月去重庆。", minutes=0)
    _commit(app, session_id, "重庆的票我已经买了。", minutes=200)
    await _extract_all(
        app,
        _extractor(
            {
                "票我已经买了": [
                    {
                        "category": "plan",
                        "subject": "用户",
                        "predicate": "下个月的计划",
                        "value": "去重庆",
                        "origin": "user_asserted",
                        "confidence": 0.8,
                        "relation": "same",
                        "plan_status": "active",
                    }
                ],
                "去重庆": [
                    {
                        "category": "plan",
                        "subject": "用户",
                        "predicate": "下个月的计划",
                        "value": "去重庆",
                        "origin": "user_asserted",
                        "confidence": 0.7,
                        "plan_status": "planned",
                    }
                ],
            }
        ),
    )
    facts = app.facts.list_facts(persona_id=PERSONA)
    assert len(facts) == 1  # same slot, same value: one fact
    assert facts[0].plan_status.value == "active"
    assert facts[0].evidence_count == 2


# --- H. restart ------------------------------------------------------------


@pytest.mark.anyio
async def test_h_pending_extraction_survives_a_restart(tmp_path) -> None:
    config = Config(data_dir=tmp_path / "pc-fact-restart")
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
    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    pending_before = app.facts.pending_extraction_episodes()
    assert len(pending_before) == 1
    app.close()

    reopened = PersonaContinuum(config, include_fake_agent=True)
    reopened.init()
    recovery = reopened.facts.recover_pending()
    assert recovery["pending"] == 1

    extract = _extractor({"最喜欢喝茉莉奶绿": [_cand("茉莉奶绿")]})
    await reopened.facts.extract_pending(extract=extract)
    assert len(_active(reopened)) == 1
    assert reopened.facts.pending_extraction_episodes() == []

    # A second pass changes nothing: the status column is the idempotency guard.
    again = await reopened.facts.extract_pending(extract=extract)
    assert again["attempted"] == 0
    assert len(reopened.facts.list_facts(persona_id=PERSONA)) == 1
    reopened.close()


# --- I. failure ------------------------------------------------------------


@pytest.mark.anyio
async def test_i_failure_leaves_no_partial_data_and_chat_continues(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    episode = _episodes(app)[0]

    async def timeout(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        raise TimeoutError("provider timed out")

    report = await app.facts.extract_episode_facts(episode.id, extract=timeout)
    assert report["error"] == "extract_failed:TimeoutError"
    assert report["pending"] is True
    assert app.facts.list_facts(persona_id=PERSONA) == []
    assert app.facts.fact_sources("nope") == []
    assert app.facts.stats(persona_id=PERSONA)["facts"] == 0
    # The Episode and its raw turns are untouched.
    assert app.episodes.get_episode(episode.id) is not None
    assert len(app.episodes.episode_turns(episode.id)) == 1
    # Chat keeps working and the work item is still on the list.
    _commit(app, session_id, "随便说点别的", minutes=10)
    assert len(app.facts.pending_extraction_episodes()) >= 1

    # Invalid JSON is a recorded failure, not a write.
    async def garbage(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return "这不是一个结构化结果"

    invalid = await app.facts.extract_episode_facts(episode.id, extract=garbage)
    assert invalid["error"] == "invalid_fact_payload"
    assert app.facts.list_facts(persona_id=PERSONA) == []

    # Retry succeeds.
    retry = await app.facts.extract_episode_facts(
        episode.id, extract=_extractor({"最喜欢喝茉莉奶绿": [_cand("茉莉奶绿")]}), force=True
    )
    assert retry["facts_created"] == 1
    assert len(_active(app)) == 1


# --- J. scope isolation ---------------------------------------------------


@pytest.mark.anyio
async def test_j_scope_isolation(app: PersonaContinuum) -> None:
    session_a = _session(app, counterpart="user_a")
    session_b = _session(app, counterpart="user_b")
    _commit(app, session_a, "我最喜欢喝茉莉奶绿。", counterpart="user_a")
    _commit(app, session_b, "我最喜欢喝美式。", counterpart="user_b")
    extract = _extractor(
        {"美式": [_cand("美式")], "最喜欢喝茉莉奶绿": [_cand("茉莉奶绿")]}
    )
    await _extract_all(app, extract, counterpart="user_a")
    await _extract_all(app, extract, counterpart="user_b")

    a_values = {fact.value_json["text"] for fact in _active(app, counterpart="user_a")}
    b_values = {fact.value_json["text"] for fact in _active(app, counterpart="user_b")}
    assert a_values == {"茉莉奶绿"}
    assert b_values == {"美式"}

    # A different branch is a different scope even for the same counterpart.
    branch_episode_id = app.episodes.assign_turn(
        persona_id=PERSONA,
        session_id=session_a,
        turn_id="turn_alt_branch",
        counterpart_id="user_a",
        branch_id="alt",
        room_id=None,
        occurred_at=BASE,
        user_message="另一个分支里的对话",
        persona_response="回应",
    )["current_episode_id"]
    branch_episode = app.episodes.get_episode(branch_episode_id)
    assert branch_episode is not None
    app.facts.apply_candidates(
        branch_episode,
        app.facts.parse_extraction(
            {"facts": [_cand("乌龙茶", predicate="常喝的茶", relation="unrelated")]}
        ),
    )
    main_branch = app.facts.list_facts(
        persona_id=PERSONA, counterpart_id="user_a", branch_id="main", status="active"
    )
    alt_branch = app.facts.list_facts(
        persona_id=PERSONA, counterpart_id="user_a", branch_id="alt", status="active"
    )
    assert {fact.value_json["text"] for fact in main_branch} == {"茉莉奶绿"}
    assert {fact.value_json["text"] for fact in alt_branch} == {"乌龙茶"}
    assert len(_active(app, counterpart="user_b")) == 1  # untouched


# --- K. provenance --------------------------------------------------------


@pytest.mark.anyio
async def test_k_provenance_walks_back_to_the_raw_text(app: PersonaContinuum) -> None:
    session_id = _session(app)
    turn_id = _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    episode = _episodes(app)[0]
    await _extract_all(
        app,
        _extractor({"最喜欢喝茉莉奶绿": [_cand("茉莉奶绿", evidence_turn_id=turn_id)]}),
    )
    fact = _active(app)[0]
    payload = app.facts.inspect_fact(fact.id)
    assert payload is not None
    assert payload["source_episode_ids"] == [episode.id]
    assert payload["source_turn_ids"] == [turn_id]
    source = payload["sources"][0]
    assert source["episode_available"] is True
    assert source["resolvable"] is True

    # Fact -> Episode -> source turn -> original text.
    episode_turns = app.episodes.episode_turns(episode.id)
    assert [item.turn_id for item in episode_turns] == [turn_id]
    assert "我最喜欢喝茉莉奶绿。" in app.episodes.resolve_turn_text(episode_turns[0])

    # A turn the extractor invented is not accepted as evidence.
    fabricated = app.facts.apply_candidates(
        episode,
        app.facts.parse_extraction(
            {
                "facts": [
                    _cand(
                        "伪造饮品",
                        predicate="另一些偏好",
                        evidence_turn_id="turn_does_not_exist",
                    )
                ]
            }
        ),
    )
    assert fabricated["facts_created"] == 1
    invented = next(
        item for item in _active(app) if item.value_json["text"] == "伪造饮品"
    )
    invented_sources = app.facts.fact_sources(invented.id)
    assert invented_sources[0]["turn_id"] == ""  # fell back to episode-level


# --- idempotency ----------------------------------------------------------


@pytest.mark.anyio
async def test_replay_never_duplicates_or_flip_flops(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    _commit(app, session_id, "茉莉奶绿喝腻了，现在更喜欢美式。", minutes=3 * 24 * 60)
    extract = _extractor(
        {
            "喝腻了": [_cand("美式", relation="supersedes", confidence=0.85)],
            "最喜欢喝茉莉奶绿": [_cand("茉莉奶绿")],
        }
    )
    await _extract_all(app, extract)
    before = {
        (fact.value_json["text"], fact.status.value)
        for fact in app.facts.list_facts(persona_id=PERSONA)
    }
    assert before == {("茉莉奶绿", "superseded"), ("美式", "active")}

    # Re-running both Episodes must not chain A -> B -> B2 nor flip back.
    for episode in _episodes(app):
        await app.facts.extract_episode_facts(episode.id, extract=extract, force=True)
    after = {
        (fact.value_json["text"], fact.status.value)
        for fact in app.facts.list_facts(persona_id=PERSONA)
    }
    assert after == before
    facts = app.facts.list_facts(persona_id=PERSONA)
    superseded = next(item for item in facts if item.status is FactStatus.SUPERSEDED)
    active = next(item for item in facts if item.status is FactStatus.ACTIVE)
    assert superseded.superseded_by_fact_id == active.id
    assert active.supersedes_fact_id == superseded.id
    assert not any(
        item.supersedes_fact_id == active.id for item in facts
    )  # no chain beyond one hop


# --- prompt isolation -----------------------------------------------------


def test_facts_are_not_injected_into_the_prompt(app: PersonaContinuum) -> None:
    """Phase 4 stores; it does not change 苏禾's production replies."""

    assert app.config.memory_fact_extraction_enabled is True
    # The store is independent of any Context Policy budget.
    assert app.facts.confidence_step >= 0.0
    assert not hasattr(app.facts, "prompt_target_tokens")
    assert app.facts.count_active(persona_id=PERSONA) == 0


# --- real-ish conversation -------------------------------------------------


@pytest.mark.anyio
async def test_three_episode_preference_history(app: PersonaContinuum) -> None:
    """Episode A states it, B confirms it, C changes it -- history survives."""

    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    _commit(app, session_id, "还是茉莉奶绿最好喝。", minutes=240)
    _commit(app, session_id, "茉莉奶绿喝腻了，现在更喜欢美式。", minutes=3 * 24 * 60)
    episodes = _episodes(app)
    # Three separate sittings: state it, confirm it, change it.
    assert len(episodes) == 3

    extract = _extractor(
        {
            "喝腻了": [_cand("美式", relation="supersedes", confidence=0.85)],
            "最好喝": [_cand("茉莉奶绿", relation="same")],
            "最喜欢喝茉莉奶绿": [_cand("茉莉奶绿")],
        }
    )
    for episode in episodes:
        await app.facts.extract_episode_facts(episode.id, extract=extract)

    facts = app.facts.list_facts(persona_id=PERSONA)
    assert len(facts) == 2
    old = next(item for item in facts if item.value_json["text"] == "茉莉奶绿")
    new = next(item for item in facts if item.value_json["text"] == "美式")
    assert old.status is FactStatus.SUPERSEDED
    assert old.valid_until is not None
    assert old.evidence_count == 2  # stated AND reconfirmed
    assert new.status is FactStatus.ACTIVE
    assert new.evidence_count == 1
    assert new.valid_from == old.valid_until

    active_now = [fact.value_json["text"] for fact in _active(app)]
    assert active_now == ["美式"]

    coverage = app.episodes.coverage(persona_id=PERSONA)
    assert coverage["orphaned"] == 0
    assert coverage["unassigned_pending_backfill"] == 0
