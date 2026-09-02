# [RFC] 为长篇小说增加非正史剧情多线推演（Narrative Forecast）

## Problem

长篇作者在正式写下一章前，经常需要比较多个尚未发生的未来：

- 主角接受提议、拒绝提议或第三方介入，分别会怎样发展？
- 哪条路线更符合人物动机、作者意图和当前卷目标？
- 每条路线会推进或破坏哪些伏笔、关系与世界状态？

InkOS 已经维护 `story/state/*.json`、作者意图、当前焦点、大纲、伏笔、章节摘要和人物关系等上下文，但目前缺少一个工作流，让作者从**当前长篇正史**出发，并排比较 2-5 条候选未来，再决定是否采用其中一条。

这与现有 InkOS Play 不同：

- **Play / 分支互动 / 开放世界**：玩家通过选项或自由动作持续推进一个互动世界，并保存该世界的运行状态。
- **Narrative Forecast / 剧情多线推演**：在长篇正史不变的前提下，临时推演多个未来方向，输出供作者比较的规划材料。

候选分支默认都是 non-canonical，不应成为新的正史时间线，也不应直接修改小说正文或权威状态。

## Proposed solution

建议增加一个小型的 `narrative-forecast` 工作流：

1. 读取当前长篇上下文：
   - `story/state/*.json`
   - `author_intent.md` 与 `current_focus.md`
   - story bible、book rules 与当前大纲
   - 活跃伏笔、支线和最近章节摘要
   - 当前人物、关系与世界状态
2. 根据作者提供的分歧点和约束，生成 2-5 个相互隔离的候选分支。
3. 每个分支记录：
   - 前提与假设
   - 未来若干章的剧情节拍
   - 人物决策
   - 人物、关系、世界和伏笔的预计变化
   - 连续性、因果一致性和人物一致性风险
   - 不确定性与作者意图匹配度
4. 将结果保存为非正史 runtime artifacts。
5. 作者可以显式选择一个分支，但第一版只生成规划文档，不自动修改正史。

可能的 CLI / Agent 接口：

```bash
inkos forecast create [bookId] --divergence <text> --branches 3 --horizon 5 --json
inkos forecast show [bookId] <forecastId> --json
inkos forecast select [bookId] <forecastId> <branchId> --json
```

对应内部操作可以是：

```text
create_narrative_forecast
get_narrative_forecast
select_narrative_branch
```

建议存储位置：

```text
story/runtime/narrative-forecasts/<forecastId>/
  forecast.json
  comparison.md
  selected-branch-plan.md
```

`forecast.json` 应记录基础章节号与 `contextFingerprint`。当正史章节、结构化状态或相关控制输入变化时，旧推演应标记为 `stale`。

第一版的安全边界：创建和选择推演都不能直接修改以下内容：

```text
story/state/*.json
story/current_state.md
story/current_focus.md
story/pending_hooks.md
chapters/
```

选择分支后仅写出 `selected-branch-plan.md`。将该计划应用到大纲、章节 intent 或正史状态，应是另一个需要用户确认的操作。

建议第一版范围：

- Zod schema 与结构化输出校验
- 本地确定性存储
- 基于现有长篇状态和控制面的 context builder
- 生成、读取和选择三个操作
- `contextFingerprint` 与 stale detection
- CLI / Agent 的 `--json` 输出
- 文档和测试

最低测试范围：

- Schema 校验
- 兄弟分支状态隔离
- 创建和选择操作不会修改正史文件
- 上下文变化后旧推演会过期
- 不存在的分支不能被选择
- 非法模型输出不会留下半成品文件
- LLM 使用 mock，测试不调用真实 API

## Alternatives considered

1. **直接使用现有 InkOS Play**

   Play 面向持续互动世界；本提案面向已有长篇正史的写前比较。二者的状态生命周期和最终产物不同。

2. **直接让模型一次性给出几个剧情建议**

   这种方式缺少分支隔离、结构化状态变化、过期检测和可审阅的本地产物，后续也难以安全地选择或复用。

3. **让 InkOS 直接依赖 Persona Continuum**

   不建议。InkOS 是 TypeScript/Node 项目，为该功能增加 Python、另一个运行时或后台进程会扩大安装和维护成本。更适合在 InkOS 内部实现轻量、自包含的 TypeScript 版本。

## Additional context

分支隔离和反事实状态的概念参考了 [Persona Continuum](https://github.com/Flowershangfromthebranches/persona-continuum) 中“模拟 continuation 不写入基础历史数据”的设计。

但本提案中的 InkOS 实现将保持完全自包含：

- 不依赖 Persona Continuum 运行时
- 不引入 Python 依赖
- 不改造或替代 InkOS Play
- 不自动重写正式大纲或正文
- 不生成所有分支的完整小说正文
- 不声称预测现实世界

想请教维护者：

1. 这个面向长篇规划的工作流是否适合 InkOS？
2. 更适合接入现有 Planner/Composer 输入治理链路，还是单独放在一个小型 `narrative-forecast` 模块？
3. 第一版是否应只输出 `selected-branch-plan.md`，把任何正史更新留给后续确认操作？

如果方向合适，我可以从最新 `master` 准备一个聚焦的第一版 PR，并保持它与 PR #304 相互独立。
