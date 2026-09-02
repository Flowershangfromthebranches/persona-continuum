#!/usr/bin/env python3
"""Run the Narrative Studio P0 path against a discovered real Agent runtime.

The runner never registers FakeAgent and never silently changes generation
mode. It prints and optionally persists a machine-readable evidence record.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from persona_continuum.agent.models import AgentProbeResult, AgentStatus
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.performance.runtime_pool import default_runtime_pool

STAGES = (
    "story_architect",
    "outline_writer",
    "forecast_simulator",
    "scene_actor",
    "screenwriter",
    "reviewer",
    "production_planner",
)


def _select_runtime(
    probes: list[AgentProbeResult],
    *,
    requested_agent: str | None = None,
    requested_model: str | None = None,
    requested_reasoning: str | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    ready = [
        probe
        for probe in probes
        if probe.status == AgentStatus.READY
        and probe.runtime_source != "test"
        and probe.models
        and (requested_agent is None or probe.id == requested_agent)
        and (
            requested_model is None
            or any(model.id == requested_model and model.selectable for model in probe.models)
        )
    ]
    if not ready:
        raise RuntimeError("No non-test READY Agent with a discovered model is available")
    ready.sort(
        key=lambda probe: (
            0 if probe.capabilities.structured_output_mode in {"native", "prompt_only"} else 1,
            0 if probe.capabilities.persistent_session else 1,
            probe.name,
        )
    )
    probe = ready[0]
    models = [
        model
        for model in probe.models
        if model.selectable and (requested_model is None or model.id == requested_model)
    ]
    if not models:
        raise RuntimeError(f"Requested model is not selectable for {probe.id}")
    model = models[0]
    effort: Any = requested_reasoning or model.default_reasoning_effort
    if not effort:
        effort = (
            model.supported_reasoning_efforts[0] if model.supported_reasoning_efforts else "none"
        )
    effort_value = effort.value if hasattr(effort, "value") else str(effort)
    runtime = {
        "agent_id": probe.id,
        "model_id": model.id,
        "reasoning_effort": effort_value,
    }
    evidence = {
        "agent": probe.id,
        "agent_name": probe.name,
        "agent_version": probe.version,
        "runtime_source": probe.runtime_source,
        "model": model.id,
        "reasoning": effort_value,
        "structured_output_mode": probe.capabilities.structured_output_mode,
    }
    return runtime, evidence


async def run_live_acceptance(
    *,
    data_dir: Path,
    requested_agent: str | None = None,
    requested_model: str | None = None,
    requested_reasoning: str | None = None,
    branch_count: int = 3,
    horizon: int = 2,
) -> dict[str, Any]:
    app = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=False)
    app.init()
    evidence: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "data_dir": str(data_dir),
        "generation_mode": "agent",
        "checks": {},
        "steps": [],
    }
    started = time.monotonic()

    async def step(name: str, operation: Any) -> Any:
        tick = time.monotonic()
        print(f"LIVE {name}: running", file=sys.stderr, flush=True)
        try:
            result = await operation
        except Exception as exc:
            evidence["steps"].append(
                {
                    "name": name,
                    "status": "failed",
                    "duration_ms": round((time.monotonic() - tick) * 1000, 1),
                    "error_class": type(exc).__name__,
                    "error": str(exc),
                }
            )
            print(f"LIVE {name}: failed", file=sys.stderr, flush=True)
            raise
        evidence["steps"].append(
            {
                "name": name,
                "status": "passed",
                "duration_ms": round((time.monotonic() - tick) * 1000, 1),
            }
        )
        print(f"LIVE {name}: passed", file=sys.stderr, flush=True)
        return result

    try:
        probes = await app.agent_discovery.scan(force_refresh=True)
        runtime, runtime_evidence = _select_runtime(
            probes,
            requested_agent=requested_agent,
            requested_model=requested_model,
            requested_reasoning=requested_reasoning,
        )
        evidence["runtime"] = runtime_evidence
        project = app.narratives.create_project(
            title="裁员通知来自十年后",
            logline=(
                "方宁收到2036年寄来的邮件：18:30她将被裁员，19:17她将死亡，"
                "落款是十年后的数字方宁。"
            ),
            description=(
                "近未来职场悬疑微短剧。方宁追查来自十年后的裁员与死亡预告，"
                "但角色只能依照各自当前知识行动。全季只使用方宁、主管陈默、"
                "数字人格 ORACLE 三个核心角色。EP01在18:30验证裁员通知，并在"
                "19:17前发现死亡预告中的可验证线索；EP02确认邮件来自受控数字人格"
                "实验并决定是否公开；EP03完成当面对质、取得可验证承认并暂停实验。"
                "隐藏真相是邮件来自未来数字方宁，不能提前泄露。每集不超过四个事件，"
                "不引入监管、诉讼、媒体或额外支线。"
            ),
            format="micro_drama",
            genre=["悬疑", "科幻", "职场"],
            tone=["克制", "紧张", "现实主义"],
            planned_episode_count=3,
            episode_duration_seconds_min=75,
            episode_duration_seconds_max=120,
            runtime_assignment={stage: dict(runtime) for stage in STAGES},
        )
        evidence["project_id"] = project.id
        bible = await step(
            "story_bible",
            app.narratives.generate_story_bible(
                project.id, runtime=runtime, generation_mode="agent"
            ),
        )
        for design in bible.characters[:3]:
            app.narratives.add_character(
                project.id,
                name=design.name,
                role=design.role,
                description=design.description,
            )
        characters = app.narratives.list_characters(project.id)
        if len(characters) < 2:
            characters.extend(
                [
                    app.narratives.add_character(project.id, name="方舟", role="主角"),
                    app.narratives.add_character(project.id, name="方舟-β", role="数字人格"),
                ]
            )
        app.narratives.create_missing_personas(project.id)
        characters = app.narratives.list_characters(project.id)
        secret = app.narratives.add_fact(
            project.id,
            "未来通知由方舟的数字人格在公司重组后发送",
            secret=True,
        )
        for index, character in enumerate(characters):
            app.narratives.set_character_knowledge(
                project.id,
                character.id,
                secret.id,
                "known" if index == len(characters) - 1 else "unknown",
                fact_text=secret.text if index == len(characters) - 1 else "",
            )
        plans = await step(
            "outline_ep01_ep03",
            app.narratives.generate_outline(
                project.id, 3, runtime=runtime, generation_mode="agent"
            ),
        )
        app.narratives.ensure_story_world(project.id)
        direction_result = await step(
            "forecast_direction_generation",
            app.narratives.generate_forecast_directions(
                project.id,
                1,
                count=branch_count,
                runtime=runtime,
                generation_mode="agent",
            ),
        )
        directions = list(direction_result["directions"])[:branch_count]
        if len(directions) != branch_count:
            raise AssertionError(
                f"Expected {branch_count} forecast directions, got {len(directions)}"
            )
        forecast = await step(
            "forecast_real_horizon",
            app.narratives.forecast_episode_async(
                project.id,
                1,
                directions,
                horizon_episodes=horizon,
                runtime=runtime,
                generation_mode="agent",
            ),
        )
        app.narratives.select_forecast_direction(forecast.id, forecast.directions[0].id)
        lead = characters[0]
        scene = app.narratives.create_scene(
            project.id,
            {
                "episode_number": 1,
                "scene_goal": "方舟在不知真相的前提下验证未来通知的来源",
                "location": "公司夜间办公室",
                "time": "EP01",
                "participants": [
                    {
                        "character_id": lead.id,
                        "name": lead.name,
                        "goal": "找到可验证的线索",
                        "must_not_reveal": [secret.text],
                    }
                ],
            },
        )
        scene = await step(
            "scene_simulation",
            app.narratives.simulate_scene(
                project.id,
                scene,
                branch_id=forecast.directions[0].world_branch_id,
                runtime=runtime,
                generation_mode="agent",
                max_turns=1,
            ),
        )
        roles = [
            "head_writer",
            "mystery_editor",
            "character_editor",
            "continuity_editor",
            "commercial_editor",
        ]
        room_participants = []
        for index, role in enumerate(roles):
            persona = characters[index % len(characters)]
            room_participants.append(
                {
                    "role": role,
                    "display_name": role,
                    "persona_id": persona.persona_id,
                    "runtime_selection": runtime["agent_id"],
                    "model_selection": runtime["model_id"],
                    "reasoning_selection": runtime["reasoning_effort"],
                }
            )
        room_result = await step(
            "writer_room",
            app.narratives.run_writer_room(project.id, 1, room_participants, cross_review=True),
        )
        version = await step(
            "screenwriter",
            app.narratives.generate_episode_draft(
                project.id, 1, runtime=runtime, generation_mode="agent"
            ),
        )
        audit = await step(
            "continuity_audit",
            app.narratives.audit_episode_async(
                project.id, version.id, runtime=runtime, generation_mode="agent"
            ),
        )
        production = await step(
            "production_planner",
            app.narratives.generate_production_package_async(
                project.id,
                1,
                version.id,
                runtime=runtime,
                generation_mode="agent",
                is_preview=True,
            ),
        )
        commit = None
        if audit.passed:
            commit = app.narratives.commit_episode(project.id, 1, version.id)
        ep02 = app.narratives.prepare_episode(project.id, 2)
        traces = [dict(item) for item in app.narratives._runtime_traces]
        pool_snapshot = default_runtime_pool().snapshot()
        evidence.update(
            {
                "status": "passed" if audit.passed and commit else "failed_quality_gate",
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "story_bible": {"version": bible.version, "premise": bible.premise},
                "outline": [
                    {"episode": plan.episode_number, "title": plan.title} for plan in plans
                ],
                "forecast": {
                    "id": forecast.id,
                    "direction_count": len(forecast.directions),
                    "horizon": forecast.horizon_episodes,
                    "canonical_branch": forecast.canonical_branch_id,
                    "selected_branch": forecast.directions[0].world_branch_id,
                    "steps_per_direction": [len(item.steps) for item in forecast.directions],
                    "evaluations": [item.evaluation for item in forecast.directions],
                },
                "scene": {
                    "id": scene.id,
                    "room_id": scene.room_id,
                    "firewall": scene.runtime_trace.get("knowledge_firewall"),
                },
                "writer_room": {
                    "room_id": room_result["room_id"],
                    "protocol_state": room_result["protocol_state"],
                    "stage_output_count": len(room_result["stage_outputs"]),
                    "synthesis_id": room_result["synthesis"]["id"],
                },
                "screenwriter": {
                    "version_id": version.id,
                    "generation_mode": version.generation_mode,
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
                    "generation_mode": production.generation_mode,
                },
                "commit": commit,
                "ep02_context": {
                    "canon_snapshot_count": len(ep02.get("canon_snapshot") or []),
                    "episode_number": ep02.get("episode_plan", {}).get("episode_number"),
                },
                "runtime_traces": traces,
                "runtime_pool": pool_snapshot,
                "checks": {
                    "real_agent_calls": bool(traces)
                    and all(item.get("agent") != "fake_agent" for item in traces),
                    "no_deterministic_fallback": all(
                        item.get("generation_mode") == "agent" for item in traces
                    ),
                    "forecast_isolated": all(
                        item.world_branch_id != forecast.canonical_branch_id
                        for item in forecast.directions
                    ),
                    "forecast_horizon_executed": all(
                        len(item.steps) == horizon for item in forecast.directions
                    ),
                    "knowledge_firewall_passed": bool(
                        scene.runtime_trace.get("knowledge_firewall", {}).get("passed")
                    ),
                    "writer_room_synthesized": bool(
                        room_result["synthesis"].get("final_writer_instruction")
                    ),
                    "screenwriter_structured": bool(version.structured_draft),
                    "production_structured": bool(production.shot_list),
                    "runtime_leases_released": pool_snapshot.get("active_leases") == 0,
                    "ep01_committed": bool(commit),
                    "ep02_consumed_canon": bool(ep02.get("canon_snapshot")),
                },
            }
        )
        return evidence
    except Exception as exc:
        evidence.update(
            {
                "status": "failed",
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "error_class": type(exc).__name__,
                "error": str(exc),
                "runtime_traces": [dict(item) for item in app.narratives._runtime_traces],
                "runtime_pool": default_runtime_pool().snapshot(),
            }
        )
        return evidence
    finally:
        app.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--agent")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning", default="low")
    parser.add_argument("--branches", type=int, default=3)
    parser.add_argument("--horizon", type=int, default=2)
    args = parser.parse_args()
    data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="narrative-live-e2e-"))
    result = asyncio.run(
        run_live_acceptance(
            data_dir=data_dir,
            requested_agent=args.agent,
            requested_model=args.model,
            requested_reasoning=args.reasoning,
            branch_count=args.branches,
            horizon=args.horizon,
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
