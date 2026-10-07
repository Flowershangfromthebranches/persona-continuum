# Gemini / agy structured-output contract

GeminiCliAdapter uses PlainCliAdapter with PROMPT_ONLY. An expected_output
schema is rendered under [EXPECTED OUTPUT SCHEMA] by AgentPromptRenderer.
No --json-schema or --output-format flag is emitted. The existing
StructuredOutputEngine remains responsible for parsing, validation and repair.

The reported agy CLI contract requires --json-schema to accompany
--output-format json or stream-json. Native schema support must implement
both --output-format json and JSON envelope unwrapping: extract the actual
result from structured_output before schema validation. Adding the format
flag alone is not compatible with the current plain-text pipeline.

Deterministic CLI argument failures produce AGENT_CLI_INVALID_ARGUMENT with
retriable=false, including failures with a startup banner on stdout. The
distinct code avoids legacy empty-output retry fallbacks and material
classification's partial-output retries. Temporary failures keep their
existing retry policy. No Large Conversation Pipeline implementation changed.

Validation: 71 focused tests passed across Gemini adapter, command assembly,
response/structured-output contracts and material classification. Tests mock
subprocess transport; no real provider request or private corpus run was made.
