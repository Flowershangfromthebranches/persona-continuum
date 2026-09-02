# Persona Continuum 第二轮性能重构验收

日期：2026-08-28

## 结论

本轮“性能重构收尾与 P0 修复”已实现并通过全量自动化门禁。核心结果：

- RuntimePool 不再让 persistent logical session 长期独占 physical runtime lease；physical lease 只覆盖一次实际执行 turn。
- pool=4 时，20 个 persistent logical session 以及 8 dimensions + 10 Persona jobs + 10 World actors + 12 Room participants 均能完成，无饥饿或永久等待。
- Persona 创建从“同一批证据按八维反复阅读全文”改为一次跨维 Evidence Intelligence，再由八维读取有界 Evidence Ledger。
- 编译前固定执行两次全局严格审计（事实/证据审计、人格一致性审计）；失败维度定向检索修复，仍未全 PASS 时进入 `failed_quality_gate`，禁止编译。
- 正常 60 来源 Deep Policy 回归的 Persona LLM Turn 数被锁定为 `<= 30`；八维、来源量、provenance 和完整质量门禁不削减。
- 生产 Job 会持久化真实性能 breakdown，可从 Job API 和只读 `/api/performance/summary` 查询。

## 1. RuntimePool P0

旧设计把 logical session 生命周期与 physical process lease 绑定。超过池容量后，第 5 个长期会话可能永久等待。

新设计：

1. `thread/start` 后 logical session 只保留对 managed physical runtime 的 affinity，不保留执行 lease。
2. 每次 `send` 前获取该 physical runtime 的 turn-scoped exclusive lease，turn 结束立即释放。
3. logical session 关闭只释放 affinity；physical process 由有界池复用或 idle reaper 回收。
4. 崩溃 runtime 标记 unhealthy、关闭并由下一次 acquire 替换。
5. Research worker、八维 session、最终审计与修复 session 都在安全边界关闭，不长期占用执行槽。

隔离边界不变：Persona、Room participant、World actor 和 dimension 各自拥有独立 logical thread；共享的是物理进程，不共享人格上下文。同一物理 stdio 同时只有一个 turn。

## 2. Persona LLM 调用拓扑

优化后的正常 public research 主链：

```text
Research plan
  -> batch search / batch fetch
  -> 每个新增 Evidence batch 做一次跨维 Evidence Intelligence
  -> 持久 Evidence Ledger（按 source hash / namespace 缓存）
  -> 八维各做一次有界检索合成
  -> 全局 evidence audit + consistency audit
  -> 必要时仅修复失败维度
  -> Compile / Validate
```

关键约束：

- 八个 Persona 维度全部保留。
- 维度合成只读取结构化 Ledger 与检索到的相关证据，不再每轮对同一原文重复八遍。
- legacy execution 仍保留给对照；optimized execution 不降低模型、reasoning 或 Research Policy。
- 公共来源可跨 Persona 共享客观事实缓存；private material 不进入共享 factual cache，Persona-specific judgments 也不会跨 Persona 复用。
- Global Audit payload 有硬边界：默认 32 个多样化 Ledger 单元、每维 8 个关键 claims，并保留完整 source-id 集、冲突、uncertainty 与组件摘要。60 来源测试要求每个 audit prompt `< 50,000` 字符，避免用超大 prompt 假装减少 Turn。

## 3. 严格最终质量门禁

编译前同时运行：

- Evidence Audit：事实准确性、claim/source linkage、时间线、冲突、负面证据、uncertainty、错误归因。
- Consistency Audit：八维完整性、跨维一致性、价值观/行为、表达证据、关系和动机推断边界。

每个审计都必须显式返回八个 dimension 的 `pass`。任何缺失、`repair_required` 或 `fail` 都触发按维定向 Evidence retrieval 和重新合成；达到重试上限仍未全 PASS 时：

- Job 状态为 `failed_quality_gate`；
- 不进入 compile；
- Task Center 将该状态作为终态正确分页；
- simulated continuation / inference 仍不得提升为 historical fact。

## 4. Research Transport 与缓存

- `ResearchTransport` 支持 `search`、`fetch`、`batch_search`、`batch_fetch`。
- 有原生 hook 时直接并发调用；无 hook 的 CLI backend 会在一个 Agent turn 内请求多个 search/fetch tool call，避免“一 URL 一完整模型 turn”。
- `CachingResearchBackend` 先查 query/source cache，只对 miss 做一次批量 transport 调用。
- `SourceIntelligenceCache` 的 key 包含内容 hash、model、reasoning 和 schema namespace，避免旧分析或不同推理配置串用。
- 原始来源批量、确定性入库；缓存不改变 canonical URL、来源数量或 provenance。

## 5. Adaptive Persona Scheduler

移除了固定 `max_parallel_persona_research=2`。新控制器使用可配置的 `min / initial / max` 并发：

- 429、rate limit、timeout、transport/crash、memory pressure 会降低 target；
- 连续成功达到恢复阈值后逐步加一；
- 保留 ExecutionScheduler 的 INTERACTIVE > FOREGROUND > BACKGROUND 优先级；
- 当前 target、峰值、回退原因和恢复状态进入 Job trace。

默认 Persona research 并发范围是 1–6，初始 3；不是通过延长 timeout 掩盖无活动或无 liveness。

## 6. Room、Static Kernel 与 World

- `StaticPersonaKernel` 先使用 persona id、compile/runtime/continuation/digital/manifest revisions 和 branch 计算 cache key，再决定是否 build；cache hit 不再先做昂贵 build。
- persistent Room 只发送 transcript cursor 后的 delta；runtime 崩溃后重建 thread，并用 static kernel + summary + memories + recent raw turns 做有界 rehydration。
- 12 persistent participants 回归全部完成，重返 participant 使用 delta。
- World actor session 按 `(world, branch, actor)` 持久化；runtime 错误会丢弃损坏 session，下次重建。
- World 的 active actor limit 与 proposal concurrency 分离：默认每 tick 选 6，可配置到 10/20/50；无论 actor 数量多大，proposal 并发仍受 semaphore 约束，resolution 保持串行因果顺序。

## 7. Production tracing

每个 Persona Job 持久化 `job_config.performance_trace`，包括：

- research、evidence intelligence、dimension synthesis、global audit、compile 等阶段耗时；
- LLM Turn 总数及 research plan/search/fetch/intelligence/dimension/audit/repair/compile 分类；
- prompt/output token 估算或 provider 报告值；
- LLM latency、scheduler wait、search/fetch latency 的 count/average/P50/P95/max；
- search query、fetch URL、unique source、session open、process spawn、model/list、probe 等计数；
- adaptive scheduler snapshot 和 research stop reason。

公开 Job API 删除上传材料正文后返回 trace；`/api/performance/summary` 提供只读的 recent tasks、global counters、runtime pool、scheduler、capability cache、Room/World snapshot。

## 8. 性能对照

命令：

```bash
uv run python scripts/performance_benchmark.py
```

基准为离线确定性 fake runtime。两列使用相同 Research Policy 和相同最终审计门禁；每个 fake 模型 Turn 固定 0.2 秒，用于比较调用拓扑，不代表真实 provider 的绝对吞吐。

| metric | optimized | legacy |
|---|---:|---:|
| wall_ms | 18,804 | 25,843 |
| persona_total_ms | 17,747 | 23,963 |
| research_sources | 60 | 60 |
| broker_fetch_calls | 20 | 66 |
| model_call_count | 57 | 79 |
| model_list_count | 3 | 69 |
| agent_session_open_count | 42 | 65 |
| room_delta_turns | 6 | 0 |

该场景连续创建 3 个来源重叠的 Persona，因此 optimized 平均约 19 Turn/Persona。独立的单 Persona、60 来源完整 Deep Policy 回归断言 `llm_turn_count <= 30`。

## 9. 验证证据

最终门禁：

```text
uv run pytest -q
602 passed, 2 skipped, 1 warning in 795.33s

uv run ruff check .
All checks passed!

uv run mypy
Success: no issues found in 177 source files
```

唯一 warning 来自既有 tamper-detection 测试故意在 ZIP 中写入重复 `data/sources.jsonl`。

重点新增/扩展回归：

- pool=4 / logical=20 无饥饿；8 dimensions、10 Persona、10 World、12 Room 组合共 40 logical sessions。
- 12 persistent Room participants；runtime crash 后重建与有界 rehydration。
- 10 persistent World actors，active limit=10，峰值 proposal concurrency `<= 4`。
- Adaptive scheduler 限流回退与连续成功恢复。
- batch research transport 和 public factual cache namespace。
- Static Persona Kernel 两次获取只 build 一次。
- Strict audit PASS、定向 repair、最终 fail-closed。
- 60 来源完整 Deep Policy：min/preferred targets、life stages、contradiction、negative evidence、marginal gain 全开启；八维和 provenance 完整，optimized `<= 30` Turn，audit prompt 有界。

## 10. 验证边界

本轮没有启动一次可能耗时数小时的真实付费 Persona 创建，也没有据此声称真实 Codex 的端到端绝对耗时已经实测下降。已完成的是：真实生产调用链改造、离线确定性前后对照、完整 Deep Policy 质量回归和全仓回归；新的 production trace 会在下一次真实创建时直接给出各阶段、模型 Turn、P50/P95 和等待时间，可与用户此前的真实运行记录对照。
