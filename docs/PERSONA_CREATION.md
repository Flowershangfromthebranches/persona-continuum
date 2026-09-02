# Persona Creation Runtime

Persona Continuum 的人格创建只有一条正式链路：

`PersonaService.create()` → `EvidenceSource` → `CompilationService.create_task()` → 八维 `ResearchArtifact` → `submit_research_artifact()` → `compile_persona()` → `validate_persona()`。

LLM 不直接写 Persona package、`system_prompt` 或 personality card。每个 claim、memory 和 compiled component 都必须能沿 lineage 回到真实 EvidenceSource。

## 创建模式

### Public Research

`public_living_person` 与 `public_historical_person` 使用 `PUBLIC_RESEARCH`。任务先冻结 Agent、Model、Reasoning、Credential/Auth profile 和 capability snapshot，然后通过 ResearchToolBroker 执行计划、搜索、抓取、去重、来源摄取和覆盖率审计。

Research policy 支持 `standard`、`deep`、`exhaustive` 与 `custom`。新建公共人物默认使用 Deep：至少 30 个独立来源、目标 60+、软预算 100、硬预算 150、至少 6 个来源类别、八维每维至少 4 个独立证据。资料丰富的人物会根据覆盖率、生命阶段、矛盾检索和边际信息增益继续扩展到 60–100+，不会达到最低数量就提前停止。Exhaustive 的默认目标为 100、硬预算 200。旧 job 只读取其保存的旧 policy snapshot，不会静默采用新默认值。

停止条件由 `AdaptiveResearchStopGate` 统一判断：独立来源、八维、动态生命阶段、来源多样性、primary/secondary balance、主动矛盾检索、优先级缺口和连续低边际收益必须同时满足。软预算可以为高优先级缺口继续超出；硬预算或公开资料空间耗尽时显式标记 `completed_with_gaps`，并保存 `research_stop_reason`。

每轮都保存 `ResearchCheckpoint`、`InformationGainSnapshot`、gap/query history 和来源 cluster。`SourceIndependenceAnalyzer` 按 canonical URL、内容 hash、近似文本、标题/发布者与引用链归并转载；覆盖率只按独立 origin 计数。`LifeStageModel` 由研究计划动态生成，来源按事件发生时间映射，不能使用来源发表年份代替。每个来源还保存质量、权威性、primary/secondary、具体性、独立性、历史相关性和 metadata 完整度评估。

canonical URL、标题、出版者、作者、发布时间、访问时间、内容 hash 和 source type 进入 Evidence metadata；`persona_source_clusters` 只保存 cluster 元数据和 source IDs，不复制正文。

ResearchCapability 区分 `declared`、`verified`、`unknown` 和
`unavailable`。READY 本地 CLI 即使未声明 `web_search`/`web_fetch`，也会
先启动一次低消耗 Behavioral Research Probe；Persona Continuum 会独立
验证真实 URL 的 HTTP 状态与正文，成功后才标记 `verified` 并进入研究。
API Provider 不会被当作 CLI 探针对象，仍需显式 native capability、Research
Broker 或 MCP。模型声称搜索但无法提供可访问真实 URL 时，任务给出具体错误
并 fail-closed，不使用训练记忆替代联网研究。能力结果按 Agent 版本、Model
和 runtime source 缓存；预算用尽仍缺资料时标记 `completed_with_gaps`。

### Private Materials / Guided Interview

`private_living_person` 与 `private_deceased_person` 默认不进行 Web Search。TXT、Markdown、JSON、CSV、HTML、PDF、DOCX、聊天导出和用户文字通过现有 `SourceLoader`/`add_source_text` 摄取为 `user_provided EvidenceSource`。API Provider 发送私密资料前必须有显式同意。

Guided Interview 根据已有材料和八维缺口动态生成问题；用户可以回答“不清楚”。回答也是 `user_provided EvidenceSource`，不会被模型自动补齐。资料不足的人格保留 `draft` 或 `completed_with_gaps`，UI 展示低覆盖率并允许继续完善。Private 不使用公共人物来源数量门槛，而是记录消息量、对话跨度、交互场景、关系场景、行为事件、访谈回答和八维缺口；大聊天导出按消息/事件信息量计数，不按文件数计数。

### Fictional / Synthetic

虚构人物使用作品文本、角色设定、用户资料和对话样本，不自动进行现实人物 Web Research，仍然走 Evidence 与八维 Compilation。覆盖率按作品文本量、场景、对话、行为事件、关系和用户设定计数，不使用公共人物 Web source threshold。

## 持久化与状态

`persona_creation_jobs` 保存 job 配置、runtime binding snapshot、research policy、来源 ID、dimension/life-stage progress、coverage、information gain、research gaps、query history、checkpoint、事件、interview questions、private coverage、compilation task 和错误。`persona_research_checkpoints` 保存每轮可恢复的研究质量快照，`persona_source_clusters` 保存独立性归并结果。任务可在浏览器关闭或进程重启后通过 `resume_pending_jobs()` 继续；运行时版本或能力消失会进入 `paused_runtime_unavailable`，不会静默切换 Agent/Model。

状态包括：`created`、`planning`、`researching`、`ingesting_sources`、`extracting`、`compiling`、`completed`、`completed_with_gaps`、`waiting_for_materials`、`failed`、`cancelled`、`paused`、`paused_runtime_unavailable`。

## Parallel World 自动补全

World Builder 解析出的 actor roster 先走 Persona Match：exact Persona ID、规范化名称、alias，最后只给出需确认的安全模糊候选。组织和环境 Actor 为 `NOT_PERSON`，不会被要求创建 Persona。

如果存在 `MISSING`/`AMBIGUOUS` 人物，Preview 与 Direct Create 都返回 `requires_persona_completion_confirmation`。用户确认后调用 `/api/worlds/persona-completion/confirm`，最多并行两个 Persona Research Job；完成后将新 Persona ID 绑定到 actor，再继续创建世界。私人或未知人物如果没有材料，只能等待资料或保留 Generated Actor。

Persona Creation Engine 可以继承 World Builder 的 runtime，也可以独立选择 Agent/Provider/Model/Reasoning。三处 UI 共享同一套 discovery 与 model capability 选择规则，只显示 READY/Connected 运行时。

## API 与事件

核心 REST：

- `POST /api/persona-creation/jobs`
- `GET /api/persona-creation/jobs/{job_id}`
- `POST /api/persona-creation/jobs/{job_id}/pause|resume|cancel`
- `POST /api/persona-creation/jobs/{job_id}/retry`（仅对 retriable Agent 输出失败）
- `POST /api/persona-creation/jobs/{job_id}/continue`（保留旧证据的 targeted enrichment）
- `POST /api/persona-creation/jobs/{job_id}/materials`
- `POST /api/persona-creation/jobs/{job_id}/interview-answer`
- `GET /api/persona-creation/jobs/{job_id}/events`
- `POST /api/worlds/persona-match`
- `POST /api/worlds/persona-completion/confirm`

事件持久化并同时通过 `GET .../events`、`/api/persona-creation/jobs/{job_id}/ws` 推送：`persona_creation_started`、`persona_research_plan_created`、`persona_search_started`、`persona_source_found`、`persona_source_ingested`、`persona_dimension_started`、`persona_dimension_completed`、`persona_coverage_updated`、`persona_interview_question`、`persona_compilation_started`、`persona_compilation_completed`、`persona_creation_failed`、`persona_creation_completed`，以及 World Persona completion 事件。进度面板同时展示 raw/independent sources、target/soft budget、八维、life stages、primary/secondary、recent marginal gain、stop reason 和 gaps。

人物页和 Parallel World 使用同一套 Standard / Deep / Exhaustive / Custom 选择器。已完成任务可以从人物详情继续研究：新任务保留已有 Evidence、claims、memories 和 lineage，按指定 life stage、dimension、relationship 或 event 做 targeted enrichment 并生成新版本。

后台任务观察使用持久化 `JobProgress`（stage、label、percent、当前项、完成数、失败详情）。关闭进度窗口不会取消任务；人物页任务中心可重新打开运行中或失败任务。失败对象记录 Agent 调用阶段、runtime/model/reasoning、protocol、最后事件、事件计数、脱敏 stderr 和是否可重试。出现空输出、协议无最终文本或结构化输出失败时，不会跳过编译。

### Agent Runtime contract

Persona Creation 的文本、结构化输出和研究调用统一经过
`AgentRuntimeExecutor`。它先用 `AgentPromptRenderer` 保留 system/user
边界，再按模型 context window 由 `AgentContextBudgetManager` 分批输入，最后
由 `StructuredOutputEngine` 解析、校验并最多执行有界 Repair Turn。结构化失败
区分 parse/schema/repair code；不会由业务层直接对 Agent final text 调用
`json.loads()`。超时区分可由 thinking/tool/chunk 活动重置的 idle timeout 与
绝对 hard timeout。完整协议说明见 `AGENT_RUNTIME_CONTRACT.md`。
# Private material evidence layer

For `private_materials` and `guided_interview`, configured files and interview
answers are ingested as normal `EvidenceSource` records and then analyzed by
`MaterialIntelligenceService`. The resulting message/paragraph-level evidence
units, duplicate clusters, fused claims, episodes and contradictions are
retrieved by each of the eight dimension extraction prompts. This keeps the
formal `ResearchArtifact -> CompilationService` path intact while preventing a
large chat export from being counted as one source or one fact.

The material job is resumable and reports `PARSING`, `SEGMENTING`, `ANALYZING`,
`CLUSTERING`, `FUSING`, `INDEXING`, `GAP_ANALYSIS` and
`READY_FOR_COMPILATION`. Raw source provenance and verbatim expression samples
are retained. See `docs/PRIVATE_PERSONA_MATERIAL_INTELLIGENCE.md` for the data
model and local inspector endpoints.
