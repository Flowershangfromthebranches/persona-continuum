# Agent Runtime Core Refactor — Implementation Report

## Status

The unified Agent Runtime / Adapter Layer refactor is implemented in the
working tree. This report is the handoff boundary; implementation stops here
for review approval.

## Delivered contracts

- `AgentTurn`, `PromptEnvelope`, and `AgentPromptRenderer` preserve system,
  user, messages, and explicit full prompts across native-role and single
  string transports.
- All built-in CLI/ACP/API/manifest adapters declare prompt and structured
  output modes, expose the explicit `capabilities()` contract, and use
  `AgentRuntimeExecutor` for text, structured, research, world, profile, and
  room calls.
- `StructuredOutputEngine` provides pure/fenced/prose JSON parsing, schema
  validation, one or two bounded repair attempts, and typed
  parse/schema/repair diagnostics.
- `AgentTimeoutPolicy` separates resettable idle timeouts from absolute hard
  timeouts and records last Agent activity.
- `AgentContextBudgetManager` reserves system/schema/output/reasoning space,
  batches by item count and prompt budget, and keeps evidence lossless.
- `RuntimeBindingSnapshot` records requested/effective model and reasoning.
  OpenCode ACP fails closed when selected bindings cannot be verified; Codex
  `exec` fallback and compatible HTTP gateways also fail closed when an
  explicitly requested reasoning effort cannot be verified.
- ACP JSONL framing uses one finite stdout/stderr limit and a shared bounded
  frame reader. Overflows become `AGENT_TRANSPORT_FRAME_TOO_LARGE`, close the
  corrupted session, and retain frame/event/tool diagnostics without private
  response bodies.
- Large internal tool results use artifact reference, preview, character
  count, and evidence references at the shared runtime event boundary. The
  original value is retained in the bounded private live-session artifact map
  when no durable evidence ID is available; authoritative Evidence records are
  never replaced by the public compact observation.

## Call-site migration

Persona Creation, Profile Enrichment, Profile Library summaries, World Builder,
Actor Runtime, and Room streaming now use the shared executor. The Profile
Enrichment child Persona Creation failure is copied as the root typed failure
with `child_job_id` and parent context; it is not wrapped as a second
`PROFILE_ENRICHMENT_ERROR`. Evidence batch sizing uses the same rendered item
shape that is sent to the Agent, so the budget decision does not rely on a
smaller surrogate serialization.

## Tests and documentation

Contract/regression tests were added under
`tests/unit/test_agent_runtime_contract.py` and the existing ACP/Task Center
coverage remains under `tests/unit/test_acp_and_job_center_upgrade.py`.
Runtime documentation is split into:

- `docs/AGENT_RUNTIME_CONTRACT.md`;
- `docs/STRUCTURED_OUTPUT_ENGINE.md`;
- `docs/AGENT_CONTEXT_BUDGET.md`;
- `docs/AGENT_TIMEOUT_POLICY.md`.

## Verification boundary

The implementation turn stopped before execution as requested. After explicit
review approval, the review remediation pass ran focused local tests and Ruff;
it did not run a real CLI, ACP Agent, Persona Enrichment, or Web E2E flow.

## Review remediation

The first code review identified six runtime-contract regressions. They are
resolved as follows:

- `AgentResponseCollector` now reconstructs known typed failures and preserves
  unknown reported failure codes instead of replacing them with
  `AGENT_TRANSPORT_ERROR`.
- OpenAI-compatible HTTP read timeouts now use the turn's phase-aware idle
  budget. A provider 400 is classified as a reasoning-binding rejection only
  when its sanitized error identifies the reasoning parameter.
- Google native requests send the system prompt only through
  `systemInstruction`; the user payload renderer omits the duplicate system
  text.
- Codex checks explicit reasoning again whenever a live app-server turn falls
  back to `codex exec`, so a mid-turn transport failure cannot silently
  downgrade reasoning.
- Codex stdio subprocesses use a configurable, bounded 1–32 MiB stream limit
  with a 16 MiB default and continuously drain sanitized stderr tails.
- Focused verification passed 56 Agent runtime, HTTP, ACP/Job Center, and Codex
  app-server protocol tests; Ruff and diff checks passed after formatting.

## Review handoff

Review should inspect the unified call graph, typed failure lineage, adapter
wire payloads, and the intentionally unexecuted regression tests before
approving runtime validation.
