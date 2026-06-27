# Persona Continuum

Persona Continuum is a local-first digital persona platform for Agent hosts
such as Codex, Claude Code, Cursor, and OpenCode. It provides a Python package,
CLI, MCP server, and Agent Skill for creating, storing, running, evaluating,
exporting, and importing structured persona packages.

## What It Is

- A local persona repository backed by SQLite and SQLite FTS5.
- A persona compiler that stores evidence-backed artifacts submitted by the host Agent.
- A runtime that prepares structured turn context and commits memory, affect,
  need, relationship, and session state.
- A host-artifact counterfactual continuation engine that keeps simulated data separate from history.
- A CLI and stdio MCP server for Agent hosts.

## What It Is Not

- Not a cloud LLM wrapper.
- Not an embedding API client.
- Not a role-play prompt generator that hides evidence boundaries.
- Not a claim that simulated continuations are historical facts.

## No LLM API Mechanism

Persona Continuum does not require or call OpenAI, Anthropic, Gemini, DeepSeek,
OpenAI-compatible, cloud embedding, Zep Cloud, or other paid model APIs. The
running Agent host performs natural-language research and reasoning, then
submits structured artifacts to the local MCP server.

## Install

```bash
uv sync
uv run persona-continuum init
uv run persona-continuum doctor --json
```

## Codex MCP Configuration

Codex stores MCP servers in `config.toml`. For stdio:

```toml
[mcp_servers.persona_continuum]
command = "uv"
args = ["run", "persona-continuum-mcp"]
cwd = "/absolute/path/to/persona-continuum"
startup_timeout_sec = 20
tool_timeout_sec = 60
```

## Skill Installation

Repo-scoped Codex skills are discovered from `.agents/skills` or parent skill
locations. For this project, copy or symlink:

```bash
mkdir -p .agents/skills
ln -s "$(pwd)/skills/persona-continuum" .agents/skills/persona-continuum
```

Codex also supports user skills under `$HOME/.agents/skills`.

## First Run

```bash
uv run persona-continuum persona create "Alex Chen"
uv run persona-continuum persona list
```

## Create A Persona

Through MCP, the host Agent should call:

1. `persona_create`
2. `persona_add_sources`
   or `persona_add_source_text` for structured web research text
3. `persona_create_compilation_task`
4. `persona_submit_research_artifact`
5. `persona_compile`
6. `persona_structural_check` for structural completeness
7. `evaluation_create_suite` / `evaluation_prepare_case` / `evaluation_commit_result`
   for host-driven persona quality benchmarks

## Chat With A Persona

Use `persona_start_session`, `persona_prepare_turn`, generate the final reply in
the host Agent, then call `persona_commit_turn`.

## Upload Sources

Supported formats: `.txt`, `.md`, `.json`, `.jsonl`, `.csv`, `.html`, `.docx`,
text-extractable `.pdf`, and `.zip` containing those files. ZIP path traversal
and oversized files are rejected. ZIP members are parsed with their own format
parsers; DOCX/PDF bytes are never decoded as UTF-8 text.

## Persona Packages

V1.1 exports use:

```text
manifest.yaml
package_schema.json
checksums.json
data/*.jsonl
files/{identity,cognition,affect,expression,evidence,continuation,evaluation,runtime}
```

Each `data/*.jsonl` record carries `schema_version: "1.1"`. `checksums.json`
covers `manifest.yaml`, `package_schema.json`, every data file, and every
persona file. Imports reject duplicate ZIP members, unchecked files, modified
component files, invalid schema, unsafe paths, and checksum mismatches before
transactional import. Modes are `full`, `identity_only`, and `redacted`.
Single-persona `full` export omits active cross-persona rooms by default to
avoid dangling sessions; use `room_export_mode="bundle"` when a multi-persona
room and all room personas must be portable together.

## Counterfactual Continuation

Use `continuation_create`, add world events, create branches, then call
`continuation_prepare_step`. The MCP server returns constraints and an artifact
schema; the host Agent must call `continuation_commit_step` with structured
state deltas before any branch advances. Without that artifact, branches stay
`waiting_for_host`. A branch can be selected or compiled only after at least one
valid host step has been committed; branch compilation writes isolated
counterfactual files under `files/continuation/branches/<branch_id>/` and does
not append simulated events to base historical identity files.

## Reflection

`persona_prepare_reflection` returns recent turns, affect, relationships, needs,
goals, activated memory slots, host questions, and an output schema.
Pass `branch_id` to bind reflection to a single branch. By default reflection
uses the persona's current main branch or `main`; it does not mix sibling branch
turns. `persona_commit_reflection` validates and persists host semantic
reflection atomically.
`persona_run_reflection` remains an extractive local fallback and is marked
`reflection_type=extractive_fallback`.

## Data Location

Default data lives under `~/.persona-continuum`. Override with:

```bash
PERSONA_CONTINUUM_HOME=/path/to/data uv run persona-continuum doctor
```

## Privacy

Private materials stay local by default. Export is explicit. For private real
people, obtain appropriate consent and consider identity, privacy, likeness,
voice, and distribution risks.

Source-level deletion removes the source row, invalidates directly supported
claims and memories, recursively removes source-derived artifacts, compiled
components, snapshots, task artifact payloads, and lineage, rewrites evidence
files, removes FTS entries, and marks the persona `needs_recompile`. Session
deletion can delete derived digital-experience, reflection, relationship-update,
and unresolved-event data.

Runtime affect, needs, relationships, active goals, self narrative updates,
unresolved conflicts, and reflection insights are branch-scoped. Branch runtime
files live under `runtime/branches/<branch_id>/runtime_state.json`; siblings do
not read each other's runtime state, and child branches inherit only a snapshot
from their parent at creation.

Deleting a persona removes its database rows, FTS entries, lineage, runtime
state, continuation/evaluation state, package directory, and room sessions. If a
deleted persona was in a room, the transcript marks those turns as deleted, the
speaker order is repaired, and empty rooms are closed.

## Known Limits

- Audio transcription, video understanding, and image OCR are extension points,
  not implemented features.
- Public research, semantic reflection, benchmark judging, and continuation
  reasoning must be performed by the host Agent, not by the MCP server.
- `persona_structural_check` reports structural completeness. `persona_validate`
  remains a compatibility alias. Persona quality requires the host-driven
  evaluation suite.
