# Gemini Web Research 与数值输入边界

## Gemini Headless Research

Gemini 的公共联网研究会创建 `PermissionProfile.RESEARCH_READ_ONLY` 会话，并由
`GeminiCliAdapter.build_permission_args()` 生成：

```text
--allowed-tools google_web_search,web_fetch
```

`--yolo`、shell、写入和 replace 工具不属于该 profile。`AgentSessionConfig.tools`
保留为可审计的声明，真正的 Gemini 权限由适配器的 CLI 参数映射负责。

未知但处于 READY 的本地 CLI 不会仅因缺少静态 capability metadata 被判定为不可用。
行为探针分为两个独立步骤：

1. `PROBE_SEARCH`：实际发现并返回可验证 URL；
2. `PROBE_FETCH`：对该 URL 实际读取非空正文；
3. Persona Continuum 独立验证 URL 的 HTTPS、允许域名和 HTTP 正文。

结果记录 `can_discover_sources`、`can_read_sources` 和探针方法，不保存完整私人正文。
权限、认证、headless/sandbox 或 policy 拒绝使用 `BLOCKED` 与 typed web error；真正
没有能力才使用 `UNAVAILABLE`。

研究 capability cache 的 fingerprint 包含 Agent version、Model、runtime source、
permission profile、研究 tool policy 和相关 CLI flags。成功结果 TTL 为 24 小时，
blocked/unavailable 结果为短 TTL；手动入口为：

```text
POST /api/agents/{agent_id}/research/revalidate
```

## Numeric normalization

来自 LLM JSON、CLI metadata、HTTP body 和 SQLite snapshot 的数值统一通过
`persona_continuum.numeric`：

- `safe_float` / `safe_int`：处理 `None`、空字符串、`"null"`、非法字符串、bool、NaN
  和 Infinity；
- `safe_probability`：限制在 `[0, 1]`；
- `safe_timeout`：默认 120 秒，限制在 5–3600 秒；
- `safe_acp_stream_limit`：默认 16 MiB，限制在 1–32 MiB；
- `InvalidNumericFieldError`：必填数值无法恢复时携带
  `INVALID_NUMERIC_FIELD`、phase、field 和 received type。

材料分类的 dimension score 为 null 或非法时会跳过该维度，并记录
`invalid_optional_numeric_skipped`；不会把“未提供”伪装成 0 分。confidence 等可选
值使用确定性的默认值。Material Intelligence、Research Quality、Compilation、
Persona Creation 和 Agent Protocol Runtime Config 的模型/持久化边界均在解析前完成
归一化。

## Job Center interaction

Task Center 默认只返回 `visibility=user` 且 `dismissed_at IS NULL` 的记录。内部的
Profile Enrichment → Persona Creation child run 使用 `visibility=internal`；调试时可
使用 `include_internal=true`。删除按钮执行 dismiss，不物理删除人格、证据、版本、
failure 或 lineage。只有 terminal 状态允许 dismiss；运行中任务只能查看、暂停或取消。

旧失败 Job 重试时保留旧记录，并通过 `superseded_by` 指向新 run。历史 child Job 会根据
`parent_job_id`/Profile Enrichment 引用在 migration 时标记为 internal。
