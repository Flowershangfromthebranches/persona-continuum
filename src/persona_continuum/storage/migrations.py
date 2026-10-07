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

CREATE TABLE IF NOT EXISTS persona_material_uploads (
  id TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  stored_filename TEXT NOT NULL,
  path TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  source_type TEXT NOT NULL,
  status TEXT NOT NULL,
  job_id TEXT,
  persona_id TEXT,
  created_at TEXT NOT NULL,
  bound_at TEXT,
  consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_persona_material_uploads_status
  ON persona_material_uploads(status, created_at);
CREATE INDEX IF NOT EXISTS idx_persona_material_uploads_job
  ON persona_material_uploads(job_id, status);

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
CREATE INDEX IF NOT EXISTS idx_persona_evidence_units_order
  ON persona_evidence_units(persona_id, source_id,
    COALESCE(CAST(json_extract(source_locator_json, '$.segment_index') AS INTEGER), 0), id);

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

-- Deterministic chat Expression DNA statistics (Large Conversation Pipeline
-- V2, P0-E).  One derived record per persona; raw evidence rows and their
-- provenance are untouched and the profile can be rebuilt from them at any
-- time without calling any model.
CREATE TABLE IF NOT EXISTS persona_chat_style_profiles (
  persona_id TEXT PRIMARY KEY REFERENCES personas(id) ON DELETE CASCADE,
  contract TEXT NOT NULL,
  corpus_size INTEGER NOT NULL DEFAULT 0,
  time_range_json TEXT NOT NULL DEFAULT '[]',
  statistics_json TEXT NOT NULL DEFAULT '{}',
  representative_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  profile_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

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
  research_focus TEXT,
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

-- Verified independent-session concurrency.  Identity covers adapter, CLI
-- binary identity/version, model, credential identity hash, runtime origin and
-- the probe contract version, so a stale verification can never be reused for
-- a different CLI, account, model, or probe contract.  Only an irreversible
-- credential hash is stored; raw secrets never reach this table.
CREATE TABLE IF NOT EXISTS runtime_concurrency_capabilities (
  capability_key TEXT PRIMARY KEY,
  adapter_id TEXT NOT NULL,
  binary_identity TEXT NOT NULL DEFAULT '',
  binary_version TEXT NOT NULL DEFAULT '',
  model_id TEXT NOT NULL DEFAULT '',
  credential_identity_hash TEXT NOT NULL DEFAULT '',
  runtime_origin TEXT NOT NULL DEFAULT '',
  probe_version TEXT NOT NULL DEFAULT '',
  max_verified_independent_sessions INTEGER NOT NULL DEFAULT 1,
  parallel_independent_sessions_verified INTEGER NOT NULL DEFAULT 0,
  parallel_same_session_verified INTEGER NOT NULL DEFAULT 0,
  current_recommended INTEGER NOT NULL DEFAULT 1,
  probe_status TEXT NOT NULL DEFAULT 'unverified',
  probe_sample_count INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT,
  expires_at TEXT,
  last_runtime_downgrade_at TEXT,
  downgrade_reason TEXT,
  downgrade_expires_at TEXT,
  last_failure_kind TEXT,
  last_failure_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_concurrency_adapter
  ON runtime_concurrency_capabilities(adapter_id, model_id);

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

-- ---------------------------------------------------------------------------
-- Memory Architecture v2, Phase 3: Episodes.
--
-- An Episode groups committed turns into "a stretch of shared experience".
-- It is a derived ORGANISATION layer: it never replaces the raw transcript and
-- never replaces the per-turn digital_experience memory.  Provenance lives in
-- memory_episode_turns, so an Episode can always be walked back to the exact
-- turns (and therefore to the raw text) that support it.
--
-- Scope is (persona_id, counterpart_id, branch_id, session_id).  A room is a
-- runtime/UI container and is NOT an isolation key -- the same persona talking
-- to the same counterpart in another room continues the same relationship --
-- but it is recorded because room_transcripts is keyed by it and provenance
-- must be resolvable.  Those two fields are deliberately not equivalent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_episodes (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL,
  counterpart_id TEXT NOT NULL,
  branch_id TEXT NOT NULL DEFAULT 'main',
  session_id TEXT NOT NULL,
  room_id TEXT,
  sequence INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open',
  boundary_reason TEXT NOT NULL DEFAULT 'none',
  started_at TEXT NOT NULL,
  ended_at TEXT,
  turn_count INTEGER NOT NULL DEFAULT 0,
  source_token_estimate INTEGER NOT NULL DEFAULT 0,
  source_first_turn_id TEXT,
  source_last_turn_id TEXT,
  source_range_hash TEXT NOT NULL DEFAULT '',
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 0.5,
  consolidation_version INTEGER NOT NULL DEFAULT 1,
  summary_status TEXT NOT NULL DEFAULT 'pending',
  consolidation_attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  consolidated_at TEXT,
  -- Phase 4: the Episode is also the unit of fact extraction, so its status
  -- lives here as a real column (queryable, indexable) instead of in JSON.
  fact_extraction_status TEXT NOT NULL DEFAULT 'pending',
  fact_extraction_attempts INTEGER NOT NULL DEFAULT 0,
  fact_extraction_error TEXT,
  facts_extracted_at TEXT,
  -- Phase 5: the Episode is likewise the unit of Active-Thread resolution, and
  -- the status column is the durable work list a restart resumes from.
  thread_resolution_status TEXT NOT NULL DEFAULT 'pending',
  thread_resolution_attempts INTEGER NOT NULL DEFAULT 0,
  thread_resolution_error TEXT,
  threads_resolved_at TEXT,
  visibility TEXT NOT NULL DEFAULT 'private_session',
  provenance TEXT NOT NULL DEFAULT 'digital_experience',
  material_scope TEXT NOT NULL DEFAULT 'character_visible',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
-- One Episode per scope position: this is what makes a replayed commit and a
-- re-run consolidation land on the SAME row instead of creating A, A2, A3.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_episodes_scope_sequence
  ON memory_episodes(persona_id, counterpart_id, branch_id, session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_memory_episodes_scope_status
  ON memory_episodes(persona_id, counterpart_id, branch_id, status);
CREATE INDEX IF NOT EXISTS idx_memory_episodes_session_status
  ON memory_episodes(session_id, status);
CREATE INDEX IF NOT EXISTS idx_memory_episodes_persona_time
  ON memory_episodes(persona_id, started_at, ended_at);
-- Drives the durable consolidation sweep after a restart.
CREATE INDEX IF NOT EXISTS idx_memory_episodes_pending
  ON memory_episodes(status, summary_status, updated_at);
-- Drives the durable FACT-extraction sweep after a restart.
CREATE INDEX IF NOT EXISTS idx_memory_episodes_fact_pending
  ON memory_episodes(fact_extraction_status, updated_at);
-- Drives the durable THREAD-resolution sweep after a restart.
CREATE INDEX IF NOT EXISTS idx_memory_episodes_thread_pending
  ON memory_episodes(thread_resolution_status, updated_at);

CREATE TABLE IF NOT EXISTS memory_episode_turns (
  episode_id TEXT NOT NULL REFERENCES memory_episodes(id) ON DELETE CASCADE,
  turn_id TEXT NOT NULL,
  source_kind TEXT NOT NULL DEFAULT 'session_turn',
  position INTEGER NOT NULL DEFAULT 0,
  session_id TEXT,
  room_id TEXT,
  speaker TEXT NOT NULL DEFAULT '',
  occurred_at TEXT,
  token_estimate INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (episode_id, turn_id)
);
-- Given a turn -> which Episode owns it.
CREATE INDEX IF NOT EXISTS idx_memory_episode_turns_turn
  ON memory_episode_turns(turn_id);
CREATE INDEX IF NOT EXISTS idx_memory_episode_turns_session
  ON memory_episode_turns(session_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_memory_episode_turns_episode_position
  ON memory_episode_turns(episode_id, position);

-- ---------------------------------------------------------------------------
-- Memory Architecture v2, Phase 4: Semantic Facts.
--
-- An Episode records what happened; a Fact records what is true about a
-- counterpart, distilled from Episodes.  A fact is never overwritten: a value
-- that stops being true gets ``valid_until`` and ``superseded_by_fact_id``
-- while the new value becomes a new row, so the history stays readable.
-- Scope is (persona_id, counterpart_id, branch_id) -- deliberately the same
-- isolation as Episode/memory, with no implicit cross-persona sharing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_semantic_facts (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL,
  counterpart_id TEXT NOT NULL,
  branch_id TEXT NOT NULL DEFAULT 'main',
  category TEXT NOT NULL DEFAULT 'other',
  fact_key TEXT NOT NULL DEFAULT '',
  value_key TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  predicate TEXT NOT NULL DEFAULT '',
  value_json TEXT NOT NULL DEFAULT '{}',
  display_text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  origin TEXT NOT NULL DEFAULT 'inferred',
  durability TEXT NOT NULL DEFAULT 'unknown',
  plan_status TEXT NOT NULL DEFAULT 'not_applicable',
  confidence REAL NOT NULL DEFAULT 0.5,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  last_confirmed_at TEXT,
  valid_from TEXT,
  valid_until TEXT,
  observed_at TEXT,
  temporal_expression TEXT,
  temporal_normalized TEXT,
  temporal_confidence REAL NOT NULL DEFAULT 0.0,
  superseded_by_fact_id TEXT,
  supersedes_fact_id TEXT,
  extraction_version INTEGER NOT NULL DEFAULT 1,
  visibility TEXT NOT NULL DEFAULT 'private_session',
  material_scope TEXT NOT NULL DEFAULT 'character_visible',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
-- One ACTIVE fact per (scope, slot, value): re-extraction reinforces the
-- existing row instead of piling up duplicates.  Superseded rows leave the
-- partial index, which is what keeps the history intact.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_semantic_facts_active_key
  ON memory_semantic_facts(persona_id, counterpart_id, branch_id, fact_key, value_key)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_memory_semantic_facts_slot
  ON memory_semantic_facts(persona_id, counterpart_id, branch_id, fact_key);
CREATE INDEX IF NOT EXISTS idx_memory_semantic_facts_status
  ON memory_semantic_facts(persona_id, counterpart_id, branch_id, status, category);
CREATE INDEX IF NOT EXISTS idx_memory_semantic_facts_validity
  ON memory_semantic_facts(valid_from, valid_until);

CREATE TABLE IF NOT EXISTS memory_fact_sources (
  fact_id TEXT NOT NULL REFERENCES memory_semantic_facts(id) ON DELETE CASCADE,
  source_type TEXT NOT NULL DEFAULT 'episode',
  episode_id TEXT NOT NULL DEFAULT '',
  turn_id TEXT NOT NULL DEFAULT '',
  session_id TEXT,
  room_id TEXT,
  evidence_role TEXT NOT NULL DEFAULT 'supporting',
  excerpt_available INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  PRIMARY KEY (fact_id, source_type, episode_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_fact_sources_episode
  ON memory_fact_sources(episode_id);
CREATE INDEX IF NOT EXISTS idx_memory_fact_sources_turn
  ON memory_fact_sources(turn_id);

-- ---------------------------------------------------------------------------
-- Memory Architecture v2, Phase 5: Active Threads.
--
-- A Fact says "what is true"; a Thread says "what is still going on".  It is
-- the thing a later conversation can continue -- a trip being planned, an
-- argument not yet settled, a project still in progress -- and it must NOT
-- disappear when the recent-dialogue window moves past it.  Its lifetime is
-- therefore owned by the memory layer, never by a prompt budget or a message
-- count.
--
-- Scope is (persona_id, counterpart_id, branch_id): the same isolation as
-- Episode/Fact.  ``thread_key`` is the canonical identity inside that scope, so
-- "重庆旅行" / "去重庆" / "重庆计划" converge on ONE live thread instead of four.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_active_threads (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL,
  counterpart_id TEXT NOT NULL,
  branch_id TEXT NOT NULL DEFAULT 'main',
  thread_key TEXT NOT NULL,
  thread_type TEXT NOT NULL DEFAULT 'general',
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 0.5,
  opened_at TEXT NOT NULL,
  last_activity_at TEXT NOT NULL,
  resolved_at TEXT,
  cancelled_at TEXT,
  stale_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  current_state_json TEXT NOT NULL DEFAULT '{}',
  consolidation_version INTEGER NOT NULL DEFAULT 1,
  visibility TEXT NOT NULL DEFAULT 'private_session',
  material_scope TEXT NOT NULL DEFAULT 'character_visible',
  -- A thread that revives after a long gap may be recorded as a NEW thread
  -- instead of a reopen; the link keeps that from erasing the earlier history.
  related_previous_thread_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
-- One LIVE thread per (scope, canonical key): a replay, a re-resolution, or a
-- model that paraphrases the same subject cannot fork a second live thread.
-- RESOLVED/CANCELLED rows leave the index, so history is kept while a genuinely
-- new episode of the same subject is still representable.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_active_threads_live_key
  ON memory_active_threads(persona_id, counterpart_id, branch_id, thread_key)
  WHERE status IN ('open', 'active', 'waiting', 'stale');
CREATE INDEX IF NOT EXISTS idx_memory_active_threads_scope_status
  ON memory_active_threads(persona_id, counterpart_id, branch_id, status);
CREATE INDEX IF NOT EXISTS idx_memory_active_threads_activity
  ON memory_active_threads(persona_id, counterpart_id, branch_id, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_active_threads_type
  ON memory_active_threads(persona_id, counterpart_id, branch_id, thread_type, status);

-- Thread history as an append-only ledger.  A status change never overwrites
-- the previous step: 决定去 → 选时间 → 买票 → 出发 → 完成 stays readable instead of
-- collapsing into a single ``status=resolved`` row.
CREATE TABLE IF NOT EXISTS memory_thread_events (
  event_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES memory_active_threads(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  -- Stable identity for a replay: the same episode producing the same event
  -- twice inserts once.  Without it, a retried resolution would duplicate a
  -- milestone and the history would lie.
  event_key TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  state_json TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL,
  source_episode_id TEXT,
  source_turn_id TEXT,
  -- Phase 3.1 semantics: an explicitly deleted source must be visible as such,
  -- never a silent dangling reference.
  source_availability TEXT NOT NULL DEFAULT 'complete',
  confidence REAL NOT NULL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_thread_events_replay
  ON memory_thread_events(thread_id, event_key);
CREATE INDEX IF NOT EXISTS idx_memory_thread_events_thread_time
  ON memory_thread_events(thread_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_memory_thread_events_episode
  ON memory_thread_events(source_episode_id);

-- Thread -> Episode -> Turn -> raw text.  A Thread that cannot be explained by
-- a source turn is not allowed to exist.
CREATE TABLE IF NOT EXISTS memory_thread_sources (
  thread_id TEXT NOT NULL REFERENCES memory_active_threads(id) ON DELETE CASCADE,
  source_type TEXT NOT NULL DEFAULT 'episode',
  episode_id TEXT NOT NULL DEFAULT '',
  turn_id TEXT NOT NULL DEFAULT '',
  session_id TEXT,
  room_id TEXT,
  evidence_role TEXT NOT NULL DEFAULT 'supporting',
  excerpt_available INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  PRIMARY KEY (thread_id, source_type, episode_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_thread_sources_episode
  ON memory_thread_sources(episode_id);
CREATE INDEX IF NOT EXISTS idx_memory_thread_sources_turn
  ON memory_thread_sources(turn_id);

-- Thread <-> Fact lineage.  The Fact stays the single source of its own text;
-- the Thread only points at it, and a deleted fact takes its link with it.
CREATE TABLE IF NOT EXISTS memory_thread_facts (
  thread_id TEXT NOT NULL REFERENCES memory_active_threads(id) ON DELETE CASCADE,
  fact_id TEXT NOT NULL REFERENCES memory_semantic_facts(id) ON DELETE CASCADE,
  relation TEXT NOT NULL DEFAULT 'supporting',
  created_at TEXT NOT NULL,
  PRIMARY KEY (thread_id, fact_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_thread_facts_fact
  ON memory_thread_facts(fact_id);

-- Ambiguity safety: a low-confidence continuation is recorded as a CANDIDATE
-- instead of being applied.  "她联系我了" with three plausible relationship
-- threads must not silently bind to the wrong one.
CREATE TABLE IF NOT EXISTS memory_thread_link_candidates (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL,
  counterpart_id TEXT NOT NULL,
  branch_id TEXT NOT NULL DEFAULT 'main',
  episode_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.0,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_thread_link_candidates_pair
  ON memory_thread_link_candidates(episode_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_memory_thread_link_candidates_pending
  ON memory_thread_link_candidates(status, updated_at);

-- ---------------------------------------------------------------------------
-- Memory Architecture v2, Phase 6: Hierarchical Summaries.
--
-- Episodes answer "what happened in this stretch"; a Chapter answers "what
-- phase were we in", and a Long-term segment answers "what have we been
-- through".  They are DERIVED ORGANISATION layers over Episodes (level 1) and
-- over Chapters (level 2+), never a replacement for the raw turns, Episodes,
-- Facts or Threads underneath them.
--
-- A summary always names its real sources in ``memory_summary_sources``: a
-- Chapter points at Episodes, a Long-term segment points at Chapters, so the
-- chain Long-term -> Chapter -> Episode -> Turn -> raw text stays walkable.
-- ``level`` is a plain integer on purpose: nothing in the schema assumes that
-- history only has two layers.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_hierarchical_summaries (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL,
  counterpart_id TEXT NOT NULL,
  branch_id TEXT NOT NULL DEFAULT 'main',
  level INTEGER NOT NULL DEFAULT 1,
  summary_type TEXT NOT NULL DEFAULT 'chapter',
  sequence INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  source_count INTEGER NOT NULL DEFAULT 0,
  source_token_estimate INTEGER NOT NULL DEFAULT 0,
  importance REAL NOT NULL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  consolidation_version INTEGER NOT NULL DEFAULT 1,
  -- pending / provisional / ready / stale / failed.  An OPEN chapter may carry
  -- a PROVISIONAL summary; closing it regenerates from the full source set, so
  -- a provisional text can never be mistaken for final history.  STALE means
  -- the text was right for the sources it was built from but a source has
  -- changed since (Phase 6.1) -- readable, but not current, and owed a refresh.
  summary_status TEXT NOT NULL DEFAULT 'pending',
  consolidation_attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  consolidated_at TEXT,
  -- Stable identity of the source set, so replaying the same range updates one
  -- row instead of creating "September Chapter 2 / 3 / 4".
  source_range_hash TEXT NOT NULL DEFAULT '',
  -- Phase 6.1: identity of the sources IN THEIR CURRENT STATE, and the same
  -- identity as it was when the stored text was generated.  A mismatch is what
  -- turns "a child changed underneath me" into an arithmetic question instead
  -- of a guess, without re-running a model.
  source_fingerprint TEXT NOT NULL DEFAULT '',
  consolidated_fingerprint TEXT NOT NULL DEFAULT '',
  parent_summary_id TEXT,
  visibility TEXT NOT NULL DEFAULT 'private_session',
  material_scope TEXT NOT NULL DEFAULT 'character_visible',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_hierarchical_summaries_range
  ON memory_hierarchical_summaries(persona_id, counterpart_id, branch_id, level, source_range_hash)
  WHERE source_range_hash != '' AND status != 'retracted';
CREATE INDEX IF NOT EXISTS idx_memory_hierarchical_summaries_scope_level
  ON memory_hierarchical_summaries(persona_id, counterpart_id, branch_id, level, status);
CREATE INDEX IF NOT EXISTS idx_memory_hierarchical_summaries_time
  ON memory_hierarchical_summaries(persona_id, counterpart_id, branch_id, started_at);
CREATE INDEX IF NOT EXISTS idx_memory_hierarchical_summaries_pending
  ON memory_hierarchical_summaries(summary_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_hierarchical_summaries_parent
  ON memory_hierarchical_summaries(parent_summary_id);

CREATE TABLE IF NOT EXISTS memory_summary_sources (
  summary_id TEXT NOT NULL REFERENCES memory_hierarchical_summaries(id) ON DELETE CASCADE,
  source_type TEXT NOT NULL DEFAULT 'episode',
  source_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  ended_at TEXT,
  importance REAL NOT NULL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  PRIMARY KEY (summary_id, source_type, source_id)
);
-- One Chapter per Episode, and one Long-term seat per Chapter: this is what
-- makes re-running the grouping idempotent instead of producing parallel
-- summaries over the same history.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_summary_sources_episode
  ON memory_summary_sources(source_type, source_id) WHERE source_type = 'episode';
CREATE INDEX IF NOT EXISTS idx_memory_summary_sources_lookup
  ON memory_summary_sources(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_memory_summary_sources_summary_position
  ON memory_summary_sources(summary_id, position);

-- Applied schema migrations, so an upgrade can be identified without guessing
-- from the table list.
CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}'
);

"""

#: Phase 3 migration id recorded in ``schema_migrations``.
MEMORY_EPISODES_MIGRATION_ID = "phase3_memory_episodes"

#: Phase 3.1: shared room user turns become provenance of every persona Episode
#: that answered them (no schema change, but a semantic contract worth stamping).
SHARED_USER_PROVENANCE_MIGRATION_ID = "phase31_shared_user_provenance"

#: Phase 4 migration id recorded in ``schema_migrations``.
SEMANTIC_FACTS_MIGRATION_ID = "phase4_semantic_facts"

#: Phase 5 migration id recorded in ``schema_migrations``.
ACTIVE_THREADS_MIGRATION_ID = "phase5_active_threads"

#: Phase 6 migration id recorded in ``schema_migrations``.
HIERARCHICAL_SUMMARIES_MIGRATION_ID = "phase6_hierarchical_summaries"

#: Phase 6.1: parent summaries record the state of their sources, so a changed
#: child invalidates its direct parent instead of the parent quietly quoting a
#: stale version.  Additive columns only; recorded separately so an operator can
#: tell a Phase 6 directory from a hardened one.
HIERARCHY_DEPENDENCY_MIGRATION_ID = "phase61_hierarchy_dependency"

#: Phase 7: provenance-backed raw recall.  No new table -- a raw excerpt is a
#: *view* over the authoritative ``session_turns`` / ``room_transcripts`` built
#: by walking the provenance the earlier phases already recorded.
RAW_RECALL_MIGRATION_ID = "phase7_raw_recall"

#: Reverse of the Phase 3 migration.  Safe by construction: both tables are new
#: and hold only derived organisation data -- no other table references them,
#: and dropping them cannot touch raw transcripts, memories, or lineage.  It is
#: never run automatically; an operator applies it deliberately to roll back.
MEMORY_EPISODES_DOWN_SQL = """
DROP TABLE IF EXISTS memory_episode_turns;
DROP TABLE IF EXISTS memory_episodes;
DELETE FROM schema_migrations WHERE migration_id = 'phase3_memory_episodes';
DELETE FROM schema_migrations WHERE migration_id = 'phase31_shared_user_provenance';
"""

#: Reverse of the Phase 4 migration.  Same reasoning: the Fact tables are new
#: and derived, and nothing outside them references them.
SEMANTIC_FACTS_DOWN_SQL = """
DROP TABLE IF EXISTS memory_fact_sources;
DROP TABLE IF EXISTS memory_semantic_facts;
DELETE FROM schema_migrations WHERE migration_id = 'phase4_semantic_facts';
"""

#: Reverse of the Phase 5 migration.  Same reasoning as Phase 3/4: the Thread
#: tables are new, derived, and referenced by nothing outside themselves.
ACTIVE_THREADS_DOWN_SQL = """
DROP TABLE IF EXISTS memory_thread_link_candidates;
DROP TABLE IF EXISTS memory_thread_facts;
DROP TABLE IF EXISTS memory_thread_sources;
DROP TABLE IF EXISTS memory_thread_events;
DROP TABLE IF EXISTS memory_active_threads;
DELETE FROM schema_migrations WHERE migration_id = 'phase5_active_threads';
"""

#: Reverse of the Phase 6 migration.  Same reasoning again: the summary tables
#: are new, derived, and referenced by nothing outside themselves.
HIERARCHICAL_SUMMARIES_DOWN_SQL = """
DROP TABLE IF EXISTS memory_summary_sources;
DROP TABLE IF EXISTS memory_hierarchical_summaries;
DELETE FROM schema_migrations WHERE migration_id = 'phase6_hierarchical_summaries';
DELETE FROM schema_migrations WHERE migration_id = 'phase61_hierarchy_dependency';
DELETE FROM schema_migrations WHERE migration_id = 'phase7_raw_recall';
"""

