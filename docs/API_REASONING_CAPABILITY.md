# API Reasoning Capability

Reasoning is an explicit capability contract, not a provider-name guess. Each
model may report:

- `NATIVE_EFFORT`
- `PROVIDER_SPECIFIC`
- `MANUAL_CONFIG`
- `DEFAULT_ONLY`
- `UNSUPPORTED`
- `UNKNOWN`

The record also contains supported efforts, default effort, binding strategy,
verification state, source, and an optional verification error.

OpenAI-compatible `/models` metadata is parsed when it explicitly reports
reasoning efforts (including nested `reasoning.supported_efforts` and
`supportedReasoningEfforts`). Those values are preserved as
`protocol_model_list` evidence and become selectable automatically. Generic
models remain `UNKNOWN` when the endpoint only returns an id or a generic
`reasoning_effort` parameter name without allowed values. The API Provider form
supports automatic detection, Provider default, and manual configuration of
user-confirmed values, including future provider-defined effort names.

For a generic runtime with a real declared/native or manual binding, the
adapter sends `reasoning_effort`. If the provider rejects that field, the
runtime fails closed with `REASONING_BINDING_REJECTED`; it does not silently
remove the field and retry at the default effort.

The current Anthropic and Google/Gemini HTTP paths do not implement a
provider-specific thinking-field binding. They therefore remain
`UNSUPPORTED`, with no effective explicit reasoning and no verified reasoning
snapshot, even if stale metadata claims otherwise.

Runtime snapshots retain requested reasoning, effective reasoning, binding
method/status, and verification state. These values are shown in Agent Call
Audits and Task Center diagnostics.
