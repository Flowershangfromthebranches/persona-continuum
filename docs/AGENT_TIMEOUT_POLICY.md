# Agent Timeout Policy

`AgentTimeoutPolicy` replaces the old single 120-second Agent-turn deadline
with two bounded clocks:

- `idle_timeout`: no valid Agent event has arrived during the interval;
- `hard_timeout`: absolute safety limit for the phase.

Thinking, tool calls, tool results, chunks, and terminal events all count as
activity and reset the idle clock. A continuously thinking Agent therefore is
not killed merely because it has been active for longer than the idle period,
but the hard limit still applies.

The phase defaults are intentionally different. Classification, semantic
relations, and evidence fusion use a five-minute hard limit; dimension
extraction uses eight minutes; public research uses fifteen minutes; world,
actor, and room phases have their own bounded defaults. The policy can be
overridden through session `extra` values for `idle_timeout_seconds` and
`hard_timeout_seconds`; the legacy `turn_timeout_seconds` is accepted only as
an explicit hard-timeout compatibility input.

Failures are typed as `AGENT_IDLE_TIMEOUT` or `AGENT_HARD_TIMEOUT` and include
phase, protocol, configured limits, last activity age, and timestamp.
