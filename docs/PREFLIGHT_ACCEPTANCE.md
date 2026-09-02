# Preflight Acceptance Report

Date: 2026-08-23

Scope: Persona Continuum Tavern Runtime final official-protocol fix for Codex
app-server V2 model discovery, Grok ACP authentication/readiness, Generic ACP
authentication conformance, and reasoning capability honesty. Previously passed
Room, Persona, Recall Gate, Director, Random Resolver, Web UI, Tool Calling, and
Transaction Integrity behavior remains covered by the full suite.

## Current Counts

- MCP tools: 57 (`scripts/preflight_acceptance.py`)
- Tests: 167 pytest tests
- New official-protocol regression tests: 20 in
  `tests/unit/test_final_official_protocol_fix.py`

## Official Protocol Red Baseline

The new regression file was run before implementation. The old adapter code
produced 14 failures and 4 passes. The failures reproduced:

- Codex object-shaped `supportedReasoningEfforts` rejection, unknown effort
  loss, missing pagination, and silent fallback after schema errors
- non-standard ACP initialize fields and secrets in `authenticate.params.token`
- Grok probe treating any advertised auth method as immediately auth-required
- incorrect Grok 4.5/4.6 fallback reasoning capabilities
- fabricated Generic ACP, Cursor, Manifest, and JSON-RPC reasoning metadata
- JSON-RPC version success being reported as READY without a protocol smoke

## Official Protocol Gates

`uv run python scripts/preflight_acceptance.py` executes exact test nodes for
each gate. Current result:

| Gate | Result | Evidence |
|---|---|---|
| `codex_model_list_official_schema` | PASS | 5 tests |
| `grok_official_acp_auth` | PASS | 1 strict-shape test |
| `grok_probe_authenticated_ready` | PASS | 3 tests |
| `generic_acp_auth_conformance` | PASS | 3 tests |
| `generic_reasoning_honesty` | PASS | 2 tests |
| `cursor_reasoning_honesty` | PASS | 2 tests |
| `manifest_reasoning_honesty` | PASS | 1 test |

Preflight output: `overall_status = COMPLETED`.

## Protocol Behavior

- Codex `model/list` consumes V2 `data` plus `nextCursor` until null. Object
  reasoning options are converted from `reasoningEffort`, unknown strings are
  preserved, and `defaultReasoningEffort` is retained verbatim.
- Codex model schema/protocol failures are exposed as
  `model_discovery_error`; the UI-facing fallback is `Agent Default` with
  `source=manual`, never a fake dynamic discovery result.
- ACP initialize advertises only `protocolVersion` and implemented
  `clientCapabilities`.
- Grok chooses `xai.api_key` only when `XAI_API_KEY` is available, otherwise
  `cached_token` when advertised. Authenticate sends only `methodId` and
  `_meta.headless`; no secret is serialized into ACP JSON-RPC.
- Generic ACP handles agent/default, API-key-via-environment, cached, and
  terminal auth types. Unsupported terminal login returns
  `INTERACTIVE_AUTH_REQUIRED` without calling `authenticate`.
- ACP READY requires initialize, any required authentication, and a successful
  `session/new`; probe never calls `session/prompt`.
- Grok 4.6 supports exactly `low, medium, high, xhigh`; Grok 4.5 supports
  exactly `low, medium, high`; both default to `high`.
- Unknown ACP/Cursor/Manifest/JSON-RPC models have no invented reasoning
  efforts. A bare `--effort` flag does not imply allowed values.

## Preserved RC5 Coverage

The existing RC5 suite continues to cover:

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

Result: 169 members, bad_count 0; content privacy scan found no local user path
or credential-shaped fixture value.

## Verification Commands

```bash
uv run pytest
uv run ruff check .
uv run mypy
uv run python scripts/preflight_acceptance.py
python scripts/build_source_package.py --output persona-continuum-v1.1-source.zip
```

Current results:

- `pytest`: 167 passed; 2 non-failing warnings (Starlette TestClient
  deprecation and the expected duplicate-ZIP-member checksum tampering case)
- `ruff`: all checks passed
- `mypy`: success, 114 source files
- `doctor`: ok through preflight with SQLite FTS5, writable temporary data
  directory, and Skill present
- `preflight`: `overall_status=COMPLETED`, tool_count 57, all seven official
  protocol gates PASS, branch runtime isolation ok, atomic commit rollback ok
- source ZIP scan: 169 members, bad_count 0, privacy scan clean

## Remaining Limits

- No LLM API, embedding API, cloud database, or Docker dependency was added;
  this protocol fix adds no new GUI, voice, or avatar feature.
- `persona_run_reflection` remains an extractive fallback; semantic reflection
  must use branch-bound `persona_prepare_reflection` and
  `persona_commit_reflection`.
- Runtime component retrieval is local keyword and evidence ranking, not neural
  embedding search.
- Structural checks are not persona-quality scores. Quality evaluation requires
  host-agent benchmark cases and committed results.
