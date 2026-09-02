# Persona Creation System Implementation

## 新增文件

- `src/persona_continuum/application/persona_creation_service.py` — `PersonaCreationOrchestrator`、job 状态、ResearchPolicy、ResearchToolBroker、Persona Match、私有资料与 Guided Interview。
- `docs/PERSONA_CREATION.md` — Public Research、Private Materials、Fictional、Coverage Gate、World Auto Completion 和 Runtime Binding 文档。
- `tests/integration/test_persona_creation_runtime.py` — Persona Creation、研究能力、来源 provenance、私有资料、重复保护与 enrichment 测试。
- `tests/integration/test_world_persona_completion.py` — Actor Persona Match、组织排除、缺失确认和自动创建测试。
- `tests/e2e/test_persona_creation_e2e.py` — 人物页、共享 Runtime Selector 与 Parallel World 确认流程契约测试。

## 修改文件

- `src/persona_continuum/application/container.py`：注册 Persona Creation application service。
- `src/persona_continuum/agent/models.py`：增加显式 `web_search`、`web_fetch`、`browser`、`research_mcp` capability flags。
- `src/persona_continuum/storage/migrations.py`：增加 `persona_creation_jobs` 及状态/人物索引。
- `src/persona_continuum/web/api.py`：增加 Persona Creation、events、World Persona Match/Completion API；Direct Create 与 Preview 共用缺失 Persona gate。
- `src/persona_continuum/web/server.py`：增加 REST routes、Persona Creation WebSocket 和启动后的 pending-job resume。
- `src/persona_continuum/world/llm_builder.py`：保留结构化 raw actor roster，供 Persona Match 使用。
- `src/persona_continuum/world/engine.py`：将已有 actor 自动绑定收紧为 exact name/id/alias，避免低置信度 substring 绑定。
- `src/persona_continuum/web/static/index.html`：人物页“创建人格”入口、异步进度面板、Parallel World Persona Creation Engine 配置区。
- `src/persona_continuum/web/static/app.js`：共享 runtime/model/reasoning capability 选择、创建任务轮询、暂停/继续/取消、访谈回答和 World 缺失 Persona 确认流程。

## DB migration

`persona_creation_jobs` 保存：job config、persona type、creation mode、runtime source、agent/model/reasoning、auth profile、agent version、capability snapshot、Persona/CompilationTask ID、research policy、source IDs、八维 progress、coverage、事件、访谈问题和错误。现有 `personas`、`sources`、`research_artifacts`、`compilation_tasks`、`lineage` 表未替换，外键在 Persona 删除时将 job 的 persona/task 引用置空。

## API / UI flow

人物页面点击“创建人格”后选择人物类型、创建模式、READY/Connected Runtime、Agent、动态 Model、ModelCapability 报告的 Reasoning 和研究策略。POST 立即返回 `job_id`/job snapshot，前端通过 persisted events polling 或 WebSocket 显示 Sources、Dimensions、Artifacts、当前阶段和错误；私有资料还显示远程发送 consent。

Parallel World Preview 和 Direct Create 都先执行 Persona Match。发现 MISSING/AMBIGUOUS 后返回 `requires_persona_completion_confirmation`，用户确认后启动最多两个并行创建任务；任务完成才把新 Persona ID 写入 actor binding 并继续 world create。组织/环境 actor 不进入缺失清单。

## Creation Pipeline

1. 冻结 Agent/Model/Reasoning/Credential/Auth/Capability snapshot。
2. `PersonaService.create()` 或选择已有 Persona 进行 enrichment。
3. Public Research 通过 ResearchToolBroker 计划、搜索、抓取、去重并调用 `PersonaService.add_source_text()`；Private/Fictional 通过现有 loaders 或 user-provided `add_source_text()`。
4. 八个维度分别由 Agent Adapter 输出严格 `ResearchArtifact`，且 source IDs 必须真实存在；Public Deep 的每个维度至少要求四个独立来源，并由 adaptive gate 逐维检查。
5. `CompilationService.submit_research_artifact()` → `compile_persona()` → `validate_persona()`；缺口显式进入 `completed_with_gaps`，不生成简化 personality card。
6. job、Evidence、artifact、claim、memory、compiled component、lineage 和事件都持久化。

## Private / Public / World integration

Public 无研究能力会在 job 创建前 fail-closed；运行中 runtime 消失会暂停为 `paused_runtime_unavailable`。Private 默认不 Web Search，资料不足保持 draft/waiting 并可继续访谈。已有 Persona 默认走 enrichment，不创建 `_2`；旧 provenance 保留，新版本继续追加。

## 已写测试列表

- `test_persona_creation_uses_existing_compiler`
- `test_public_persona_requires_research_capability`
- `test_public_deep_research_coverage_gate`
- `test_public_research_sources_have_provenance`
- `test_private_persona_does_not_auto_web_search`
- `test_private_guided_interview_fills_gaps`
- `test_insufficient_private_materials_stays_draft`
- `test_duplicate_persona_detected`
- `test_existing_persona_can_be_enriched`
- `test_actor_persona_match`
- `test_organization_not_marked_missing_persona`
- `test_missing_public_actor_requests_confirmation`
- `test_missing_public_actor_auto_creation`
- `test_private_missing_actor_requires_materials`
- `test_new_persona_auto_bound_to_actor`
- `test_direct_world_create_pauses_for_persona_confirmation`
- Runtime Selector 与人物页/World flow E2E contract tests。

## 验证状态

前一阶段的历史验证记录保留在根目录 `PERSONA_CREATION_VALIDATION.md`。本次 Adaptive
Research Quality System 已在用户批准后完成自动化验证，结果记录在
`ADAPTIVE_PERSONA_RESEARCH_VALIDATION.md`。真实联网研究、真实 Agent/Provider 调用和真实
60/100+ 来源 Persona 创建未在本轮执行。

## Adaptive Research Quality System（本次升级）

- `src/persona_continuum/application/research_quality.py` 新增 `ResearchGapAnalyzer`、
  `AdaptiveResearchStopGate`、`MarginalInformationGainTracker`、`LifeStageModel`、
  `SourceIndependenceAnalyzer`、`SourceQualityAssessment`、`ContradictionCoverage`、
  `ResearchCheckpoint` 和 Private coverage records。
- `ResearchPolicy` 支持 Standard / Deep / Exhaustive / Custom，Deep 默认 30 / 60+ /
  100 soft / 150 hard、6 类来源、八维每维 4 个独立证据；旧 job 的 `max_sources`
  snapshot 仍然可读且不会被新默认覆盖。
- Public research 现在按 gap-driven query、动态 life stage、独立来源 cluster、
  primary/secondary balance、主动 contradiction search 和 marginal information gain
  迭代，并在硬预算或资料空间耗尽时保存明确 stop reason。
- `persona_creation_jobs` 增加 life-stage、gain、gap、query history、checkpoint、
  private coverage 字段；新增 `persona_research_checkpoints` 和
  `persona_source_clusters` 表。
- 人物页与 Parallel World 的策略选项和进度面板改为 adaptive metrics：raw/independent
  sources、target/soft budget、dimensions、life stages、primary/secondary、recent gain、
  stop reason 与 gaps。

## 本次测试代码

新增 `tests/unit/test_adaptive_research_quality.py`，覆盖 policy defaults、旧 snapshot
兼容、转载 cluster、生命阶段事件时间映射、gap/query planner、primary/secondary、
contradiction gate、marginal stop gate、soft/hard budget、rich/sparse subject、private
coverage 和 UI copy contracts。验证阶段又补强了“70 个转载只计 3 个独立来源”和“单个
10,000 消息聊天导出按消息量而不是文件数计量”的行为测试。

最终自动化结果：`307 passed, 1 skipped, 1 warning`；Ruff、Mypy、JavaScript 语法检查和
`git diff --check` 通过。详见 `ADAPTIVE_PERSONA_RESEARCH_VALIDATION.md`。
