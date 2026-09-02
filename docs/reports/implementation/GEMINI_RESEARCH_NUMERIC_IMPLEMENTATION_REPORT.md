# Implementation Report — Gemini Research / Numeric Boundaries

## Scope delivered

- Gemini headless Web Research now maps the explicit read-only permission profile to
  `--allowed-tools google_web_search,web_fetch`; dangerous broad-permission flags are not
  emitted.
- Local CLI behavioral verification is split into search and fetch probes, with independent
  URL validation and `BLOCKED` versus `UNAVAILABLE` status semantics.
- Typed Web Research errors and model capability fields preserve policy/auth/timeout causes;
  the capability cache fingerprints model, version, source, permission/tool policy and CLI
  flags, with short-lived blocked entries and a manual revalidation API/UI action.
- Shared numeric helpers normalize nullable, string, non-finite and out-of-range values.
  Optional dimension scores are skipped with metadata diagnostics; optional confidence and
  timeout values use deterministic defaults; required invalid numeric values become
  `INVALID_NUMERIC_FIELD` failures.
- Material Intelligence, Research Quality, Compilation schemas, Persona Creation, Profile
  Enrichment, Agent Protocol Runtime Config, legacy artifact loading and job progress now use
  bounded numeric parsing at model/persistence boundaries.
- HTTP runtime snapshots and child job configs omit null control keys and normalize retained
  timeout/ACP values.
- Existing ACP bounded-frame handling and Job Center changes remain intact: typed ACP overflow
  failures, transport-corruption close behavior, user/internal visibility, dismiss semantics,
  terminal-only cleanup, active-first pagination, failure details, child lineage, and retry
  supersession.

## Main files

- `src/persona_continuum/numeric.py`
- `src/persona_continuum/agent/models.py`
- `src/persona_continuum/agent/adapter.py`
- `src/persona_continuum/agent/adapters/gemini.py`
- `src/persona_continuum/application/research_backend.py`
- `src/persona_continuum/application/research_capability_cache.py`
- `src/persona_continuum/application/persona_creation_service.py`
- `src/persona_continuum/application/profile_enrichment_service.py`
- `src/persona_continuum/application/material_intelligence.py`
- `src/persona_continuum/application/research_quality.py`
- `src/persona_continuum/compiler/schemas.py`
- `src/persona_continuum/storage/database.py`
- `src/persona_continuum/storage/migrations.py`
- `src/persona_continuum/web/api.py`
- `src/persona_continuum/web/server.py`
- `src/persona_continuum/web/static/app.js`
- `src/persona_continuum/web/static/index.html`

## Regression coverage added or updated

`tests/unit/test_gemini_research_numeric_normalization.py` contains the requested exact
regression names for Gemini permissions/probes, blocked-cache revalidation, numeric defaults,
null OpenCode runtime values, material dimension skipping, legacy artifact loading and typed
required-number failures. Existing timeout contract tests were updated to the 5-second lower
bound.

## Validation boundary

Per the task's hard-stop instruction, this handoff does not run `pytest`, Ruff, mypy, pyright,
Web E2E, real Gemini/OpenCode CLI, real Persona Creation, real Web Research, real ACP Agent, or
code review. Review approval is required before those runtime gates are executed.
