# Profile Enrichment

Enrichment is a versioned application service, not a second Persona format.
`ProfileEnrichmentService` stores a `ProfileEnrichmentJob` with the target
profile, runtime snapshot, research policy, requested scope, progress and
result version.

## Scopes

`full_refresh`, `more_sources`, `fill_dimension_gaps`,
`fill_life_stage_gaps`, `enrich_relationships`, `enrich_expression`,
`enrich_decision_style`, and `add_user_materials` are supported scopes. The
scope is included in the Agent prompt and persisted for restart/resume.

For Persona targets the service delegates to the existing Persona Creation and
Compilation pipeline. Existing evidence and memories remain intact, new
sources/artifacts are appended, the Persona manifest version advances, and the
previous unified profile is copied into `profile_versions`.

For organizations, institutions and collectives the selected Agent returns a
typed JSON payload. The service validates the result envelope, regenerates the
summary, updates coverage and stores a new version. A failed Agent turn leaves
the job failed; it never silently downgrades to a generic card.

## API

`POST /api/profiles/{profile_id}/enrich` accepts
`enrichment_input_mode=local_materials|web_research|hybrid`. The response is
`202` with a persisted job; clients poll
`/api/profile-enrichment/jobs/{job_id}` until a terminal status. Progress
contains material stages, Agent call counts, dimension progress, evidence
delta and a `NO_NEW_INFORMATION` result when every supplied material is a
duplicate.

- `GET/POST /api/profiles`
- `GET /api/profiles/{profile_id}`
- `GET /api/profiles/{profile_id}/versions`
- `POST /api/profiles/{profile_id}/enrich`
- `POST /api/profiles/{profile_id}/archive`
- `GET /api/profile-enrichment/jobs`
- `GET /api/profile-enrichment/jobs/{job_id}`
- `POST /api/profile-enrichment/jobs/{job_id}/pause|resume|cancel`
- `POST /api/profile-enrichment/jobs/{job_id}/retry`（保持原 runtime binding，从最近持久化阶段重试）
- `WS /api/profile-enrichment/jobs/{job_id}/ws`（断线时使用自适应 polling）

The server resumes jobs that were interrupted while actively researching or
compiling. Explicitly paused and cancelled jobs stay in that state; if the
captured Agent runtime disappears, the job becomes
`paused_runtime_unavailable` instead of silently switching runtimes.

Private/user-provided materials are persisted only in the local job record and
are represented in API progress by a material count. Remote API runtimes require
an explicit `remote_material_consent` flag before a material-bearing job is
created or resumed.

进度窗口显示持久化 stage 映射的真实百分比，而不是按历史 stage 字符串累计。关闭、ESC 或 backdrop 只关闭窗口；暂停、继续和取消任务是独立操作，其中取消需要再次确认。失败时显示可诊断的 code、phase、protocol、last event、文本输出数量和脱敏诊断；仅 `retriable=true` 的失败显示“重试当前阶段”。

For Persona targets, new chat/files/interview answers also pass through
`MaterialIntelligenceService`: raw `EvidenceSource` -> message/paragraph
`EvidenceUnit` -> duplicate cluster -> provenance-preserving `FusedEvidence`
and `Contradiction` -> full-corpus dimension retrieval -> existing
eight-dimensional compiler. Enrichment creates a new profile/persona version
and retains the old version; the derived evidence layer is additive and
rebuildable from `sources`.

## Runtime and failure lineage

Non-Persona profile enrichment uses the shared `AgentRuntimeExecutor`, including
prompt rendering, context-budget batching, phase timeout, structured parsing,
and diagnostics. Its internally created Persona Creation run is marked
`visibility=internal` and is shown only through parent detail lineage.

If that child fails with a typed `failure_json`, the parent copies the child's
root `code`, `phase`, `protocol`, runtime, event counts, and retryability, then
adds `child_job_id` and parent context. The parent does not replace the root
failure with `PROFILE_ENRICHMENT_ERROR`.
# Runtime repair note

The detailed runtime contract is documented in
[`PERSONA_ENRICHMENT_RUNTIME.md`](PERSONA_ENRICHMENT_RUNTIME.md). In
particular, local material upgrades still invoke the selected Agent, while web
research is resolved from explicit adapter capability and never inferred from
the fact that a runtime is a CLI.
