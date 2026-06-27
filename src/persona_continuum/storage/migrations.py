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
"""
