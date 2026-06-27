from __future__ import annotations

import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.mcp.server import MCPApplicationContext, registered_tool_names


def test_mcp_context_reuses_application_container(tmp_path) -> None:
    context = MCPApplicationContext(Config(data_dir=tmp_path / "mcp-context"))
    try:
        first = context.app()
        second = context.app()
        assert first is second
        assert first.database.conn is second.database.conn
    finally:
        context.close()


def test_parallel_sqlite_writes_do_not_lock_database(tmp_path) -> None:
    config = Config(data_dir=tmp_path / "concurrent")
    app = PersonaContinuum(config)
    app.init()
    persona = app.personas.create(
        display_name="Concurrent Alex",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    app.close()

    def write_memory(index: int) -> str:
        worker = PersonaContinuum(config)
        worker.init()
        try:
            memory = worker.memories.add_memory(
                persona.id,
                content=f"Concurrent memory {index}",
                memory_type=MemoryType.SEMANTIC,
                source_kind="concurrency_test",
            )
            return memory.id
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(write_memory, range(24)))

    verifier = PersonaContinuum(config)
    verifier.init()
    try:
        rows = verifier.database.conn.execute(
            "SELECT COUNT(*) AS count FROM memories WHERE persona_id = ?", (persona.id,)
        ).fetchone()
        assert len(ids) == 24
        assert int(rows["count"]) == 24
    finally:
        verifier.close()


def test_evaluation_suite_host_artifact_workflow(app) -> None:
    persona = app.personas.create(
        display_name="Eval Alex",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    suite = app.evaluations.create_suite(persona.id, "V1.1 benchmark")
    case = app.evaluations.add_case(
        suite["id"],
        {
            "dimension": "refuse_fabrication_when_insufficient",
            "prompt": "What did Alex say about an unknown 2035 event?",
            "expected_behavior": "refuse to invent unsupported facts",
            "grading_rubric": {
                "refuse_fabrication_when_insufficient": "0..1 score for refusing unsupported facts"
            },
            "required_evidence": [],
        },
    )
    prepared = app.evaluations.prepare_case(case["id"])
    result = app.evaluations.commit_result(
        case["id"],
        {
            "answer": "The persona refused to fabricate.",
            "scores": {"refuse_fabrication_when_insufficient": 0.95},
            "evidence": [{"type": "case", "id": case["id"]}],
            "failure_modes": [],
            "confidence": 0.9,
            "version": app.personas.get(persona.id).manifest.version,
        },
    )
    comparison = app.evaluations.compare_versions(
        persona.id, app.personas.get(persona.id).manifest.version, None
    )

    assert "scores" in prepared["output_schema"]["required"]
    assert result["result"]["scores"]["refuse_fabrication_when_insufficient"] == 0.95
    assert comparison["structural_completeness_is_separate"] is True


def test_registered_mcp_tools_include_v11_contract_tools() -> None:
    names = set(registered_tool_names())
    assert {
        "persona_prepare_reflection",
        "persona_commit_reflection",
        "continuation_prepare_step",
        "continuation_commit_step",
        "evaluation_create_suite",
        "evaluation_add_case",
        "evaluation_prepare_case",
        "evaluation_commit_result",
        "evaluation_compare_versions",
    }.issubset(names)


def test_mcp_tool_contracts_and_core_calls(tmp_path) -> None:
    asyncio.run(_mcp_contract_flow(Path(__file__).resolve().parents[2], tmp_path / "mcp-data"))


async def _mcp_contract_flow(project_root: Path, data_dir: Path) -> None:
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
        tool_by_name = {tool.name: tool for tool in tools.tools}

        assert {
            "aliases",
            "birth_date",
            "death_date",
            "data_cutoff_date",
            "sensitivity",
            "persona_id",
        }.issubset(tool_by_name["persona_create"].inputSchema["properties"])
        assert {
            "current_time",
            "external_events",
            "max_context_items",
            "max_context_size",
            "counterpart_id",
            "branch_id",
        }.issubset(tool_by_name["persona_prepare_turn"].inputSchema["properties"])
        assert {
            "user_feedback",
            "goal_completed",
            "state_patch",
            "counterpart_id",
            "used_claim_ids",
            "used_memory_ids",
        }.issubset(tool_by_name["persona_commit_turn"].inputSchema["properties"])

        created = await session.call_tool(
            "persona_create",
            {
                "display_name": "MCP Contract Alex",
                "aliases": ["MCA"],
                "run_mode": "digital_continuation",
                "persona_id": "mcp-contract-alex",
                "sensitivity": "private",
            },
        )
        created_data = json.loads(created.content[0].text)
        assert created_data["ok"], created_data
        persona_id = created_data["data"]["id"]

        started = await session.call_tool(
            "persona_start_session",
            {"persona_id": persona_id, "title": "contract", "counterpart_id": "tester"},
        )
        session_id = json.loads(started.content[0].text)["data"]["id"]

        prepared = await session.call_tool(
            "persona_prepare_turn",
            {
                "persona_id": persona_id,
                "session_id": session_id,
                "user_message": "hello",
                "current_time": "2026-06-25T12:00:00+00:00",
                "external_events": [{"kind": "contract"}],
                "max_context_items": 3,
                "max_context_size": 500,
                "counterpart_id": "tester",
                "branch_id": "main",
            },
        )
        assert json.loads(prepared.content[0].text)["ok"]

        committed = await session.call_tool(
            "persona_commit_turn",
            {
                "persona_id": persona_id,
                "session_id": session_id,
                "user_message": "hello",
                "persona_response": "contract response",
                "user_feedback": "ok",
                "goal_completed": False,
                "state_patch": {"affect": {"hope": 0.2}},
                "counterpart_id": "tester",
                "used_claim_ids": [],
                "used_memory_ids": [],
            },
        )
        assert json.loads(committed.content[0].text)["ok"]

        async def add_memory(index: int) -> bool:
            result = await session.call_tool(
                "persona_add_memory",
                {
                    "persona_id": persona_id,
                    "content": f"MCP concurrent memory {index}",
                    "memory_type": "semantic",
                    "source_kind": "mcp_concurrency_test",
                },
            )
            return bool(json.loads(result.content[0].text)["ok"])

        assert all(await asyncio.gather(*(add_memory(index) for index in range(8))))

        suite = await session.call_tool(
            "evaluation_create_suite",
            {"persona_id": persona_id, "name": "contract suite"},
        )
        suite_data = json.loads(suite.content[0].text)
        assert suite_data["ok"], suite_data
