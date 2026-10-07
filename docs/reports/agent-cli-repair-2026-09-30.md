# Agent CLI 模型目录和联网验证修复

日期：2026-09-30。保留原有未提交工作；未提交 Git、更改全局 CLI 配置或自动重跑人格创建任务。

## 已修复

- 保留 `gemini_cli` 对应 agy，新增 `gemini_google` 对应官方 Google CLI；两者显示不同名称并使用各自参数。官方入口从安装包读取模型 ID，不再把 `models` 当作子命令调用，也不接收 agy 的 `--effort` 和推理后缀。官方 CLI 的显式凭据传入 `GEMINI_API_KEY`。
- agy 使用真实模型目录的短期缓存，按二进制身份失效；手动重新扫描强制刷新。刷新失败保留同一二进制近期成功发现的目录，同时显示诊断；不返回旧静态模型表。
- OpenCode 保留成功发现的模型目录；模型可选性与推理档位验证分别处理，不再凭模型名称猜测 effort。ACP 仍需验证实际模型绑定。
- Command Code 的探测进程设置 `DO_NOT_TRACK=1`，避免元数据已生成后仍被遥测拖住。该设置仅作用于探测子进程。实测原生查询从 24.6 秒及偶发超过 30 秒，降至约 3 秒；保留 10 秒目录预算和独立 22 秒整项探测预算，缓存成功目录 5 分钟。失败明确报告，不再静默返回静态模型表。
- Qoder 去除重复 dispatcher 入口，并行探测不同版本；未登录明确返回 `auth_required`、空模型目录。
- stderr 摘要保留开头与尾部，分类先提取真正的参数错误行。CLI 参数不兼容不再显示为搜索失败。
- agy stream-json 的 `ERROR` 终态产生不可重试错误；地区资格拒绝不会变成空回答，也不会静默再次用 argv 执行同一请求。
- 共享联网验证改用各 CLI 的原生工具描述。必须满足 `searched=true`、`fetched=true`、非空正文和对应 URL；标题或可访问的网址不再替代抓取结果。验证方式明确标为 `response_declaration+http_validation`，不声称单凭 JSON 已观察到工具执行事件。
- 研究能力缓存增加契约版本，旧宽松验证不会继承。Agent 卡片标注验证模型；一个模型的失败不再在前端阻止另一个模型。弃用 Policy Engine 提示不再被单独识别为权限拒绝，缺少 Gemini API Key 归为认证问题。

## 本机和运行中服务检查

确认没有正在执行的人格创建任务及服务子进程后，已重启 8000 端口的本地 Web 服务。最终 `/api/agents`：

| 入口 | 当前版本 | 模型数 | 结果 |
| --- | --- | ---: | --- |
| Gemini CLI (agy), `gemini_cli` | 1.2.14 | 14 | 包含 Gemini 3.8 Flash high、medium、low |
| Gemini CLI (Google), `gemini_google` | 0.62.0 | 10 | 包含 `gemini-3.8-flash`，不虚构 effort 支持 |
| OpenCode | 1.18.22 | 52 | 保留真实 CLI 目录 |
| Command Code | 1.72.4 | 87 | 目录探测成功，无探测错误 |
| Qoder | 1.1.41 | 0 | `auth_required`，需要登录 |

CLI 版本在调查期间发生变化；以上为最终实测值。

通过真实应用端点重新验证 `gemini-3.8-flash-high`，返回 `ok=true` 的诊断响应：`research_model_id=gemini-3.8-flash-high`、`verification_status=unavailable`、`verification_error_code=WEB_RESEARCH_REGION_BLOCKED`。

## 外部条件仍未满足

- 当前 agy/Antigravity 账户返回：`not currently available in your location`。直连及当前 7897 代理均被服务方地区资格检查拒绝。这是账户/出口条件，代码修复无法代替资格检查；本次没有得到真实联网成功的结果。
- 官方 Google CLI 当前选择的 API 认证方式缺少 `GEMINI_API_KEY`。需要配置对应凭据或在官方 CLI 中选择有效认证方式；本次没有修改认证配置。
- Qoder 当前尚未登录。

## 核心验证

106 项核心测试通过，覆盖 Gemini 模型和 effort 绑定、agy 流式错误、真实目录缓存、OpenCode 目录保留、Command Code 探测环境及失败行为、Qoder 认证状态、共享联网假阳性、错误分类和旧验证缓存失效。

```text
uv run pytest tests/unit/test_cli_discovery_research_regressions.py tests/unit/test_gemini_cli_effort_binding.py tests/unit/test_agy_streaming_adapter.py tests/unit/test_research_probe_failure_classification.py tests/unit/test_qoder_adapter.py tests/unit/test_command_code_adapter.py tests/unit/test_gemini_research_numeric_normalization.py tests/unit/test_agent_runtime_contract.py tests/integration/test_cli_research_capability.py -q --tb=short
106 passed

uv run ruff check .
All checks passed

uv run mypy
Success: no issues found in 257 source files

node --check src/persona_continuum/web/static/app.js
Passed
```

工作区仍存在既有的其他文件 EOF 空行和 app.js 其他位置空白问题；未清理这些无关改动。

## 截图问题的补充修复（2026-10-01）

首轮 API 曾返回完整目录，但没有验证用户实际使用的选择页面，且后续冷扫描发生过目录超时。原有实现把空目录缓存为 READY；正在扫描时，请求还可能提前取到不完整的缓存。

- 发现服务现在等待正在执行的扫描完成，再给页面返回目录。agy 首次远程目录失败时，在其他并行探测完成后单独重试一次；目录预算独立设为 20 秒。手动刷新失败不会覆盖同一二进制 6 小时内的成功目录。前端明确显示探测失败，不把失败伪装成默认模型能力。
- agy 的每个 Gemini 模型根据真实目录中同一模型家族的 ID 显示支持的全部档位。3.8 Flash 显示 high/medium/low；3.1 Pro 只显示实际报告的 high/low。选择 medium 后实际绑定 `gemini-3.8-flash-medium` 与 `--effort medium`。
- Google CLI 没有 `--effort`，但支持原生模型配置。新增每次调用隔离的 `GEMINI_CLI_HOME` 和模型别名，把思考强度传到 `generateContentConfig.thinkingConfig.thinkingLevel`。原有用户配置不被修改，登录文件通过符号链接保留，临时配置权限为 0600，调用结束清理。不能使用临时“系统配置”：当前 CLI 会忽略非 root 目录中的系统设置，已通过真实解析发现并避开这一问题。
- Google 3.8 Flash 支持 low/medium/high，不提供不支持的 minimal；从对话模型目录排除了 embedding 模型。

本机最终 API：agy 14 项模型，Google 9 项对话模型，均包含 3.8 Flash；无目录错误。正式服务等当前调用结束、房间恢复 ready 后重启，未中断正在生成的回答。

实际安装的 Google CLI `loadSettings` + `ModelConfigService` 已分别解析出 `LOW`、`MEDIUM`、`HIGH`，最终模型仍为 `gemini-3.8-flash`。这验证了原生参数绑定；尚不能替代账户认证后的生成验收。

本轮 56 项核心测试通过，1 项环境条件跳过；`uv run ruff check .`、`uv run mypy`（257 文件）和 JS 语法检查通过。另用 Playwright 在真实 8000 页面验证两个入口的模型与思考强度选择，保留截图在 `output/playwright/`。初次修复的 106 项测试结果属于首轮验证，本轮只重跑相关核心范围。

官方依据：[Google Thinking 档位](https://ai.google.dev/gemini-api/docs/generate-content/thinking?hl=en)、[Gemini CLI 模型配置](https://geminicli.com/docs/cli/generation-settings/)、[配置文件与 GEMINI_CLI_HOME](https://geminicli.com/docs/reference/configuration/)。账户地区限制、Google 认证条件未发生改变。
