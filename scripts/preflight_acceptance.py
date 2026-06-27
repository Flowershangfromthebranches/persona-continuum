from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.persona import PersonaType, RunMode

DIMENSIONS = [
    "identity_and_timeline",
    "works_and_views",
    "interviews_and_dialogue",
    "expression_dna",
    "decisions_and_behavior",
    "third_party_views",
    "affect_relationship_defense",
    "values_desires_contradictions",
]


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_cli(project_root: Path, data_dir: Path, *args: str) -> str:
    env = os.environ.copy()
    env["PERSONA_CONTINUUM_HOME"] = str(data_dir)
    result = subprocess.run(
        [sys.executable, "-m", "persona_continuum.cli.app", *args],
        cwd=project_root,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def build_artifact(source_id: str, dimension: str) -> dict[str, Any]:
    return {
        "artifact_id": f"preflight_{dimension}",
        "schema_version": "1.1",
        "dimension": dimension,
        "source_ids": [source_id],
        "claims": [
            {
                "content": "Alex Chen values careful observation before launches.",
                "source_id": source_id,
                "claim_type": "historical_self_report",
                "confidence": 0.88,
                "reliability": 0.84,
                "inference_strength": 0.2,
            },
        ],
        "memories": [
            {
                "content": "Alex delayed a launch after Mina raised an accessibility concern.",
                "type": "episodic",
                "importance": 0.9,
                "participants": ["Alex", "Mina"],
                "source_kind": "historical_self_report",
                "source_id": source_id,
                "source_confidence": 0.88,
            }
        ],
        "extracted_components": {
            "identity_profile": {"summary": "Alex Chen builds calm tools."},
            "timeline_events": [{"date": "2024", "event": "Delayed a risky launch."}],
            "self_narrative_evidence": ["I slow down when trust is at risk."],
            "mental_models": ["Trust compounds through careful observation."],
            "decision_heuristics": ["Delay launches when accessibility evidence is weak."],
            "values": ["careful observation", "accessibility trust"],
            "contradictions": [{"claim": "speed versus accessibility review"}],
            "failure_patterns": ["over-caution under ambiguity"],
            "temperament": {"baseline": "quiet, specific, reflective"},
            "emotional_triggers": ["launch theater"],
            "attachment_patterns": {"style": "protective collaborator"},
            "needs_and_desires": ["trusted review loops"],
            "defenses": ["asks for source evidence"],
            "expression_style": {"tone": "quiet, specific, reflective"},
            "vocabulary": ["careful observation", "trust", "review"],
            "dialogue_examples": ["I need to slow down and inspect the risk."],
            "anti_patterns": ["unsupported certainty"],
            "relationships": [{"counterpart": "Mina", "trust": 0.7}],
        },
        "conflicts": [],
        "uncertainty": {"level": 0.18, "notes": []},
        "created_by": "preflight_host",
        "artifact_hash": f"preflight-{dimension}",
    }


def run_service_flow(project_root: Path, data_dir: Path) -> dict[str, str]:
    source = data_dir / "sources" / "alex.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "Alex Chen values careful observation. Mina once challenged a launch.\n",
        encoding="utf-8",
    )
    app = PersonaContinuum(Config(data_dir=data_dir))
    app.init()
    persona = app.personas.create(
        display_name="Alex Chen",
        aliases=["Alex"],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
        birth_date="1988-04-12",
    )
    added = app.personas.add_sources(persona.id, [source])
    task = app.compilation.create_task(persona.id)
    for dimension in DIMENSIONS:
        app.compilation.submit_research_artifact(task.id, build_artifact(added[0].id, dimension))
    app.compilation.compile_persona(persona.id, task.id)
    app.compilation.validate_persona(persona.id)
    app.personas.activate(persona.id)

    session = app.sessions.start_session(persona.id, "preflight")
    prepared = app.sessions.prepare_turn(persona.id, session.id, "Mina is worried about launch.")
    assert_true(prepared.relevant_memories, "prepare_turn returned no memory context")
    assert_true(
        "careful observation" in json.dumps(prepared.compiled_persona_context, ensure_ascii=False),
        "prepare_turn did not include compiled persona components",
    )
    commit = app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="Mina is worried about launch.",
        persona_response="I need to slow down and inspect the accessibility risk.",
        used_memory_ids=[memory.id for memory in prepared.relevant_memories],
        user_feedback="good recall",
    )
    prepared_again = app.sessions.prepare_turn(persona.id, session.id, "What did we discuss?")
    assert_true(
        any(
            memory.source_kind == "digital_experience"
            for memory in prepared_again.relevant_memories
        ),
        "commit_turn did not feed later prepare_turn with digital experience memory",
    )

    continuation = app.continuations.create(persona.id, "digital_persona_activated")
    app.continuations.add_world_events(
        continuation.id,
        [
            {"date": "2027-01-01", "content": "A school pilot used Alex's checklist."},
            {"date": "2027-03-01", "content": "Mina published an accessibility critique."},
        ],
    )
    branch_a = app.continuations.create_branch(continuation.id, seed=1)
    branch_b = app.continuations.create_branch(continuation.id, seed=2)
    app.continuations.prepare_step(branch_a.id, "2027-06-01")
    app.continuations.prepare_step(branch_b.id, "2027-06-01")
    assert_true(
        app.continuations.get_branch(branch_a.id).status == "waiting_for_host"
        and app.continuations.get_branch(branch_b.id).status == "waiting_for_host",
        "continuation branches should wait for host artifacts",
    )
    app.continuations.commit_step(
        branch_a.id,
        {
            "evaluated_events": [{"date": "2027-06-01", "content": "School pilot expanded."}],
            "chosen_actions": [
                {
                    "action": "Ask Mina to critique the checklist before expansion",
                    "reason": "fits the documented trust repair pattern",
                }
            ],
            "world_state_delta": {"pilot": "expanded"},
            "persona_state_delta": {"trust_strategy": "collaborative_review"},
            "relationship_deltas": [
                {"counterpart_id": "Mina", "changes": {"trust": 0.75}, "reason": "review"}
            ],
            "affect_deltas": {"hope": 0.2},
            "goal_deltas": [{"goal_id": "accessibility", "status": "active", "delta": 0.2}],
            "new_memories": [
                {
                    "content": "In the branch, Alex asked Mina to critique the checklist.",
                    "importance": 0.6,
                }
            ],
            "rejected_alternatives": [
                {"option": "Expand without review", "reason": "breaks evidence constraints"}
            ],
            "causal_explanation": "The prior trust pattern makes collaborative review plausible.",
            "uncertainty": 0.25,
            "evidence_links": [],
            "next_step_date": "2027-09-01",
        },
    )
    app.continuations.commit_step(
        branch_b.id,
        {
            "evaluated_events": [{"date": "2027-06-01", "content": "Critique became public."}],
            "chosen_actions": [
                {
                    "action": "Publish a transparent correction note",
                    "reason": "public critique requires accountability",
                }
            ],
            "world_state_delta": {"critique": "public"},
            "persona_state_delta": {"trust_strategy": "public_accountability"},
            "relationship_deltas": [
                {"counterpart_id": "Mina", "changes": {"respect": 0.8}, "reason": "critique"}
            ],
            "affect_deltas": {"anxiety": 0.2},
            "goal_deltas": [{"goal_id": "accountability", "status": "active", "delta": 0.2}],
            "new_memories": [
                {
                    "content": "In the branch, Alex published a transparent correction note.",
                    "importance": 0.6,
                }
            ],
            "rejected_alternatives": [
                {"option": "Ignore the critique", "reason": "unexplained avoidance"}
            ],
            "causal_explanation": "The public critique makes accountability plausible.",
            "uncertainty": 0.3,
            "evidence_links": [],
            "next_step_date": "2027-09-01",
        },
    )
    app.continuations.score_branch(branch_a.id)
    app.continuations.score_branch(branch_b.id)
    comparison = app.continuations.compare_branches(continuation.id)
    assert_true(comparison["branch_count"] == 2, "continuation did not persist two branches")
    app.continuations.select_main_branch(continuation.id, branch_a.id)
    app.continuations.compile_persona(continuation.id)
    branch_session = app.sessions.start_session(
        persona.id, "Branch A check", branch_id=branch_a.id
    )
    branch_a_context = app.sessions.prepare_turn(
        persona.id,
        branch_session.id,
        "transparent correction note branch",
        branch_id=branch_a.id,
    )
    assert_true(
        "transparent correction note" not in json.dumps(
            [memory.content for memory in branch_a_context.relevant_memories],
            ensure_ascii=False,
        ),
        "branch A prepare_turn leaked branch B memory",
    )
    counterfactual = app.memories.search_memories(
        persona.id, "branch Alex", limit=10, branch_id=branch_a.id
    )
    assert_true(
        any(memory.source_kind == "counterfactual_host_artifact" for memory in counterfactual),
        "counterfactual host artifact memories were not marked correctly",
    )
    isolation_session_a = app.sessions.start_session(
        persona.id, "Branch A isolation", branch_id=branch_a.id, counterpart_id="Alice"
    )
    app.sessions.commit_turn(
        persona.id,
        isolation_session_a.id,
        user_message="Set branch-local runtime.",
        persona_response="This state remains on branch A.",
        counterpart_id="Alice",
        state_patch={
            "affect": {"anger": 0.9},
            "relationships": [{"counterpart_id": "Alice", "changes": {"trust": 0.8}}],
        },
    )
    isolation_session_b = app.sessions.start_session(
        persona.id, "Branch B isolation", branch_id=branch_b.id, counterpart_id="Alice"
    )
    branch_b_runtime = app.sessions.prepare_turn(
        persona.id,
        isolation_session_b.id,
        "Read branch B runtime.",
        branch_id=branch_b.id,
        counterpart_id="Alice",
    )
    branch_b_anger = next(
        state for state in branch_b_runtime.current_emotions if state.name == "anger"
    )
    assert_true(branch_b_anger.intensity < 0.2, "branch B leaked branch A anger")
    assert_true(
        branch_b_runtime.relationship_state.trust == 0,
        "branch B leaked branch A relationship trust",
    )
    invalid_session = app.sessions.start_session(
        persona.id, "Atomic rollback", branch_id=branch_a.id, counterpart_id="Alice"
    )
    before_counts = {
        table: app.database.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ["session_turns", "memories", "change_events"]
    }
    try:
        app.sessions.commit_turn(
            persona.id,
            invalid_session.id,
            user_message="bad state",
            persona_response="rollback",
            counterpart_id="Alice",
            state_patch={"affect": {"anger": "invalid"}},
        )
    except Exception:
        pass
    else:
        raise AssertionError("invalid commit_turn did not fail")
    after_counts = {
        table: app.database.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ["session_turns", "memories", "change_events"]
    }
    assert_true(after_counts == before_counts, "invalid commit_turn partially wrote data")

    export_path = app.personas.export_persona(persona.id, data_dir / "alex.zip")
    redacted_path = app.personas.export_persona(
        persona.id, data_dir / "alex.redacted.zip", mode="redacted"
    )
    identity_path = app.personas.export_persona(
        persona.id, data_dir / "alex.identity.zip", mode="identity_only"
    )
    with zipfile.ZipFile(redacted_path) as archive:
        redacted_payload = "\n".join(
            archive.read(name).decode("utf-8", errors="ignore")
            for name in archive.namelist()
            if not name.endswith("/")
        )
    assert_true(str(source) not in redacted_payload, "redacted export leaked local path")
    assert_true(
        "Mina once challenged a launch" not in redacted_payload,
        "redacted export leaked source body",
    )
    imported = app.personas.import_persona(export_path, "alex-imported")
    assert_true(imported.id == "alex-imported", "persona import failed")

    app.memories.add_memory(
        persona.id,
        content="Private deletion check.",
        memory_type=MemoryType.SEMANTIC,
        source_kind="user_correction",
    )
    db_path = app.config.database_path
    app.close()
    assert_true(db_path.exists(), "database was not created")
    with sqlite3.connect(db_path) as conn:
        memory_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE persona_id = ?", (persona.id,)
        ).fetchone()[0]
        assert_true(memory_count >= 3, "database did not persist expected memories")
    return {
        "persona_id": persona.id,
        "session_id": session.id,
        "turn_id": commit["turn_id"],
        "export_path": str(export_path),
        "redacted_export_path": str(redacted_path),
        "identity_export_path": str(identity_path),
        "branch_a": branch_a.id,
        "branch_b": branch_b.id,
        "branch_runtime_isolation": "ok",
        "atomic_commit_turn_rollback": "ok",
    }


async def run_mcp_flow(project_root: Path, data_dir: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["PERSONA_CONTINUUM_HOME"] = str(data_dir)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "persona_continuum.mcp.server"],
        cwd=project_root,
        env=env,
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        required = {
            "persona_create",
            "persona_prepare_turn",
            "persona_commit_turn",
            "continuation_advance_branch",
            "persona_create_room",
        }
        assert_true(required.issubset(names), f"missing MCP tools: {required - names}")
        created = await session.call_tool(
            "persona_create", {"display_name": "MCP Alex", "run_mode": "digital_continuation"}
        )
        data = json.loads(created.content[0].text)
        assert_true(data["ok"], f"MCP persona_create failed: {data}")
        persona_id = data["data"]["id"]
        session_result = await session.call_tool(
            "persona_start_session", {"persona_id": persona_id, "title": "mcp"}
        )
        session_data = json.loads(session_result.content[0].text)
        assert_true(session_data["ok"], f"MCP persona_start_session failed: {session_data}")
        prepared = await session.call_tool(
            "persona_prepare_turn",
            {
                "persona_id": persona_id,
                "session_id": session_data["data"]["id"],
                "user_message": "hello",
            },
        )
        prepared_data = json.loads(prepared.content[0].text)
        assert_true(prepared_data["ok"], f"MCP persona_prepare_turn failed: {prepared_data}")
        resources = await session.list_resource_templates()
        assert_true(resources.resourceTemplates, "MCP resource templates not listed")
        prompts = await session.list_prompts()
        assert_true(prompts.prompts, "MCP prompts not listed")
        return {"tool_count": len(names), "persona_id": persona_id}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]

    if args.data_dir:
        data_dir = args.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        result = {
            "data_dir": str(data_dir),
            "service": run_service_flow(project_root, data_dir),
            "cli_doctor": run_cli(project_root, data_dir, "doctor", "--json"),
            "cli_list": run_cli(project_root, data_dir, "persona", "list"),
            "mcp": asyncio.run(run_mcp_flow(project_root, data_dir)),
        }
    else:
        with tempfile.TemporaryDirectory(prefix="persona-continuum-preflight-") as tmp:
            data_dir = Path(tmp)
            result = {
                "data_dir": str(data_dir),
                "service": run_service_flow(project_root, data_dir),
                "cli_doctor": run_cli(project_root, data_dir, "doctor", "--json"),
                "cli_list": run_cli(project_root, data_dir, "persona", "list"),
                "mcp": asyncio.run(run_mcp_flow(project_root, data_dir)),
            }
            if args.keep_data:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
