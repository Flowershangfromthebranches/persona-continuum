#!/usr/bin/env python3
"""Resume the expensive Narrative Studio Live chain from an existing project."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.performance.runtime_pool import default_runtime_pool

try:
    from scripts.narrative_live_acceptance import _select_runtime
except ModuleNotFoundError:  # Direct execution places scripts/ on sys.path.
    from narrative_live_acceptance import _select_runtime


async def run_resume(
    *,
    data_dir: Path,
    project_id: str,
    agent: str,
    model: str,
    reasoning: str,
) -> dict[str, Any]:
    app = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=False)
    app.init()
    evidence: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "data_dir": str(data_dir),
        "project_id": project_id,
        "steps": [],
    }

    async def step(name: str, operation: Any) -> Any:
        started = time.monotonic()
        print(f"LIVE RESUME {name}: running", file=sys.stderr, flush=True)
        try:
            result = await operation
        except Exception as exc:
            evidence["steps"].append(
                {
                    "name": name,
                    "status": "failed",
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "error_class": type(exc).__name__,
                    "error": str(exc),
                }
            )
            raise
        evidence["steps"].append(
            {
                "name": name,
                "status": "passed",
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
        print(f"LIVE RESUME {name}: passed", file=sys.stderr, flush=True)
        return result

    try:
        probes = await app.agent_discovery.scan(force_refresh=True)
        runtime, runtime_evidence = _select_runtime(
            probes,
            requested_agent=agent,
            requested_model=model,
            requested_reasoning=reasoning,
        )
        evidence["runtime"] = runtime_evidence
        project = app.narratives.get_project(project_id)
        characters = app.narratives.list_characters(project_id)
        if not characters or any(not character.persona_id for character in characters):
            raise RuntimeError("Existing project has no complete narrative Persona bindings")

        roles = [
            "head_writer",
            "mystery_editor",
            "character_editor",
            "continuity_editor",
            "commercial_editor",
        ]
        participants = [
            {
                "role": role,
                "display_name": role,
                "persona_id": characters[index % len(characters)].persona_id,
                "runtime_selection": runtime["agent_id"],
                "model_selection": runtime["model_id"],
                "reasoning_selection": runtime["reasoning_effort"],
            }
            for index, role in enumerate(roles)
        ]
        writer_room = await step(
            "writer_room",
            app.narratives.run_writer_room(project_id, 1, participants, cross_review=True),
        )
        synthesis = writer_room["synthesis"]
        if not str(synthesis.get("final_writer_instruction") or "").strip():
            raise AssertionError("Writer Room produced an empty final instruction")

        version = await step(
            "screenwriter",
            app.narratives.generate_episode_draft(
                project_id, 1, runtime=runtime, generation_mode="agent"
            ),
        )
        audit = await step(
            "continuity_audit",
            app.narratives.audit_episode_async(
                project_id, version.id, runtime=runtime, generation_mode="agent"
            ),
        )
        production = await step(
            "production_planner",
            app.narratives.generate_production_package_async(
                project_id,
                1,
                version.id,
                runtime=runtime,
                generation_mode="agent",
                is_preview=True,
            ),
        )
        commit = app.narratives.commit_episode(project_id, 1, version.id) if audit.passed else None
        ep02 = app.narratives.prepare_episode(project_id, 2)
        room_traces = list(synthesis.get("runtime_trace") or [])
        narrative_traces = [dict(trace) for trace in app.narratives._runtime_traces]
        pool = default_runtime_pool().snapshot()
        checks = {
            "writer_room_success": writer_room["protocol_state"] == "success",
            "writer_instruction_nonempty": bool(synthesis["final_writer_instruction"].strip()),
            "writer_room_all_requested_runtime": bool(room_traces)
            and all(
                trace.get("agent") == agent
                and trace.get("model") == model
                for trace in room_traces
            ),
            "downstream_all_requested_runtime": bool(narrative_traces)
            and all(
                trace.get("agent") == agent
                and trace.get("effective_model") == model
                and (trace.get("effective_reasoning") or trace.get("reasoning")) == reasoning
                for trace in narrative_traces
            ),
            "no_deterministic_fallback": all(
                trace.get("generation_mode") == "agent" for trace in narrative_traces
            ),
            "screenwriter_structured": bool(version.structured_draft),
            "audit_passed": audit.passed,
            "production_structured": bool(production.shot_list),
            "ep01_committed": bool(commit),
            "ep02_consumed_canon": bool(ep02.get("canon_snapshot")),
            "runtime_leases_released": pool.get("active_leases") == 0,
        }
        evidence.update(
            {
                "status": "passed" if all(checks.values()) else "failed_quality_gate",
                "completed_at": datetime.now(UTC).isoformat(),
                "project": {"title": project.title, "revision_before": project.revision},
                "writer_room": {
                    "room_id": writer_room["room_id"],
                    "synthesis_id": synthesis["id"],
                    "stage_output_count": len(writer_room["stage_outputs"]),
                    "runtime_trace": room_traces,
                    "final_writer_instruction": synthesis["final_writer_instruction"],
                },
                "screenwriter": {
                    "version_id": version.id,
                    "structured_scene_count": len(
                        (version.structured_draft or {}).get("scenes", [])
                    ),
                },
                "audit": {
                    "id": audit.id,
                    "passed": audit.passed,
                    "blocking": audit.blocking_count,
                    "warning": audit.warning_count,
                },
                "production": {
                    "id": production.id,
                    "shot_count": len(production.shot_list),
                },
                "commit": commit,
                "ep02_canon_snapshot_count": len(ep02.get("canon_snapshot") or []),
                "narrative_runtime_traces": narrative_traces,
                "runtime_pool": pool,
                "checks": checks,
            }
        )
        return evidence
    except Exception as exc:
        evidence.update(
            {
                "status": "failed",
                "completed_at": datetime.now(UTC).isoformat(),
                "error_class": type(exc).__name__,
                "error": str(exc),
                "runtime_pool": default_runtime_pool().snapshot(),
            }
        )
        return evidence
    finally:
        app.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--agent", default="codex")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning", default="low")
    args = parser.parse_args()
    result = asyncio.run(
        run_resume(
            data_dir=args.data_dir,
            project_id=args.project_id,
            agent=args.agent,
            model=args.model,
            reasoning=args.reasoning,
        )
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
