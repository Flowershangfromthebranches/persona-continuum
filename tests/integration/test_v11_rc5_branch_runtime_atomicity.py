from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.security.validation import PersonaContinuumError


def _create_persona(app: PersonaContinuum, persona_id: str = "rc5-persona") -> Any:
    return app.personas.create(
        display_name="RC5 Alex",
        aliases=["RC5"],
        persona_type=PersonaType.PUBLIC_LIVING_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
        birth_date="1981-02-03",
        data_cutoff_date="2026-06-26",
        sensitivity="high",
        persona_id=persona_id,
    )


def _branches(app: PersonaContinuum, persona_id: str) -> tuple[Any, Any]:
    continuation = app.continuations.create(persona_id, "rc5 branch split")
    return (
        app.continuations.create_branch(continuation.id),
        app.continuations.create_branch(continuation.id),
    )


def _commit_turn(
    app: PersonaContinuum,
    persona_id: str,
    branch_id: str,
    *,
    session_title: str,
    user_message: str = "hello",
    response: str = "I will keep this local to the branch.",
    counterpart_id: str = "Alice",
    state_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session = app.sessions.start_session(
        persona_id,
        session_title,
        branch_id=branch_id,
        counterpart_id=counterpart_id,
    )
    app.sessions.prepare_turn(
        persona_id,
        session.id,
        user_message,
        branch_id=branch_id,
        counterpart_id=counterpart_id,
    )
    result = app.sessions.commit_turn(
        persona_id,
        session.id,
        user_message=user_message,
        persona_response=response,
        counterpart_id=counterpart_id,
        state_patch=state_patch,
    )
    return {"session": session, **result}


def _reflection_artifact(turn_ids: list[str], *, tag: str = "RC5") -> dict[str, Any]:
    return {
        "reflection_artifact_id": f"refl_{tag}",
        "new_insights": [{"content": f"{tag} branch insight", "importance": 0.74}],
        "relationship_deltas": [
            {
                "counterpart_id": "Alice",
                "changes": {"trust": 0.8},
                "reason": f"{tag} relationship evidence",
            }
        ],
        "affect_deltas": {"anger": 0.9},
        "need_deltas": {"safety": 0.25},
        "goal_updates": [{"goal_id": f"{tag}_goal", "status": "active", "content": "repair"}],
        "unresolved_conflicts": [
            {"id": f"{tag}_conflict", "content": "unresolved branch conflict"}
        ],
        "self_narrative_updates": [f"{tag} self narrative update"],
        "memory_candidates": [{"content": f"{tag} reflection memory", "importance": 0.7}],
        "confidence": 0.82,
        "supporting_turn_ids": turn_ids,
    }


def _runtime_branch_file(app: PersonaContinuum, persona_id: str, branch_id: str) -> Path:
    return (
        Path(app.personas.get(persona_id).package_path)
        / "runtime"
        / "branches"
        / branch_id
        / "runtime_state.json"
    )


def test_branch_affect_need_and_relationship_are_isolated(app: PersonaContinuum) -> None:
    persona = _create_persona(app, "rc5-branch-runtime")
    branch_a, branch_b = _branches(app, persona.id)
    _commit_turn(
        app,
        persona.id,
        branch_a.id,
        session_title="branch A",
        state_patch={
            "affect": {"anger": 0.9},
            "needs": {"safety": 0.3},
            "relationships": [{"counterpart_id": "Alice", "changes": {"trust": 0.8}}],
        },
    )

    prepared_b = app.sessions.prepare_turn(
        persona.id,
        app.sessions.start_session(
            persona.id, "branch B", branch_id=branch_b.id, counterpart_id="Alice"
        ).id,
        "what does B feel?",
        branch_id=branch_b.id,
        counterpart_id="Alice",
    )

    anger_b = next(state for state in prepared_b.current_emotions if state.name == "anger")
    safety_b = next(state for state in prepared_b.current_needs if state.name == "safety")
    assert anger_b.intensity < 0.2
    assert safety_b.level == pytest.approx(0.5)
    assert prepared_b.relationship_state.trust == pytest.approx(0)


def test_branch_runtime_goals_narrative_and_conflicts_are_isolated(
    app: PersonaContinuum,
) -> None:
    persona = _create_persona(app, "rc5-branch-reflection-runtime")
    branch_a, branch_b = _branches(app, persona.id)
    turn = _commit_turn(app, persona.id, branch_a.id, session_title="A reflect")
    app.sessions.commit_reflection(
        persona.id,
        _reflection_artifact([turn["turn_id"]], tag="RC5_A"),
        branch_id=branch_a.id,
    )

    session_b = app.sessions.start_session(
        persona.id, "B runtime", branch_id=branch_b.id, counterpart_id="Alice"
    )
    prepared_b = app.sessions.prepare_turn(
        persona.id,
        session_b.id,
        "show runtime",
        branch_id=branch_b.id,
        counterpart_id="Alice",
    )
    runtime_b = dict(prepared_b.compiled_persona_context.get("runtime", {}))
    payload_b = json.dumps(runtime_b, ensure_ascii=False)

    assert not any(goal.get("goal_id") == "RC5_A_goal" for goal in prepared_b.active_goals)
    assert "RC5_A self narrative update" not in payload_b
    assert "unresolved branch conflict" not in payload_b
    assert _runtime_branch_file(app, persona.id, branch_a.id).exists()
    assert _runtime_branch_file(app, persona.id, branch_b.id).exists()


def test_prepare_reflection_filters_to_requested_branch(app: PersonaContinuum) -> None:
    persona = _create_persona(app, "rc5-prepare-reflection")
    branch_a, branch_b = _branches(app, persona.id)
    turn_a = _commit_turn(app, persona.id, branch_a.id, session_title="A", user_message="A_ONLY")
    _commit_turn(app, persona.id, branch_b.id, session_title="B", user_message="B_ONLY")

    prepared = app.sessions.prepare_reflection(
        persona.id,
        branch_id=branch_a.id,
        session_ids=[turn_a["session"].id],
        limit=10,
    )
    transcript = json.dumps(prepared["recent_important_dialogue"], ensure_ascii=False)

    assert "A_ONLY" in transcript
    assert "B_ONLY" not in transcript
    assert prepared["branch_id"] == branch_a.id


def test_commit_reflection_rejects_mixed_branch_supporting_turns(
    app: PersonaContinuum,
) -> None:
    persona = _create_persona(app, "rc5-mixed-reflection")
    branch_a, branch_b = _branches(app, persona.id)
    turn_a = _commit_turn(app, persona.id, branch_a.id, session_title="A")
    turn_b = _commit_turn(app, persona.id, branch_b.id, session_title="B")

    with pytest.raises(PersonaContinuumError) as exc:
        app.sessions.commit_reflection(
            persona.id,
            _reflection_artifact([turn_a["turn_id"], turn_b["turn_id"]], tag="RC5_MIXED"),
            branch_id=branch_a.id,
        )
    assert exc.value.code == "invalid_reflection"
    assert app.memories.search_memories(persona.id, "RC5_MIXED", branch_id=branch_a.id) == []


def test_reflection_memory_and_deltas_are_written_to_current_branch(
    app: PersonaContinuum,
) -> None:
    persona = _create_persona(app, "rc5-reflection-branch-write")
    branch_a, branch_b = _branches(app, persona.id)
    turn = _commit_turn(app, persona.id, branch_a.id, session_title="A")
    result = app.sessions.commit_reflection(
        persona.id,
        _reflection_artifact([turn["turn_id"]], tag="RC5_BRANCH_WRITE"),
        branch_id=branch_a.id,
    )

    memory_rows = app.database.conn.execute(
        "SELECT branch_id FROM memories WHERE id IN ({})".format(
            ",".join("?" for _ in result["memory_ids"])
        ),
        tuple(result["memory_ids"]),
    ).fetchall()
    assert {str(row["branch_id"]) for row in memory_rows} == {branch_a.id}
    assert app.relationships.get_relationship(persona.id, "Alice", branch_id=branch_a.id).trust
    assert app.relationships.get_relationship(persona.id, "Alice", branch_id=branch_b.id).trust == 0
    assert app.memories.search_memories(
        persona.id, "RC5_BRANCH_WRITE", branch_id=branch_b.id
    ) == []


def test_invalid_commit_turn_rolls_back_all_writes(app: PersonaContinuum) -> None:
    persona = _create_persona(app, "rc5-commit-turn-atomic")
    branch_a, _ = _branches(app, persona.id)
    session = app.sessions.start_session(
        persona.id, "atomic", branch_id=branch_a.id, counterpart_id="Alice"
    )
    before = {
        table: app.database.conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
            "count"
        ]
        for table in ["session_turns", "memories", "change_events"]
    }

    with pytest.raises(PersonaContinuumError) as exc:
        app.sessions.commit_turn(
            persona.id,
            session.id,
            user_message="bad patch",
            persona_response="must rollback",
            counterpart_id="Alice",
            state_patch={"affect": {"anger": "not-a-number"}},
        )

    assert exc.value.code == "invalid_state_patch"
    after = {
        table: app.database.conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
            "count"
        ]
        for table in ["session_turns", "memories", "change_events"]
    }
    assert after == before
    relationship = app.relationships.get_relationship(
        persona.id, "Alice", branch_id=branch_a.id
    )
    assert relationship.familiarity == 0


def test_invalid_commit_reflection_rolls_back_all_writes(app: PersonaContinuum) -> None:
    persona = _create_persona(app, "rc5-reflection-atomic")
    branch_a, _ = _branches(app, persona.id)
    turn = _commit_turn(app, persona.id, branch_a.id, session_title="A")
    before_memories = app.database.conn.execute(
        "SELECT COUNT(*) AS count FROM memories WHERE source_kind = 'reflection_summary'"
    ).fetchone()["count"]
    artifact = _reflection_artifact([turn["turn_id"]], tag="RC5_BAD_REFLECTION")
    artifact["relationship_deltas"] = [
        {"counterpart_id": "Alice", "changes": {"trust": "not-a-number"}, "reason": "bad"}
    ]

    with pytest.raises(PersonaContinuumError) as exc:
        app.sessions.commit_reflection(persona.id, artifact, branch_id=branch_a.id)

    assert exc.value.code == "invalid_reflection"
    after_memories = app.database.conn.execute(
        "SELECT COUNT(*) AS count FROM memories WHERE source_kind = 'reflection_summary'"
    ).fetchone()["count"]
    assert after_memories == before_memories
    assert app.relationships.get_relationship(persona.id, "Alice", branch_id=branch_a.id).trust == 0


def test_multisession_reflection_records_all_support_edges(app: PersonaContinuum) -> None:
    persona = _create_persona(app, "rc5-reflection-supports")
    branch_a, _ = _branches(app, persona.id)
    turn_one = _commit_turn(app, persona.id, branch_a.id, session_title="one")
    turn_two = _commit_turn(app, persona.id, branch_a.id, session_title="two")
    result = app.sessions.commit_reflection(
        persona.id,
        _reflection_artifact([turn_one["turn_id"], turn_two["turn_id"]], tag="RC5_MULTI"),
        branch_id=branch_a.id,
    )

    event_rows = app.database.conn.execute(
        """
        SELECT id FROM change_events
        WHERE persona_id = ? AND json_extract(data_json, '$.reflection_artifact_id') = ?
        """,
        (persona.id, "refl_RC5_MULTI"),
    ).fetchall()
    assert event_rows
    for event in event_rows:
        supports = app.database.conn.execute(
            """
            SELECT session_id, turn_id FROM change_event_supports
            WHERE event_id = ?
            ORDER BY turn_id
            """,
            (event["id"],),
        ).fetchall()
        assert {(row["session_id"], row["turn_id"]) for row in supports} == {
            (turn_one["session"].id, turn_one["turn_id"]),
            (turn_two["session"].id, turn_two["turn_id"]),
        }
    for memory_id in result["memory_ids"]:
        metadata = json.loads(
            app.database.conn.execute(
                "SELECT metadata_json FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()["metadata_json"]
        )
        assert set(metadata["supporting_session_ids"]) == {
            turn_one["session"].id,
            turn_two["session"].id,
        }


def test_deleting_one_support_session_keeps_multisession_reflection_until_last_support(
    app: PersonaContinuum,
) -> None:
    persona = _create_persona(app, "rc5-delete-support")
    branch_a, _ = _branches(app, persona.id)
    turn_one = _commit_turn(app, persona.id, branch_a.id, session_title="one")
    turn_two = _commit_turn(app, persona.id, branch_a.id, session_title="two")
    result = app.sessions.commit_reflection(
        persona.id,
        _reflection_artifact([turn_one["turn_id"], turn_two["turn_id"]], tag="RC5_DELETE_SUPPORT"),
        branch_id=branch_a.id,
    )
    assert (
        app.relationships.get_relationship(persona.id, "Alice", branch_id=branch_a.id).trust
        == 0.8
    )

    app.sessions.delete_session(persona.id, turn_one["session"].id, delete_derived_memories=True)

    remaining = app.database.conn.execute(
        "SELECT id, metadata_json FROM memories WHERE id IN ({})".format(
            ",".join("?" for _ in result["memory_ids"])
        ),
        tuple(result["memory_ids"]),
    ).fetchall()
    assert len(remaining) == len(result["memory_ids"])
    assert (
        app.relationships.get_relationship(persona.id, "Alice", branch_id=branch_a.id).trust
        == 0.8
    )
    for row in remaining:
        metadata = json.loads(row["metadata_json"])
        assert metadata["supporting_session_ids"] == [turn_two["session"].id]

    app.sessions.delete_session(persona.id, turn_two["session"].id, delete_derived_memories=True)

    assert (
        app.database.conn.execute(
            "SELECT COUNT(*) AS count FROM memories WHERE id IN ({})".format(
                ",".join("?" for _ in result["memory_ids"])
            ),
            tuple(result["memory_ids"]),
        ).fetchone()["count"]
        == 0
    )
    assert app.relationships.get_relationship(persona.id, "Alice", branch_id=branch_a.id).trust == 0


def test_migration_adds_branch_columns_and_moves_legacy_rows_to_main(tmp_path: Path) -> None:
    import sqlite3

    from persona_continuum.config import Config

    data_dir = tmp_path / "legacy"
    data_dir.mkdir()
    db_path = data_dir / "persona_continuum.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE personas (
          id TEXT PRIMARY KEY,
          manifest_json TEXT NOT NULL,
          package_path TEXT NOT NULL,
          archived INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE affect_states (
          persona_id TEXT NOT NULL,
          name TEXT NOT NULL,
          kind TEXT NOT NULL,
          intensity REAL NOT NULL,
          baseline REAL NOT NULL,
          decay_rate REAL NOT NULL,
          updated_at TEXT NOT NULL,
          triggers_json TEXT NOT NULL,
          confidence REAL NOT NULL,
          PRIMARY KEY(persona_id, name, kind)
        );
        CREATE TABLE needs (
          persona_id TEXT NOT NULL,
          name TEXT NOT NULL,
          level REAL NOT NULL,
          baseline REAL NOT NULL,
          updated_at TEXT NOT NULL,
          confidence REAL NOT NULL,
          reasons_json TEXT NOT NULL,
          PRIMARY KEY(persona_id, name)
        );
        CREATE TABLE relationships (
          persona_id TEXT NOT NULL,
          counterpart TEXT NOT NULL,
          state_json TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(persona_id, counterpart)
        );
        CREATE TABLE change_events (
          id TEXT PRIMARY KEY,
          persona_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          target_type TEXT NOT NULL,
          target_id TEXT NOT NULL,
          session_id TEXT,
          turn_id TEXT,
          data_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO personas VALUES (?, ?, ?, ?, ?, ?)",
        (
            "legacy",
            "{}",
            str(data_dir / "personas" / "legacy"),
            0,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO affect_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy", "anger", "emotion", 0.4, 0, 0.08, "2026-01-01T00:00:00+00:00", "[]", 0.5),
    )
    conn.execute(
        "INSERT INTO needs VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy", "safety", 0.7, 0.5, "2026-01-01T00:00:00+00:00", 0.5, "[]"),
    )
    conn.execute(
        "INSERT INTO relationships VALUES (?, ?, ?, ?)",
        (
            "legacy",
            "Alice",
            json.dumps({"persona_id": "legacy", "counterpart": "Alice", "trust": 0.6}),
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO change_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "evt_legacy",
            "legacy",
            "affect_delta",
            "affect",
            "current",
            "sess",
            "turn",
            "{}",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    migrated = PersonaContinuum(Config(data_dir=data_dir))
    migrated.init()
    try:
        for table in ["affect_states", "needs", "relationships", "change_events"]:
            columns = {
                row["name"]
                for row in migrated.database.conn.execute(f"PRAGMA table_info({table})")
            }
            assert "branch_id" in columns
            rows = migrated.database.conn.execute(f"SELECT branch_id FROM {table}").fetchall()
            assert rows and {row["branch_id"] for row in rows} == {"main"}
    finally:
        migrated.close()
