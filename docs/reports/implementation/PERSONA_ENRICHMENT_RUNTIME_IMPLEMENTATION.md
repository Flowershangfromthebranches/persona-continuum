# Persona Enrichment Runtime Implementation

Implemented the requested runtime repair without replacing Persona,
EvidenceSource, Material Intelligence, CompilationService, AgentAdapter or
CredentialManager.

## Changes

- Added adapter-level `ResearchCapability` and native Gemini/Codex probing.
- Added `NativeCliResearchBackend` and fail-closed native → Broker → MCP
  resolution.
- Added `enrichment_input_mode` (`local_materials`, `web_research`, `hybrid`).
- Added immutable child enrichment runs with material snapshots and evidence
  delta fields; completed parent jobs remain terminal.
- Added material/dimension Agent invocation counters and completion gate.
- Added duplicate-only `NO_NEW_INFORMATION` handling.
- Added persisted enrichment progress events, runtime snapshot, material and
  dimension statistics, and a polling progress dialog.
- Added the input mode selector and real capability text to the enrichment UI.
- Added migration/backfill columns for enrichment run metadata.

## Compatibility

Existing Persona creation and profile APIs remain valid. If no input mode is
provided, enrichment defaults to `local_materials`; callers can opt into web or
hybrid research explicitly. Existing source/artifact/manifest provenance is
preserved.

## Validation note

Targeted existing asyncio tests, syntax compilation, Ruff checks for changed
files, and targeted MyPy checks were performed. The sandbox cannot bind the
loopback port used by browser fixtures and does not have Trio installed, so
those environment-bound checks are reported separately from product failures.
