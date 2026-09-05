from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.domain.memory import MemoryRecord, MemoryType
from persona_continuum.web.server import create_web_app


def _persona(app: PersonaContinuum, persona_id: str = "reset_hero") -> str:
    app.personas.create_from_manifest(
        {
            "id": persona_id,
            "display_name": persona_id,
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    return persona_id


def _dirty_runtime(app: PersonaContinuum, persona_id: str) -> None:
    session = app.sessions.start_session(persona_id=persona_id, title="reset probe")
    app.sessions.commit_turn(
        persona_id=persona_id,
        session_id=session.id,
        user_message="你这个骗子，我讨厌你，闭嘴！",
        persona_response="哼，你才是骗子！",
    )
    app.memories.add_memory(
        MemoryRecord(
            id=f"mem_{persona_id}_keep",
            persona_id=persona_id,
            type=MemoryType.EPISODIC,
            source_kind="seed",
            content="A seed memory that soft reset must keep.",
        )
    )


def _counts(app: PersonaContinuum, persona_id: str) -> dict[str, int]:
    conn = app.database.conn
    out = {}
    for table, extra in (
        ("affect_states", "AND branch_id = 'main'"),
        ("needs", "AND branch_id = 'main'"),
        ("relationships", "AND branch_id = 'main'"),
        ("change_events", "AND branch_id = 'main'"),
        ("sessions", ""),
        ("memories", ""),
    ):
        row = conn.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE persona_id = ? {extra}",
            (persona_id,),
        ).fetchone()
        out[table] = int(row["count"])
    return out


def test_soft_reset_restores_initial_state_but_keeps_history(app: PersonaContinuum) -> None:
    persona_id = _persona(app)
    _dirty_runtime(app, persona_id)
    before = _counts(app, persona_id)
    assert before["affect_states"] > 0
    assert before["change_events"] > 0

    result = app.sessions.reset_runtime_state(persona_id)
    assert result["persona_id"] == persona_id
    assert result["branch_id"] == "main"

    state = app.runtime_state(persona_id)
    for emotion in state["emotions"]:
        assert emotion["intensity"] == emotion["baseline"]
    for need in state["needs"]:
        assert need["level"] == need["baseline"]
    assert state["relationships"] == []

    # Reset deleted the dirty rows, so the visible values must all sit at
    # baseline (no elevated anger survives).  Note get_emotions() re-seeds
    # baseline affect rows eagerly on read, while get_needs() does not
    # persist untouched defaults -- so only assert persisted-row values
    # where rows exist, and full value equality on the returned state.
    after = _counts(app, persona_id)
    rows = app.database.conn.execute(
        "SELECT name, intensity, baseline FROM affect_states "
        "WHERE persona_id = ? AND branch_id = 'main'",
        (persona_id,),
    ).fetchall()
    assert rows
    assert all(float(row["intensity"]) == float(row["baseline"]) for row in rows)
    rows = app.database.conn.execute(
        "SELECT name, level, baseline FROM needs "
        "WHERE persona_id = ? AND branch_id = 'main'",
        (persona_id,),
    ).fetchall()
    assert all(float(row["level"]) == float(row["baseline"]) for row in rows)
    assert after["relationships"] == 0
    assert after["change_events"] == 0
    # History stays: sessions and memories are untouched by a soft reset.
    assert after["sessions"] == before["sessions"] > 0
    assert after["memories"] == before["memories"] > 0


def test_full_reset_clears_conversational_footprint(app: PersonaContinuum) -> None:
    persona_id = _persona(app)
    _dirty_runtime(app, persona_id)

    result = app.sessions.reset_runtime_state(persona_id, include_memories=True)
    assert result["removed"]["sessions"] > 0

    after = _counts(app, persona_id)
    assert after["affect_states"] == 0
    assert after["change_events"] == 0
    assert after["sessions"] == 0
    # Derived experience memories go; the seed memory stays.
    remaining = [
        str(row["id"])
        for row in app.database.conn.execute(
            "SELECT id FROM memories WHERE persona_id = ?", (persona_id,)
        ).fetchall()
    ]
    assert remaining == [f"mem_{persona_id}_keep"]

    # Compiled persona content is never touched by either reset mode.
    assert app.personas.get(persona_id).manifest.display_name == persona_id


def test_reset_runtime_api(app: PersonaContinuum) -> None:
    persona_id = _persona(app)
    _dirty_runtime(app, persona_id)
    with TestClient(create_web_app(app)) as client:
        soft = client.post(f"/api/personas/{persona_id}/runtime/reset", json={})
        assert soft.status_code == 200, soft.text
        assert soft.json()["data"]["removed"]["change_events"] > 0

        runtime = client.get(f"/api/personas/{persona_id}/runtime")
        assert runtime.status_code == 200
        assert runtime.json()["data"]["relationships"] == []

        full = client.post(
            f"/api/personas/{persona_id}/runtime/reset",
            json={"include_memories": True},
        )
        assert full.status_code == 200, full.text

        missing = client.post("/api/personas/ghost/runtime/reset", json={})
        assert missing.status_code == 404


@pytest.mark.anyio
async def test_research_focus_flows_into_child_job(app: PersonaContinuum) -> None:
    from persona_continuum.domain.persona import PersonaType
    from persona_continuum.domain.profile import EnrichmentInputMode, ProfileEnrichmentJob

    persona = app.personas.create(
        display_name="Focus Hero",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode="counterfactual_continuation",
    )
    profile = app.profile_library.sync_persona(persona)
    job = app.profile_library.save_enrichment_job(
        ProfileEnrichmentJob(
            id="focus_job",
            target_profile_id=profile.id,
            target_profile_type=profile.profile_type,
            enrichment_input_mode=EnrichmentInputMode.WEB_RESEARCH,
            research_focus="着重她的晚年作品与获奖争议",
        )
    )
    assert job.research_focus == "着重她的晚年作品与获奖争议"
    reloaded = app.profile_library.get_enrichment_job(job.id)
    assert reloaded.research_focus == "着重她的晚年作品与获奖争议"

    # Empty focus stays None: the model plans the direction itself.
    unfocused = app.profile_library.save_enrichment_job(
        ProfileEnrichmentJob(
            id="unfocused_job",
            target_profile_id=profile.id,
            target_profile_type=profile.profile_type,
            enrichment_input_mode=EnrichmentInputMode.LOCAL_MATERIALS,
        )
    )
    assert unfocused.research_focus is None
