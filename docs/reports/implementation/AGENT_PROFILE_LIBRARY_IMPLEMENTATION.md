# Agent / Profile Library Implementation Report

## Scope

Implemented the Actor/Persona Library, LLM-assisted World Entity
Classification, Actor Completion Engine and versioned Profile Enrichment
upgrade. Existing Persona, EvidenceSource, ResearchArtifact, Compilation,
Memory, Agent Adapter, Model Capability and CredentialManager seams remain in
use; no replacement Persona package or unsourced system prompt path was added.

## New files

- `src/persona_continuum/domain/profile.py` — unified profile models, typed
  profile views, versions and enrichment jobs.
- `src/persona_continuum/application/profile_library_service.py` — Persona
  synchronization, library queries, summary generation, version snapshots and
  archival.
- `src/persona_continuum/application/profile_enrichment_service.py` — async
  enrichment/upgrade runtime with Persona pipeline delegation and restart
  recovery.
- `src/persona_continuum/application/world/entity_classification_service.py` —
  LLM classification, deterministic safety validation and profile matching.
- `src/persona_continuum/application/world/__init__.py`.
- `docs/PROFILE_LIBRARY.md`.
- `docs/WORLD_ENTITY_CLASSIFICATION.md`.
- `docs/ACTOR_COMPLETION_ENGINE.md`.
- `docs/PROFILE_ENRICHMENT.md`.

## Modified backend and storage

- `domain/persona.py`: evidence-grounded `manifest.summary`.
- `application/container.py`, `persona_service.py`, `persona_creation_service.py`:
  service wiring, Persona synchronization and post-compile summary refresh.
- `application/world_service.py`, `world/models.py`, `world/actors.py`,
  `world/engine.py`, `world/repository.py`, `world/state.py`,
  `world/director.py`: profile-aware Actor types, runtime bindings, Agent-only
  decision selection, non-Agent initial-state entities and world seed metadata.
- `storage/migrations.py`, `storage/database.py`: `actor_profiles`,
  `profile_versions`, `profile_enrichment_jobs`, and backward-compatible world
  binding columns.
- `web/api.py`, `web/server.py`: Profile Library, enrichment, entity
  classification, Actor Completion and archive endpoints; restart workers are
  attached to server lifespan. Persona/profile job responses redact uploaded
  material text and require explicit remote-material consent for API runtimes.

## UI changes

The 人物 page is now 人物 / 档案库 with grouped Persona/Organization/
Institution/Collective cards, search/type/status/sort controls, summary,
coverage, evidence/source counts, versions, detail drawer, create-profile,
upgrade/enrichment and archive interactions. The World Builder preview shows
separate Agent and non-Agent tables. Actor Completion Engine replaces the
Persona-only copy and Direct Create handles the same confirmation gate as
Preview.

## Pipeline behavior

World flow: World Builder → candidate entities → LLM + deterministic
classification → profile matching → missing Agent confirmation → typed Actor
Completion → profile binding → world persistence. `default_actor_runtime` is
only inherited by Agent-capable Actor types; environment/non-Agent conditions
remain unconfigured world entities.

Persona enrichment: existing Persona Creation/Compilation/Evidence/Memory
services remain authoritative. Non-person profiles use the typed structured
profile prompt and preserve old versions. Summary generation is bounded to a
card blurb and falls back to evidence-derived text only when the summary turn
is unavailable.

## Compatibility

Legacy `/api/personas`, Persona matching and Persona creation endpoints remain
available for Tavern and existing integrations. Existing Persona rows are
lazily migrated into `actor_profiles`; no database reset is required. Existing
world runtime bindings gain nullable profile columns through a safe migration.

## Tests written and validated

- `tests/unit/test_world_entity_classification.py`
- `tests/integration/test_profile_library.py`
- `tests/integration/test_profile_enrichment.py`
- `tests/integration/test_agent_profile_world_completion.py`
- `tests/e2e/test_agent_profile_library_e2e.py`

The tests cover Agent/non-Agent classification, deterministic correction,
profile migration/filter/detail/summary, version retention, enrichment scopes,
typed profile enrichment, and UI/API contracts for world completion. Validation
also added regressions for profile-version export/import integrity and cancelled
CLI subprocess cleanup.

## Validation status

Validation was authorized on 2026-08-25. The final local gates passed:

- pytest: 344 passed, 1 skipped, 1 intentional archive-tampering warning
- Ruff: passed
- mypy: passed across 154 source files

The skipped test requires an explicitly selected real Provider credential
profile. No real remote Provider call, network research, or live world
simulation was performed. See `AGENT_PROFILE_LIBRARY_VALIDATION.md` for the
commands, fixes, evidence, and remaining external-runtime boundary.
