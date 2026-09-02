SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS personas (
  id TEXT PRIMARY KEY,
  manifest_json TEXT NOT NULL,
  package_path TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  source_type TEXT NOT NULL,
  path TEXT NOT NULL,
  title TEXT NOT NULL,
  hash TEXT NOT NULL,
  content TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(persona_id, hash)
);

-- Derived private-material Evidence Layer.  Raw `sources` are immutable and
-- remain the provenance root; these tables can be rebuilt without losing the
-- uploaded originals, source locators, or compilation lineage.
CREATE TABLE IF NOT EXISTS persona_material_jobs (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  progress_json TEXT NOT NULL DEFAULT '{}',
  coverage_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  runtime_snapshot_json TEXT NOT NULL DEFAULT '{}',
  incremental INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_material_jobs_persona
  ON persona_material_jobs(persona_id, created_at DESC);

CREATE TABLE IF NOT EXISTS persona_evidence_units (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  source_locator_json TEXT NOT NULL DEFAULT '{}',
  speaker TEXT,
  speaker_role TEXT,
  timestamp TEXT,
  text TEXT NOT NULL,
  normalized_text TEXT NOT NULL,
  normalized_text_hash TEXT NOT NULL,
  evidence_type TEXT NOT NULL,
  dimension_candidates_json TEXT NOT NULL DEFAULT '[]',
  dimension_scores_json TEXT NOT NULL DEFAULT '{}',
  life_stage_candidates_json TEXT NOT NULL DEFAULT '[]',
  relationship_entities_json TEXT NOT NULL DEFAULT '[]',
  context_tags_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL DEFAULT 0.5,
  extraction_method TEXT NOT NULL DEFAULT 'deterministic_segmenter',
  source_kind TEXT NOT NULL DEFAULT 'user_provided',
  event_time TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_evidence_units_persona
  ON persona_evidence_units(persona_id, evidence_type, created_at);
CREATE INDEX IF NOT EXISTS idx_persona_evidence_units_source
  ON persona_evidence_units(source_id, normalized_text_hash);

CREATE TABLE IF NOT EXISTS persona_evidence_clusters (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  cluster_type TEXT NOT NULL,
  canonical_evidence_id TEXT NOT NULL,
  member_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  similarity_type TEXT NOT NULL DEFAULT 'single',
  confidence REAL NOT NULL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_evidence_clusters_persona
  ON persona_evidence_clusters(persona_id, cluster_type, updated_at);

CREATE TABLE IF NOT EXISTS persona_fused_evidence (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  canonical_claim TEXT NOT NULL,
  evidence_type TEXT NOT NULL,
  dimension_scores_json TEXT NOT NULL DEFAULT '{}',
  supporting_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  unique_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  contradiction_ids_json TEXT NOT NULL DEFAULT '[]',
  conditions_json TEXT NOT NULL DEFAULT '[]',
  temporal_scope_json TEXT NOT NULL DEFAULT '[]',
  relationship_scope_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL DEFAULT 0.5,
  synthesis_method TEXT NOT NULL DEFAULT 'deterministic_union',
  verbatim_samples_json TEXT NOT NULL DEFAULT '[]',
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_fused_evidence_persona
  ON persona_fused_evidence(persona_id, evidence_type, created_at);

CREATE TABLE IF NOT EXISTS persona_contradictions (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  contradiction_type TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL,
  conditions_json TEXT NOT NULL DEFAULT '[]',
  resolution TEXT,
  confidence REAL NOT NULL DEFAULT 0.5,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_contradictions_persona
  ON persona_contradictions(persona_id, contradiction_type, created_at);

CREATE TABLE IF NOT EXISTS persona_conversation_episodes (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  participants_json TEXT NOT NULL DEFAULT '[]',
  start_time TEXT,
  end_time TEXT,
  topics_json TEXT NOT NULL DEFAULT '[]',
  emotional_context_json TEXT NOT NULL DEFAULT '[]',
  message_ids_json TEXT NOT NULL DEFAULT '[]',
  evidence_unit_ids_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_conversation_episodes_persona
  ON persona_conversation_episodes(persona_id, start_time, end_time);

CREATE TABLE IF NOT EXISTS persona_identity_aliases (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.5,
  status TEXT NOT NULL DEFAULT 'suggested',
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(persona_id, normalized_alias)
);

CREATE INDEX IF NOT EXISTS idx_persona_identity_aliases_lookup
  ON persona_identity_aliases(persona_id, normalized_alias);

CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  dimension TEXT NOT NULL,
  source_id TEXT,
  claim_type TEXT NOT NULL,
  raw_location TEXT,
  event_time TEXT,
  reliability REAL NOT NULL,
  is_self_report INTEGER NOT NULL,
  is_third_party_report INTEGER NOT NULL,
  has_counter_evidence INTEGER NOT NULL,
  inference_strength REAL NOT NULL,
  confidence REAL NOT NULL,
  created_by TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compilation_tasks (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  artifacts_json TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Durable asynchronous Persona Creation Runtime jobs.  Research artifacts and
-- compiled manifests remain in their existing tables; this table only stores
-- orchestration state, runtime binding snapshots and resumable progress.
CREATE TABLE IF NOT EXISTS persona_creation_jobs (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  persona_type TEXT NOT NULL,
  creation_mode TEXT NOT NULL,
  status TEXT NOT NULL,
  runtime_source TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  reasoning_effort TEXT,
  auth_profile_id TEXT,
  agent_version TEXT,
  capability_snapshot_json TEXT NOT NULL DEFAULT '{}',
  persona_id TEXT REFERENCES personas(id) ON DELETE SET NULL,
  compilation_task_id TEXT REFERENCES compilation_tasks(id) ON DELETE SET NULL,
  research_policy_json TEXT NOT NULL DEFAULT '{}',
  source_count INTEGER NOT NULL DEFAULT 0,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  dimension_progress_json TEXT NOT NULL DEFAULT '{}',
  current_stage TEXT NOT NULL DEFAULT 'created',
  coverage_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  job_config_json TEXT NOT NULL DEFAULT '{}',
  events_json TEXT NOT NULL DEFAULT '[]',
  interview_questions_json TEXT NOT NULL DEFAULT '[]',
  life_stage_progress_json TEXT NOT NULL DEFAULT '{}',
  life_stages_json TEXT NOT NULL DEFAULT '[]',
  information_gain_json TEXT NOT NULL DEFAULT '[]',
  research_gaps_json TEXT NOT NULL DEFAULT '[]',
  research_checkpoints_json TEXT NOT NULL DEFAULT '[]',
  research_stop_reason TEXT,
  query_history_json TEXT NOT NULL DEFAULT '{}',
  private_coverage_json TEXT NOT NULL DEFAULT '{}',
  progress_json TEXT NOT NULL DEFAULT '{}',
  failure_json TEXT,
  agent_call_audits_json TEXT NOT NULL DEFAULT '[]',
  checkpoints_json TEXT NOT NULL DEFAULT '[]',
  visibility TEXT NOT NULL DEFAULT 'user',
  dismissed_at TEXT,
  superseded_by TEXT,
  worker_state TEXT NOT NULL DEFAULT 'starting',
  worker_started_at TEXT,
  worker_heartbeat_at TEXT,
  worker_finished_at TEXT,
  agent_call_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_creation_jobs_status
  ON persona_creation_jobs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_persona_creation_jobs_persona
  ON persona_creation_jobs(persona_id, created_at);

-- Control-plane state for background jobs.  Pause/cancel are NOT job data:
-- keeping them in their own table is what stops two Job objects from
-- overwriting each other's flags, and it makes a pause survive a restart.
CREATE TABLE IF NOT EXISTS persona_job_control (
  job_id TEXT PRIMARY KEY,
  pause_requested INTEGER NOT NULL DEFAULT 0,
  pause_requested_at TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  cancel_requested_at TEXT,
  control_version INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS persona_research_checkpoints (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES persona_creation_jobs(id) ON DELETE CASCADE,
  round_index INTEGER NOT NULL,
  raw_source_count INTEGER NOT NULL DEFAULT 0,
  independent_source_count INTEGER NOT NULL DEFAULT 0,
  quality_source_count INTEGER NOT NULL DEFAULT 0,
  coverage_json TEXT NOT NULL DEFAULT '{}',
  information_gain_json TEXT NOT NULL DEFAULT '{}',
  gaps_json TEXT NOT NULL DEFAULT '[]',
  stop_reason TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_research_checkpoints_job
  ON persona_research_checkpoints(job_id, round_index);

CREATE TABLE IF NOT EXISTS persona_source_clusters (
  cluster_id TEXT NOT NULL,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  origin_type TEXT NOT NULL DEFAULT 'unknown',
  origin_identifier TEXT NOT NULL DEFAULT '',
  member_source_ids_json TEXT NOT NULL DEFAULT '[]',
  canonical_source_id TEXT,
  independence_confidence REAL NOT NULL DEFAULT 0.0,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(persona_id, cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_persona_source_clusters_persona
  ON persona_source_clusters(persona_id, updated_at);

-- Unified Actor/Profile Library.  Persona profiles point back to the existing
-- personas table; organization/institution/collective records keep only their
-- typed profile payload here and never masquerade as Persona packages.
CREATE TABLE IF NOT EXISTS actor_profiles (
  id TEXT PRIMARY KEY,
  profile_type TEXT NOT NULL,
  display_name TEXT NOT NULL,
  slug TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  source_count INTEGER NOT NULL DEFAULT 0,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  coverage_state TEXT NOT NULL DEFAULT 'unknown',
  compile_state TEXT NOT NULL DEFAULT 'draft',
  persona_id TEXT REFERENCES personas(id) ON DELETE SET NULL,
  runtime_snapshot_json TEXT NOT NULL DEFAULT '{}',
  coverage_json TEXT NOT NULL DEFAULT '{}',
  payload_json TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_actor_profiles_slug ON actor_profiles(slug);
CREATE INDEX IF NOT EXISTS idx_actor_profiles_type_status
  ON actor_profiles(profile_type, status, updated_at);

CREATE TABLE IF NOT EXISTS profile_versions (
  id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL REFERENCES actor_profiles(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT 'profile_runtime',
  UNIQUE(profile_id, version)
);

CREATE INDEX IF NOT EXISTS idx_profile_versions_profile
  ON profile_versions(profile_id, version DESC);

CREATE TABLE IF NOT EXISTS profile_enrichment_jobs (
  id TEXT PRIMARY KEY,
  target_profile_id TEXT NOT NULL REFERENCES actor_profiles(id) ON DELETE CASCADE,
  target_profile_type TEXT NOT NULL,
  job_type TEXT NOT NULL DEFAULT 'upgrade',
  selected_runtime_json TEXT NOT NULL DEFAULT '{}',
  research_policy_json TEXT NOT NULL DEFAULT '{}',
  requested_scope TEXT NOT NULL DEFAULT 'full_refresh',
  enrichment_input_mode TEXT NOT NULL DEFAULT 'local_materials',
  status TEXT NOT NULL DEFAULT 'created',
  progress_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  failure_json TEXT,
  agent_call_audits_json TEXT NOT NULL DEFAULT '[]',
  source_count INTEGER NOT NULL DEFAULT 0,
  new_version INTEGER,
  persona_creation_job_id TEXT REFERENCES persona_creation_jobs(id) ON DELETE SET NULL,
  parent_job_id TEXT,
  base_persona_version INTEGER,
  input_material_ids_json TEXT NOT NULL DEFAULT '[]',
  input_material_count INTEGER NOT NULL DEFAULT 0,
  new_source_ids_json TEXT NOT NULL DEFAULT '[]',
  visibility TEXT NOT NULL DEFAULT 'user',
  dismissed_at TEXT,
  superseded_by TEXT,
  worker_state TEXT NOT NULL DEFAULT 'starting',
  worker_started_at TEXT,
  worker_heartbeat_at TEXT,
  worker_finished_at TEXT,
  agent_call_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_profile_enrichment_jobs_target
  ON profile_enrichment_jobs(target_profile_id, created_at DESC);

-- Behavioral Web Research capability results.  The runtime binding tuple is
-- part of the primary key so upgrading an Agent or selecting another Model
-- automatically requires a fresh verification instead of reusing stale data.
CREATE TABLE IF NOT EXISTS research_capability_cache (
  cache_key TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  agent_version TEXT NOT NULL,
  model_id TEXT NOT NULL,
  runtime_source TEXT NOT NULL,
  configuration_fingerprint TEXT NOT NULL DEFAULT '',
  verification_status TEXT NOT NULL,
  capability_json TEXT NOT NULL DEFAULT '{}',
  verification_method TEXT,
  verified_at TEXT,
  error TEXT,
  expires_at TEXT,
  cache_ttl_seconds REAL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_capability_cache_runtime
  ON research_capability_cache(agent_id, agent_version, model_id, runtime_source);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  type TEXT NOT NULL,
  occurred_at TEXT,
  written_at TEXT NOT NULL,
  participants_json TEXT NOT NULL,
  emotions_json TEXT NOT NULL,
  source_id TEXT,
  source_kind TEXT NOT NULL,
  source_confidence REAL NOT NULL,
  importance REAL NOT NULL,
  validity TEXT NOT NULL,
  access_count INTEGER NOT NULL,
  last_accessed_at TEXT,
  branch_id TEXT NOT NULL,
  unresolved INTEGER NOT NULL,
  user_corrected INTEGER NOT NULL,
  forgettable INTEGER NOT NULL,
  supersedes_id TEXT,
  metadata_json TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  memory_id UNINDEXED,
  persona_id UNINDEXED,
  content
);

CREATE TABLE IF NOT EXISTS affect_states (
  persona_id TEXT NOT NULL,
  branch_id TEXT NOT NULL DEFAULT 'main',
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  intensity REAL NOT NULL,
  baseline REAL NOT NULL,
  decay_rate REAL NOT NULL,
  updated_at TEXT NOT NULL,
  triggers_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  PRIMARY KEY(persona_id, branch_id, name, kind)
);

CREATE TABLE IF NOT EXISTS needs (
  persona_id TEXT NOT NULL,
  branch_id TEXT NOT NULL DEFAULT 'main',
  name TEXT NOT NULL,
  level REAL NOT NULL,
  baseline REAL NOT NULL,
  updated_at TEXT NOT NULL,
  confidence REAL NOT NULL,
  reasons_json TEXT NOT NULL,
  PRIMARY KEY(persona_id, branch_id, name)
);

CREATE TABLE IF NOT EXISTS relationships (
  persona_id TEXT NOT NULL,
  branch_id TEXT NOT NULL DEFAULT 'main',
  counterpart TEXT NOT NULL,
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(persona_id, branch_id, counterpart)
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  title TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_turns (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  persona_id TEXT NOT NULL,
  user_message TEXT NOT NULL,
  persona_response TEXT NOT NULL,
  used_memory_ids_json TEXT NOT NULL,
  user_feedback TEXT,
  context_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS continuations (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  task_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS continuation_branches (
  id TEXT PRIMARY KEY,
  continuation_id TEXT NOT NULL REFERENCES continuations(id) ON DELETE CASCADE,
  persona_id TEXT NOT NULL,
  branch_json TEXT NOT NULL,
  score REAL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  persona_ids_json TEXT NOT NULL,
  topic TEXT,
  state_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Room Protocol Engine tables are additive. Existing rooms remain authoritative
-- in rooms.state_json and are interpreted as free_discussion when protocol data
-- is absent, so no destructive data rewrite is required.
CREATE TABLE IF NOT EXISTS room_templates (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  protocol TEXT NOT NULL DEFAULT 'free_discussion',
  template_json TEXT NOT NULL,
  built_in INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS room_runs (
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  protocol TEXT NOT NULL,
  status TEXT NOT NULL,
  current_stage TEXT NOT NULL,
  state_json TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_room_runs_room_status
  ON room_runs(room_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS room_protocol_events (
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES room_runs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  stage TEXT,
  actor_id TEXT,
  target_id TEXT,
  task_id TEXT,
  status TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_room_protocol_events_timeline
  ON room_protocol_events(room_id, run_id, created_at);

CREATE TABLE IF NOT EXISTS room_protocol_tasks (
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES room_runs(id) ON DELETE CASCADE,
  parent_task_id TEXT REFERENCES room_protocol_tasks(id) ON DELETE SET NULL,
  stage TEXT NOT NULL,
  participant_id TEXT,
  task_type TEXT NOT NULL,
  status TEXT NOT NULL,
  input_json TEXT NOT NULL DEFAULT '{}',
  output_json TEXT NOT NULL DEFAULT '{}',
  error_type TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_room_protocol_tasks_run
  ON room_protocol_tasks(run_id, stage, status);

CREATE TABLE IF NOT EXISTS room_protocol_votes (
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES room_runs(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  vote TEXT NOT NULL,
  reason TEXT NOT NULL,
  confidence REAL NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, participant_id)
);

CREATE TABLE IF NOT EXISTS lineage (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  child_type TEXT NOT NULL,
  child_id TEXT NOT NULL,
  parent_type TEXT NOT NULL,
  parent_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(persona_id, child_type, child_id, parent_type, parent_id, relation)
);

CREATE TABLE IF NOT EXISTS research_artifacts (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES compilation_tasks(id) ON DELETE CASCADE,
  dimension TEXT NOT NULL,
  artifact_hash TEXT NOT NULL,
  artifact_canonical_sha256 TEXT,
  artifact_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(persona_id, task_id, artifact_hash),
  UNIQUE(persona_id, task_id, artifact_canonical_sha256)
);

CREATE TABLE IF NOT EXISTS compiled_components (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  component_type TEXT NOT NULL,
  component_key TEXT NOT NULL,
  content_json TEXT NOT NULL,
  source_artifact_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(persona_id, version, component_type, component_key)
);

CREATE TABLE IF NOT EXISTS compile_snapshots (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  task_id TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  files_manifest_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(persona_id, version)
);

CREATE TABLE IF NOT EXISTS change_events (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL DEFAULT 'main',
  event_type TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  session_id TEXT,
  turn_id TEXT,
  data_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_event_supports (
  event_id TEXT NOT NULL REFERENCES change_events(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  support_weight REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY(event_id, session_id, turn_id)
);

CREATE TABLE IF NOT EXISTS evaluation_suites (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_cases (
  id TEXT PRIMARY KEY,
  suite_id TEXT NOT NULL REFERENCES evaluation_suites(id) ON DELETE CASCADE,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  case_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_results (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES evaluation_cases(id) ON DELETE CASCADE,
  persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  version TEXT,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS room_transcripts (
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  turn_id TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  persona_id TEXT NOT NULL,
  speaker_name TEXT NOT NULL,
  agent_runtime_id TEXT NOT NULL,
  agent_session_id TEXT,
  model_id TEXT,
  reasoning_effort TEXT,
  content TEXT NOT NULL,
  director_reason TEXT,
  recall_ids_json TEXT NOT NULL DEFAULT '[]',
  commit_status TEXT NOT NULL DEFAULT 'committed',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  provider_type TEXT NOT NULL,
  base_url TEXT NOT NULL,
  auth_env_var TEXT,
  default_model TEXT,
  headers_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  base_url TEXT NOT NULL,
  encrypted_secret BLOB NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS room_create_requests (
  request_id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worlds (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  seed_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_branches (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  parent_branch_id TEXT,
  parent_snapshot_id TEXT,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_snapshots (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  timestamp TEXT NOT NULL,
  state_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_timeline_events (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  event_time TEXT NOT NULL,
  actors_json TEXT NOT NULL DEFAULT '[]',
  cause TEXT NOT NULL,
  effect TEXT NOT NULL,
  causal_chain_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL DEFAULT 1.0,
  data_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_actor_states (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  actor_id TEXT NOT NULL,
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(world_id, branch_id, actor_id)
);

CREATE TABLE IF NOT EXISTS world_scenes (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  room_id TEXT NOT NULL,
  participant_ids_json TEXT NOT NULL DEFAULT '[]',
  topic TEXT,
  status TEXT NOT NULL DEFAULT 'created',
  summary TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_simulation_runs (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  branch_results_json TEXT NOT NULL DEFAULT '[]',
  distribution_json TEXT NOT NULL DEFAULT '{}',
  summary TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_directors (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  policy_json TEXT NOT NULL DEFAULT '{}',
  current_speed TEXT NOT NULL DEFAULT 'month',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(world_id, branch_id)
);

CREATE TABLE IF NOT EXISTS causal_nodes (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  node_type TEXT NOT NULL,
  name TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  properties_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS causal_edges (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  properties_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organizations (
  id TEXT NOT NULL,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  leadership_json TEXT NOT NULL DEFAULT '[]',
  departments_json TEXT NOT NULL DEFAULT '[]',
  employees_count INTEGER NOT NULL DEFAULT 0,
  budget_billions REAL NOT NULL DEFAULT 0.0,
  cash_reserves_billions REAL NOT NULL DEFAULT 0.0,
  technology_json TEXT NOT NULL DEFAULT '[]',
  projects_json TEXT NOT NULL DEFAULT '[]',
  strategy_json TEXT NOT NULL DEFAULT '{}',
  culture_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(world_id, branch_id, id)
);

CREATE TABLE IF NOT EXISTS technologies (
  id TEXT NOT NULL,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  maturity REAL NOT NULL DEFAULT 0.0,
  cost REAL NOT NULL DEFAULT 1.0,
  performance REAL NOT NULL DEFAULT 1.0,
  adoption REAL NOT NULL DEFAULT 0.0,
  dependencies_json TEXT NOT NULL DEFAULT '[]',
  lead_org_id TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(world_id, branch_id, id)
);

CREATE TABLE IF NOT EXISTS world_memories (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  persona_id TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  content TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  written_at TEXT NOT NULL,
  importance REAL NOT NULL DEFAULT 0.5,
  emotional_valence REAL NOT NULL DEFAULT 0.0,
  source_event_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS world_runtime_bindings (
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  actor_id TEXT NOT NULL,
  persona_id TEXT,
  profile_id TEXT,
  profile_type TEXT,
  runtime_source TEXT NOT NULL DEFAULT 'local_cli',
  agent_id TEXT NOT NULL DEFAULT 'default',
  model_id TEXT NOT NULL DEFAULT 'default',
  reasoning_effort TEXT NOT NULL DEFAULT 'none',
  auth_profile_id TEXT,
  resolved_at TEXT NOT NULL,
  capability_snapshot_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(world_id, actor_id)
);

CREATE TABLE IF NOT EXISTS replay_snapshots (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
  timestamp TEXT NOT NULL,
  state_json TEXT NOT NULL,
  causal_subgraph_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
-- ============================================================
-- Narrative Studio (author control layer over Parallel World)
-- ============================================================

CREATE TABLE IF NOT EXISTS narrative_projects (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  logline TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  format TEXT NOT NULL DEFAULT 'series',
  genre_json TEXT NOT NULL DEFAULT '[]',
  tone_json TEXT NOT NULL DEFAULT '[]',
  target_audience TEXT NOT NULL DEFAULT '',
  planned_episode_count INTEGER NOT NULL DEFAULT 12,
  episode_duration_seconds_min INTEGER NOT NULL DEFAULT 60,
  episode_duration_seconds_max INTEGER NOT NULL DEFAULT 120,
  story_world_id TEXT,
  canonical_world_branch_id TEXT,
  story_bible_version INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'draft',
  revision INTEGER NOT NULL DEFAULT 1,
  runtime_assignment_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_story_bible_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  bible_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, version)
);

CREATE TABLE IF NOT EXISTS narrative_story_facts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'story_truth',
  secret INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_characters (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  persona_id TEXT,
  world_actor_id TEXT,
  persona_origin TEXT NOT NULL DEFAULT 'fictional_author_defined',
  provenance TEXT NOT NULL DEFAULT 'fictional_author_defined',
  visual_json TEXT NOT NULL DEFAULT '{}',
  dialogue_samples_json TEXT NOT NULL DEFAULT '[]',
  backstory TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_character_bindings (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  story_character_id TEXT NOT NULL,
  persona_id TEXT,
  world_actor_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_episode_plans (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER NOT NULL,
  plan_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, episode_number)
);

CREATE TABLE IF NOT EXISTS narrative_episode_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER NOT NULL,
  version INTEGER NOT NULL,
  created_by TEXT NOT NULL DEFAULT 'author',
  version_json TEXT NOT NULL,
  is_canon INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, episode_number, version)
);

CREATE TABLE IF NOT EXISTS narrative_scenes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_id TEXT,
  episode_number INTEGER,
  scene_order INTEGER NOT NULL DEFAULT 0,
  scene_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_canon_facts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER,
  entry_type TEXT NOT NULL DEFAULT 'fact',
  text TEXT NOT NULL,
  data_json TEXT NOT NULL DEFAULT '{}',
  source TEXT NOT NULL DEFAULT 'narrative_commit',
  world_branch_id TEXT,
  world_event_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_canon_project
  ON narrative_canon_facts(project_id, episode_number);

CREATE TABLE IF NOT EXISTS narrative_canon_revisions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  canon_entry_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  previous_text TEXT NOT NULL DEFAULT '',
  new_text TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_knowledge (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  character_id TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  entry_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, character_id, fact_id)
);
CREATE INDEX IF NOT EXISTS idx_narrative_knowledge_project
  ON narrative_knowledge(project_id, character_id);

CREATE TABLE IF NOT EXISTS narrative_audience_knowledge (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  fact_id TEXT NOT NULL,
  entry_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, fact_id)
);

CREATE TABLE IF NOT EXISTS narrative_plot_threads (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  thread_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_character_arcs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  character_id TEXT NOT NULL,
  arc_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, character_id)
);

CREATE TABLE IF NOT EXISTS narrative_clues (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  clue_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_clues_project
  ON narrative_clues(project_id, status);

CREATE TABLE IF NOT EXISTS narrative_forecasts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  forecast_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_audits (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER NOT NULL,
  episode_version_id TEXT,
  passed INTEGER NOT NULL DEFAULT 1,
  audit_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_writer_room_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER NOT NULL,
  room_id TEXT NOT NULL,
  synthesis_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_writer_room_runs
  ON narrative_writer_room_runs(project_id, episode_number, created_at DESC);

CREATE TABLE IF NOT EXISTS narrative_production_packages (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER NOT NULL,
  episode_version_id TEXT,
  package_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_episode_summaries (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER NOT NULL,
  summary TEXT NOT NULL,
  context_fingerprint TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(project_id, episode_number)
);

CREATE INDEX IF NOT EXISTS idx_narrative_episode_versions
  ON narrative_episode_versions(project_id, episode_number, version DESC);

CREATE TABLE IF NOT EXISTS narrative_jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'created',
  payload_json TEXT NOT NULL DEFAULT '{}',
  progress_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_jobs_project
  ON narrative_jobs(project_id, status);

-- Narrative Director Agent sessions (logical conversations, no runtime lease).
CREATE TABLE IF NOT EXISTS narrative_director_sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER,
  mode TEXT NOT NULL DEFAULT 'agent',
  status TEXT NOT NULL DEFAULT 'active',
  runtime_json TEXT NOT NULL DEFAULT '{}',
  conversation_summary TEXT NOT NULL DEFAULT '',
  project_revision INTEGER NOT NULL DEFAULT 0,
  story_bible_version INTEGER NOT NULL DEFAULT 0,
  pending_action_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_director_sessions_project
  ON narrative_director_sessions(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS narrative_director_messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES narrative_director_sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_director_messages_session
  ON narrative_director_messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS narrative_director_actions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES narrative_director_sessions(id) ON DELETE CASCADE,
  message_id TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  arguments_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  error_code TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_narrative_director_actions_session
  ON narrative_director_actions(session_id, started_at);

CREATE INDEX IF NOT EXISTS idx_narrative_production_packages_project
  ON narrative_production_packages(project_id, created_at DESC);

-- Model prompt packages: profile-specific compiled prompt packages derived
-- from narrative_production_packages. package_json holds the full payload.
CREATE TABLE IF NOT EXISTS narrative_model_prompt_packages (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER NOT NULL,
  production_package_id TEXT NOT NULL,
  episode_version_id TEXT NOT NULL,
  target_profile_id TEXT NOT NULL,
  target_profile_version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'clip_planned',
  stale INTEGER NOT NULL DEFAULT 0,
  context_fingerprint TEXT NOT NULL DEFAULT '',
  profile_update_available INTEGER NOT NULL DEFAULT 0,
  package_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_model_prompt_packages_project
  ON narrative_model_prompt_packages(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_narrative_model_prompt_packages_production
  ON narrative_model_prompt_packages(production_package_id, status);

-- Production assets: reference images / frames registered for generation.
CREATE TABLE IF NOT EXISTS narrative_production_assets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER,
  production_package_id TEXT,
  asset_type TEXT NOT NULL,
  asset_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_production_assets_project
  ON narrative_production_assets(project_id);
CREATE INDEX IF NOT EXISTS idx_narrative_production_assets_production
  ON narrative_production_assets(production_package_id);

-- Narrative shooting sessions (logical conversations, no runtime lease).
CREATE TABLE IF NOT EXISTS narrative_shooting_sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER,
  mode TEXT NOT NULL DEFAULT 'agent',
  status TEXT NOT NULL DEFAULT 'active',
  runtime_json TEXT NOT NULL DEFAULT '{}',
  conversation_summary TEXT NOT NULL DEFAULT '',
  project_revision INTEGER NOT NULL DEFAULT 0,
  story_bible_version INTEGER NOT NULL DEFAULT 0,
  pending_action_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_shooting_sessions_project
  ON narrative_shooting_sessions(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS narrative_shooting_messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES narrative_shooting_sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_shooting_messages_session
  ON narrative_shooting_messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS narrative_shooting_actions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES narrative_shooting_sessions(id) ON DELETE CASCADE,
  message_id TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  arguments_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  error_code TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_narrative_shooting_actions_session
  ON narrative_shooting_actions(session_id, started_at);

-- Executable video production guides: third deterministic export layer on
-- top of model prompt packages (assets -> clips -> prompts -> guide).
-- guide_json holds the full payload.
CREATE TABLE IF NOT EXISTS narrative_video_production_guides (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES narrative_projects(id) ON DELETE CASCADE,
  episode_number INTEGER NOT NULL,
  production_package_id TEXT NOT NULL,
  prompt_package_id TEXT NOT NULL,
  episode_version_id TEXT NOT NULL,
  target_profile_id TEXT NOT NULL,
  target_profile_version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'drafting',
  stale INTEGER NOT NULL DEFAULT 0,
  context_fingerprint TEXT NOT NULL DEFAULT '',
  guide_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_video_production_guides_project
  ON narrative_video_production_guides(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_narrative_video_production_guides_prompt_pkg
  ON narrative_video_production_guides(prompt_package_id, status);
CREATE INDEX IF NOT EXISTS idx_narrative_video_production_guides_production
  ON narrative_video_production_guides(production_package_id, status);

"""
