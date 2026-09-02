# Agent Runtime Activity Refactor — Implementation Report

## Status

IMPLEMENTATION COMPLETE — WAITING FOR REVIEW APPROVAL

## Delivered

- Added `AgentActivityTracker` and a shared `SubprocessAgentTransport` with a
  bounded 16 MiB default stream limit and 1–32 MiB safety bounds.
- Made Runtime Executor the single timeout owner. The executor preserves one
  pending protocol read, observes activity at a fixed interval, and records
  first-response/idle/hard timeout diagnostics.
- Declared output modes for CLI, JSON protocol, ACP, Codex app-server, and API
  adapters. Buffered CLIs emit a bounded final response after process exit.
- Kept stderr drained concurrently with a bounded sanitized tail. Protocol
  frames, stdout/stderr bytes, thinking, tool, and output activity are tracked
  without copying private bodies into events or UI metadata.
- Preserved typed ACP failures, including
  `AGENT_TRANSPORT_FRAME_TOO_LARGE`, frame type, event type, tool name, byte
  counts, and configured limit. An overflowing ACP session is closed and is
  never read again for guessed JSON boundaries.
- Separated semantic `phase` from `participant_id`; material calls use
  `material_classification`, `semantic_relation`, and `evidence_fusion`, while
  dimension calls use `dimension_extraction`.
- Added persistent worker state, heartbeat, Agent call count, child snapshot,
  bounded child restart, and `CHILD_JOB_WORKER_LOST` propagation. Profile
  Enrichment preserves a child root `failure_json` code and adds only lineage
  context.
- Added truthful API reasoning capability metadata and UI configuration. The
  generic API path fails closed on unsupported explicit reasoning; Anthropic and
  Google/Gemini remain unbound/unsupported until a real provider binding exists.
- Added API Provider capability editing through `PUT /api/auth-profiles/{id}`;
  replacing a secret remains optional and capability metadata is preserved at
  the model key.
- Persisted requested/effective runtime binding snapshots and surfaced them in
  task detail diagnostics.
- Added/retained user/internal visibility, dismissed-at history semantics,
  terminal-only dismissal, unified cleanup, active-first Task Center results,
  paginated terminal history, child-job suppression, and active-only badges.
- Added Reasoning capability status and configuration affordances to the API
  and runtime selector UI, plus worker/heartbeat/transport/call diagnostics and
  no-Agent-call warnings to Task Center views.
- Added migration-safe columns/backfill and historical child detection based on
  parent references and Profile Enrichment child foreign keys.

## Main files

- `src/persona_continuum/agent/activity.py`
- `src/persona_continuum/agent/subprocess_transport.py`
- `src/persona_continuum/agent/runtime_executor.py`
- `src/persona_continuum/agent/timeout.py`
- `src/persona_continuum/agent/protocols/acp.py`
- `src/persona_continuum/agent/protocols/openai_compatible.py`
- `src/persona_continuum/application/persona_creation_service.py`
- `src/persona_continuum/application/profile_enrichment_service.py`
- `src/persona_continuum/application/job_progress.py`
- `src/persona_continuum/storage/database.py`
- `src/persona_continuum/web/api.py`
- `src/persona_continuum/web/server.py`
- `src/persona_continuum/web/static/app.js`
- `src/persona_continuum/web/static/index.html`

## Test code delivered

The new written contracts cover activity accounting, one pending read,
buffered output, concurrent stderr draining, cross-adapter frame activity,
phase mapping, child heartbeat/restart/liveness, monotonic parent progress,
reasoning capability truthfulness, fail-closed rejection, and runtime binding
snapshots. Existing ACP and Task Center regression coverage remains in
`tests/unit/test_acp_and_job_center_upgrade.py`; new focused contracts are in:

- `tests/unit/test_agent_runtime_activity_refactor.py`
- `tests/unit/test_api_reasoning_capability.py`
- `tests/unit/test_background_child_liveness.py`

## Verification boundary

Per the requested hard stop, this implementation turn did not run `pytest`,
`ruff`, `mypy`, `pyright`, Web E2E/browser checks, real CLI/API calls, real
Persona Enrichment, or a real ACP/Agent. Review approval is the next gate.
