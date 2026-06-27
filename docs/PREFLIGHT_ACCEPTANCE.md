# Preflight Acceptance Report

Date: 2026-06-26

Scope: Persona Continuum V1.1-RC5 branch runtime isolation, reflection branch
binding, atomic commit rollback, multi-session reflection lineage, database
upgrade, and release source-package acceptance.

## Current Counts

- MCP tools: 57 (`scripts/preflight_acceptance.py`)
- Tests: 91 pytest tests
- New RC5 regression tests: 10 in
  `tests/integration/test_v11_rc5_branch_runtime_atomicity.py`

## New Failing Tests Added First

The RC5 regression file was added before implementation and initially failed
against RC4 behavior. The observed red state included:

- branch B reading branch A anger from global `affect_states`
- `persona_prepare_reflection` and `persona_commit_reflection` missing
  `branch_id`
- invalid `state_patch` raising after turn/memory writes had already started
- legacy tables not upgrading with `branch_id`

The RC5 tests now cover:

- affect, need, and relationship isolation between sibling branches
- goal, self narrative, unresolved conflict, and runtime state isolation
- `prepare_reflection` filtering to requested branch and optional sessions
- `commit_reflection` rejecting mixed-branch supporting turns
- reflection memories and deltas being written to the target branch
- invalid `commit_turn` rolling back turn, memory, change event, and runtime
  mutations
- invalid `commit_reflection` rolling back insight memory, relationship delta,
  change event, and runtime mutations
- multi-session reflection storing all support edges in `change_event_supports`
- deleting one support session preserving multi-session reflection conclusions
  until the final support is deleted
- migration from legacy runtime tables to `branch_id='main'`

## Runtime Isolation Evidence

Runtime state is now branch-scoped:

- `affect_states`: primary key `(persona_id, branch_id, name, kind)`
- `needs`: primary key `(persona_id, branch_id, name)`
- `relationships`: primary key `(persona_id, branch_id, counterpart)`
- `change_events`: includes `branch_id`
- files: `runtime/branches/<branch_id>/runtime_state.json`

The preflight service flow now verifies:

- branch A can set `anger=0.9` and Alice `trust=0.8`
- branch B prepare does not read that anger or trust
- invalid `commit_turn` with malformed `state_patch` produces no new
  `session_turns`, `memories`, or `change_events`

## Reflection Evidence

- `persona_prepare_reflection(persona_id, branch_id, session_ids?, limit)` only
  returns turns from the requested branch and selected sessions.
- `persona_commit_reflection(..., branch_id)` validates a strict nested schema
  before writes.
- Supporting turn ids must be unique and belong to the submitted branch.
- Relationship delta keys are restricted to known relationship fields.
- Affect, need, importance, confidence, and severity values are range checked.
- Reflection memories, relationship/affect/need/goal/conflict/self-narrative
  deltas, and runtime files are written to the target branch.
- `change_event_supports` records all supporting session/turn pairs. Deleting
  one support removes only that edge; events with remaining support are replayed
  and retained.

## Package Integrity

- Checksums cover `manifest.yaml`, `package_schema.json`, all `data/*`, all
  `files/*`, and redaction manifests.
- `data/change_event_supports.jsonl` is exported/imported with event, session,
  and turn ids remapped.
- Single-persona full export omits active cross-persona rooms by default.
- `room_export_mode="bundle"` includes secondary personas in
  `data/personas.jsonl` and remaps persona/session/room ids on import.
- Source package build uses a top-level allowlist and rejects any path component
  containing `venv`, plus caches, databases, `personas`, `exports`, `__MACOSX`,
  and `.DS_Store`.

Formal source package command:

```bash
python scripts/build_source_package.py --output persona-continuum-v1.1-source.zip
```

Result: 107 members, bad_count 0.

## Verification Commands

```bash
.venv/bin/pytest -q --tb=short
.venv/bin/ruff check .
.venv/bin/mypy
env PERSONA_CONTINUUM_HOME=/tmp/persona-continuum-doctor .venv/bin/persona-continuum doctor --json
.venv/bin/python scripts/preflight_acceptance.py
python scripts/build_source_package.py --output persona-continuum-v1.1-source.zip
```

Current results:

- `pytest`: 91 passed, 1 expected duplicate-ZIP-member warning in checksum
  tampering coverage
- `ruff`: all checks passed
- `mypy`: success, 78 source files
- `doctor`: ok with SQLite FTS5, writable temporary data directory, and Skill
  present
- `preflight`: ok, tool_count 57, branch runtime isolation ok, atomic commit
  rollback ok
- source ZIP scan: 107 members, bad_count 0

## Remaining Limits

- No LLM API, embedding API, cloud database, Docker, GUI, voice, or avatar is
  included.
- `persona_run_reflection` remains an extractive fallback; semantic reflection
  must use branch-bound `persona_prepare_reflection` and
  `persona_commit_reflection`.
- Runtime component retrieval is local keyword and evidence ranking, not neural
  embedding search.
- Structural checks are not persona-quality scores. Quality evaluation requires
  host-agent benchmark cases and committed results.
