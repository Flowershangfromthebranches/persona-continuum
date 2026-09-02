# Room Protocol Engine 升级验收报告

日期：2026-08-28。工作树内既有用户未提交改动全部保留，未重置。

## 1. Implementation Summary

将原有“多人格讨论房间”增量升级为通用 Room Protocol Engine：

- 新增 `RoomProtocolType`（free_discussion / host_moderated / expert_consultation / debate / committee / custom）、`RoomRole`、`RoomSharedContext`、`RoomProtocolConfig`（协议策略）与 `RoomProtocolState`（运行状态），Definition 与 Runtime 严格分离。
- 新增声明式 `ProtocolDefinition`（阶段、允许角色、动作、并行/可选标记）与统一 `RoomProtocolRuntime` 状态机；五种内置协议和自定义协议共用同一执行内核，没有按协议复制 while loop，也没有任何 Taibu 业务逻辑进入 Engine。
- 参与成员增加 role / enabled / authority / specialties / permissions / tool_permissions；`expertise` 旧字段双向同步，旧数据自动映射 `member`。
- 新增 Room Run / 结构化 Event / Task / Submission / CrossReview / Vote 持久化，根任务-子任务层级与终态（success / partial_success / failed / cancelled）。
- 新增 Room Template（可保存/编辑/删除/从模板创建；模板与实例快照隔离，改模板不影响已建 Room），内置“术数综合会诊”示例模板仅存在于模板层。
- 协议动作通过既有统一 Agent execution abstraction 执行：Persona static kernel 缓存、Recall Gate（recall_started < recall_completed < agent_started）、StaticPersonaKernel、上下文 cursor、RuntimePool turn-scoped lease 全部复用，未新增常驻 physical lease。
- 新增/扩展 API：`GET /api/room-protocols`、Room Templates CRUD、`PATCH /api/rooms/{id}`、`POST /api/rooms/{id}/run|cancel|finalize`、`GET /api/rooms/{id}/events`、`GET/DELETE /api/room-runs`。
- 前端：创建房间支持模板选择与回填、协议选择、成员角色/专长/Authority/工具权限、Shared Context、按协议动态显示的高级设置；房间页新增“房间协作状态”面板（当前模式/阶段/主持人/激活与未调用成员、事件时间线）、停止（cancel）与“主持人立即总结”（finalize，基于已完成结果的独立 finalization run）。

## 2. Architecture

```text
Room (定义 + 缓存的运行时镜像)
├── participants: ParticipantSlot(persona, role, bindings, specialties, tool_permissions)
├── shared_context: RoomSharedContext(不属于任何 Persona)
├── protocol + protocol_config: RoomProtocolRuntime 只读策略
├── protocol_state: RoomProtocolState(run_id/stage/tasks/submissions/reviews/votes/final)
├── protocol_events: 结构化时间线（WS 实时广播 + SQLite 持久化）
└── RoomTemplate: 可复用定义，创建 Room 时快照拷贝，不反向绑定

ProtocolRuntime（确定性状态机）
  ── 每个 participant action ──> MultiAgentOrchestrator._execute_protocol_action
        Persona kernel cache -> Recall Gate -> PromptComposer 分层上下文
        -> AgentRuntimeExecutor（统一 adapter/model/reasoning 抽象，
          scheduler 有界并发，RuntimePool 每 turn acquire->execute->release）
```

Persona != Agent != Model != Reasoning != Room Session 解耦保持不变；协议层只发送 Persona Context / Room Shared Context / Role Context / Stage Instructions / Relevant Conversation / Task / Optional Peer Results 的分层请求，不依赖任何具体 CLI 或模型名。

## 3. Files Changed

新增：`room/protocols.py`、`room/protocol_runtime.py`、`room/repository.py`；测试 `tests/unit/test_room_protocol_engine.py`、`tests/integration/test_room_protocol_runtime.py`、`tests/integration/test_room_protocol_api.py`。
修改：`room/models.py`、`room/orchestrator.py`、`room/__init__.py`、`storage/migrations.py`、`storage/database.py`、`application/container.py`、`web/api.py`、`web/server.py`、`web/static/`（app.js / index.html / app.css）、`agent/adapters/fake.py`（协议结构化响应，仅测试用）。

## 4. Database Migration

纯增量：新表 `room_templates`、`room_runs`、`room_protocol_events`、`room_protocol_tasks`、`room_protocol_votes`（含索引与外键级联），随 `SCHEMA_SQL` 的 `CREATE TABLE IF NOT EXISTS` 创建；旧库无需删除。`Database._ensure_room_protocol_state()` 对旧 `rooms.state_json` 原位补齐 `protocol=free_discussion`、空 shared/protocol 字段并将旧成员映射为 `member`；`RoomSessionState` 的 before-validator 同时兜底。老房间可继续打开、继续自由讨论（已在浏览器与测试中验证）。

## 5. Protocols

- free_discussion：完全保留旧行为（Director/AI Director/round-robin/manual、自动讨论、用户插话）；协议房间明确禁止误入旧 autonomous loop。
- host_moderated：host 分析 -> 点名 -> 成员回应 -> host 决定 -> host 最终总结。
- expert_consultation：host 分析 -> 路由（host_decides/rule_based/all/manual，min/max_experts 校验）-> 独立首轮（peer_results 为空）-> 收集屏障 -> 交叉评审 -> 可选 rebuttal -> host synthesis -> final。已验证 3 专家并行峰值=3 且 review 前互不可见。
- debate：pro/con/judge/host 分阶段，立场仅是 Room Role，不改 Persona；支持交叉质询开关、judge 与 host 同人格。
- committee：独立意见 -> 交叉评审 -> 可选讨论 -> 结构化投票（approve/reject/abstain/custom + reason + confidence，简单多数或 authority 加权、可匿名；匿名时公开事件不携带 actor 与 reason）-> chair 综合；chair 否决多数必须给出理由（无 override_reason 时 finalize 报错）。
- custom：schema 已含 stages/transitions/allowed_roles/completion_condition；第一版按规格未开放可视化编辑器。

阶段转换、任务完成、超时、计票、权限、最大轮数、取消均由代码确定；LLM 只负责“选谁/如何评审/如何综合”。

## 6. Runtime Lifecycle

沿用第二轮性能重构的边界：Room 的 logical session（participant 线程与 Persona 会话）持久存在且零物理占用；每次协议动作经 `AgentRuntimeExecutor` 在 ExecutionScheduler 有界槽位内排队，RuntimePool 的 physical lease 只在单个 turn 执行期间持有、结束立即 release。容量测试：10 个专家任务在 4 个 LLM 槽位下全部到达终态、队列归零、无永久等待；Room 数量可远超池容量。协议层未引入新的常驻 lease。

## 7. Validation

- `uv run pytest -q`：实现前基线 602 passed；本轮新增 13 项协议测试；最终全量结果以提交时最后一次运行为准（见提交说明），2 skipped 为环境 gated 真实 CLI 测试，1 warning 为既有 import 往返测试的 zip 重复名。
- `uv run ruff check .`：通过；`uv run mypy`：严格模式通过（181 个源文件）。
- 前端 `node --check app.js` 通过；无构建步骤（静态 JS/CSS 直接服务）。
- 浏览器人工验收（in-app browser + 本地服务器 + 用户真实数据）：旧房间正常打开且转写完整；创建房间显示模板/协议/角色/Shared Context/专家协议高级设置；CLI 下拉仅 [READY]；“术数综合会诊”模板正确回填角色、专长与工具权限。
- API smoke：`/api/room-protocols` 返回 5 协议；`/api/room-templates` 返回内置模板；旧 Room 列表 200。并行/隔离/部分失败/投票/计票/迁移/模板快照、真实 orchestrator 端到端、容量 10>4、模板与 Run API 均有自动化测试。

## 8. Remaining Issues

- 自定义协议（custom）按规格只保留底层 schema，未开放前端编辑（第一版预期）。
- “术数综合会诊”模板引用的 Taibu 工具未在本项目集成；模板只声明工具权限，未伪造任何工具调用；运行时权限门只放行已注册工具。
- 本轮端到端验证全部使用 fake adapter；真实付费 provider 的会诊语义质量未复跑（协议层验证的是结构合同与状态机，不替代模型质量验收）。
- manual 路由第一版通过房间配置实现，未提供逐问手动点名 UI。
