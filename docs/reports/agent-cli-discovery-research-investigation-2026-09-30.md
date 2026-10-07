# Agent CLI 模型发现与联网能力调查

日期：2026-09-30（Asia/Shanghai）。本次仅调查，未修改程序、CLI 配置、数据库或运行中的 Web 服务。保留工作区既有未提交改动。

## 结论

Gemini 的模型缺失与当前联网失败有同一条主要因果链：项目用一个适配器承接两种不兼容的 CLI；当前选中的官方 `gemini` 被传入 `agy` 的模型发现命令、模型 ID 和推理参数。模型查询失败后返回旧静态表；随后联网验证在 CLI 参数解析阶段退出。共享诊断又截掉了原始参数错误，把它展示为搜索不可用。

OpenCode、Command Code 已确认有其他模型发现问题。Qoder 当前未登录，外层探测超时还会遮蔽认证原因。共享联网验证存在可复现的假阳性。

## 1. Gemini：二进制身份与协议混用（已确认）

当前 `/api/agents` 的 Gemini 信息：

- 二进制：`/Users/leaf/.npm-global/bin/gemini`，官方 npm 包 `@google/gemini-cli`，版本 `0.62.0`。
- 模型：`gemini-3.7-flash-high`、`gemini-3.7-flash`、`gemini-2.5-pro`、`gemini-2.5-flash`；来源全部为 `official_capability_table`。
- 联网：`unavailable`，错误 `WEB_SEARCH_UNAVAILABLE`，说明文本为 CLI help 的尾部。

本机同时安装 `/Users/leaf/.local/bin/agy`，版本 `1.2.8`。`agy models` 返回 14 项，包括：

```text
gemini-3.8-flash-high    Gemini 3.8 Flash (High)
gemini-3.8-flash-medium  Gemini 3.8 Flash (Medium)
gemini-3.8-flash-low     Gemini 3.8 Flash (Low)
```

官方 Gemini 安装包也包含 `LATEST_GEMINI_FLASH_MODEL = "gemini-3.8-flash"`。这证明安装包知道该 ID；不等于已经验证当前账户的模型调用权限。

原因位置：

- `src/persona_continuum/agent/adapters/gemini.py:83`：候选二进制优先选择 `gemini`，随后才是 `agy`。
- 同文件 `:99`：两者统一设置 `reasoning_flag="--effort"`。
- 同文件 `:503`：静态兜底表仍只有旧四项。
- 同文件 `:546`：两者统一调用 `<binary> models`。官方 `gemini --help` 没有这个模型列表子命令，`models` 会进入默认 query 路径；`agy` 则明确提供 `models` 子命令。
- 同文件模型后缀处理逻辑将 `-high/-medium/-low` 与 effort 绑定，这是 agy 的契约，不能据此验证官方 Gemini 的参数支持。

最小实测：

```text
gemini --effort high -p "Reply OK"
exit 1
Unknown argument: effort
```

约一秒退出，未进入模型生成或搜索工具执行。因此仅添加 Gemini 3.8 到静态表不能解决本次联网失败。

## 2. 原始参数错误被截断，导致错误归类失真（已确认，共享影响）

`src/persona_continuum/agent/subprocess_transport.py:94` 在交给适配器分类前，将 stderr 缩为尾部 4,000 字符。

实测官方 Gemini 参数错误 stderr 长度为 4,014；原文包含 `Unknown argument: effort`，截断后不包含。`GeminiCliAdapter.classify_process_failure()` 收到截断版本，未产生 `cli_invalid_argument` 子类型。

`application/research_backend.py:673` 的分类器也未单独映射 CLI 参数错误，最终统一变成 `WEB_SEARCH_UNAVAILABLE`。即使原始错误保留，当前分类器仍将 `Unknown argument: effort` 归为该码。

这解释了数据库及界面只显示 output-format/raw-output 等帮助尾部的现象，也会影响其他使用共享 Plain CLI transport 的工具。建议从有界原始 stderr 中先提取关键错误行再分类，保留开头和尾部的安全摘要，并将 CLI 契约错误与搜索失败分别表示。

## 3. 其他 Agent 工具

| 工具 | 当前调查结果 | 原因与边界 |
| --- | --- | --- |
| OpenCode 1.18.22 | 原生 `opencode models` 返回 52 项，适配器返回旧 9 项 | `_parse_models()` 将所有动态项设为 `selectable=False`，而 `list_models()` 只有存在 selectable 项才返回动态列表。因此成功探测也必定兜底。应保留已发现列表与能力不确定性，模型选择和 effort 验证分别处理。 |
| Command Code 1.66.0 | 原生 `--list-models` 耗时 24.58 秒，解析出 87 项；运行中服务仅有 21 项 | `list_models()` 限时 10 秒，失败后静默返回 20 项静态表；状态查询会额外插入当前模型。Discovery 总限时为 12 秒，即使放宽内部限时也仍会被外层取消。当前缺失项包括原生命令实际返回的 Gemini 3.8、GPT 6、Claude 5.5 等。 |
| Qoder 1.1.41 | 全局 `qodercli` 和 `~/.qoder/entry/qoder` 均返回 `Not logged in`；服务显示探测超时 | 两个入口分别约 6.32/5.96 秒，dispatcher 实际转发同一个 CLI；串行尝试加版本查询可能超过 12 秒总预算。当前认证错误被超时遮蔽。代码在无动态结果时仍兜底，基础 probe 又只凭 version 成功判断 READY；因此完成探测后也存在误报 ready 的路径。 |
| Codex 0.147.0 | 服务列出 23 项，来源 `protocol_model_list`，含 GPT 6 和其他当前模型 | 本次未发现同类静态列表缺失。联网仅为 help 推断的 declared，未进行新的真实搜索/抓取验收；仍受共享验证逻辑影响。 |
| Grok 1.0.41 | 服务列出 27 项，来源包含 `official_cli` | 本次未确认同类模型缺失。联网 unknown 代表未验证，不能等同于不支持或故障。未进行新的模型生成调用。 |
| DeepSeek Harness | 服务列出 5 项，全部来源 config | 使用静态模型表，存在随版本落后的风险；未证明本机实际遗漏哪些可用模型。联网未验证。 |
| Claude / Qwen / Kimi / CodeBuddy / WorkBuddy | 服务报告 disabled，模型为空 | 本机对应 CLI 未探测到，不能据此判断其工具能力有故障。 |
| Copilot | 服务报告 broken，选中的是 gh | 当前 `gh copilot --version` 路径不可用；适配器混列 gh 与独立 copilot，却统一使用 gh 子命令参数，存在入口契约混用风险。未验证独立 copilot 安装。 |

相关位置：`agent/adapters/opencode.py:159,216`；`agent/adapters/command_code.py:107,170`；`agent/adapters/other_vendors.py:259,327`；`agent/protocols/plain_cli.py:296`；`agent/discovery.py:21`。

## 4. 共享联网验证存在假阳性（已用模拟实验确认）

`application/research_backend.py:512` 固定要求所有 CLI 使用 `google_web_search`，并在 `:255` 固定声明 Gemini 风格工具名。其他 CLI 工具名可能不同；这是适配风险，本次未证明它已经导致某个非 Gemini CLI 的真实搜索失败。

更确定的问题是：验证不检查 `searched` 必须为真，fetch 结果只要含 title 或 url 就可通过；没有正文也可由独立 HTTP URL 检查补足。这只能证明 Web 服务能读取该网址，不能证明 Agent 执行了搜索和抓取。

本次在内存中替换 `_ask` 与 URL validator，返回以下明确的模拟响应：

```json
{"searched": false, "sources": [{"url": "https://developers.openai.com/", "title": "Known public page"}]}
{"fetched": false, "url": "https://developers.openai.com/", "title": "Known public page"}
```

在 validator 返回非空成功信息时，结果仍为 `verified, search=True, fetch=True`。没有写入真实能力缓存；该实验不属于任何 CLI 的真实联网验收。

建议优先使用协议可观察的工具事件。没有事件的 CLI 至少应严格要求搜索/抓取成功字段、非空正文、对应 URL，并将验证级别标为响应声明；独立 HTTP 校验只能补充来源可访问性。

## 5. 当前网络与缓存边界

- 调查 shell 和正在监听 8000 的服务进程均未发现代理环境变量；本机 7897 代理端口开放。
- `https://developers.openai.com/` 本次直连与经 7897 代理均返回 HTTP 200、非空正文。因此没有证据把本次 Gemini 参数解析失败归因于缺少代理。
- 该 HTTP 检查不证明 Google 模型端点、账户地区限制或其他供应商网络路径正常。旧的地区拒绝历史不能作为当前失败原因。
- 数据库曾记录 agy 1.1.26 / Gemini 3.8 high 联网验证通过；这是历史记录，当前 agy 1.2.8 没有进行新的真实模型联网验收。
- Discovery 在显示 Agent 卡片时使用同版本最新的模型级研究结果，且内存缓存无自动 TTL 刷新；前端发起重新验证又默认使用列表第一项。某个模型失败可能被展示并作为整个 Agent 不可研究的依据，应让模型/权限配置与能力状态一一对应。

## 6. 修复顺序建议

1. 明确官方 Gemini 与 agy 的 runtime 身份、模型列表获取、推理参数和输出协议。保留既有 agent_id 的迁移兼容，不以静态换 ID 代替协议修复。
2. 修复共享 stderr 关键错误提取、CLI 参数错误分类与联网验证假阳性。
3. 修复 OpenCode 丢弃动态目录；让 Command Code 模型元数据探测独立于短时状态探测，使用明确的新鲜度/失败状态与后台刷新，避免全局盲目加超时。
4. Qoder 去重实际 CLI 入口、显示 auth_required，并区分历史目录与当前账户可用目录。
5. 新版本适配完成后才做最小真实验收：选定模型一次搜索、一次抓取，核对工具证据和来源；不自动重跑人格创建任务。

## 7. 核心检查

本次已有核心测试：

```text
uv run pytest tests/unit/test_agent_adapters.py tests/unit/test_gemini_cli_effort_binding.py tests/unit/test_agy_streaming_adapter.py tests/unit/test_research_probe_failure_classification.py tests/unit/test_qoder_adapter.py tests/unit/test_command_code_adapter.py -q
39 passed

uv run ruff check .
All checks passed

uv run mypy
Success: no issues found in 257 source files
```

这些检查证明当前核心测试与静态检查通过；真实 CLI 元数据及最小参数解析实验另行揭示了上述兼容缺陷，不能将测试通过理解为工具已经正常。
