# Private Persona Material Intelligence

Persona Continuum keeps private material local-first and evidence-traceable. The
existing `EvidenceSource` row is the immutable raw source and remains the
provenance root. `MaterialIntelligenceService` adds a rebuildable derived layer
before the existing eight-dimension `CompilationService`.

## Pipeline

```text
Raw EvidenceSource
  -> format-aware segmentation
  -> PersonaEvidenceUnit (paragraph/message/row, source locator, verbatim text)
  -> exact and near-duplicate EvidenceCluster
  -> FusedEvidence (semantic union, all supporting source/evidence IDs)
  -> PersonaContradiction (fact conflict, temporal change, context dependence)
  -> ConversationEpisode / PersonaEvidenceIndex
  -> dimension retrieval
  -> ResearchArtifact
  -> existing eight-dimension compiler
```

Supported files continue to be `.txt`, `.md`, `.json`, `.jsonl`, `.csv`,
`.html`, `.htm`, `.pdf`, `.docx` and ZIP bundles accepted by `SourceLoader`.
JSON/JSONL/CSV chat rows are segmented into messages; text documents are split
into paragraphs or speaker-prefixed turns. Each derived unit retains source ID,
path/row/segment locator and the original verbatim text.

## Deduplication and fusion

Exact normalized text is clustered first. Conservative token/containment
similarity identifies near duplicates without requiring a remote embedding
service. Fusion keeps shared information once, retains unique sentences from
all members, and stores every supporting evidence ID and source ID. It never
deletes or rewrites a raw source.

Contradictions are first-class records. Different numbers or negation are not
silently overwritten. If event dates differ the record is `TEMPORAL_CHANGE`;
different context/relationship tags produce `CONTEXT_DEPENDENT`; self-report
and third-party material remain distinguishable. The default resolution is
`preserve_both_with_scope`.

## Retrieval and guided interview

`PersonaEvidenceIndex.retrieve()` searches the complete derived corpus and
diversifies by source and time. It is intentionally not a `sources[-12:]`
window. Dimension, relationship, life-stage, behavior, expression-sample and
contradiction helpers are available to the Persona Creation Runtime.

Coverage counts messages, episodes, behavior examples, relationships, life
events, expression samples, decisions, contradictions and eight-dimension
evidence. Guided interview questions are generated from remaining gaps; “不
清楚” is accepted and is not converted into an invented fact.

## Jobs and privacy

`persona_material_jobs` persists progress through `UPLOADED`, `PARSING`,
`SEGMENTING`, `ANALYZING`, `CLUSTERING`, `FUSING`, `INDEXING`, `GAP_ANALYSIS`,
`READY_FOR_COMPILATION` and `FAILED`. The job stores only runtime metadata and
counts in the public progress payload; uploaded material is never echoed by the
web API job serializer. The existing private-material remote consent gate is
unchanged.

The API exposes the progress and local inspector through:

* `GET /api/personas/{persona_id}/evidence-index`
* `POST /api/personas/{persona_id}/material-analysis`
* `GET /api/persona-material/jobs`
* `GET /api/persona-material/jobs/{job_id}`

Persona creation and profile enrichment call this service before compiling.
Existing personas and old package exports remain compatible; derived tables are
empty until analysis is requested and can be rebuilt from `sources`.

材料 Agent 阶段使用统一 `AgentResponseCollector`。空的 `DONE` 不再视为成功：
任务会以带有 `phase` 的 typed failure 停止，保留已完成的 parsing、segmenting、
classification、relation、fusion checkpoint，用户可在任务中心使用同一 runtime
重试，而不是重新上传或静默跳过材料分析。
