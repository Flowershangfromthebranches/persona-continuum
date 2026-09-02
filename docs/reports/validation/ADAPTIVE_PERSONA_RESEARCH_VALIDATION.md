# Adaptive Persona Research Validation

验证日期：2026-08-25
验证范围：ResearchPolicy、Adaptive Stop Gate、来源独立性、人生阶段、边际信息增益、
Research Gap、私人资料覆盖、Persona Creation/World 集成、存储迁移、UI 契约和完整项目回归。

## 结论

自动化验证通过。验证期间修复了三类问题：

- 测试错误地把 `PrivatePersonaCoverage` 当成 `PrivatePersonaCoveragePolicy`；
- 新增实现存在格式、联合类型和可空 EvidenceSource 静态错误；
- Public Research 每轮停止判断复用了检索前的旧 gap，已改为在新来源摄取和增量八维提取后
  重新执行 coverage/gap audit，避免已补齐缺口仍把研究错误推到 hard budget。

## 自动化证据

- Research Quality 单元测试：`27 passed in 0.04s`。
- Persona Creation / World Completion / Persona E2E 聚焦回归：`49 passed in 112.60s`。
- 最终完整套件：`307 passed, 1 skipped, 1 warning in 497.21s`。
- Ruff：`All checks passed!`
- Mypy：`Success: no issues found in 149 source files`
- `node --check src/persona_continuum/web/static/app.js`：通过。
- `git diff --check`：通过。

跳过项是需要显式真实 Provider 配置的测试。warnings summary 中的单条 warning 来自 ZIP
篡改回归测试故意写入重复 `data/sources.jsonl`。完整套件进程退出时还打印了一次 asyncio
subprocess transport 在 event loop 关闭后析构的提示，但退出码为 0，且未计入失败。

## 核心验收覆盖

- Deep 不会因达到 30 个来源直接完成；八维、life stage、来源类别、primary/secondary、
  contradiction、high-priority gaps 与 marginal gain 会共同进入停止门。
- 70 个 raw repost 在确定性测试中只形成 3 个独立 SourceCluster，不能通过 Deep 的 30 个
  independent source floor。
- 所有来源只覆盖晚期阶段时，`life_stages_complete=false` 会继续研究。
- 任一 required dimension 未过门时，即使来源总量足够也不会 completed。
- coverage 全部通过且连续低 gain window 达标时，stop reason 为
  `coverage_satisfied_and_low_gain`；高 gain 时继续。
- 单个含 10,000 条消息的私人聊天导出按 message volume 计量，不套用公共人物来源数门槛。
- 旧 `max_sources` policy snapshot 仍按旧 soft/hard budget 继续，不被新默认 30/60 覆盖。
- Research checkpoint、source cluster、stop reason 和新增 job JSON 字段由完整存储回归覆盖。

## 未执行的外部验收

本轮没有启动 Web Server 做人工视觉验收，没有真实 CLI/API Provider 调用，没有真实 Web
Research Broker 调用，也没有创建真实 60/100+ 来源 Persona。因此以下内容仍需具备外部
Runtime、Credential 和 Research Broker 后单独验收：

- 真实搜索结果的 source quality / independence 分布；
- 真实模型生成 life stages、ResearchArtifact 与 gap-driven query 的质量；
- 真实 token、LLM call、Web fetch 和成本数据；
- 浏览器中的长任务进度、停止原因和继续研究交互。

自动化验证证明当前代码契约和本地运行链路一致，但不把模拟证据表述为真实深度研究完成。
