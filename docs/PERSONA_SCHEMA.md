# Persona Schema

Each persona package has:

```text
manifest.yaml
package_schema.json
checksums.json
data/
  personas.jsonl        # optional for multi-persona room bundles
  sources.jsonl
  claims.jsonl
  memories.jsonl
  affect_states.jsonl
  needs.jsonl
  relationships.jsonl
  sessions.jsonl
  session_turns.jsonl
  compilation_tasks.jsonl
  continuations.jsonl
  continuation_branches.jsonl
  change_events.jsonl
  change_event_supports.jsonl
files/
  identity/
  cognition/
  affect/
  expression/
  relationships/
  evidence/
  continuation/
  evaluation/
  runtime/
```

`manifest.yaml` stores persona id, display name, aliases, type, version, dates,
run mode, active status, dimension confidence, source count, continuation flags,
main branch, and sensitivity.

All `data/*.jsonl` records have `schema_version: "1.1"` and a `data` object.
`checksums.json` stores SHA-256 hashes for `manifest.yaml`,
`package_schema.json`, every `data/*` file, and every `files/*` file. Import
rejects checksum or schema mismatches and runs in a transaction.

`room_export_mode="bundle"` adds secondary persona rows to `data/personas.jsonl`
and remaps all persona ids on import. The default single-persona export omits
active rooms that would otherwise reference personas not present in the package.

Compiled V1.1 personas write non-empty eight-layer files:

- identity profile, timeline, self narrative, and boundaries
- cognition mental models, decision heuristics, values, contradictions, failure patterns
- affect temperament, triggers, attachment, needs, defenses
- expression style, vocabulary, dialogue examples, anti-patterns
- relationships, evidence, continuation, evaluation, and runtime files

Counterfactual branch compilation writes branch-specific state under
`files/continuation/branches/<branch_id>/` and records compiled components with
branch provenance. Base historical identity and self-narrative files are not
mutated by simulated branch deltas.

Runtime state is branch-scoped:

```text
files/runtime/branches/main/runtime_state.json
files/runtime/branches/<branch_id>/runtime_state.json
```

`affect_states`, `needs`, `relationships`, and `change_events` carry
`branch_id`; `change_event_supports` carries all supporting session/turn pairs
for reflection-derived change events.
