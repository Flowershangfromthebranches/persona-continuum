# Narrative Episode Pipeline

`application/narrative_service.py` orchestrates per-episode production:

```
PREPARE → FORECAST → SIMULATE → DRAFT → AUDIT → COMMIT
```

## PREPARE
`prepare_episode()` assembles a task-scoped context via
`NarrativeContextBuilder.build_writer_context`: bible essentials, current plan,
canon snapshot, required character kernels, knowledge/audience state, active
threads, relevant clues, the last ≤5 episode summaries and ≤3 future plan
hints. Never the whole novel; Context Budget discipline applies.

## FORECAST
`forecast_episode()` forks one isolated **non-canonical** world branch per
author direction (`worlds.fork_branch`), stores fingerprints
(context_fingerprint / bible version / project revision / canonical branch).
Any canon or bible change marks old forecasts stale (`stale=true`).

## SIMULATE
`simulate_scene()` runs `WorldEngine.scene_engine.execute_scene` — the same
Room Protocol runtime, Recall Gate, RuntimePool and Timeout Policy as any
world scene. The NarrativeKnowledgeFirewall supplies per-character context;
the world's temporal firewall stays active.

## DRAFT
`ScreenwriterStage` converts simulation results into a commercial script: a
timed beat sheet derived from format + duration (micro drama compresses hook
and cliffhanger windows; no fixed template is hardcoded), rendered into a
versioned `EpisodeVersion`.

## AUDIT
`NarrativeContinuityAuditor` checks (severity BLOCKING/WARNING/INFO): canon
(must-happen/must-not-happen), knowledge leaks per speaker, audience secret
leaks, timeline ordering, clue reveal timing (overdue = WARNING, early =
BLOCKING), plot progress, character arcs, repetition vs previous episodes,
micro-drama hook/cliffhanger, draft completeness.

## COMMIT (atomic)
`commit_episode()` validates audit + staleness (force requires an explicit
override reason recorded in audit history), then in **one SQLite transaction**:
episode version → canon pointer, canon events, knowledge/audience updates,
plot threads, clues, arcs, project revision, episode summary. Failure rolls
back everything — no half canon. After commit, dependent artifacts are marked
stale and the next episode continues from the new canonical branch.

## Background jobs
`create_job(kind=story_bible|outline|episode_pipeline|production_package)` runs
on the existing Job control semantics (safe pause at stage boundaries, cancel,
retry, dismiss) with progress pushed over `/api/narrative-jobs/{id}/ws`.
