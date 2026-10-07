#!/usr/bin/env python3
"""Synthetic Grok ACP independent-session concurrency probe.

Uses only synthetic prompts ("Reply with exactly: TASK-0N").  It never reads the
Persona database, uploads, private sources, or any user chat body.

The result is written to the persistent RuntimeCapabilityStore, so an App
started later in a different process can read the verified width.  A probe that
did not fully succeed never raises the verified concurrency, never computes a
meaningless speedup, and never repeats a payment failure against the provider.

Exit is always 0 for a completed probe (including NOT_RUN / FAILED_*), so the
machine-readable JSON is the source of truth.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from persona_continuum.agent.adapters.grok import GrokBuildAdapter
from persona_continuum.agent.models import AgentEventType, AgentSessionConfig, AgentTurn
from persona_continuum.performance.concurrency_cache import (
    IndependentSessionProbe,
    default_concurrency_cache,
)
from persona_continuum.performance.provider_failure import (
    classify_probe_failure,
    derive_max_verified,
    derive_probe_status,
    speedup_or_none,
)
from persona_continuum.performance.runtime_capability_store import (
    CapabilityState,
    ProbeStatus,
)
from persona_continuum.performance.runtime_identity import resolve_runtime_identity

DEFAULT_MODEL = "grok-4.6"
DEFAULT_WAVES = (1, 2, 4)


def _grok_binary(adapter: GrokBuildAdapter) -> str | None:
    finder = getattr(adapter, "_find_binary", None)
    if callable(finder):
        with_bin = finder()
        if with_bin:
            return str(with_bin)
    return shutil.which("grok")


def _load_credential_manager() -> Any | None:
    """Best-effort app CredentialManager so identity matches the App exactly."""

    try:
        from persona_continuum.auth.credentials import CredentialManager
        from persona_continuum.config import Config
        from persona_continuum.storage.database import Database

        config = Config()
        if not config.database_path.exists():
            return None
        return CredentialManager(Database(config.database_path), config.data_dir / "credential.key")
    except Exception:
        return None


async def _binary_version(adapter: GrokBuildAdapter, binary: str) -> str:
    from persona_continuum.agent.adapter import safe_exec_cmd

    code, out, _ = await safe_exec_cmd([binary, "--version"], timeout=8.0)
    if code == 0 and out.strip():
        return out.strip().splitlines()[0]
    return ""


async def _one(adapter: GrokBuildAdapter, label: str, model_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="__probe_concurrency__",
            participant_id=f"probe-{label}",
            persona_id="__probe__",
            model_id=model_id or None,
            allow_mcp=False,
        )
    )
    session_id = session.config.session_id
    chunks: list[str] = []
    error: str | None = None
    try:
        async for event in adapter.send(
            session, AgentTurn(user_message=f"Reply with exactly: {label}", stream=True)
        ):
            if event.type == AgentEventType.CHUNK and event.content:
                chunks.append(str(event.content))
            if event.type == AgentEventType.ERROR:
                error = str(event.error or "error")
    except Exception as exc:  # transport-level failure must not abort the wave
        error = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await adapter.close(session)
    text = "".join(chunks)
    # Correlation: the response must contain its own label.
    matched = label in text
    return {
        "label": label,
        "session_id": session_id,
        "elapsed_s": round(time.perf_counter() - started, 4),
        "text_head": text[:80],
        "matched": matched,
        "error": error,
    }


async def _wave(
    adapter: GrokBuildAdapter, labels: list[str], model_id: str
) -> dict[str, Any]:
    started = time.perf_counter()
    rows = await asyncio.gather(
        *[_one(adapter, label, model_id) for label in labels], return_exceptions=True
    )
    wall = round(time.perf_counter() - started, 4)
    ok_rows = [row for row in rows if isinstance(row, dict)]
    exceptions = [f"{type(row).__name__}: {row}" for row in rows if not isinstance(row, dict)]
    session_ids = [str(row.get("session_id")) for row in ok_rows]
    requested = len(labels)
    success = sum(1 for row in ok_rows if row.get("matched") and not row.get("error"))
    unique_sessions = len(set(session_ids))
    # Cross-talk = the number of distinct responses not equal to the number of
    # labels requested, or a response that failed to match its own label.
    correlation_ok = (
        success == requested
        and unique_sessions == requested
        and not exceptions
        and all(row.get("matched") and not row.get("error") for row in ok_rows)
    )
    return {
        "width": requested,
        "wall_s": wall,
        "success_count": success,
        "requested_width": requested,
        "unique_sessions": unique_sessions,
        "correlation_ok": correlation_ok,
        "errors": [str(row.get("error")) for row in ok_rows if row.get("error")] + exceptions,
        "session_ids": session_ids,
        "rows": ok_rows,
    }


def _speedup(serial_wall: float, width: int, wave: dict[str, Any]) -> float | None:
    if not wave.get("correlation_ok"):
        return None
    wall = float(wave.get("wall_s") or 0.0)
    if wall <= 0 or serial_wall <= 0:
        return None
    return round((serial_wall * width) / wall, 3)


def _first_failure_status(waves: dict[str, Any]) -> str:
    return classify_probe_failure(waves)


async def run_probe(options: dict[str, Any]) -> dict[str, Any]:
    model_id = str(options.get("model") or DEFAULT_MODEL)
    max_concurrency = max(1, int(options.get("max_concurrency") or 4))
    requested_waves = [w for w in DEFAULT_WAVES if w <= max_concurrency] or [1]
    adapter = GrokBuildAdapter()
    credential_manager = _load_credential_manager()
    if credential_manager is not None:
        adapter.credential_manager = credential_manager
    binary = _grok_binary(adapter)
    if not binary:
        return {
            "status": "NOT_RUN",
            "probe_status": ProbeStatus.FAILED_ENVIRONMENT,
            "reason": "grok_binary_missing",
            "GROK_CONCURRENCY_LIVE_PROBE": "NOT_RUN",
        }

    version = await _binary_version(adapter, binary)
    identity = resolve_runtime_identity(
        adapter,
        model_id=model_id,
        credential_manager=credential_manager,
        binary_version=version,
    )
    cache = default_concurrency_cache()
    if not options.get("force"):
        existing = cache.store.get(identity)
        if existing is not None and existing.state in {
            CapabilityState.VERIFIED,
            CapabilityState.PARTIAL,
        }:
            return {
                "status": "RAN",
                "probe_status": existing.probe_status,
                "already_verified": True,
                "max_verified": existing.max_verified,
                "current_recommended": existing.current_recommended,
                "identity": identity.as_dict(),
                "capability": existing.as_dict(),
                "GROK_CONCURRENCY_LIVE_PROBE": "SKIPPED_ALREADY_VERIFIED",
            }

    waves: dict[str, Any] = {}
    serial = await _wave(adapter, ["TASK-01"], model_id)
    waves["serial"] = serial
    serial_ok = bool(serial.get("correlation_ok"))
    if not serial_ok:
        # Do not hammer a provider that is failing the baseline (payment/auth).
        status = _first_failure_status(waves)
        capability = cache.store.record_probe(
            identity,
            probe_status=status,
            max_verified=1,
            parallel_independent_sessions_verified=False,
            probe_sample_count=1,
        )
        return {
            "status": "RAN",
            "probe_status": status,
            "max_verified": 1,
            "serial": serial,
            "speedup_valid": False,
            "two_speedup": None,
            "four_speedup": None,
            "identity": identity.as_dict(),
            "capability": capability.as_dict(),
            "GROK_CONCURRENCY_LIVE_PROBE": "RAN",
        }

    serial_wall = float(serial["wall_s"])
    correlation_failure = False
    for width in requested_waves:
        if width <= 1:
            continue
        labels = [f"TASK-{index:02d}" for index in range(width + 1, width + 1 + width)]
        wave = await _wave(adapter, labels, model_id)
        key = f"{width}_way"
        wave["speedup"] = speedup_or_none(serial_wall, width, wave)
        wave["speedup_valid"] = wave["speedup"] is not None
        waves[key] = wave
        if wave["requested_width"] != wave["unique_sessions"]:
            correlation_failure = True
        if not wave["correlation_ok"] and not correlation_failure:
            # A wave that failed for non-correlation reasons stops escalation.
            break

    verified = derive_max_verified(waves, requested_waves)
    status = derive_probe_status(
        waves, requested_waves, correlation_failure=correlation_failure
    )
    two_wave = waves.get("2_way") or {}
    four_wave = waves.get("4_way") or {}
    two_speedup_valid = bool(two_wave.get("speedup_valid"))
    four_speedup_valid = bool(four_wave.get("speedup_valid"))
    # Speedup validity is per width: a failed 4-way wave must not erase a valid
    # 2-way result.
    overall_probe_complete = all(
        bool(waves.get(f"{width}_way", {}).get("correlation_ok"))
        for width in requested_waves
        if width > 1
    )

    probe = IndependentSessionProbe(
        adapter_id=identity.adapter_id,
        max_verified=verified,
        status=status,
        binary_identity=identity.binary_identity,
        binary_version=identity.binary_version,
        model_id=identity.model_id,
        credential_identity_hash=identity.credential_identity_hash,
        runtime_origin=identity.runtime_origin,
        probe_version=identity.probe_version,
        probe_sample_count=sum(int(w.get("requested_width") or 0) for w in waves.values()),
        serial_s=serial_wall,
        two_way_s=two_wave.get("wall_s"),
        four_way_s=four_wave.get("wall_s"),
        two_speedup=two_wave.get("speedup"),
        four_speedup=four_wave.get("speedup"),
        speedup_valid=bool(two_speedup_valid or four_speedup_valid),
        last_failure_kind=None if status in {ProbeStatus.VERIFIED, ProbeStatus.PARTIAL} else status,
        notes={
            "waves": {k: _wave_summary(v) for k, v in waves.items()},
            "two_speedup_valid": two_speedup_valid,
            "four_speedup_valid": four_speedup_valid,
            "overall_probe_complete": overall_probe_complete,
        },
    )
    capability = cache.store_probe(probe)
    if correlation_failure:
        cache.store.invalidate(identity, reason="session_correlation_failure")

    return {
        "status": "RAN",
        "probe_status": status,
        "max_verified": verified,
        "overall_probe_complete": overall_probe_complete,
        "serial": waves["serial"],
        "two_way": waves.get("2_way"),
        "four_way": waves.get("4_way"),
        "two_speedup": probe.two_speedup,
        "two_speedup_valid": two_speedup_valid,
        "four_speedup": probe.four_speedup,
        "four_speedup_valid": four_speedup_valid,
        "speedup_valid": probe.speedup_valid,
        "identity": identity.as_dict(),
        "capability": capability.as_dict(),
        "GROK_CONCURRENCY_LIVE_PROBE": "RAN",
    }


def _wave_summary(wave: dict[str, Any]) -> dict[str, Any]:
    return {
        "width": wave.get("width"),
        "wall_s": wave.get("wall_s"),
        "success_count": wave.get("success_count"),
        "correlation_ok": wave.get("correlation_ok"),
        "speedup": wave.get("speedup"),
        "speedup_valid": wave.get("speedup_valid"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--force", action="store_true", help="re-probe even if verified")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    parser.add_argument(
        "--out", default="docs/reports/implementation/grok-concurrency-probe.json"
    )
    args = parser.parse_args()
    payload = asyncio.run(
        run_probe(
            {
                "model": args.model,
                "max_concurrency": args.max_concurrency,
                "force": args.force,
            }
        )
    )
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
