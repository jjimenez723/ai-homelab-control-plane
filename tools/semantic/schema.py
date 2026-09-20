"""Additive Phase 3 catalog tables."""

SEMANTIC_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS semantic_classifications (
  classification_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  sha256 TEXT NOT NULL,
  client_id TEXT,
  client_confidence REAL NOT NULL,
  project_id TEXT,
  project_confidence REAL NOT NULL,
  subject TEXT,
  tags TEXT NOT NULL,
  tag_confidences TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  model_id TEXT NOT NULL,
  model_digest TEXT,
  prompt_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  validator TEXT NOT NULL,
  raw_output TEXT NOT NULL,
  human_review_state TEXT NOT NULL,
  human_corrected INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS semantic_classifications_by_asset
  ON semantic_classifications(asset_id, created_at);
CREATE INDEX IF NOT EXISTS semantic_classifications_by_client
  ON semantic_classifications(client_id);
CREATE INDEX IF NOT EXISTS semantic_classifications_by_project
  ON semantic_classifications(project_id);

CREATE TABLE IF NOT EXISTS semantic_corrections (
  correction_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  field TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT NOT NULL,
  corrected_by TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS semantic_corrections_by_asset
  ON semantic_corrections(asset_id, created_at);

CREATE TABLE IF NOT EXISTS embeddings (
  embedding_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  content_sha256 TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  model_id TEXT NOT NULL,
  model_version TEXT NOT NULL,
  model_digest TEXT,
  schema_version TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  vector_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(asset_id, input_sha256, model_id, model_version, schema_version)
);
CREATE INDEX IF NOT EXISTS embeddings_by_asset ON embeddings(asset_id);

CREATE TABLE IF NOT EXISTS organized_view_runs (
  run_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  dry_run INTEGER NOT NULL,
  drift_fraction REAL,
  drift_aborted INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organized_view_proposals (
  run_id TEXT NOT NULL REFERENCES organized_view_runs(run_id),
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  client_id TEXT,
  project_id TEXT,
  relative_path TEXT,
  tags TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT,
  estimated_bytes INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, asset_id)
);
CREATE INDEX IF NOT EXISTS organized_view_proposals_by_asset
  ON organized_view_proposals(asset_id, created_at);

CREATE TABLE IF NOT EXISTS materializations (
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  relative_path TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  destination_sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(asset_id, relative_path)
);
"""
