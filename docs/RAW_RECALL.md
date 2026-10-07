# RAW_RECALL.md — Memory Architecture v2, Phase 7（Provenance-backed Raw Recall）

## 0. 这一层解决什么

Phase 3–6 让系统能说「过去发生过这件事」。Phase 7 让它在需要细节时能够回到
**当时的原话**：

```
Semantic Fact：用户计划去重庆
   ↓ provenance
Episode：讨论重庆旅行
   ↓ provenance
Raw Turn：用户："我订的是晚上 8 点那班车。"
```

Summary 永远是压缩，压缩必然丢细节。当用户问「我当时买的几点的票？」，
只有回到 raw turn 才能不撒谎地回答。

**不是**什么：不是把旧 transcript 重新塞进 prompt，不是 GraphRAG，不是新的
向量系统，不是 Context Policy 的一部分，**不进入 production prompt**（那是
Phase 8）。

## 1. 硬规则（代码强制，不是偏好）

| 规则 | 落点 |
|---|---|
| 原文不得重写 / 总结 / 润色 | `HistoricalExcerpt.messages[].raw_text` 只来自 `session_turns` / `room_transcripts` |
| 允许的加工只有三种 | 结构化 speaker label、时间格式化、**消息边界**截断（`_trim_excerpt_tokens` 逐条弹出，绝不劈断一条消息） |
| 不拿 Summary 当事实源 | Summary 只是检索入口；`_resolve_summary_ref` 一路走到 Episode/Turn |
| 证据不足不许编 | `unavailable` / `partial` / `deleted` 显式返回，绝不用 Summary 现编一段 |
| 不做第三套 speaker 推断 | 复用 `EpisodeService.turn_speaker_label()` / 新增 `resolve_turn_messages()` |
| Token 是唯一预算单位 | `RawRecallBudget`，message count 只是 safety cap（`max_turns_per_excerpt`） |
| 与 Context Policy 完全解耦 | service 源码里没有 6000 / 7000 / 8，不 import `context_policy`（有测试锁定） |

## 2. 统一入口：`RawRecallService`

```python
result = continuum.raw_recall.recall(
    scope=RawRecallScope(persona_id, counterpart_id, branch_id),
    query="我那次为什么没去成重庆？",
    memory_refs=[RawMemoryRef(ref_type="fact", ref_id=fact_id), ...],
    budget=RawRecallBudget(max_total_tokens=3000, max_excerpts=3,
                           max_tokens_per_excerpt=1200, max_turns_per_excerpt=12),
)
result.excerpts        # list[HistoricalExcerpt]
result.unavailable     # 解析失败的 memory ref + 原因
result.explain()       # 每个 excerpt 的「为什么选中我」
```

五种入口 + 一条可选 lineage 通路，全部走同一套 anchor 模型：

| 入口 | provenance 路径 | anchor 强度 |
|---|---|---|
| Fact → Raw | `memory_fact_sources`（turn 直连） | 1.00 `fact_direct_evidence` |
| Thread → Raw | `memory_thread_events.source_turn_id` | 0.90 `thread_event_source` |
| Thread → Raw | `memory_thread_sources.turn_id` | 0.85 `thread_source` |
| Episode → Raw | `memory_episode_turns` | 0.50 `episode_anchor` |
| Chapter → Raw | `memory_summary_sources` → Episode | 0.35 `chapter_descendant` |
| Long-term → Raw | summary → chapter → episode（深度 ≤ 4） | 0.30 `long_term_descendant` |
| Digital Experience（可选） | `lineage` 表 `episode → session_turn` | 0.60，**不为此改任何现有结构** |

作用域：`(persona_id, counterpart_id, branch_id)` 三元组全部校验，任何一条
ref 跨 scope 都以 `unavailable(scope_mismatch)` 返回，绝不跨域取原文。

## 3. 选择：anchor → window → excerpt

```
anchors (memory 指到的 turn)
   ↓ 排序  score = provenance × (0.4 + 0.3×memory_relevance)
   ↓       query 不改变 provenance，只在同一 memory 内部决定「问的是哪一句」
windows (anchor ± 少量上下文, token 预算内)
   ↓ 合并 相邻 / 重叠窗口 → 一个 excerpt（不跨 Episode，不留洞）
excerpts (按相关性排序, 全局 token 预算内)
```

* **Anchor 优先**：fact 直指的 turn > thread milestone turn > lineage 直指 >
  query 命中 > episode 泛型 turn > chapter/long-term 后代。
* **Query-aware**：局部 BM25（CJK bigram + latin word，IDF 归一化到 0..1）。
  「我那次为什么没去成重庆？」会浮出取消相关 turn，而不是最早的「我想去」。
  「住宿后来怎么处理的？」能命中「换成江北区」，即使原话里没有「住宿」两个字
  —— 靠 lineage + 实体词，不靠新向量库。
* **Coherence**：anchor 的**前一条**永远优先纳入（那是它在回答的问题）；
  之后才向外交替扩展。某侧出现洞（来源缺失）即停住该侧，绝不跨洞拼接。
* **Dedup / merge**：同一 turn 被多个 memory 指到只出现一次（保留最强理由）；
  相邻或重叠窗口合并，重叠时保留 provenance 更强的一方。
* **Shared user 去重**：Phase 3.1 的 shared_user 事件与 persona 自己
  `session_turn` 的 user 半句会带同一段文字。excerpt 内两者同文时保留
  room 级原始事件，绝不把用户原话重复两遍（`_dedupe_shared_user`）。

## 4. `HistoricalExcerpt`

```
excerpt_id / persona_id / counterpart_id / branch_id
source_memory_refs / episode_ids / turn_ids
started_at / ended_at
messages[]: turn_id, speaker, timestamp, raw_text, source_kind, token_estimate
token_estimate
relevance_score / provenance_score
selection_reason (+ selection_detail: anchor、window_positions、query_score)
truncated / truncation_reason      # per_excerpt_budget | total_budget | too_many_turns
source_availability                # complete | partial | deleted | unavailable
```

可用性语义沿用 Phase 3.1 / Phase 6：

| 情况 | 返回 |
|---|---|
| 部分 provenance 被删 | `partial`，只返回仍可解析的 turns |
| 全部被删 | Episode ref → `unavailable`；excerpt 不产生 |
| Summary 仍在、raw 全没了 | **绝不**用 Summary 现编原文 |

## 5. 预算（Recall 参数，不是 Context Policy）

```
RawRecallBudget(max_total_tokens=4000, max_excerpts=4,
                max_tokens_per_excerpt=1200, max_turns_per_excerpt=12,
                max_context_window=4)
```

* 默认值与任何 profile 无关；`local_constrained` 的 6000/7000、
  `recent_message_window=8` 在这一层不存在。
* 给多大就给多大：`max_total_tokens=200000` 时 20 轮 × 2 条消息全部返回
  （测试 K 锁定），不会被偷偷夹到本地档位。
* `historical_excerpt_token_budget` / `historical_excerpt_messages` 这两个
  profile 字段是 **Phase 8** 的接缝，本轮不动（仍为 0/0/12000/6）。

## 6. 不进入 production prompt

Context Assembly Report 新增：

```
historical_excerpt_tokens = 0
raw_excerpts_selected    = 0
raw_excerpts_expanded    = 0
raw_excerpt_injection    = "phase8_not_enabled"
raw_recall_available     = true   # 层存在；数字为 0 是设计使然
```

Phase 7 只做 **Store resolution / Retrieval / Ranking / Excerpt construction /
Inspect / Benchmark**。之所以先不注入：必须先独立验证「Raw Recall 选出来的东西
对不对」，否则模型答错时无法区分是 retrieval 错、expansion 错、assembly 错还是
模型本身错。

## 7. Inspect / CLI

```
persona-continuum recall raw <persona_id> \
    [--counterpart user] [--branch main] [--query "..."] \
    [--episode id ...] [--fact id ...] [--thread id ...] [--summary id ...] \
    [--ref type:id ...] \
    [--max-total-tokens 4000] [--max-excerpts 4] \
    [--max-tokens-per-excerpt 1200] [--max-turns-per-excerpt 12] \
    [--json] [--text]
```

默认输出 score / selection reason / memory source / episode / turn ids /
speaker / time / token estimate / availability，**不打印正文**；`--text` 才显示
原文，`--json` 下正文以 `<N chars>` 占位。普通日志永不携带聊天正文。

## 8. 验证

* 单元 + 集成：`tests/integration/test_raw_recall.py`（19 个，覆盖 brief 的
  A–O 全部场景 + speaker / merge 纯逻辑 / CLI / prompt 注入关闭）。
* 真实数据快照：`scripts/phase7_raw_recall_snapshot.py` —— 生产库 sqlite
  backup 只读副本，≥20 个真实 provenance 对象，报告
  `docs/reports/phase7-raw-recall-snapshot.{json,md}`（含 excerpt 原文 vs 存储
  行的逐字对照样本，供人工抽查）。
* 真实模型 A/B：`scripts/phase7_raw_recall_ab.py` —— 临时 data dir，turns 内
  植入 Summary 通常不保存的细节，细节问题，A（仅 memory 层）vs
  B（memory 层 + Raw Recall），报告 `docs/reports/phase7-raw-recall-ab.json`。

### 8.1 A/B 基准的诚实结论（本轮实测）

实测（`gemini_cli` / gemini-3.8-flash-high / 16 turns / 3 Episodes）：
**Arm A = 12/12，Arm B = 12/12，detail_improvement = 0 → PARTIAL，不判 PASS。**

归因（有证据，不是猜测）：把记忆层 view 压到 613 字符后 Arm A 仍全对——因为
**Episode Summary 本身就把每个细节都留下来了**（车厢铺位、晚点时长、老板娘
名字、行程顺序、没吃成的火锅、落在民宿的伞、猫名、小陈原话、错题本颜色）。
记忆层在这个规模上**没有丢失信息**，raw recall 自然没有可补的东西。

这恰恰说明 A/B 想测的差距不属于 Phase 7，而属于 **Phase 8 的 Context Policy**：
只有当记忆块真正有预算、必须丢掉整段信息时，「按 query 定向回原文」才有可
测量的增量。本轮基准已经把 `--memory-view-chars` 这个预算参数做进去了，
Phase 8 接线后直接复跑即可。

**因此本轮的完成判定是：Phase 7 的 22 条 PASS 标准里，第 20 条（真实模型 A/B
有明确收益）为 NOT MET，其余全部满足。**
