# ACP Transport and Persona Job Center Implementation

## Scope delivered

This change addresses the ACP JSONL framing failure that previously surfaced
as `Separator is not found, and chunk exceed the limit`, and upgrades the
durable background-job Task Center.

### ACP transport

- `ACPAdapter.create_session()` passes one finite `limit` to both subprocess
  stdout and stderr. The default is 16 MiB; session config can request
  `extra.acp_stream_limit_bytes` within 1 MiB to 32 MiB.
- `ACPJsonLineReader` is the only ACP frame reader for initialize,
  authenticate, `session/new`, and `session/prompt`. It accounts frame bytes,
  decodes strict JSON, describes frame/event/tool type, and marks the stream
  corrupted on overflow.
- Overflow becomes typed `AGENT_TRANSPORT_FRAME_TOO_LARGE` with ACP protocol,
  phase, configured limit, observed bytes when available, and bounded frame
  diagnostics. The stream is closed instead of guessing a later JSON boundary.
- Known protocol, process-exit, timeout, and frame-overflow failures remain
  typed. Unknown exceptions become `AGENT_TRANSPORT_ERROR`; the old generic
  `ACP error: ...` prefix is gone.
- Grok Build uses the same ACP reader and, after closing a corrupted ACP
  session, may execute its existing headless fallback with the same model and
  reasoning selection. No retry loop hides framing failures.
- Large internal tool results are compacted only at AgentEvent/WebSocket/UI
  boundaries to an artifact reference, preview, character count, and evidence
  references. The complete authoritative result remains available to the
  model request and Evidence persistence path.

### Failure lineage

Profile Enrichment no longer wraps a typed child Persona Creation failure in a
second `PROFILE_ENRICHMENT_ERROR`. The parent copies the child failure's root
`code`, `phase`, `protocol`, runtime, event counts, and retryability, then adds
`child_job_id` plus parent context. A user therefore sees
`AGENT_TRANSPORT_FRAME_TOO_LARGE` directly.

### Task Center and storage

Both job tables now have additive `visibility`, `dismissed_at`, and
`superseded_by` columns with indexes. Existing rows default to `user`; rows
referenced by a Profile Enrichment child link and carrying a parent job config
are migrated to `internal`.

Task Center queries:

- default to `visibility=user` and `dismissed_at IS NULL`;
- return all non-terminal jobs before terminal history;
- paginate terminal history at 20 per page;
- keep internal children out unless `include_internal=true`;
- count only active work in the running badge.

Terminal dismiss APIs are available at:

```text
DELETE /api/persona-creation/jobs/{job_id}
DELETE /api/profile-enrichment/jobs/{job_id}
POST   /api/background-jobs/cleanup
GET    /api/background-jobs?page=1&page_size=20
```

Dismiss never cancels or physically deletes data. It returns `409
job_is_not_terminal` for running, queued, researching, compiling, paused, or
other non-terminal jobs. Retry creates a new lineage row and retains the old
failure record.

The UI adds All / Active / Failed / Completed categories, active and failed
counts, terminal cleanup confirmation, terminal-specific buttons, and active-
first rendering without `jobs.slice(0, 20)` hiding running work.

## Verification boundary

Implementation initially stopped before runtime validation as requested. After
review approval, the automated validation gates were run and are recorded in
`ACP_JOB_CENTER_VALIDATION.md`. Live ACP Agent and real Persona Enrichment
acceptance remain separate physical-runtime gates.
