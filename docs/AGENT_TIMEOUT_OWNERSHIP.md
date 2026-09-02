# Agent Timeout Ownership

`AgentRuntimeExecutor` owns the turn timeout lifecycle. Adapters are limited to
startup/handshake safety, protocol frame parsing, HTTP connect/read safety, and
process cleanup.

Adapters declare one `OutputStreamingMode`:

- `STREAMING`: text may arrive incrementally.
- `BUFFERED_FINAL`: the process may emit the complete answer only at exit.
- `PROTOCOL_STREAM`: protocol frames are observable even when text is not.
- `UNKNOWN`: no output timing claim is made.

The policy keeps bounded defaults. Streaming turns have an idle floor of 180
seconds and a hard floor of 900 seconds. Buffered turns receive a bounded
first-response window in the 300–600 second range and a hard floor of 900
seconds. High/xhigh/max/deep reasoning and tool use can increase the hard
budget, but no mode uses an infinite buffer or an unbounded timeout.

The optional `turn_timeout_seconds` value is treated as a legacy explicit hard
timeout override. `idle_timeout_seconds` and `hard_timeout_seconds` remain
separate, and activity semantics are not replaced by simply raising the idle
value.
