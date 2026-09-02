# Agent Activity Contract

Persona Continuum now separates transport activity from user-visible Agent
events. Every `AgentSession` owns one `AgentActivityTracker` for the current
turn.

The tracker records bounded metadata only:

- `last_transport_activity`, `last_model_activity`, `last_output_activity`
- `first_response_at`
- stdout/stderr byte counts and protocol-frame count
- thinking, tool, and text event counts
- the last safe frame/event metadata and protocol method counts

The shared subprocess transport updates stdout and stderr activity while the
protocol adapters update frame, thinking, tool, and output activity. HTTP/SSE
adapters update activity for every received SSE frame, including frames that
carry no assistant text. Full private message/tool bodies are not stored in the
tracker.

`AgentRuntimeExecutor` is the single owner of first-response, idle, and hard
deadlines. It keeps one pending async iterator read alive and uses a one-second
observation interval. An observation tick never cancels the adapter read;
cancellation happens only at a real deadline or explicit cancellation.

Runtime diagnostics and Agent Call Audits expose the output mode, timeout
policy, activity snapshot, process liveness, frame counts, and binding
snapshot. This makes a silent text stream with active protocol traffic
distinguishable from a genuinely idle Agent.
