# Local Material Pipeline 性能重构验收

日期：2026-08-28

## 结论

本轮按《优化计划.md》在统一 Persona Material Pipeline / Agent Runtime
Abstraction 层完成增量重构。实现不识别 Qoder、Codex、OpenAI 或其他 Provider
名称；执行策略只依赖解析后的 context window、persistent session、parallel safe、
structured output、reasoning 与工具能力。

这次没有删除八维、Evidence Ledger、contradiction、uncertainty、final audit、
consistency audit 或 provenance，也没有默认降低 reasoning。

## 数据流

当前异步本地资料链路为：

```text
format-aware parsing
→ atomic EvidenceUnit
→ pre-LLM exact/normalized/near dedup
→ canonical evidence + complete supporting provenance
→ token-aware AnalysisWindow
→ one-pass Evidence Intelligence
→ global deterministic Candidate Index
→ targeted relation verification
→ deterministic singleton/exact fusion bypass
→ semantic fusion only for affected multi-evidence groups
→ Fused Evidence Index
→ 8-dimension bounded retrieval/extraction
→ Evidence/Consistency audit and targeted repair
→ compile/validate
```

增量路径只把新 Evidence 送入 Evidence Intelligence，并用 `AffectedSet` 记录新证据、
候选邻居、受影响 cluster 和 dimension。历史原子证据仍保存在 Ledger；不会因为减少
Agent turn 而合成不可反查的大证据块。

## 关键实现

- `application/material_pipeline.py`
  - `ResolvedExecutionProfile`
  - `PreLLMDeduplicator`
  - `AnalysisWindow`
  - `GlobalCandidateIndex`
  - `AffectedSet`
  - `MaterialPipelineMetrics`
- `application/material_intelligence.py`
  - canonical-only one-pass intelligence
  - persisted semantic cache key：material hash、parser/schema、model、reasoning、prompt
  - AnalysisWindow checkpoint persistence
  - candidate-group relation；不再无条件 `all_units → Agent relate`
  - singleton/exact duplicate 为零 Agent fusion
  - delta + affected-neighbor incremental execution
  - Agent turn、input/output token estimate、cache hit、elapsed、affected-set 指标
- `agent/context_budget.py`
  - 从实际选中 model capability 解析 context window
  - 记录 `provider_reported / adapter_declared / config_override / fallback`
  - 统一 `TokenEstimator`；CJK 不再按 `chars / 4` 严重低估
- `application/persona_creation_service.py`
  - `LOCAL_MATERIAL_SYSTEM_PROMPT` 与 `PUBLIC_RESEARCH_SYSTEM_PROMPT` 隔离
  - Local Material session 保持 `allow_mcp=False, tools=[]`
  - capability snapshot 与 runtime binding 合并后传入 material pipeline
  - parallel-safe runtime 为每个并发 worker 使用独立 logical participant
- `agent/models.py`
  - capability contract 增加 provider-neutral `parallel_safe`

## 性能基准

命令：

```bash
uv run python scripts/local_material_performance_benchmark.py
```

条件：同一 1000 条短聊天、40% 重复、同一 fake runtime、32,768 context、high
reasoning。八维与两次严格 audit turn 在两列保持相同。

| 指标 | Legacy topology | Optimized topology |
|---|---:|---:|
| EvidenceUnit | 1000 | 1000 |
| Unique Evidence | 1000 | 600 |
| AnalysisWindow | N/A | 4 |
| Material Intelligence turn | 42 | 4 |
| Relation turn | 34 | 0 |
| Fusion turn | 38 | 0 |
| Dimension turn | 8 | 8 |
| Audit turn | 2 | 2 |
| 总 Agent turn | 124 | 14 |
| Input token（统一估算器） | 63,000 | 12,600 |
| Output token（同一确定性响应模型估算） | 58,896 | 17,900 |
| Provenance coverage | 100% | 100% |
| Singleton Agent fusion | 未旁路 | 0 |

结果：Agent turn 下降约 88.7%，估算 input token 下降 80%，估算 output token
下降约 69.6%。收益来自 40% pre-LLM
去重、AnalysisWindow token packing、取消全量 relation rescan、singleton/exact fusion
bypass；不是来自更换模型、降低 reasoning、删维度或放宽审计。

该 benchmark 是离线调用拓扑证据，不是实际付费 Provider 的绝对延迟测试。
`wall_clock_ms` 只表示本地 planner CPU 时间。表中的 token 是统一 estimator 对同一输入与
确定性响应结构的估算；真实 Provider usage 仍必须以 production trace 中的 runtime
metadata 为准。

## 性能与质量测试

`tests/performance/test_local_material_creation.py` 覆盖：

- 1000 / 5000 条聊天
- 10 万字资料
- 40% 重复
- 10,000 历史 + 20 增量
- 多文件重叠 provenance
- 长文本无静默截断
- 大量超短中文 token estimation
- 8 种 Runtime capability matrix
- singleton fusion 0 Agent call
- semantic cache 命中
- 增量 Agent intelligence 只读取 delta

验证状态：

- Focused Local Material + Performance：45 passed
- Ruff：passed
- mypy：passed（178 source files）
- Unit：364 passed
- E2E + Performance：29 passed, 1 skipped
- Integration（33 个文件、三个互斥并行分片）：245 passed, 1 skipped, 1 warning
- 全量覆盖合计：638 passed, 2 skipped, 1 warning

## 架构审计

已在本轮修改面搜索以下形式：

```text
if provider == ...
if adapter_id == ...
if model_id.startswith(...)
qoder / codex / cursor / gemini / openai
```

Material Pipeline、context budget、性能测试和 benchmark 未新增 Provider-specific
fast path。未来 Adapter 只需报告统一 capability，不需要修改 Persona Material
Pipeline。
