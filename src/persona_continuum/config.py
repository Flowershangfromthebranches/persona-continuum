from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

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
    # Large private chat/material ingest.  This does not replace
    # ``max_source_bytes``, which remains the inline/legacy source limit.
    max_private_material_bytes: int = 2 * 1024 * 1024 * 1024
    material_intelligence_batch_size: int = 1000
    material_intelligence_in_memory_unit_limit: int = 8000
    private_material_upload_orphan_seconds: int = 24 * 60 * 60

    # --- Large Conversation Pipeline V2 (chat -> Material Intelligence) ------
    # Consecutive same-speaker messages in one conversation fold into a single
    # analytical ConversationTurn while this gap (seconds) is not exceeded.
    # Raw EvidenceUnits are never merged or deleted by the fold.
    material_chat_turn_gap_seconds: int = 90
    # Loose safety ceiling for one analysis window (ConversationTurns).
    # None / auto scales with the working token budget.  Token and
    # prompt-transport budgets stay the primary constraints.
    material_analysis_window_max_units: int | None = None
    # P0.3-B/C: a silent gap longer than this starts a new ConversationEpisode
    # (semantic atomic block).  Episodes no longer force a model dispatch
    # flush; many episodes pack into one window until a budget/ceiling is hit.
    material_conversation_episode_gap_seconds: int = 7200
    # Safety ceiling on episodes packed into one analysis window.  None / auto
    # scales with working context.  The token budget remains primary.
    material_analysis_window_max_episodes: int | None = None
    # Per-phase working-context ratios.  These are policy, never model limits.
    phase_working_ratios: dict[str, float] = Field(
        default_factory=lambda: {
            "material_classification": 0.80,
            "persona_compilation": 0.65,
            "research": 0.65,
            "audit": 0.70,
            "persistent_agent_session": 0.60,
            "unknown": 0.50,
        }
    )
    # Deterministic, LLM-free Expression DNA statistics for target chat
    # messages (P0-E ChatStyleProfiler).
    # Enable balanced explicitly until real compiler quality acceptance is complete.
    material_semantic_gate_mode: Literal["full", "balanced", "fast"] = "full"
    material_chat_style_profiler_enabled: bool = True
    material_contradiction_max_variants_per_cluster: int = 64
    # Adaptive concurrency retry: how many times one AnalysisWindow may be
    # requeued after a provider concurrency-limit rejection before the task
    # fails.  Downgrade ladder is N -> N/2 -> 1.
    material_concurrency_retry_limit: int = 2
    # TTL for a persistently verified independent-session concurrency
    # capability (seconds).  Past it the runtime falls back to one worker.
    runtime_concurrency_capability_ttl_seconds: float = 24 * 60 * 60

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
    # Deprecated alias.  Historical name; it always counted transcript ENTRIES
    # (a user message and a persona reply are two entries), never user/persona
    # rounds, and the room summary cadence was derived from it.  Kept so older
    # call sites and deployments keep loading.
    room_raw_turn_window: int = 8

    # --- Room context budget -------------------------------------------------
    # How many of the most recent transcript ENTRIES may be sent to the model.
    # One entry == one message (user message or persona reply counted
    # separately), so 8 == roughly 4 exchanges.  Once history exceeds this the
    # full transcript is NEVER sent again: older content reaches the model only
    # through the rolling summary and retrieved long-term memories.
    room_raw_message_window: int = 8
    # Hard ceiling on the rendered rolling summary.  The summary is a bounded
    # digest, not a second transcript.  Both limits apply; the tighter one wins.
    room_summary_max_chars: int = 2400
    room_summary_output_max_tokens: int = 800
    # Hard ceiling on what the summariser itself may be asked to read
    # (previous summary + the dialogue delta that just left the raw window).
    # Kept well below the room prompt budget: it is a compression task.
    room_summary_input_max_tokens: int = 4096
    # Refresh the rolling summary every N turns once it exists.  The first
    # summary is built as soon as history exceeds the raw window (not at 2x the
    # window, which used to leave turns 9..15 sending the full transcript).
    room_summary_refresh_every_turns: int = 4
    # Long-term memories retrieved (and injected) per turn.
    room_recall_top_k: int = 8
    # Total prompt token ceiling for one room turn.  ``None`` derives it from
    # the provider's reported context window.  An explicit value is honoured
    # because a model can report a large window and still be unable to cold
    # prefill it on this machine.
    room_prompt_max_tokens: int | None = None

    # --- Context strategy ----------------------------------------------------
    # Which budget shape one room turn uses.  This is POLICY, not capability:
    # the model's context window stays a capability, and the profile only
    # decides how much of it this turn may fill.
    #   auto               resolve from provider / context window / endpoint
    #                      locality / model capability (default)
    #   quality            long recent dialogue, many memories, rich summaries
    #   balanced           mid-tier models and machines with headroom
    #   local_constrained  the measured-safe local profile below
    # A room may override this in its metadata and a participant may override
    # it in its slot; both fall back to this value.
    room_context_strategy: str = "auto"
    # Force "this endpoint is memory-bound" even when its base URL is not
    # loopback (e.g. a LAN inference box shared by the household).  ``None``
    # means "decide from the base URL".  ``False`` explicitly disables the
    # locality heuristic for a remote-looking-but-local deployment.
    room_context_local_memory_constrained: bool | None = None

    # --- Conservative room prompt budget -------------------------------------
    # 16384 is the MODEL's context capability, not what this machine can safely
    # cold-prefill.  Measured on a 24 GiB M3: 6639 and 9474 tokens succeeded,
    # 8773 was killed by Metal -- so the limit is memory-state dependent, not a
    # fixed size, and the budget has to be conservative.  Above TARGET the
    # prompt is structurally trimmed (memories, then recent dialogue, then the
    # summary); above HARD the request is refused instead of risking Metal.
    # Raise these only after sustained stress testing.
    room_prompt_target_tokens: int = 6000
    room_prompt_hard_tokens: int = 7000
    # Per-memory render cap used by the first budget stage.
    room_memory_max_tokens: int = 400
    # Generation reservation for this provider (no thinking => no reasoning
    # reserve).  Kept here so it can never silently inflate the input budget.
    room_max_generation_tokens: int = 1024

    # --- Memory Episodes (Memory Architecture v2, Phase 3) -------------------
    # An Episode is a bounded stretch of shared experience, assembled from
    # committed turns.  These knobs control the ORGANISATION layer only: they
    # never change what a model is shown this turn (that is Context Policy),
    # and they never delete raw history.
    memory_episode_enabled: bool = True
    # Deterministic boundary thresholds.  Deliberately unrelated to the room
    # working-window numbers above: an Episode is a unit of memory, not a unit
    # of prompt.  24 exchanges is roughly one sitting; 6000 tokens is the point
    # where a summary stops being able to represent the stretch faithfully.
    memory_episode_max_turns: int = 24
    memory_episode_max_tokens: int = 6000
    # A silence longer than this starts a new Episode (a new "sitting").
    memory_episode_idle_gap_minutes: int = 180
    # Summarisation is asynchronous and never in the reply path.  It is a model
    # call, so it is skipped while a live turn is in flight on the same machine.
    memory_episode_consolidation_enabled: bool = True
    # Ceiling on what one consolidation call may be asked to read.
    memory_episode_summary_input_max_tokens: int = 16384
    # How many Episodes one background pass may summarise.  Bounded on purpose:
    # each pass is a real model call, and a room reopened after a long absence
    # must converge over a few passes rather than monopolise the model.
    memory_episode_consolidation_batch: int = 3
    # Lazy background backfill batch size for turns committed before Phase 3.
    memory_episode_backfill_batch: int = 40

    # --- Semantic Facts (Memory Architecture v2, Phase 4) --------------------
    # Facts distilled from Episodes: durable, provenance-backed, time-scoped.
    memory_fact_extraction_enabled: bool = True
    # Whether a pure persona INFERENCE may become a long-term fact.  Off by
    # default: turning 苏禾's "你肯定就是舍不得她" into the user's history is
    # exactly the failure this flag exists to prevent.
    memory_fact_persist_inferred: bool = False
    # Bounded validity for a temporary state that is still worth recording.
    memory_fact_temporary_valid_hours: int = 24
    # How much one extra piece of evidence raises confidence.
    memory_fact_confidence_step: float = 0.05
    # Existing active facts shown to the extractor so it can classify relations.
    memory_fact_max_existing_context: int = 30
    memory_fact_extraction_input_max_tokens: int = 12000
    memory_fact_extraction_batch: int = 3

    # --- Active Threads (Memory Architecture v2, Phase 5) --------------------
    # A Thread is "something still in flight" (a plan, an unresolved conflict, a
    # project).  These knobs are Memory-Consolidation budgets: none of them is a
    # prompt budget, and none of them may be tied to the room working window.
    memory_thread_resolution_enabled: bool = True
    # How many Episodes one background pass may resolve (each is a model call).
    memory_thread_resolution_batch: int = 2
    # Ceiling on the candidate shortlist handed to the resolver.  Deliberately a
    # consolidation budget: 20-30 live threads is a lot of context, and this
    # number must never be derived from room_prompt_* values.
    memory_thread_max_candidates: int = 24
    # Live threads that are ALWAYS shown, however weak the lexical match, so a
    # continuation like "票我买好了。" can still find "重庆旅行".
    memory_thread_top_recent: int = 8
    # Below this confidence nothing is written to a thread; the link becomes an
    # explicit pending candidate instead of a wrong binding.
    memory_thread_link_confidence_threshold: float = 0.55
    # Inactivity may only downgrade to STALE -- never resolve.  Silence is not
    # evidence that something is over.
    memory_thread_stale_after_days: int = 60
    # A resolved thread mentioned again within this window REOPENS; beyond it the
    # revival becomes a new thread that still links back to the old one.
    memory_thread_reopen_window_days: int = 30
    # One Episode may not spawn an unbounded number of new threads.
    memory_thread_max_creates_per_episode: int = 3
    memory_thread_resolution_input_max_tokens: int = 12000

    # --- Hierarchical Summaries (Memory Architecture v2, Phase 6) ------------
    # Chapters group Episodes, Long-term segments group Chapters.  These are
    # Memory-Consolidation budgets: the stored summary size is a policy of this
    # layer and is deliberately independent of what any model can hold this
    # turn (Context Policy).
    hierarchy_summary_enabled: bool = True
    # Deterministic grouping boundaries (time is only ONE signal, never the unit).
    hierarchy_chapter_max_episodes: int = 8
    hierarchy_chapter_max_source_tokens: int = 20000
    hierarchy_chapter_max_timespan_days: int = 14
    hierarchy_chapter_inactivity_gap_days: int = 7
    # Optional model-assisted topic boundary, consulted only when no
    # deterministic boundary fired AND the topic shift is large.  Off by
    # default: the first version is deterministic, and a failure must never
    # stall grouping.
    hierarchy_topic_boundary_enabled: bool = False
    hierarchy_topic_shift_threshold: float = 0.18
    # Long-term grouping.  A segment covers months, so it gets its own time
    # policy: the chapter-level 14-day span would split a segment after two
    # weeks of real history.
    hierarchy_long_term_min_chapters: int = 3
    hierarchy_long_term_max_chapters: int = 12
    hierarchy_long_term_max_source_tokens: int = 60000
    hierarchy_long_term_max_timespan_days: int = 365
    hierarchy_long_term_inactivity_gap_days: int = 180
    # Target sizes.  Density matters, not brevity: a chapter of 8 episodes is
    # worth more than a few hundred tokens.
    hierarchy_chapter_summary_target_tokens: int = 1600
    hierarchy_long_term_summary_target_tokens: int = 3000
    hierarchy_summary_input_max_tokens: int = 24000
    hierarchy_consolidation_batch: int = 1
    hierarchy_backfill_batch: int = 40
    # Schema allows deeper nesting; this is how far this build actually groups.
    hierarchy_max_level: int = 2

    # --- Memory Architecture v2: Phase 8 Context Assembly -------------------
    # Activates multi-tier retrieval (BASE/STANDARD/DEEP) and MemoryBundle injection.
    phase8_context_assembly: bool = True

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

    @property
    def room_uploads_dir(self) -> Path:
        return self.data_dir / "room_uploads"

    @property
    def private_material_uploads_dir(self) -> Path:
        return self.data_dir / "private_material_uploads"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.personas_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.room_uploads_dir.mkdir(parents=True, exist_ok=True)
        self.private_material_uploads_dir.mkdir(parents=True, exist_ok=True)
