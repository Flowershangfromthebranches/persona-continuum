# CLI Web Research Capability Implementation

## Scope

This implementation removes the static false-negative gate that rejected a
READY local CLI when its adapter did not declare web tools. Capability status is
now explicit and behavioral verification is separate from adapter metadata.

## Runtime behavior

- `ResearchCapability.verification_status` supports `declared`, `verified`,
  `unknown`, and `unavailable`.
- Plain local CLI adapters default to `mode=agentic_cli` and `unknown`.
- Gemini remains a declared native route until a real probe verifies it.
- Codex help/doctor text is only a declaration hint; absent tokens remain
  `unknown`.
- READY local CLI runtimes enter `AgenticCliResearchBackend` when capability is
  unknown or only declared.
- The probe starts the selected Agent session, requests a real OpenAI-hosted
  URL, and independently validates URL host, HTTP status and non-empty body.
- A failed probe is cached as `unavailable` with an actionable diagnostic.
- A successful probe is cached as `verified` and reused for the exact Agent
  version, Model and runtime source tuple.
- API runtimes never receive the local CLI behavioral probe; they still require
  explicit native capability, Research Broker, or MCP.
- `local_materials` creation/enrichment never invokes research capability
  validation or a research backend, including for public Personas.

## Files changed

- `src/persona_continuum/agent/models.py`
  - Added verification status and source-discovery/read contracts.
- `src/persona_continuum/agent/protocols/plain_cli.py`
  - Unknown agentic CLI default and effective source capability flags.
- `src/persona_continuum/agent/adapters/codex.py`
  - Help/doctor hints no longer become an unavailable verdict.
- `src/persona_continuum/agent/adapters/gemini.py`
  - Native capability is marked declared until behavioral verification.
- `src/persona_continuum/agent/manifest_adapter.py`
  - Unreported manifest CLI capability is represented as unknown.
- `src/persona_continuum/agent/discovery.py`
  - Legacy probe shapes are normalized and cached verification is exposed to
    diagnostics.
- `src/persona_continuum/application/research_backend.py`
  - Added `AgenticCliResearchBackend`, behavioral probe, independent URL
    validation and resolver fallback behavior.
- `src/persona_continuum/application/research_capability_cache.py`
  - Added durable capability cache keyed by runtime binding snapshot.
- `src/persona_continuum/application/persona_creation_service.py`
  - Removed preflight static rejection, resolved/verified research backends
    asynchronously, persisted verification metadata, and bypassed web checks
    for local-material-only jobs.
- `src/persona_continuum/application/profile_enrichment_service.py`
  - Passes explicit enrichment input mode to research validation.
- `src/persona_continuum/application/container.py`
  - Wires the cache into discovery and Persona Creation.
- `src/persona_continuum/storage/migrations.py`
  - Added `research_capability_cache` table and index.
- `src/persona_continuum/web/static/app.js`
  - Shows unknown/declared/verified/unavailable diagnostics without calling
    unknown capability fail-closed.
- `docs/PERSONA_ENRICHMENT_RUNTIME.md`
  - Documents behavioral verification, evidence validation and cache semantics.
- `docs/AGENT_ADAPTERS.md`
  - Documents the capability status contract for adapter authors.

## Tests and fixtures written

The focused capability tests are added in
`tests/integration/test_cli_research_capability.py` and cover unknown READY
CLI handling, behavioral probing, verifiable URLs, cache invalidation by
version, Codex hints, API strictness, local-material bypass and frontend
diagnostic copy.

## Review boundary

Implementation, migration, frontend changes, tests and documentation are
complete for this pass. No tests, lint, type checks, browser checks, server
startup, real CLI calls, provider calls, or real web research were executed in
this implementation phase.
