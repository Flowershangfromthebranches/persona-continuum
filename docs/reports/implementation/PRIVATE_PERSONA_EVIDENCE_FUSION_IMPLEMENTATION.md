# Private Persona Evidence Fusion Implementation

## Scope

This implementation upgrades private Persona Creation and Persona Enrichment
with a provenance-preserving Material Intelligence / Evidence Layer. It does
not add a second Persona format and does not replace the existing Persona,
EvidenceSource, ResearchArtifact or eight-dimensional Compilation pipeline.

## Added and modified files

* `src/persona_continuum/application/material_intelligence.py` — material job
  lifecycle, format-aware segmentation, EvidenceUnit, ConversationEpisode,
  duplicate clustering, FusedEvidence, Contradiction, identity aliases,
  coverage and full-corpus retrieval index.
* `src/persona_continuum/storage/migrations.py` — additive tables and indexes
  for material jobs, evidence units/clusters, fused evidence, contradictions,
  episodes and aliases.
* `src/persona_continuum/application/container.py` — registers the service.
* `src/persona_continuum/application/persona_creation_service.py` — private
  files and guided interview answers flow through material analysis; dimension
  extraction retrieves Evidence Index results rather than `sources[-12:]`; gap
  questions use material coverage.
* `src/persona_continuum/application/persona_service.py` — package export/import
  and deletion include derived Evidence Layer rows while raw sources remain
  authoritative.
* `src/persona_continuum/web/api.py`, `web/server.py` — local evidence index,
  material analysis and job progress endpoints.
* `src/persona_continuum/web/static/index.html`, `app.js` — creation progress
  counters and a local Material Evidence Layer inspector.
* `tests/integration/test_private_material_intelligence.py` — required test
  coverage for parsing, provenance, deduplication, semantic union,
  contradiction preservation, identity, retrieval, coverage and enrichment.

## Data model and provenance

`EvidenceSource` rows are never overwritten or deleted by analysis. Each
`persona_evidence_units` row contains a source ID, locator, speaker/time,
verbatim text, normalized matching text, dimension candidates and metadata.
Clusters and fused rows retain member evidence IDs and all source IDs. The
compiler still receives formal `ResearchArtifact` records whose claim/memory
source IDs point to real `EvidenceSource` rows.

## Runtime flow

1. Persona Creation/Enrichment ingests files through `SourceLoader` and
   `PersonaService`.
2. Material Intelligence segments messages, rows and paragraphs.
3. Exact and near duplicates are clustered; similar units are unioned without
   losing unique statements or provenance.
4. Contradictions are classified as fact conflicts, temporal changes,
   context-dependent behavior or self/third-party differences.
5. `PersonaEvidenceIndex` retrieves the full corpus with source/time diversity.
6. Existing Agent-backed eight-dimension ResearchArtifact extraction and
   `CompilationService` continue unchanged as the formal compilation seam.
7. Coverage gaps drive Guided Interview questions; private gaps remain visible
   as draft / completed-with-gaps instead of being filled by model imagination.

## API and UI

Added endpoints:

* `GET /api/personas/{persona_id}/evidence-index`
* `POST /api/personas/{persona_id}/material-analysis`
* `GET /api/persona-material/jobs`
* `GET /api/persona-material/jobs/{job_id}`

The Persona Creation progress card displays unit, fused-evidence and
contradiction counts plus a local inspector for coverage and gaps. Existing
remote private-material consent behavior remains in force.

## Backward compatibility

New tables are additive `CREATE TABLE IF NOT EXISTS` migrations. Existing
Personas, sources, compiled manifests, memories and package exports remain
loadable. Old Personas can compile from raw sources before their derived index
is rebuilt. Full package export/import remaps new derived IDs and preserves
source provenance; redacted export continues to omit private content.

## Tests written

`tests/integration/test_private_material_intelligence.py` includes tests for
mixed file ingestion, chat message segmentation, locators, exact/near dedup,
independence, semantic union, provenance, contradictions and conditional
patterns, identity aliases, full-corpus/diverse retrieval, message/episode
coverage, guided interview gaps, compiler evidence, expression verbatim
samples and incremental enrichment/version preservation.

## Review remediation

The post-implementation review was completed. Its blocking findings were
resolved:

* CSV remains tabular through `SourceLoader` and is parsed once with its header.
* Persona Creation now invokes its selected Agent Adapter for evidence
  classification, semantic relation analysis and constrained fusion.
* CPU-heavy segmentation/clustering/fusion has an asynchronous application
  path with stage events; the deterministic direct API remains available for
  local index rebuilds.
* Derived evidence rows are replaced in one transaction, preserving the prior
  index if a rebuild fails.
* Candidate-indexed clustering avoids all-pairs comparison on large corpora.
* Only chat imports and guided interviews create conversation episodes; guided
  interview answers count as messages.
* Dimension gaps and non-dimension evidence gaps are represented separately.
* Tests now exercise the real CSV loader, compiler-facing fused retrieval,
  Agent-assisted semantics and transaction rollback.

## Validation status

Validation completed on 2026-08-25:

* `uv run pytest -q`: **376 passed, 2 skipped, 1 warning** in 409.48 seconds.
* Material Intelligence integration suite: **32 passed**.
* Previously order-sensitive Chinese near-duplicate regression: **20/20
  repeated runs passed**.
* `uv run ruff check .`: **passed**.
* `uv run mypy`: **passed**, 155 source files checked.
* `git diff --check`: **passed**.

The single pytest warning is expected from the existing ZIP tampering test,
which deliberately creates a duplicate `data/sources.jsonl` member to verify
import rollback behavior. No real Provider, external LLM, web research, or
private-material upload was used during validation.
