"""Lightweight task performance tracing.

A *task* maps to one observable unit of work (a Persona Creation job, a Room
turn, a World tick).  Within a task the tracer records:

- named spans (``research_total_ms``, ``dimension_<name>_ms``, ...) using
  ``time.perf_counter``
- monotonic counters (``model_call_count``, ``search_query_count``,
  ``physical_process_spawn_count``, ...)
- free-form metadata (token usage, transcript sizes, actor counts)

Recording never raises into the traced flow: tracing failures must not be
able to break a persona job.  Finished summaries are kept in a bounded ring
so callers can render a before/after benchmark from a single process run.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from persona_continuum.numeric import safe_int

_MAX_FINISHED_TASKS = 256


@dataclass(slots=True)
class _TaskRecord:
    kind: str
    started_at: float = field(default_factory=time.perf_counter)
    finished_at: float | None = None
    spans_ms: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    observations: dict[str, list[float]] = field(default_factory=dict)
    _open_spans: dict[str, tuple[str, float]] = field(default_factory=dict)

    def start_span(self, name: str) -> None:
        self._open_spans[name] = (name, time.perf_counter())

    def end_span(self, name: str) -> None:
        opened = self._open_spans.pop(name, None)
        elapsed = (time.perf_counter() - opened[1]) * 1000.0 if opened else 0.0
        key = name if name.endswith("_ms") else f"{name}_ms"
        self.spans_ms[key] = self.spans_ms.get(key, 0.0) + max(0.0, elapsed)

    def count(self, metric: str, delta: int = 1) -> None:
        self.counters[metric] = safe_int(self.counters.get(metric), default=0, minimum=0) or 0
        self.counters[metric] += delta

    def observe(self, metric: str, value: int) -> None:
        # Observations replace rather than accumulate (e.g. current actor count).
        self.counters[metric] = value

    def add_metadata(self, **values: Any) -> None:
        self.metadata.update(values)

    def add_observation(self, metric: str, value: float) -> None:
        if not math.isfinite(value):
            return
        values = self.observations.setdefault(metric, [])
        values.append(max(0.0, float(value)))
        if len(values) > 512:
            del values[: len(values) - 512]

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
        return ordered[index]

    def summary(self) -> dict[str, Any]:
        total_ms = ((self.finished_at or time.perf_counter()) - self.started_at) * 1000.0
        total_key = (
            f"{self.kind}_total_ms"
            if not self.kind.endswith("_total_ms")
            else self.kind
        )
        payload: dict[str, Any] = {
            "kind": self.kind,
            total_key: round(total_ms, 3),
            "started_at_perf": self.started_at,
            "finished_at": self.finished_at,
            "counters": dict(self.counters),
            "metadata": dict(self.metadata),
        }
        payload.update({key: round(value, 3) for key, value in sorted(self.spans_ms.items())})
        if self.observations:
            payload["distributions"] = {
                metric: {
                    "count": len(values),
                    "total": round(sum(values), 3),
                    "average": round(sum(values) / max(1, len(values)), 3),
                    "p50": round(self._percentile(values, 0.50), 3),
                    "p95": round(self._percentile(values, 0.95), 3),
                    "max": round(max(values), 3),
                }
                for metric, values in sorted(self.observations.items())
                if values
            }
        return payload


class PerformanceTracer:
    """Process-wide tracer with a bounded history of finished tasks."""

    def __init__(self, *, max_finished_tasks: int = _MAX_FINISHED_TASKS) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, _TaskRecord] = {}
        self._finished: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._max_finished = max(8, max_finished_tasks)
        self._global_counters: dict[str, int] = {}

    def incr_global(self, metric: str, delta: int = 1) -> None:
        """Count a process-lifetime metric (spawns, discovery calls, probes)."""

        with self._lock:
            self._global_counters[metric] = (
                safe_int(self._global_counters.get(metric), default=0, minimum=0) or 0
            ) + delta

    def global_value(self, metric: str) -> int:
        with self._lock:
            return safe_int(self._global_counters.get(metric), default=0, minimum=0) or 0

    def global_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._global_counters)

    def start_task(self, kind: str, task_id: str) -> None:
        with self._lock:
            self._active[task_id] = _TaskRecord(kind=kind)

    def finish_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._active.pop(task_id, None)
            if record is None:
                return None
            record.finished_at = time.perf_counter()
            for name in list(record._open_spans):
                record.end_span(name)
            summary = record.summary()
            self._finished[task_id] = summary
            self._order.append(task_id)
            overflow = len(self._order) - self._max_finished
            if overflow > 0:
                for stale in self._order[:overflow]:
                    self._finished.pop(stale, None)
                del self._order[:overflow]
            return summary

    @contextmanager
    def task(self, kind: str, task_id: str) -> Iterator[dict[str, Any] | None]:
        self.start_task(kind, task_id)
        try:
            yield None
        finally:
            self.finish_task(task_id)

    @contextmanager
    def span(self, task_id: str, name: str) -> Iterator[None]:
        with self._lock:
            record = self._active.get(task_id)
            if record is not None:
                record.start_span(name)
        try:
            yield
        finally:
            with self._lock:
                record = self._active.get(task_id)
                if record is not None:
                    record.end_span(name)

    def count(self, task_id: str | None, metric: str, delta: int = 1) -> None:
        if task_id is None:
            return
        with self._lock:
            record = self._active.get(task_id)
            if record is not None:
                record.count(metric, delta)

    def observe(self, task_id: str | None, metric: str, value: int) -> None:
        if task_id is None:
            return
        with self._lock:
            record = self._active.get(task_id)
            if record is not None:
                record.observe(metric, value)

    def record(self, task_id: str | None, **values: Any) -> None:
        if task_id is None or not values:
            return
        with self._lock:
            record = self._active.get(task_id)
            if record is not None:
                record.add_metadata(**values)

    def observe_value(self, task_id: str | None, metric: str, value: float) -> None:
        if task_id is None:
            return
        with self._lock:
            record = self._active.get(task_id)
            if record is not None:
                record.add_observation(metric, value)

    def active_summary(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._active.get(task_id)
            if record is None:
                return None
            return record.summary()

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            if task_id in self._active:
                return self._active[task_id].summary()
            return self._finished.get(task_id)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            keys = self._order[-limit:]
            return [dict(self._finished[key]) for key in keys if key in self._finished]

    def reset(self, *, clear_global_counters: bool = True) -> None:
        with self._lock:
            self._active.clear()
            self._finished.clear()
            self._order.clear()
            if clear_global_counters:
                self._global_counters.clear()


_default_tracer = PerformanceTracer()


def default_tracer() -> PerformanceTracer:
    """Return the process-wide tracer instance."""

    return _default_tracer


# Historic alias used by early call sites.
get_default_tracer = default_tracer


__all__ = [
    "PerformanceTracer",
    "default_tracer",
    "get_default_tracer",
]
