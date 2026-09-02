# Agent Runtime Contract

This document defines the protocol boundary used by Persona Creation, Profile
Enrichment, World Builder, Actor Completion, and Room execution.

## Canonical turn

`AgentTurn` and `PromptEnvelope` carry canonical `system_prompt`,
`user_message`, optional `messages`, an explicit `full_prompt`, tools, expected
output, and metadata. `PromptEnvelope.system` / `.user` remain compatibility
views for older callers; they are not separate wire sources. The envelope is
the lossless normalized representation:

```text
system + user + messages + expected_output + context_budget
```

`AgentPromptRenderer` is the only prompt placement policy:

- `NATIVE_ROLES`: separate system and user messages;
- `INLINE_SYSTEM`: one string with `[SYSTEM INSTRUCTIONS]` and `[USER INPUT]`;
- `FULL_PROMPT`: an explicitly supplied complete prompt, while retaining any
  separate canonical fields;
- `PROTOCOL_SPECIFIC`: the adapter owns the wire envelope but must preserve the
  same canonical content.

The old `turn.full_prompt or turn.user_message` choice is not a valid adapter
implementation. Missing system or user content is a protocol failure, not a
silent fallback.

## Adapter contract

Built-in and manifest adapters declare `prompt_mode` and
`structured_output_mode`, expose `capabilities()` through the adapter
contract, and implement session creation/binding, send events, cancel, and
close. `RuntimeBindingSnapshot` records requested and effective
model/reasoning values plus verification status. OpenCode ACP is strict: a
requested model or reasoning effort that is not confirmed by `session/new`
fails closed with `MODEL_BINDING_UNVERIFIED` or
`REASONING_BINDING_UNVERIFIED`. Codex's unverified `exec` fallback and an
OpenAI-compatible gateway that rejects an explicitly requested reasoning
effort fail closed with the same reasoning-binding error; they do not silently
downgrade to provider defaults.

## Execution boundary

Application code uses `AgentRuntimeExecutor` only:

```python
await runtime.execute_text(binding, system_prompt=..., user_message=..., phase=...)
await runtime.execute_structured(binding, system_prompt=..., user_message=..., schema=..., phase=...)
await runtime.execute_research(binding, system_prompt=..., user_message=..., schema=...)
```

The executor owns prompt rendering, context-budget enforcement, idle/hard
timeouts, event collection, structured parsing/repair, and typed diagnostics.
Vendor names do not appear in Persona business decisions.

Large internal tool results cross the application boundary as an artifact
reference, bounded preview, character count, and evidence references. When a
live tool result has no durable store ID yet, the raw value is kept only in a
bounded private session artifact map; it is not copied into public events,
diagnostics, WebSockets, or UI state.
