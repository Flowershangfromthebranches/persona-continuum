from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Config(BaseModel):
    """Runtime configuration for local storage."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    data_dir: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("PERSONA_CONTINUUM_HOME", "~/.persona-continuum")
        ).expanduser()
    )
    max_source_bytes: int = 10 * 1024 * 1024

    # --- Performance (execution strategy only; never quality policy) ---------
    # Defaults are chosen so ordinary users never need to touch them.  The
    # values bound *how* work is scheduled, cached, and reused.  They never
    # change research depth, dimension coverage, model choice, or reasoning
    # effort.
    runtime_pool_enabled: bool = True
    runtime_pool_max_processes_per_adapter: int = 4
    runtime_pool_idle_timeout_seconds: float = 15 * 60

    model_capability_cache_enabled: bool = True
    model_capability_cache_ttl_seconds: float = 6 * 60 * 60

    persona_incremental_extraction: bool = True
    persona_final_full_audit: bool = True
    persona_evidence_intelligence: bool = True
    persona_global_audit: bool = True
    persona_audit_repair_attempts: int = 1
    persona_global_audit_ledger_items: int = 32
    persona_global_audit_claims_per_dimension: int = 8
    persona_dimension_concurrency: int = 3
    persona_dimension_retrieval_items: int = 48

    persona_research_min_concurrency: int = 1
    persona_research_initial_concurrency: int = 3
    persona_research_max_concurrency: int = 6
    persona_research_recovery_successes: int = 4

    max_llm_concurrency: int = 4
    max_search_concurrency: int = 3
    max_fetch_concurrency: int = 4
    per_adapter_llm_limits: dict[str, int] = Field(
        default_factory=lambda: {
            "codex": 4,
            "claude": 3,
            "gemini_cli": 2,
            "grok": 3,
        }
    )

    research_source_cache_enabled: bool = True
    research_query_cache_enabled: bool = True
    research_query_background_ttl_seconds: float = 12 * 60 * 60
    research_query_news_ttl_seconds: float = 30 * 60

    room_context_cursor_enabled: bool = True
    room_static_persona_cache: bool = True
    room_raw_turn_window: int = 8

    world_parallel_actor_proposals: bool = True
    world_actor_proposal_concurrency: int = 4
    world_active_actor_limit: int = 6

    job_snapshot_debounce_seconds: float = 0.5

    # --- Persona runtime state appraisal -----------------------------------
    # How one turn's material (user message, persona reply, feedback, explicit
    # events) becomes a legal state_patch.  The default mode is deterministic
    # and adds NO extra model call to a turn.
    persona_state_appraisal_mode: str = "heuristic"
    # Per-turn change ceilings.  Emotions spike and decay fast, needs drift
    # slowly, relationships must never teleport (no trust 20% -> 90%).
    persona_state_max_affect_delta: float = 0.15
    persona_state_max_need_delta: float = 0.10
    persona_state_max_relationship_delta: float = 0.08

    @classmethod
    def optimized(cls, data_dir: Path | str | None = None) -> Config:
        """Execution strategy optimized; quality policy is the default (unchanged)."""

        kwargs: dict[str, object] = {"runtime_pool_enabled": True}
        if data_dir is not None:
            kwargs["data_dir"] = Path(data_dir).expanduser()
        return cls(**kwargs)  # type: ignore[arg-type]

    @classmethod
    def legacy_execution(cls, data_dir: Path | str | None = None) -> Config:
        """Every execution-strategy optimization disabled.

        This is the *only* purpose of this preset: it reproduces the pre-
        optimization behaviour (spawn per session, discover per session, full
        extraction every round, unbounded transcript re-send, serial world
        proposals) so a benchmark can measure the optimization's effect under
        an IDENTICAL Quality Policy.  It does not reduce research depth,
        dimensions, model, or reasoning.
        """

        kwargs: dict[str, object] = {
            "runtime_pool_enabled": False,
            "model_capability_cache_enabled": False,
            "persona_incremental_extraction": False,
            "persona_final_full_audit": False,
            "persona_evidence_intelligence": False,
            "persona_global_audit": False,
            "persona_dimension_concurrency": 1,
            "max_llm_concurrency": 1,
            "max_search_concurrency": 1,
            "max_fetch_concurrency": 1,
            "research_source_cache_enabled": False,
            "research_query_cache_enabled": False,
            "room_context_cursor_enabled": False,
            "room_static_persona_cache": False,
            "world_parallel_actor_proposals": False,
            "world_actor_proposal_concurrency": 1,
            "job_snapshot_debounce_seconds": 0.0,
        }
        if data_dir is not None:
            kwargs["data_dir"] = Path(data_dir).expanduser()
        return cls(**kwargs)  # type: ignore[arg-type]

    @property
    def database_path(self) -> Path:
        return self.data_dir / "persona_continuum.sqlite"

    @property
    def personas_dir(self) -> Path:
        return self.data_dir / "personas"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.personas_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.sources_dir.mkdir(parents=True, exist_ok=True)
