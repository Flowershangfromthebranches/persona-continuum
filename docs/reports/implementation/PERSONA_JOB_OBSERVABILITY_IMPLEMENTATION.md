# Persona Job Observability Implementation

## Added

- `src/persona_continuum/agent/response_collector.py`: unified response
  collector, sanitization helpers, typed runtime/transport/output/structured
  output/timeout errors, and Agent Call Audit metadata.
- `src/persona_continuum/application/job_progress.py`: durable progress and
  failure contracts plus the private-material stage map.
- `tests/unit/test_agent_response_contract.py` and
  `tests/integration/test_persona_job_observability.py` plus UI contract tests.
- `docs/PERSONA_BACKGROUND_JOBS.md` and `docs/AGENT_RESPONSE_CONTRACT.md`.

## Modified

- Plain CLI now reports explicit empty-process output instead of emitting an
  empty `DONE` event.
- Codex App Server parses final `item/completed` assistant messages and uses a
  same-binding JSONL fallback when the first protocol has no assistant text.
- Persona Creation, Profile Enrichment, Profile Summary, World Builder, Entity
  Classification, Actor Runtime, Host Agent, and Room turns now use the common
  response collector. Empty output cannot silently become an observe/action or
  a blank summary.
- Persona Creation jobs persist numeric progress, failure JSON, call audits,
  and resumable parsing/segmenting/analyzing checkpoints. Profile Enrichment
  persists failure JSON and call audits.
- Web UI includes numeric creation/enrichment progress bars, detailed failure
  codes, an active-first paginated Task Center with user/internal visibility,
  terminal dismiss actions, cleanup controls, reopen actions, and a profile
  enrichment WebSocket endpoint. Dialog close remains separate from explicit
  cancel; pause/resume/cancel and retry actions are explicit controls.

## ACP and Task Center follow-up

The ACP follow-up is documented in `ACP_JOB_CENTER_IMPLEMENTATION.md` and
`docs/AGENT_RESPONSE_CONTRACT.md`. It adds a bounded shared ACP JSONL reader,
typed frame-overflow diagnostics, same-binding Grok fallback, large-tool-result
references, child-failure root-code propagation, additive job lifecycle
columns, terminal-only dismiss/cleanup APIs, and retry lineage.

## Database migration

Existing databases receive additive columns through `Database.migrate()`:
`progress_json`, `failure_json`, `agent_call_audits_json`, and
`checkpoints_json` on Persona Creation; `failure_json` and
`agent_call_audits_json` on Profile Enrichment. No existing Persona, evidence,
manifest, or compilation rows are removed.

## Public API

Existing job list/get/pause/resume/retry/cancel endpoints now return progress,
failure, and audit metadata. The existing Persona Creation event WebSocket is
preserved; Profile Enrichment adds
`/api/profile-enrichment/jobs/{job_id}/ws`. Task center reloads both list APIs.

## Verification boundary

Implementation and test code are written. No pytest, lint, type check, browser
E2E, server startup, real research, CLI, or Provider call was executed in this
implementation turn.
