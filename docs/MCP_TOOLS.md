# MCP Tools

All tools return:

```json
{"ok": true, "data": {}, "warnings": [], "next_actions": []}
```

Current tool count: 57.

Tool groups:

- Persona: `persona_create`, `persona_list`, `persona_get`, `persona_update`,
  `persona_archive`, `persona_delete`, `persona_export`, `persona_import`.
- Sources and compilation: `persona_add_sources`, `persona_add_source_text`,
  `persona_delete_source`, `persona_create_compilation_task`, `persona_get_task`,
  `persona_submit_research_artifact`, `persona_compile`,
  `persona_structural_check`, `persona_validate`.
- Runtime: `persona_activate`, `persona_start_session`, `persona_prepare_turn`,
  `persona_commit_turn`, `persona_end_session`, `persona_delete_session`,
  `persona_get_runtime_state`, `persona_run_reflection`,
  `persona_prepare_reflection`, `persona_commit_reflection`.
- Memory: `persona_search_memories`, `persona_add_memory`,
  `persona_correct_memory`, `persona_forget_memory`,
  `persona_consolidate_memories`.
- Relationship: `persona_get_relationship`, `persona_update_relationship`,
  `persona_list_relationships`.
- Continuation: `continuation_create`, `continuation_get`,
  `continuation_add_world_events`, `continuation_create_branch`,
  `continuation_prepare_step`, `continuation_commit_step`,
  `continuation_advance_branch`, `continuation_compare_branches`,
  `continuation_score_branch`, `continuation_select_main_branch`,
  `continuation_compile_persona`.
- Rooms: `persona_create_room`, `persona_room_add_persona`,
  `persona_room_prepare_next`, `persona_room_commit_turn`,
  `persona_room_get_state`, `persona_room_close`.
- Evaluation: `evaluation_create_suite`, `evaluation_add_case`,
  `evaluation_prepare_case`, `evaluation_commit_result`,
  `evaluation_compare_versions`.

## V1.1 Contract Notes

`persona_create` accepts `aliases`, `birth_date`, `death_date`,
`data_cutoff_date`, `sensitivity`, and explicit `persona_id`.

`persona_start_session` accepts `counterpart_id` and `branch_id`. Later prepare
or commit calls with a different counterpart are rejected with
`session_counterpart_mismatch`.

`persona_prepare_turn` validates session/persona ownership and accepts
`current_time`, `external_events`, `max_context_items`, `max_context_size`,
`counterpart_id`, and `branch_id`.

`persona_commit_turn` rejects ended or cross-persona sessions and accepts
`user_feedback`, `goal_completed`, `state_patch`, `counterpart_id`,
`used_claim_ids`, and `used_memory_ids`. Digital-experience memories record
`session_id` and `turn_id`.

`persona_delete_session` accepts `delete_derived_memories=true` to delete the
session's digital experiences, reflection summaries, relationship-update events,
and unresolved events.

`persona_prepare_reflection` accepts `branch_id`, optional `session_ids`, and
`limit`. It returns turns only from the requested branch. `persona_commit_reflection`
accepts `branch_id`, rejects mixed-branch `supporting_turn_ids`, writes reflection
memories and deltas to that branch, and records multi-session support edges.

`persona_get_runtime_state`, relationship get/update/list tools accept
`branch_id` and default to `main`.

`continuation_prepare_step` marks a branch `waiting_for_host`; the host must
submit a structured artifact through `continuation_commit_step`. Selection and
compilation reject branches that have not committed a host artifact.

`persona_export` supports `room_export_mode`: `omit` for single-persona packages
without dangling active rooms, `transcript_only` for room transcript context
without other personas, and `bundle` for portable multi-persona room packages.

`persona_add_source_text` stores web research without temporary local source
files. Required fields include title, source type, canonical URL metadata,
content, hash, and source metadata.

`persona_structural_check` is the preferred structural completeness report.
`persona_validate` remains as a compatibility alias and does not report
authenticity or hard-coded fact-boundary scores.

The MCP server uses one shared application context for its lifetime. Tool calls
do not rebuild the app or rerun migrations per request.
