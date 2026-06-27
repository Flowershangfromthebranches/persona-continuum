# Architecture

Persona Continuum is layered as:

`Agent Host -> Skill -> MCP Server -> Application Services -> Repositories -> SQLite + local files`.

The MCP server is a thin adapter. Application services own workflow behavior.
Domain models define strict Pydantic contracts. SQLite stores durable state and
FTS5 indexes memories. Persona package files mirror the durable state for import,
export, inspection, and long-term portability.

## V1.1 Storage

SQLite runs with foreign keys, WAL, and `busy_timeout`. Application services
hold workflow state; the MCP server owns one application context per server
lifetime instead of rebuilding the app on every tool call.

Core tables:

- `sources`, `claims`, `memories`, `affect_states`, `needs`, `relationships`
- `sessions`, `session_turns`, `continuations`, `continuation_branches`, `rooms`
- `compilation_tasks`, `research_artifacts`, `compiled_components`, `compile_snapshots`
- `lineage`, `change_events`, `change_event_supports`
- `evaluation_suites`, `evaluation_cases`, `evaluation_results`

`lineage` records parent-child edges such as source -> research artifact ->
claim/memory/component and session -> turn -> digital experience memory. Delete
operations use explicit ids and metadata to invalidate or remove affected data.
Persona deletion also removes FTS rows, runtime state, continuation/evaluation
rows, package files, and repairs or closes rooms that referenced the persona.

`affect_states`, `needs`, `relationships`, and `change_events` include
`branch_id`. Runtime files are written per branch at
`runtime/branches/<branch_id>/runtime_state.json`. `change_event_supports`
stores all session/turn supports for reflection events so deleting one support
session does not incorrectly revoke multi-session conclusions.

## Compilation

Compilation accepts V1.1 research artifacts with strict Pydantic validation:
dimension enum, source ids that belong to the persona, confidence ranges, claim
types, extracted components, conflicts, uncertainty, creator, and artifact hash.
Legacy artifacts are normalized for backward compatibility, but declared V1.1
artifacts are strict.

`compile_persona` is idempotent for completed tasks. It deduplicates claims and
memories, writes eight-layer persona files, stores compiled components, bumps
persona version, records a compile snapshot, and returns `completed_with_gaps`
when required dimensions are missing.

Research artifacts store both the host-provided `artifact_hash` and an
independent canonical SHA-256 of the normalized artifact payload. The canonical
hash is indexed for idempotent duplicate detection and hash-conflict rejection.

## Import And Export

Export stages a persona package:

```text
manifest.yaml
package_schema.json
checksums.json
data/*.jsonl
files/
```

Import validates paths, schema, checksums, required files, persona id, and
foreign-key remapping. A `new_id` remaps all `persona_id` fields and known
internal ids. Import runs in a transaction and removes extracted files on
rollback, then rebuilds FTS and runs consistency checks.

Single-persona full export omits active cross-persona rooms by default.
`room_export_mode="bundle"` includes secondary personas and room state so the
imported bundle can continue running without dangling persona references.

## Host-Agent Workflows

Reflection and continuation are two-stage host workflows. Prepare methods return
local state and a strict artifact schema. Commit methods validate and persist the
host artifact in a transaction. Reflection prepare/commit is branch-bound; mixed
supporting turns from sibling branches are rejected. Local extractive reflection
remains available but is explicitly marked as fallback.

Evaluation separates structural completeness from persona quality. The
benchmark suite stores cases and host-judged results for factual QA, decision
replay, refusal to fabricate, expression, continuity, memory recall, attacks,
and historical/counterfactual separation.
