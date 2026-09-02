# Agent Context Budget

`AgentContextBudgetManager` estimates input tokens conservatively (four UTF-8
characters per token), reads the selected model's context window, and reserves
space for system/schema scaffolding, output, and reasoning.

Every runtime turn is checked before transport. An over-budget prompt fails
with `CONTEXT_BUDGET_EXCEEDED`; it is never truncated or sent with the tail
silently removed.

## Lossless batching

Material classification, semantic relations, evidence fusion, dimension
extraction, Profile Enrichment, and profile summaries use `iter_batches()`.
Each batch satisfies both `max_items` and the calculated evidence-token
budget. A single item that cannot fit fails closed, preserving the evidence
for an explicit reduction path rather than slicing it.

For larger collections, `hierarchical_reduce()` requires the caller's reducer
to return provenance-preserving summaries and rejects a non-reducing round.
Dimension extraction merges batch `ResearchArtifact` values and keeps source
IDs, claims, memories, conflicts, and uncertainty across all batches.
