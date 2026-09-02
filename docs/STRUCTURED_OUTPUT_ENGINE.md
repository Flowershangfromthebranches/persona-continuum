# Structured Output Engine

`StructuredOutputEngine` is the single structured-output boundary for Agent
responses.

## Capability modes

Adapters explicitly declare one of `NATIVE_SCHEMA`, `JSON_MODE`,
`TOOL_SCHEMA`, `PROMPT_ONLY`, `UNAVAILABLE`, or `UNKNOWN`. Capability flags no
longer default to `structured_output=True`.

OpenAI-compatible adapters attach native `response_format` when supported.
Plain and streaming CLI adapters use prompt-only mode. ACP reports the mode it
can verify instead of claiming schema support by default.

## Parse and validate

The engine accepts a pure JSON value, fenced JSON, an object/array surrounded
by prose, whitespace, and JSON `null`/boolean values. It validates Pydantic or
JSON Schema targets and returns a `StructuredResult` with attempts and safe
diagnostics. The original raw output is not copied into job/UI diagnostics.

On a parse or schema failure, `AgentRuntimeExecutor` may issue at most one
bounded repair turn by default (never more than two when explicitly
configured). A repair transport failure remains a transport failure; it is not
relabelled as malformed JSON. The final typed codes are:

- `STRUCTURED_OUTPUT_PARSE_FAILED`;
- `STRUCTURED_OUTPUT_SCHEMA_FAILED`;
- `STRUCTURED_OUTPUT_REPAIR_FAILED`.

Business services do not call `json.loads()` on Agent final text.
