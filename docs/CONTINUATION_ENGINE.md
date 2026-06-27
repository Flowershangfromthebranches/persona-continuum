# Continuation Engine

The continuation engine creates a task from a divergence condition, accepts
world events, creates branches, prepares branch steps, accepts host-authored
step artifacts, scores branches, selects a main branch, and compiles a
counterfactual runtime state.

`continuation_prepare_step` returns persona state, time constraints, world
state, causal constraints, and a required output artifact schema.
`continuation_advance_branch` does not invent events. Without a committed host
artifact the branch remains `waiting_for_host`.

`continuation_commit_step` persists structured world/persona/relationship/
affect/goal deltas and marks any new continuation memories as
`counterfactual_host_artifact`. They are never promoted to historical fact.
The artifact schema is strict: relationship, goal, rejected-alternative, and
evidence-link entries have required nested fields; uncertainty must be in
`0..1`; `next_step_date` must move forward.

Branches move through `created`, `waiting_for_host`, `active`, `completed`, and
`eliminated`. `prepare_step` moves `active` or `created` branches to
`waiting_for_host`; `commit_step` is the only path back to `active`. Branches
cannot be selected or compiled until at least one host-authored step has been
committed.

Branch scoring combines persona consistency, branch-history consistency, causal
consistency, world constraints, relationship reasonableness, evidence support,
unexplained jump penalties, and uncertainty.

Compiled continuation personas are versioned and idempotent. Recompiling the
same selected branch with the same normalized deltas returns the current version
instead of duplicating components or mutating base identity files.
