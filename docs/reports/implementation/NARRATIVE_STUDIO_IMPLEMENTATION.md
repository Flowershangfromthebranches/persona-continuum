# Narrative Studio — Implementation Report

Date: 2026-08-29

## A. Architecture

```
Web UI (叙事创作 tab)      REST /api/narratives...   WS /api/narrative-jobs/{id}/ws
        │
MCP (narrative_* tools, 22)
        │
NarrativeService (application/narrative_service.py)
   ├─ NarrativeRepository        (narrative_* SQLite tables)
   ├─ NarrativeKnowledgeFirewall (Story Truth / Character / Audience)
   ├─ NarrativeContextBuilder    (task-scoped context, budget discipline)
   ├─ ScreenwriterStage          (simulation → timed beat sheet → screenplay)
   ├─ NarrativeContinuityAuditor (BLOCKING/WARNING/INFO)
   ├─ ProductionPackageBuilder   (shots, tracks, visual bibles)
   └─ writer_room protocol template (Room Protocol Engine CUSTOM stages)
        │
ParallelWorldEngine / SceneEngine / MultiAgentOrchestrator   (unchanged)
        │
Persona · Memory · Affect · Relationship · AgentRuntimeExecutor · RuntimePool
```

No second agent runtime, no second orchestrator, no RuntimePool semantic
changes. Narrative sessions never own physical runtimes; scene simulation
reuses the acquire→execute→release lease per turn.

## B. Files changed (new)

- `domain/narrative.py` — full narrative domain
- `narrative/` — repository, knowledge_firewall, context_builder,
  continuity_auditor, screenwriter, production, writer_room
- `application/narrative_service.py` — service + background jobs
- tests: 8 unit files, 4 integration files, 1 E2E (see acceptance report)

## C. Files changed (modified)

- `world/` genericization (Phase 0/P0):
  - `state.py` — empty-by-default WorldState built from backward-compatible
    `WorldSeed.initial_*` fields (metadata fallback), no example content
  - `organization.py` / `technology.py` — no default demo orgs/techs
  - `actors.py` — no template actor roster (Steve Jobs etc. removed)
  - `evaluator.py` — generic relative branch scoring (Leading/Contested/
    Constrained), never names companies or domains
  - `builder.py` — no hardcoded actor inference, neutral date/location fallbacks
  - `firewall.py` — generic anachronism categories (no product names)
  - `persona_memory.py` — domain-neutral belief evolution dimensions
  - `economy.py` — TAM growth from the most mature technology, not a fixed key
  - `actions.py` — no `apple_corp` fallbacks; clean failures without org backing
  - `engine.py` / `llm_builder.py` — neutral defaults; seed orgs/techs sync
    into managers for simulation steps
- `web/api.py`, `web/server.py` — ~50 narrative REST routes + job WS; removed
  hardcoded steve_jobs/nvidia defaults in world endpoints
- `web/static/index.html`, `app.js` — 叙事创作 workspace (projects, bible,
  cast, episodes pipeline, knowledge matrix, clues, production packages)
- `mcp/server.py` — 22 `narrative_*` tools
- `storage/migrations.py` — 19 `narrative_*` tables (+ narrative_jobs)
- `application/container.py` — `narratives` service wiring + shutdown

## D. Database

New tables (all `CREATE TABLE IF NOT EXISTS` in `SCHEMA_SQL`, so existing DBs
upgrade in place on `migrate()`; JSON payload columns keep the schema lean
without one-giant-JSON-table): narrative_projects,
narrative_story_bible_versions, narrative_story_facts, narrative_characters,
narrative_character_bindings, narrative_episode_plans, narrative_episode_versions,
narrative_scenes, narrative_canon_facts, narrative_canon_revisions,
narrative_knowledge (unique project+character+fact), narrative_audience_knowledge,
narrative_plot_threads, narrative_character_arcs, narrative_clues,
narrative_forecasts, narrative_audits, narrative_production_packages,
narrative_episode_summaries, narrative_jobs.

## E. Workflow

Idea → Bible (versioned) → Characters (+binding/auto fictional personas) →
World (`ensure_story_world`) → Outline → per Episode: PREPARE → FORECAST
(branch-per-direction) → SIMULATE (SceneEngine on personas' own knowledge) →
DRAFT (Screenwriter) → AUDIT (Continuity Auditor) → COMMIT (atomic canon) →
next episode from new canon → Production Package.

## F. Reused components

Persona (creation, compilation, manifests), Memory/Recall Gate, Room Protocol
Engine (writer room + scenes), Parallel World (branches, snapshots, replay,
scene engine), Continuation/Counterfactual isolation, RuntimePool, Structured
Output Engine, Context Budget, Agent Activity Contract/Timeout Policy, Job
control/progress semantics, Credential redaction.

## G. Remaining risks (real)

1. Forecast direction "beats" are heuristic/deterministic unless an LLM runtime
   is configured; the LLM hook (`_structured_call`) falls back silently.
2. Scene simulation writes one summary line into `scene.dialogue`; per-turn
   speaker attribution for firewall scanning of free-form dialogue relies on
   the scene engine transcript staying in room storage (world-scoped), and the
   auditor's per-speaker scan uses structured scene dialogue only.
3. Bible/outline/draft generation via LLM is prompt-driven without golden-file
   schema validation beyond Pydantic coercion.
4. The writer room currently returns room/protocol state; a synthesis-text
   extraction convenience on the protocol runtime is not yet wired into the
   episode draft automatically (the draft stage reads simulation output).
