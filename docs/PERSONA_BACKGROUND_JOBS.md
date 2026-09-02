# Persona Background Jobs

Persona creation and profile enrichment are durable application-layer jobs. A
POST creates and persists a job snapshot, then returns `202` without waiting
for an Agent turn. The worker resumes after a process restart from the last
checkpoint and never changes the selected Agent, Model, reasoning effort, or
credential binding.

## Lifecycle

`created → planning → researching/ingesting_sources → extracting → compiling → completed`

`completed_with_gaps` is an honest terminal state. Runtime disappearance is
`paused_runtime_unavailable`; explicit pause is resumable. Agent transport,
empty-output, structured-output, and timeout failures are persisted as typed
failure records and never advance to compile or 100%.

The Task Center uses `GET /api/background-jobs` with active-job priority:
all non-terminal user jobs are returned first, followed by a paginated
terminal history page. Internal Persona Creation children created by Profile
Enrichment are marked `visibility=internal` and are available only with
`include_internal=true`; they do not create a second user task card. The
active badge excludes failed, completed, cancelled, and paused history.

Closing a dialog only closes the view; only the explicit Cancel action changes
an active job. Terminal records are removed from the Task Center with
`DELETE /api/persona-creation/jobs/{job_id}` or
`DELETE /api/profile-enrichment/jobs/{job_id}`. This is a dismiss operation,
not a physical delete: `dismissed_at`, failure data, runtime snapshots,
Persona/evidence rows, and parent/child lineage remain available. A non-
terminal job returns `409 job_is_not_terminal`. Bulk cleanup uses
`POST /api/background-jobs/cleanup` and applies the same terminal-only rule.

Retry is offered only for a persisted retriable failure. It creates a new run
with the same Agent/Model/reasoning binding, preserves the failed row, and
sets the old row's `superseded_by` to the new job ID.

## Progress and failure

Every job exposes `progress.stage`, `label`, `percent`, `indeterminate`,
`current_item`, `completed`, `total`, `message`, and optional `failure`.
Agent calls expose bounded audit metadata (call id, phase, runtime binding,
duration, event counts, output characters, protocol, usage, frame diagnostics,
and failure code). Prompts and private material contents are never returned by
the public job payload. Large internal tool results are represented at the
application/UI boundary by an artifact reference, redacted preview, character
count, and evidence IDs; the authoritative Evidence/tool payload is retained
for the model and persistence path.
