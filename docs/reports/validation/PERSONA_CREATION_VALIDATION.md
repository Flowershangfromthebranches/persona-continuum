# Persona Creation Validation Report

验证日期：2026-08-25
验证范围：Persona Creation Runtime、World Persona Completion、Runtime 超时与清理、静态质量门禁、完整项目回归、受控真实 CLI Agent 调用。

## 结论

代码与自动化回归通过。验证期间修复了 exact Persona Match 重复候选、异步测试契约、
Plain CLI/ACP 无限等待、ACP 取消清理，以及 Persona Creation 只限制协议读取而未覆盖
Runtime probe/session 建立的超时边界。

真实 Grok ACP 最小调用成功；完整虚构 Persona 创建能够正式写入 EvidenceSource，但所选
模型未在验收预算内生成第一个合法 ResearchArtifact。修复后该任务会显式进入 `failed`，
持久化 `persona_creation_failed`，不会卡死、fallback 或伪造 compiled Persona。

## 自动化证据

- Persona Creation / World / UI contract 聚焦套件：`22 passed`。
- 协议超时与受影响回归：`48 passed`；最终 Persona 聚焦回归：`19 passed`。
- 完整回归：`280 passed, 1 skipped, 1 warning`，用时 `467.21s`。
- 跳过项：`test_real_provider_connection`，原因是未设置
  `PERSONA_CONTINUUM_REAL_PROVIDER_PROFILE`。
- Ruff：`All checks passed!`
- Mypy：`Success: no issues found in 148 source files`
- `node --check src/persona_continuum/web/static/app.js`：通过。
- `git diff --check`：通过。
- 严格资源警告子集：Runtime smoke `5 passed, 1 skipped`；Unit `132 passed`；
  Codex app-server + Persona Integration `14 passed`。

完整套件仍在进程退出时出现一次 asyncio subprocess transport 析构提示，但在以上严格
子集（将 `PytestUnraisableExceptionWarning` 升级为错误）中均无法复现。唯一 pytest
warnings summary 是 ZIP 篡改测试主动制造重复 `data/sources.jsonl` 成员产生的预期警告。

## 真实 Runtime 证据

实时发现到 READY Local CLI：CodeBuddy、Codex、Command Code、DeepSeek Harness、Gemini
CLI、Grok、Qoder、WorkBuddy。Cursor 为 detected。当前所有已发现 CLI 对 Persona
Continuum 报告的 `web_search/web_fetch/browser/research_mcp` 均为 false。

因此 Public Deep Research 被 Research Capability Gate 正确阻止；本次没有执行真实公众
人物联网研究，也没有使用模型训练记忆替代来源。

真实最小调用：

- Agent：`grok`
- Model：`ocx-gpt-5-6-luna`
- Reasoning：`low`
- 返回：`VALIDATION_OK`
- Stop reason：`end_turn`
- Token usage：运行时事件未返回 usage，实际 Token 数不可观测，未估算或伪造。

CodeBuddy `hy3` 在当前环境未返回模型事件。修复后 20 秒预算内返回明确错误
`CLI response timed out after 20s`。Codex `default` 返回空 `done`；Persona Creation 的
`agent_returned_empty_output` 检查会拒绝该结果。

## 真实 Persona Creation 尝试

受控对象：虚构角色“林澈验证角色”。输入三份本地材料，未进行 Web Search。

- `source_count`: 3
- EvidenceSource：已正式持久化
- Agent / Model：`grok` / `ocx-gpt-5-6-luna`
- 首维：`identity_and_timeline`
- ResearchArtifact：0
- CompilationTask：`created`
- Manifest：`draft`

模型持续 thinking 且未产生合法 artifact。将整个 Agent 调用预算设为 5 秒后，两次结构化
尝试在约 11.7 秒内结束，Job 正式变为：

- status/stage：`failed`
- error：`agent_turn_timed_out_after_5s`
- event：`persona_creation_failed`

这验证了 Evidence 保留、失败持久化、无 silent fallback、无虚假 compile。它不构成
“完整 Persona 真实编译成功”的证据。

## 本轮修复

- exact name/id/alias candidates 按 Persona ID 去重，避免同一 Persona 被误报 AMBIGUOUS。
- Plain CLI 和 ACP 使用整轮总预算，不允许 chunk/thinking 无限续期。
- Persona Creation 的总预算覆盖 Runtime snapshot、session 创建、send 与 close。
- CLI timeout/non-zero exit 转换为显式 Agent error event。
- ACP/CLI 超时和关闭路径等待子进程退出，减少 transport 泄漏。
- 新增 `test_plain_cli_send_times_out_and_cleans_process`、
  `test_acp_send_times_out_and_closes_session`、
  `test_persona_agent_turn_timeout_covers_entire_call`。

## 未完成的外部验收

- 未配置真实 API Provider profile，因此没有 Provider models endpoint 的真实连接证据。
- 没有具备 Web Research capability 的 Runtime/Broker，因此没有真实 Public Deep Research。
- Grok 未在本次材料与预算内完成八维 artifact/compile；需要换用能稳定输出严格 JSON 的
  Runtime，或配置支持 schema output 的 API Provider 后重试。
- 未执行浏览器人工视觉验收；UI contract 与现有 Web E2E 自动测试已通过。
