#!/usr/bin/env python3
"""Real subset acceptance through an existing configured adapter; no live ledger writes."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from material_p02_benchmark import read_subset, run

from persona_continuum.agent.models import AgentSessionConfig
from persona_continuum.agent.protocols.openai_compatible import OpenAICompatibleAPIAdapter
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
from persona_continuum.application.material_intelligence import (
    MATERIAL_AGENT_OUTPUT_SCHEMAS,
    MATERIAL_AGENT_SYSTEM_PROMPTS,
)
from persona_continuum.auth.credentials import CredentialManager
from persona_continuum.auth.profiles import AuthProfileService


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    db_path = args.data_dir / "persona_continuum.sqlite"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    database = SimpleNamespace(conn=conn)
    # The existing key is read only; never create a key for a live data directory.
    assert (args.data_dir / "credential.key").is_file()
    manager = CredentialManager(database, args.data_dir / "credential.key")
    auth = AuthProfileService(database, manager)
    job = conn.execute(
        "SELECT agent_id, model_id, reasoning_effort FROM persona_creation_jobs "
        "WHERE persona_id=(SELECT persona_id FROM persona_evidence_units GROUP BY persona_id "
        "ORDER BY count(*) DESC LIMIT 1) ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    profile = auth.get_profile(job["agent_id"].removeprefix("api_"))
    adapter = OpenAICompatibleAPIAdapter(
        adapter_id=job["agent_id"],
        name="Existing benchmark runtime",
        base_url=profile.base_url,
        auth_env_var=profile.auth_env_var,
        default_model=job["model_id"],
        custom_headers=profile.headers,
        model_capabilities=profile.metadata.get("model_capabilities", {}),
        provider_type=profile.provider_type,
        credential_manager=manager,
        credential_id=profile.id,
    )
    executor = AgentRuntimeExecutor(max_repair_attempts=0)
    count = 0

    async def analyzer(phase, payload):
        nonlocal count
        count += 1
        binding = await executor.open_session(
            adapter,
            AgentSessionConfig(
                session_id=f"p02-benchmark-{count}",
                model_id=job["model_id"],
                room_id="p02-benchmark",
                participant_id=f"worker-{count}",
                persona_id="fixture",
                reasoning_effort=job["reasoning_effort"],
                allow_mcp=False,
                tools=[],
                extra={"idle_timeout_seconds": 120, "total_timeout_seconds": 240},
            ),
        )
        try:
            result = await executor.execute_structured(
                binding,
                system_prompt=MATERIAL_AGENT_SYSTEM_PROMPTS[phase],
                user_message=json.dumps(payload, ensure_ascii=False),
                schema=MATERIAL_AGENT_OUTPUT_SCHEMAS[phase],
                phase="material_classification",
                max_repair_attempts=0,
            )
            print(json.dumps({"completed_call": count}), flush=True)
            return result.value
        finally:
            await adapter.close(binding.session)

    try:
        if args.smoke:
            await analyzer(
                "classify",
                {
                    "target_units": ["probe"],
                    "context_units": [],
                    "units": [{"id": "probe", "text": "我喜欢学习", "semantic_role": "target"}],
                },
            )
            (args.output_dir / "smoke.json").write_text('{"status":"passed"}')
            return
        rows = read_subset(db_path)
        for mode in ("full", "balanced"):
            report, evidence, style = await run(rows, mode, "rpc", analyzer)
            (args.output_dir / f"{mode}-metrics.json").write_text(json.dumps(report, indent=2))
            # Private outputs remain under the explicitly supplied local directory.
            (args.output_dir / f"{mode}-evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False)
            )
            (args.output_dir / f"{mode}-style.json").write_text(
                json.dumps(style, ensure_ascii=False)
            )
    except Exception as exc:
        (args.output_dir / "failure.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "code": getattr(exc, "code", None),
                    "attempted_calls": count,
                }
            )
        )
        if hasattr(exc, "errors"):
            print(json.dumps([{"loc": e["loc"], "type": e["type"]} for e in exc.errors()]))
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "code": getattr(exc, "code", None),
                }
            ),
            flush=True,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
