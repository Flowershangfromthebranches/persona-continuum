# Resumable Persona Creation + Performance Hot Path Implementation

> 覆盖：Persona Creation 可恢复任务架构、75% 八维阶段修复、Context Window 解析、
> Safe Pause / Retry / 换模型、Structured Output 本地修复、Persona Rename、
> RuntimePool 均衡放置、Room / World 持久化优化、Interactive 预留、观测指标。

## A. Confirmed Issues（审计确认的真实问题）

1. **Context Window 一律回退 32K（P0）** — `_run_dimension_extraction` 把
   `job.capability_snapshot`（`AgentProbeResult.model_dump()`，顶层没有
   `model_id` / `selected_model` 键）直接传给 ContextBudgetManager，
   `context_window_resolution` 永远找不到选中模型，即使模型声明 256K 也按
   32K 切批，导致大批量 Evidence 被切成大量小批次。
2. **Batch 级无 checkpoint（P0）** — `_run_dimension_extraction` 的
   `batch_artifacts` 只在内存累积，Batch N 失败后 Batch 1..N-1 的模型输出全部丢失。
3. **Dimension 级延迟持久化（P0）** — 八维 `asyncio.gather` 全部完成后才统一
   submit/保存/发事件；中途崩溃或暂停会丢掉已完成的维度。
4. **部分完成后 Resume/Retry 全量重跑（P0）** —
   `_retry_reusable_dimension_artifacts` 要求 8/8 全部有效才复用；
   7/8 成功后再失败会重跑全部维度。
5. **暂停是硬取消（P0）** — `pause_job` 直接 `task.cancel()`，正在生成的
   Batch 输出被丢弃。
6. **Pause/Retry 不能更换模型（P0）** — `resume_job` / `retry_job` 不接受
   runtime 覆盖，换模型只能从 0 重新创建。
7. **75% 固定进度** — `PERSONA_LOCAL_STAGE_PERCENT["extracting"] = 75`，
   八维阶段几十分钟进度条不动。
8. **Structured Output 修复直接烧模型** — 首次 JSON 解析/校验失败立即发起
   完整 LLM repair turn（原输出 + 完整 schema + repair prompt），
   trailing comma、markdown fence、截断等简单损伤也浪费一次大模型调用。
9. **单个超大 Evidence 直接 fail-closed** — `iter_batches` 遇到超预算单条
   直接抛异常，没有语义切分。
10. **Logical Session 首次放置不均衡** — `AgentRuntimePool.acquire` 用
    first-available 选择物理进程，12 个持久会话可能 12/0/0/0。
11. **Room state 每 turn 重复序列化完整 transcript** — `_persist_room_state`
    把全量历史写进 `state_json`；turn 1001 的持久化成本与 1000 turn 成正比；
    且 turn 结束时连续 `_save_room_state` 两次。
12. **Room summary 阻塞 turn** — `_maybe_update_room_summary` 在 turn 完成
    前 await。
13. **World tick 十余次独立 commit** — event / memory / actor state / org /
    tech / causal graph / replay / branch / snapshot 各自 commit，
    中途失败留下 partial tick。
14. **World 全量 Actor memory 预加载** — 50 actor 全量加载后才选 6 个 active。
15. **World Builder 高置信仍二次分类** — builder 已输出 subtype 时仍然
    无条件调用 Classification LLM。
16. **Audit repair 串行** — `_repair_audit_dimensions` 逐维串行修复。
17. **无 Interactive 资源预留** — 优先级排序会让 interactive 先拿到下一个
    空槽，但 background 仍可占满全部 capacity。
18. **无 Persona Rename 能力** — persona_id 稳定但没有任何改名入口。

## B. Fixed Issues（本轮修复）

### Persona Creation 核心（P0）
- `agent/models.py` 新增 **`EffectiveModelCapabilities`**（agent_id / adapter /
  provider / requested_model / effective_model / context_window /
  effective_context_window / reasoning / structured_output / streaming /
  persistent_session / web_capability），`resolve()` 从 probe snapshot 中按
  effective/requested model 精确定位 `ModelCapability`。所有阶段统一读取。
- `agent/context_budget.py` 的 `context_window_resolution` 原生接受该对象；
  `budget_for` / `iter_batches` / `hierarchical_reduce` 类型同步放宽。
- `persona_creation_service._effective_model_capabilities(job)`：每次提取前
  解析一次并写入 `job_config["effective_model_capabilities"]`（前端可显示
  Effective model / Context window）。
- **Batch Checkpoint**：每个成功 batch 立即持久化到
  `job_config["dimension_batch_checkpoints"]`，key 为
  `job_id + persona_id + dimension + evidence_fingerprint`，
  fingerprint 覆盖 evidence id / source ids / content hash；并包含
  `source_revision`（source 集合变更即失效）、`prompt_version`
  （`DIMENSION_EXTRACTION_PROMPT_VERSION`）、provenance（agent/model/reasoning）。
  Retry 只重跑未完成 batch；已成功的 batch 复用时计数
  `dimension_checkpoint_hit_count` 并发 `persona_dimension_batch_completed` 事件。
- **Dimension 即时持久化**：`run_dimension` 完成即 `_persist_dimension_result`
  （校验 → fictional provenance 边界 → canonical hash → submit → progress →
  processed ledger → provenance → save → emit `persona_dimension_completed`），
  不再等 gather。
- **部分复用**：新增 `_partial_reusable_dimension_artifacts`——按
  processed-source ledger 证明覆盖的维度直接复用 prior artifact；
  新证据到达的维度走增量提取。7/8 成功后 Retry 只重跑第 8 维。
- **真实进度 75–94%**：`_extraction_percent` / `_emit_extraction_progress`
  按已完成维度数映射 75→94 区间，事件携带 completed/total/current_dimension；
  batch 完成也发事件。
- **Safe Pause**：`pause_job` 只设置 `pause_requested`，worker 在原子边界
  （新 Agent call 之前、每个 batch 之前、每个 dimension 之前）检查并抛
  `_PauseRequested`（BaseException，不会被 `except Exception` 吞掉）；
  gather 使用 `return_exceptions=True`，所有 sibling 完成当前原子单元后才
  进入 PAUSED 并 checkpoint。无运行中 worker 时立即暂停。
- **换模型 Resume / Retry**：`resume_job(job_id, runtime=...)` /
  `retry_job(job_id, runtime=...)` 接受 Agent / Model / Reasoning 覆盖；
  `_apply_runtime_override` 重新 probe、校验 structured output 与
  （研究未完成时的）web capability，换目标后关闭旧 logical session，
  checkpoint / evidence / 已完成维度全部保留，并写入
  `job_config["execution_history"]`（含 `resume_with_runtime_change` /
  `retry_with_runtime_change` 动作）。`paused_runtime_unavailable` 的 job
  也可带新 runtime 重试。
- **Provenance**：`dimension_provenance[dim]` 记录 job/stage/agent/
  requested_model/effective_model/reasoning/时间戳；batch checkpoint 亦记录。
- **Checkpoint 记录**：dimensions_completed / audit_completed 阶段写入
  `last_checkpoint`，前端可显示"最后成功检查点"。
- **超大 Evidence 语义切分**：`_split_oversized_evidence` 按段落切分超预算
  evidence，保留 source_ids / 原 evidence_id，内容前缀 `[CHUNK i/n]`，
  证据链不丢失；切不完仍然 fail-closed。
- **Audit Repair 并行**：`_repair_audit_dimensions` 改为 semaphore 有界并行，
  不可变输入（audit findings + 当前 artifact snapshot），gather 后按 canonical
  顺序提交，单维失败以确定性顺序聚合为 `PersonaQualityGateError`。

### Structured Output 可靠性
- `agent/structured_output.py` 新增 `local_repair`（markdown fence 剥离、
  prose 前后缀裁剪、trailing comma 清除、截断闭合）+ `_parsable_prefix` /
  `_strip_trailing_commas` / `_close_truncated_json`，全部确定性、可验证。
- `runtime_executor.execute_structured` 先本地修复并完整 schema 校验，
  成功则零模型调用返回（计数 `structured_local_repair_count`）；
  只有本地无法修复才走原 LLM repair（计数 `structured_llm_repair_count`）。

### Persona Rename
- `PersonaService.rename(persona_id, display_name, aliases=None)`：
  校验（空 / 纯空格 / >120 字符 / 控制字符 → `ConflictError`），只改
  `manifest.display_name`；persona_id、room/world binding、memory、
  research artifact、revision 链全部不动；同步 profile library 并 invalidate
  static persona kernel 缓存（room/world prompt 立即显示新名）。
  轻量 metadata revision——不触发任何重新研究或重新编译。
- API：`POST /api/personas/{persona_id}/rename`；MCP：`persona_update`
  带 `display_name` 时走同一 rename 路径。
- 前端：Persona 卡片与详情页新增"重命名"入口。

### Runtime / Room / World
- **RuntimePool least-loaded 放置**：`acquire` 在 parked 进程中选
  `logical_sessions` 最少（同值取最久 idle）者；snapshot 增加
  `logical_sessions_per_runtime` 分布。12 会话 / 4 进程 = 3/3/3/3，
  affinity 与 `acquire_managed` 不变。
- **Interactive 预留**：`PrioritySemaphore` 支持 `reserved_units`，
  BACKGROUND 调用者不可消费预留单位（handoff 跳过 + 直取条件保护）；
  `ExecutionScheduler` 默认 `capacity-1 ≥ 1` 时预留 1 个模型槽给
  INTERACTIVE，capacity=1 自动退化。snapshot 增加
  `llm_reserved_units` / `llm_capacity`。
- **Room State / Transcript 分离**：`_persist_room_state` 只保留最近
  `room_raw_turn_window×4`（≥32）条 transcript + 截断标记；
  host 消息与用户注入也写入 `room_transcripts`（append-only 权威来源）；
  `_rehydrate_transcript` 从事件存储 + 保留窗口无损重建完整历史
  （`get_room` DB 回退路径与 `resume_from_storage` 均接入）。
  内存中的 state 仍保留完整 transcript，prompt 构建行为不变。
  turn 结束的重复 `_save_room_state` 合并为单次 force save。
- **Room 异步 summary**：turn 完成即返回，summary 刷新为
  `_schedule_room_summary` 后台任务（单飞 per-room，delete_room 时取消）；
  下一轮若刷新未完成则沿用旧 summary + 最近原始 turns，上下文不丢失。
- **World Tick 单事务**：`Database.transaction()`（嵌套即 join；
  BEGIN IMMEDIATE / COMMIT / 失败 ROLLBACK）；world repository 全部
  `save_*` 在进入时判断 `standalone`，事务内不自行 commit；
  `step_branch` 的解析 + 持久化段整体包进一个事务（同连接可见性保持，
  因果逻辑不变），tick 失败不再留下 partial tick。
- **World Lazy Memory**：`SimulationLoop.collect_proposals` 接受
  `memory_loader`，engine 传懒加载闭包——只有 director 选中的 active actors
  （默认 6）触发 heavy memory 检索；50 actor 世界从 50 次检索降到 6 次。
  tracer 记录 `memory_loaded_actor_count`。
- **World Builder 分类 fast path**：builder 已输出显式 subtype/actor_type 且
  `classification_confidence ≥ 0.9` 的候选集整体走确定性验证直收
  （warning `classification_fast_path:builder_confidence`），
  跳过第二次分类 LLM 调用；低置信/冲突/缺失仍走 classifier。
  LLM 优先语义（`require_llm`）与确定性纠错不变。

### 观测指标
- tracer 全局计数：`dimension_batch_model_call_count`、
  `dimension_checkpoint_hit_count`、`structured_local_repair_count`、
  `structured_llm_repair_count`、`logical_sessions_per_runtime`（pool snapshot）。
- Room turn：`persistence_ms`；World tick：`memory_loaded_actor_count`。
- Job payload：`dimension_batch_checkpoint_summary`（仅计数，不泄漏证据文本）、
  `effective_model_capabilities`、`dimension_provenance`、
  `execution_history`、`last_checkpoint`、`pause_requested`。

## C. Already Correct（未改动，避免重复建设）

- Job 全量持久化到 SQLite `persona_creation_jobs` + worker heartbeat +
  `resume_pending_jobs` 崩溃恢复（进程重启按 stage 恢复）。
- 研究侧 checkpoint：`research_checkpoints` / `query_history` /
  `should_repeat` 防止 resume 后重复查询；research source/query cache。
- 维度失败隔离（单维失败不破坏其余维度）与 quality-gate 全量复用路径
  （`_retry_reusable_dimension_artifacts`，8/8 有效的 retry 复用）。
- `StructuredOutputEngine.parse_json` 已处理 fence 与 JSON 截取。
- Adaptive research 并发控制（AIMD）+ per-adapter LLM 限流 +
  INTERACTIVE/FOREGROUND/BACKGROUND 三级优先队列。
- Room static persona kernel 缓存（persona_id + revision 感知、自动失效）。
- Job snapshot debounce（0.5s）与 force-save 事件集。
- 幻觉 provenance 边界（fictional 一律 counterfactual_simulated）。

## D. Benchmark（真实测试数据）

`uv run python scripts/benchmark_resumable_upgrade.py`（FakeAgent / 确定性环境，
只度量代码路径，不含模型延迟）：

```
Context window batching（120 条 ~1500-token evidence）:
  window   | batches
  32768    | 18     <- 旧版固定回退值（错误）
  131072   |  3
  262144   |  3
  1048576  |  3
=> 256K 模型从 18 次模型调用降到 3 次（同一证据，同一质量）。

Deterministic local repair: 4/4 种简单损伤 0.22ms 全部修复
=> 每例省 1 次 LLM repair 调用。

Room state persistence（transcript 增长时）:
  turn | state_json (KB) | ms
   10  |  7.1 | 0.1
  100  | 18.9 | 0.2
  500  | 18.9 | 0.7
 1000  | 18.9 | 1.5
=> 10→1000 turn 只增长 2.7x（旧版全量序列化约 ~100x）。

RuntimePool placement: 4 物理 runtime / 12 持久会话 → [3, 3, 3, 3]
（旧 first-available 行为为 12/0/0/0 型分布）。

World tick transaction: BEGIN=1, COMMIT=1
（旧版每个 save_* 独立 commit，一次 tick ~10+ 次 commit）。
```

新增回归测试 `tests/integration/test_resumable_persona_creation.py`（18 个）：
batch checkpoint 复用 / 失败维度不重跑已完成维度 / safe pause 边界 /
pause-resume 生命周期 / 换模型 retry（checkpoint 保留 + superseded 记录）/
无 web runtime 在研究未完成时被拒 / context window 解析与批次缩放 /
本地 JSON 修复 / least-loaded 放置 / interactive 预留 / rename 身份稳定 /
DB transaction commit+rollback / room 截断与重水合 / 1000-turn 持久化不增长 /
World lazy memory（50 actor 只加载 6 个）。

## E. Remaining Bottlenecks（仍然受外部因素影响）

- 八维提取 / 审计 / 修复的绝对耗时仍由 LLM 推理与 CLI/API 延迟决定；
  本轮只消除了重复计算（batch 复用、checkpoint reuse、并行 repair）。
- 公开研究的 wall time 由外部 Web 检索 / 抓取延迟决定（search/fetch slot
  有界并发，未增加研究量）。
- Room turn 首字延迟主要由模型 TTFT 与 runtime queue 决定；
  预留槽只保证 interactive 不被 background 饿死。
- World tick 中的 LLM proposal 生成依旧是主导项；事务合并只影响持久化段。
- Pause 的生效时延 ≤ 当前原子单元（一次模型 turn）的剩余时长——这是
  Safe Pause 语义的代价；需要立即停止时仍可使用 Cancel。
