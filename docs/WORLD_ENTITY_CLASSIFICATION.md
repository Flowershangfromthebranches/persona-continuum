# World Entity Classification

Parallel World creation first produces a roster of `WorldEntityCandidate`
records. `WorldEntityClassificationService` then combines the World Builder
Agent with deterministic safety validation.

## Pipeline

1. World Builder extracts candidate ids, names and context.
2. The classification Agent returns `agent` or `non_agent`, a subtype,
   rationale and confidence.
3. Deterministic validation corrects obvious event/policy/technology/resource
   promotions and marks low-confidence rows as ambiguous.
4. Agent-capable rows are matched against all profile types by exact id/slug,
   display name, aliases, then safe fuzzy matching.
5. Missing or ambiguous profiles are returned to the confirmation UI.

Preview retries may include `classification_overrides` keyed by candidate id or
name. The override is revalidated by the same deterministic safety gate, so a
manual correction can resolve ambiguity without being able to promote an
obvious policy/event/resource into an Agent.

Agent subtypes are `person`, `organization`, `institution`, and `collective`.
Non-Agent subtypes are `event`, `policy`, `technology`, `resource`,
`market_state`, `product`, `project`, `location`, `metric`, and `process`.
Non-Agent rows remain initial world conditions or graph entities and never show
a Persona/Profile creation action.

The classifier accepts an explicit user-selected `profile_id` on a retry. That
binding is validated and then carried into Actor initialization, so Preview
and Direct Create share one gate.

## Runtime semantics

`default_actor_runtime` is only inherited by rows classified as
Agent-capable. It does not identify entities, create profiles, or silently
convert a world condition into an Actor. A strict LLM classification request
fails closed when the selected World Builder runtime is unavailable.
