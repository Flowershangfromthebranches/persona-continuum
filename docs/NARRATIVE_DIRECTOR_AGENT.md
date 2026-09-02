# Narrative Director Agent（叙事导演代理）

Narrative Director Agent（简称 **Director**）是一个可以通过自然语言理解作者修改意图、并自动调用现有 Narrative 能力完成创作与修改工作的编排代理。它不是新的故事引擎，也不是 Coding Agent——它只能调用 Persona Continuum 明确定义的 Narrative Domain Actions，所有真实的数据写入仍由应用层（`NarrativeService`）完成。

## Architecture

```text
User Natural Language
        ↓
NarrativeDirectorService（Action Loop，宿主拥有）
        ↓  结构化 Decision JSON（不依赖 Native Tool Calling）
Permission / Schema / Revision Check
        ↓
NarrativeService（现有正式能力，零重复实现）
        ↓
Narrative Repository / Runtime / Persona / World
```

核心文件：

```text
narrative/director.py                     Action 注册表、权限模型、Decision Schema、System Contract
application/narrative_director_service.py 会话循环、运行时执行、Action 分发、停止规则、审核修复环
narrative/repository.py                   Director 会话/消息/Action 持久化
domain/narrative.py                       Director 领域模型（Session / Message / Action）
web/api.py + web/server.py                REST 端点
web/static（index.html/app.js/app.css）   导演 Agent 侧栏面板
```

## Director Modes

| 模式 | 说明 | 权限 |
| --- | --- | --- |
| DISCUSS（讨论） | 只读取、只回答，不产生任何副作用 | 仅 READ_ONLY |
| ADVISE（建议） | 读取、分析、提出执行计划，等用户确认后才运行 | 仅 READ_ONLY（写操作以计划形式提出） |
| AGENT（代理） | 自动执行白名单内的 SAFE_WRITE Action，直到 Canon Gate | READ_ONLY + SAFE_WRITE（HIGH_IMPACT 需确认） |

非 READ_ONLY Action 在 DISCUSS/ADVISE 模式下会被 dispatcher 以 `DIRECTOR_ACTION_NOT_ALLOWED` 拒绝，并提示模型改为提出计划。

## Action Loop

1. 用户消息 → 持久化 → 会话置 `RUNNING`，记录乐观并发快照（`project_revision` / `story_bible_version`）。
2. Context Builder 按 L0–L5 分层组装预算化上下文（项目摘要 / 圣经摘要 / 当前集状态 / 角色与正史 / 最近对话 / 最近工具结果）。
3. 通过 `AgentRuntimeExecutor.execute_structured`（复用 `NarrativeService._structured_call_async`，phase=`narrative_director`）产生 Decision JSON：

```json
{
  "decision": "answer | execute_action | request_user_input | stop",
  "message": "...",
  "action": "revise_episode_draft",
  "arguments": { "...": "..." },
  "reason": "..."
}
```

4. `execute_action` → schema 校验 → 权限校验 → revision 校验 → 分发到 `NarrativeService` → 持久化 ActionResult → 结果回填上下文 → 下一轮。
5. 每个真实 Action 都持久化到 `narrative_director_actions`（含状态、结果、错误码）。

模型只有在拿到真实 `status == succeeded` 的 Tool Result 后才能声称"已修改/已生成/已审核"；失败会如实呈现，禁止假装成功。

## Permissions

风险分级：`READ_ONLY` / `SAFE_WRITE` / `HIGH_IMPACT_WRITE` / `HUMAN_ONLY`。

- **READ_ONLY**（全部模式可用）：`get_project`、`get_story_bible`、`get_episode_plan`、`get_episode_versions`、`get_episode_audits`、`get_selected_forecast`、`get_knowledge_matrix`、`get_canon`、`get_pipeline_state` 等 21 项。
- **SAFE_WRITE**（AGENT 模式自动执行）：`patch_episode_plan`、`generate_forecast_directions`、`run_forecast`、`select_forecast_direction`、`run_persona_rehearsal`、`generate_episode_draft`、`revise_episode_draft`、`audit_episode`。
- **HIGH_IMPACT_WRITE**（需用户在聊天中明确确认后才执行）：`patch_story_bible`、`change_project_settings`。模型必须先请求确认；用户回复"确认"后 service 授予一次性的待确认令牌，模型再次携带 `user_confirmed: true` 才会真正执行。
- **HUMAN_ONLY 完全未注册**：`commit_episode`、`force_commit_episode`、`generate_production_package`、`delete_project`、`delete_canon`、`delete_persona`、`modify_persona_base`、`force_override_audit`。任何模型输出引用它们都会得到 `DIRECTOR_ACTION_NOT_ALLOWED / Human approval required.`——不是隐藏 Prompt，而是 Director 根本没有这些 Action。

## Canon Gate

Episode Pipeline 的 Step 1–5（计划 / Forecast / 排练 / 剧本 / 审核）可由 Director 自动执行；`audit_episode` 结果 **BLOCKING == 0** 时：

```text
status = WAITING_FOR_CANON_APPROVAL
STOP ACTION LOOP
```

Director 之后不得自动调用任何写 Action。提交正史（Step 6）与生成制作包永远由用户在 UI 手动完成。

## Production Gate

即使用户已手工提交正史，Director 也没有 `generate_production_package` 能力。P0 不开放 Production Automation。

## Runtime

- `NarrativeRole.DIRECTOR` 对应 runtime 阶段 `director`，在「⚙ 创作模型 → 高级：按阶段覆盖」中与其它阶段并列配置（Runtime Source / Agent / Model / Reasoning），复用统一 Runtime Selector；未单独配置时继承默认创作模型。
- Director Runtime 只负责结构化推理（prompt → JSON），宿主执行 Action。CLI / harness / OpenAI-compatible API 均可担任 Director，**不依赖 Native Function Calling**（由 Fake Plain CLI 测试证明）。
- 复用 turn-scoped acquire → execute → release；Director Session 是逻辑会话，不是持久运行时租约。

## Revision Workflow

`revise_episode_draft`（`NarrativeService`）：

- 输入：当前 Story Bible、Episode Plan、Canon Snapshot、Character/Audience Knowledge、Persona Kernels、Plot Threads、Clues、Arcs、**Base EpisodeVersion**、最新 Audit Findings、用户修改指令。
- 输出：新的不可变 `EpisodeVersion`（Vn+1），绝不覆盖旧版本。
- Provenance：`parent_version_id` / `revision_reason` / `revision_instructions` / `revision_mode`（local | medium | rewrite）/ `created_by = "director:<agent_id>"`。
- `revision_mode=local` 时 Prompt 明确要求只改动指令涉及部分，其余逐字保留。

`patch_episode_plan`（`NarrativeService`）：

- 白名单字段（`EPISODE_PLAN_PATCHABLE_FIELDS`：title/narrative_goal/hook/beats/must_happen/must_not_happen/…/cliffhanger/estimated_duration_seconds）；`id`/`project_id`/`episode_number`/`created_at`/`runtime_trace` 不可改。
- `expected_project_revision` 乐观并发校验，冲突抛 `NARRATIVE_DIRECTOR_STATE_CONFLICT`（Director 需重新读取，不得覆盖较新版本）。
- 成功后 `project.revision += 1`，并经现有 fingerprint 机制把依赖旧 Plan 的 Forecast / Draft / Audit 标记为 STALE（不删除旧数据）；补丁原因记入 plan trace 与 Director Action 历史。

## Audit Repair

- Draft/Revision 完成后必须 `audit_episode`。
- BLOCKING == 0 → 立即 Canon Gate 停止。
- BLOCKING > 0 → 允许自动 revise + audit，最多 `DIRECTOR_MAX_AUTO_AUDIT_REPAIR_ROUNDS = 2` 轮；超限进入 `NEEDS_HUMAN_GUIDANCE` 并列出阻塞项。
- WARNING 永不触发自动清零：BLOCKING 0 / WARNING N 即为合格的人工审核入口。

## Context Budget

分层上下文（L0 System Contract / L1 项目与圣经摘要 / L2 当前集状态 / L3 角色与正史 / L4 最近 12 条对话 / L5 最近 3 条工具结果），各层均有截断预算；每轮只增量携带最近工具结果，不重放全项目。

## Concurrency

- 乐观并发：写 Action 前校验 `project_revision`；其它页面修改导致的 revision 变化会拒绝旧写入并要求重读。
- 同一会话 RUNNING 期间拒绝并发消息（`DirectorSessionBusyError`）。
- Pause / Resume / Cancel：暂停/取消总是优先于循环状态迁移；cancel 取消后台 loop task；已写入的历史版本不回滚。

## UI

单集创作页面右上角「导演 Agent」按钮展开右侧抽屉：

- 模式切换（讨论 / 建议 / 代理）
- 会话线程（用户 / 助手 / 系统消息）
- 实时执行状态（Running / Waiting for user / Waiting for canon / Needs guidance…）
- Action Trace 折叠卡（✓ 修改剧集计划、✓ 修订剧本 V2→V3、✓ 连续性审核 BLOCKING 0 · WARNING 3）；技术 JSON 在高级详情内
- Action 成功后自动刷新单集页面数据（无需 F5）
- 到达 Canon Gate 时顶部横幅提示手动提交正史；停止后仍可继续自然语言修改

## Examples

- "这个秘密暴露太早，删掉" → `patch_episode_plan(forbidden_reveals/must_not_happen)` + `revise_episode_draft(local)` + `audit_episode`
- "19:17 车祸必须在第一集发生" → `patch_episode_plan(must_happen/beats/cliffhanger)` + `revise` + `audit`
- "方宁这里不像她自己" → 读取 Persona Context → `revise_episode_draft`（绝不修改 Persona Base）
- "把车祸提前到 EP01，EP02 删掉重复段" → patch 两个 Plan + revise EP01 + 标记 EP02 旧稿 stale（不自动重写 EP02）

## Testing

```bash
uv run pytest tests/unit/test_narrative_director_agent.py \
              tests/unit/test_narrative_director_firewall.py \
              tests/integration/test_narrative_director_api.py
```

覆盖：权限拒绝（commit/production）、DISCUSS 只读、AGENT 全链路修订到 Canon Gate、Plan patch 白名单与 stale 传播、状态冲突、HIGH_IMPACT 确认流、审核修复环上限（2 轮）、WARNING 不清零、知识防火墙不泄漏 final_truth、HTTP API 全流程。
