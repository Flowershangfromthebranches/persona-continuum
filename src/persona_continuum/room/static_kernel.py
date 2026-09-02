"""Precompiled static persona context for room and world runtimes.

Most persona data does not change per turn: identity, biography anchors,
core values, expression style, decision heuristics, persona rules, and stable
motivations.  ``StaticPersonaKernel`` renders that stable part exactly once
per ``(persona_id, compile version, branch revision)`` and reuses it across
every turn, so PromptComposer no longer rebuilds the same JSON blobs and the
same identity text on each ``prepare_turn``.

The cache invalidates automatically when the persona manifest is updated
(updated_at/version changes) or a new compiled context version appears.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from persona_continuum.domain.session import PreparedTurn

_CACHE_LIMIT = 64


@dataclass(slots=True)
class StaticPersonaKernel:
    persona_id: str
    display_name: str
    cache_key: tuple[str, str, str]
    identity_block: str
    internal_state_hint: str = ""
    constraints_block: str = ""
    values_json: str = ""
    expression_json: str = ""
    decision_json: str = ""
    compiled_version: str = ""

    def system_sections(self, dynamic_sections: list[str]) -> str:
        sections = [
            f"You are {self.display_name}, a persistent digital persona in Persona Continuum.",
            self.identity_block,
            *dynamic_sections,
            self.constraints_block,
        ]
        return "\n\n".join(section for section in sections if section)


@dataclass
class StaticPersonaKernelCache:
    """Bounded in-process cache of compiled persona kernels."""

    _entries: dict[tuple[str, str, str], StaticPersonaKernel] = field(default_factory=dict)
    _order: list[tuple[str, str, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    hits: int = 0
    misses: int = 0

    def get_or_build(
        self,
        prepared: PreparedTurn,
        *,
        display_name: str,
        room_topic: str | None = None,
    ) -> StaticPersonaKernel:
        manifest = prepared.identity_anchor
        compiled = prepared.compiled_persona_context or {}
        by_key = dict(compiled.get("by_key", {}) or {})
        runtime_version = compiled.get("runtime_version")
        version_component = json.dumps(
            runtime_version if isinstance(runtime_version, dict) else str(runtime_version),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        runtime_payload = runtime_version if isinstance(runtime_version, dict) else {}
        branch = str(
            runtime_payload.get("active_branch_id") or prepared.session_id or "main"
        )
        manifest_revision = str(
            getattr(manifest, "updated_at", None)
            or getattr(manifest, "version", None)
            or "0"
        )
        component_revision = json.dumps(
            {
                "base": runtime_payload.get("base_compile_version"),
                "continuations": runtime_payload.get("continuation_versions") or [],
                "runtime": runtime_payload.get("digital_runtime_revision"),
                "manifest": manifest_revision,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        cache_key = (str(manifest.id), f"{version_component}|{component_revision}", branch)
        with self._lock:
            existing = self._entries.get(cache_key)
            if existing is not None:
                self.hits += 1
                return existing
            self.misses += 1
            kernel = self._build(manifest, by_key, display_name)
            kernel.cache_key = cache_key
            kernel.compiled_version = component_revision
            self._entries[cache_key] = kernel
            self._order.append(cache_key)
            overflow = len(self._order) - _CACHE_LIMIT
            if overflow > 0:
                for stale in self._order[:overflow]:
                    self._entries.pop(stale, None)
                del self._order[:overflow]
            return kernel

    @staticmethod
    def _build(manifest: Any, compiled: dict[str, Any], display_name: str) -> StaticPersonaKernel:
        kernel_lines = [
            f"# Digital Persona: {display_name}",
            f"- Persona ID: {manifest.id}",
            f"- Type: {manifest.persona_type.value}",
            f"- Run Mode: {manifest.run_mode.value}",
        ]
        if manifest.birth_date or manifest.death_date:
            kernel_lines.append(
                "- Lifespan: "
                f"{manifest.birth_date or 'unknown'} to {manifest.death_date or 'present'}"
            )
        if manifest.aliases:
            kernel_lines.append(f"- Aliases: {', '.join(manifest.aliases)}")
        if "values" in compiled:
            kernel_lines.append(
                f"- Core Values: {json.dumps(compiled['values'], ensure_ascii=False)}"
            )
        if "expression_style" in compiled:
            kernel_lines.append(
                "- Expression Tendencies: "
                f"{json.dumps(compiled['expression_style'], ensure_ascii=False)}"
            )
        if "decision_heuristics" in compiled:
            kernel_lines.append(
                "- Decision Patterns: "
                f"{json.dumps(compiled['decision_heuristics'], ensure_ascii=False)}"
            )
        constraints_text = (
            "## Persona Execution Constraints\n"
            f"1. You are embodying {display_name} in an interactive multi-agent room.\n"
            "2. Express thoughts in first-person voice consistent with your personality.\n"
            "3. Do not break character, mention prompt structure, or output raw internal states.\n"
            "4. Respect fact boundaries: do not assert certainty for unverified facts.\n"
            "5. If referencing history, rely strictly on your memories and retrieved context."
        )
        return StaticPersonaKernel(
            persona_id=str(manifest.id),
            display_name=display_name,
            cache_key=(str(manifest.id), "", ""),
            identity_block="\n".join(kernel_lines),
            constraints_block=constraints_text,
            values_json=json.dumps(compiled.get("values", {}), ensure_ascii=False),
            expression_json=json.dumps(
                compiled.get("expression_style", {}), ensure_ascii=False
            ),
            decision_json=json.dumps(
                compiled.get("decision_heuristics", {}), ensure_ascii=False
            ),
            compiled_version="",
        )

    def invalidate_persona(self, persona_id: str) -> None:
        with self._lock:
            stale = [key for key in self._entries if key[0] == persona_id]
            for key in stale:
                self._entries.pop(key, None)
            for key in stale:
                if key in self._order:
                    self._order.remove(key)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._order.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"entries": len(self._entries), "hits": self.hits, "misses": self.misses}


_default_cache = StaticPersonaKernelCache()


def default_static_kernel_cache() -> StaticPersonaKernelCache:
    return _default_cache


__all__ = [
    "StaticPersonaKernel",
    "StaticPersonaKernelCache",
    "default_static_kernel_cache",
]
