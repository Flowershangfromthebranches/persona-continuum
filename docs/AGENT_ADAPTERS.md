# Protocol-First Agent Adapters

## Supported Agent Hosts & Protocols

Persona Continuum connects to host CLI agents and model backends through protocol-first adapters.

| Agent Runtime | Adapter Class | Execution Mode | Protocol / Transport |
|---|---|---|---|
| **Codex** | `CodexAdapter` | Persistent app-server | Codex app-server V2 JSON-RPC |
| **Cursor** | `CursorAdapter` | CLI / App Server | `vendor_app_server` |
| **Grok Build** | `GrokBuildAdapter` | Persistent stdio session | ACP V1 |
| **Claude Code** | `ClaudeCodeAdapter` | CLI / Subprocess | `acp` / `streaming_json_cli` |
| **Command Code** | `CommandCodeAdapter` | CLI / Subprocess | `plain_cli` / `streaming_json_cli` |
| **Gemini CLI** | `GeminiCliAdapter` | CLI / Subprocess | `streaming_json_cli` |
| **OpenCode** | `OpenCodeAdapter` | CLI / Subprocess | `acp` / `jsonrpc_stdio` |
| **Qwen / Kimi / DeepSeek** | `QwenAdapter`, `KimiAdapter`, `DeepSeekHarnessAdapter` | CLI / Subprocess | `plain_cli` / `streaming_json_cli` |
| **Copilot / Qoder / WorkBuddy / CodeBuddy** | `CopilotAdapter`, `QoderAdapter`, `WorkBuddyAdapter`, `CodeBuddyAdapter` | CLI / Subprocess | `plain_cli` / `vendor_app_server` |
| **OpenAI Compatible API** | `OpenAICompatibleAPIAdapter` | HTTP / SSE | REST / Server-Sent Events |
| **Custom External Adapters** | `ManifestAgentAdapter` | External TOML | Manifest defined CLI / JSON-RPC |

## Custom Adapter Manifest Format

Drop TOML manifest files into `~/.persona-continuum/adapters/` (or directory set in `Config.adapters_dir`):

```toml
id = "my_custom_agent"
name = "My Custom Agent"
binary_name = "custom-agent-cli"
protocol = "streaming_json_cli"
default_model = "custom-model-v1"
auth_env_var = "CUSTOM_AGENT_API_KEY"

[[models]]
id = "custom-model-v1"
display_name = "Custom Model v1"
supported_reasoning_efforts = ["none", "low", "medium", "high"]
default_reasoning_effort = "medium"

[[models]]
id = "custom-model-max"
display_name = "Custom Model Max"
supported_reasoning_efforts = ["none", "high", "max"]
default_reasoning_effort = "max"
```

## Security & Secret Scrubbing

- Tokens matching `sk-...`, `Bearer ...`, `ghp_...`, `xai-...`, or user-supplied secrets are automatically redacted via `redact_secrets()`.
- Unmasked API keys are never stored in SQLite, room state JSON, event logs, or WebSocket broadcasts.
- ACP authentication never serializes a generic secret as
  `authenticate.params.token`. Credentials remain in provider environment or
  provider-managed login caches; JSON-RPC carries only the declared method id
  and vendor-approved metadata.

## Capability Honesty

- Codex app-server V2 model discovery follows `data` and `nextCursor`, converts
  object-shaped reasoning options through `reasoningEffort`, and preserves new
  server strings without a fixed enum.
- Grok fallback capability data is versioned: Grok 4.6 supports `low`, `medium`,
  `high`, `xhigh`; Grok 4.5 supports `low`, `medium`, `high`; both default to
  `high`.
- Generic ACP, JSON-RPC, streaming JSON, plain CLI, Cursor, and string-only
  Manifest models expose no reasoning efforts unless machine-readable metadata,
  an explicit help enum, or explicit configuration provides them.
- A successful version command means DETECTED. READY requires the adapter's
  actual protocol/auth/session smoke to succeed.

### Web Research Capability

`ResearchCapability.verification_status` is one of `declared`, `verified`,
`unknown`, or `unavailable`. Plain local CLIs with no research metadata are
`agentic_cli/unknown`, not unavailable. When such a CLI is READY, Persona
Creation starts a behavioral probe in the selected Agent session and then
validates the returned URL independently. The result is cached by Agent id,
Agent version, Model id and runtime source. API providers do not receive the
CLI probe; they require explicit native Web Research, Research Broker, or MCP.

## Runtime contract

Every adapter declares `prompt_mode` and `structured_output_mode` and is
invoked through `AgentRuntimeExecutor`. `AgentPromptRenderer` carries the
canonical system prompt, user message, conversation messages, and expected
schema to the adapter's native roles or its single-string wire format. The
adapter must not choose between `full_prompt` and `user_message` ad hoc.

Session binding is recorded as `RuntimeBindingSnapshot` with requested and
effective model/reasoning values. OpenCode ACP requires server confirmation;
an unverified selected model or reasoning value returns a typed binding
failure instead of displaying a guessed selection. Codex's `exec` fallback
does not claim an explicit reasoning binding, and the compatible HTTP adapter
does not retry a 400 by removing requested reasoning. See
`AGENT_RUNTIME_CONTRACT.md` and `AGENT_RESPONSE_CONTRACT.md` for the shared
execution and failure semantics.
