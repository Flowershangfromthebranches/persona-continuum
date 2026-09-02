# Agent Response Contract

All application Agent calls consume the same `AgentResponseCollector`. Adapters
may emit chunks, thinking, tool calls, tool results, raw protocol logs, and a
terminal event; the collector deduplicates a terminal full response and records
the protocol, usage, event counts, last event, and bounded diagnostics.

An exit-0 process with no assistant text is not success. It produces the typed
`AGENT_PROCESS_EXITED_WITHOUT_OUTPUT` diagnostic with sanitized command shape,
return code, stderr tail, and event counts. Structured output is parsed and
validated by `StructuredOutputEngine`: parse, schema, and bounded-repair
failures use `STRUCTURED_OUTPUT_PARSE_FAILED`,
`STRUCTURED_OUTPUT_SCHEMA_FAILED`, and `STRUCTURED_OUTPUT_REPAIR_FAILED`.
Transport, session-start, protocol-no-final-text, idle timeout, hard timeout,
and cancellation failures have distinct codes. Runtime disappearance is
distinct (`RuntimeUnavailableError`) so the worker can pause and resume
without silently switching runtime.

Codex App Server accepts both streamed deltas and final `item/completed`
assistant messages. If the selected App Server protocol produces no assistant
text, the adapter may perform one fallback to `codex exec --json` using the
same Agent, Model, reasoning, and credential snapshot. Fallback start and
completion are emitted as protocol events.

Agent Call Audit records are deliberately metadata-only. Full prompts,
uploaded files, API keys, and raw private material are not persisted or sent
to the browser.

## ACP JSONL framing

ACP subprocesses use a finite shared stdout/stderr stream limit: 16 MiB by
default, configurable through `AgentSessionConfig.extra["acp_stream_limit_bytes"]`
between 1 MiB and the 32 MiB hard maximum. `ACPJsonLineReader` owns all ACP
frame reads and strict JSON decoding. An oversized frame closes the corrupted
session and becomes `AGENT_TRANSPORT_FRAME_TOO_LARGE` with bounded protocol,
frame/event/tool diagnostics; the lower-level separator/limit exception is not
exposed. A compatible adapter may then use its explicit same-binding fallback.

Large tool results use an artifact reference, redacted preview, character
count, and evidence references at event/WebSocket/UI boundaries while the
authoritative result remains on the model/Evidence path.

## Unified runtime execution

Business services call `AgentRuntimeExecutor.execute_text()`,
`execute_structured()`, or `execute_research()` with a canonical
`AgentTurn`. The executor applies the adapter's declared `PromptMode`, context
budget, phase timeout policy, response collection, structured parsing, and
runtime binding snapshot in one path. An adapter cannot silently select between
`full_prompt` and `user_message`; `AgentPromptRenderer` preserves the system,
user, messages, and explicit full-prompt fields for both native-role and
single-string protocols.

`idle_timeout` is reset by every valid thinking, tool, chunk, result, or done
event. `hard_timeout` remains an absolute safety bound. The resulting
diagnostics include requested/effective model and reasoning, prompt mode,
structured mode, estimated input tokens, and last activity timestamp.
