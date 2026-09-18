"""Table definitions shared by the T7 catalog and the Mac-side local ledger.

The two databases hold different scopes (see `migrations.py`) but the same
*shapes* for naming lineage, classification runs, and media facts -- so a file
that starts life in Downloads and later gets ingested into Angel keeps one
continuous, comparable record on both sides.

Three separations are structural here and must not be collapsed:

1. **Deterministic facts vs. model proposals.** `media_metadata` holds what a
   probe measured. `naming_history` and `classification_runs` hold what a model
   (or the deterministic proposer standing in for one) *suggested*. A proposal
   never overwrites a fact.
2. **History vs. current state.** `naming_history` and `classification_runs`
   are append-only ledgers. Nothing rewrites a prior row when a model changes
   its mind; a new run is appended. For classification authority, append order
   is canonical and `created_at` is audit metadata rather than a sequencing
   primitive, so clock skew cannot move an owner decision behind a model row.
3. **Machine vs. human.** `approved_by` / `human_corrected` mark rows a person
   authored or overrode. Those rows outrank every later model output
   (invariant 12) -- which is why they are rows and not a mutable flag.
"""

from __future__ import annotations

# Lineage of every filename decision, including the decisions *not* to rename.
# `local_file_id` is the identity that survives a rename (see `identity.py`);
# `asset_id` is filled in only once/if the file is ingested into Angel, which is
# what keeps "this file was named" and "this file was kept" separable.
NAMING_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS naming_history (
  naming_id TEXT PRIMARY KEY,
  local_file_id TEXT NOT NULL,
  asset_id TEXT,
  source_volume_id TEXT NOT NULL,
  source_class TEXT NOT NULL,
  directory TEXT NOT NULL,
  sha256 TEXT,
  original_filename TEXT NOT NULL,
  proposed_filename TEXT,
  applied_filename TEXT,
  name_quality TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT,
  model TEXT,
  model_version TEXT,
  confidence REAL,
  approved_by TEXT NOT NULL,
  human_corrected INTEGER NOT NULL DEFAULT 0,
  policy_outcome TEXT,
  created_at TEXT NOT NULL,
  -- Which content generation of `local_file_id` this row describes. Inodes
  -- are reused and can also be rewritten in place, so one local identity can
  -- describe different bytes over time; earlier rows stay as audit history but
  -- must never be read back as the current bytes' history (D-028). T7 rows are
  -- reached by asset and simply carry the generation they were written with.
  generation INTEGER NOT NULL DEFAULT 0,
  -- D-057 naming audit: what the proposer was asked and what it answered.
  -- All nullable, because the deterministic proposer has no prompt and no raw
  -- output, and an absent column is a truer statement than an empty string.
  --
  -- `prompt_*` identify the exact rendered prompt by the registry's own
  -- (id, version) plus the SHA-256 of the fully rendered text, so a naming
  -- decision can be reproduced rather than merely attributed.
  prompt_id TEXT,
  prompt_version TEXT,
  prompt_sha256 TEXT,
  -- The content hash of the *perceived evidence* this decision saw. Equal to
  -- `sha256` in normal operation (evidence is keyed by content), and stored
  -- separately anyway: it is the column that answers "which evidence produced
  -- this filename?" without assuming the two can never diverge.
  evidence_sha256 TEXT,
  -- JSON array of `PerceivedEvidence.sources` -- which kinds of perception
  -- were actually in front of the proposer ("native_text", "ocr", "visual",
  -- "transcript", "structure").
  evidence_sources TEXT,
  -- The model's answer exactly as it came back, bounded, BEFORE cleanup or
  -- validation. Recorded even when the answer was refused: a rejected hostile
  -- proposal that leaves no trace is a rejection nobody can audit.
  raw_output TEXT
);
CREATE INDEX IF NOT EXISTS naming_history_by_file
  ON naming_history(local_file_id, created_at);
CREATE INDEX IF NOT EXISTS naming_history_by_asset
  ON naming_history(asset_id);
CREATE INDEX IF NOT EXISTS naming_history_by_applied
  ON naming_history(local_file_id, applied_filename);
"""

# One row per classification/retention evaluation. Deliberately *not* a set of
# mutable columns on the asset row: a reclassification appends, so an earlier
# recommendation (and who/what made it) stays auditable after a model upgrade.
CLASSIFICATION_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS classification_runs (
  run_id TEXT PRIMARY KEY,
  local_file_id TEXT,
  asset_id TEXT,
  source_volume_id TEXT NOT NULL,
  source_class TEXT NOT NULL,
  sha256 TEXT,
  recommendation TEXT NOT NULL,
  confidence REAL,
  reason TEXT,
  client_proposal TEXT,
  project_proposal TEXT,
  tags TEXT,
  content_description TEXT,
  canonical_filename_proposal TEXT,
  destination_proposal TEXT,
  model TEXT,
  model_version TEXT,
  policy_outcome TEXT NOT NULL,
  human_state TEXT,
  human_corrected INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  -- See `naming_history.generation`: an inode-reuse reset must not let a
  -- stranger's file inherit the owner's standing retention decision.
  generation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS classification_runs_by_file
  ON classification_runs(local_file_id, created_at);
-- The sequence/latest lookups key on COALESCE(local_file_id, asset_id, ''),
-- which no plain column index can serve; without this SQLite scans the whole
-- table once per file per run.
CREATE INDEX IF NOT EXISTS classification_runs_by_subject
  ON classification_runs(COALESCE(local_file_id, asset_id, ''), created_at);
CREATE INDEX IF NOT EXISTS classification_runs_by_asset
  ON classification_runs(asset_id, created_at);
"""

# Deterministic, measured facts only. Nothing a model guessed belongs here.
MEDIA_METADATA_SQL = """
CREATE TABLE IF NOT EXISTS media_metadata (
  subject_id TEXT NOT NULL,
  subject_kind TEXT NOT NULL,
  media_kind TEXT NOT NULL,
  mime TEXT,
  probe TEXT NOT NULL,
  probe_version TEXT NOT NULL,
  facts TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(subject_id, subject_kind)
);
"""

# Observed relationships only -- never a deletion instruction. `kind`
# distinguishes byte-identical content from a same-name-different-bytes clash
# from a derived/transcoded relationship, because those call for different
# human decisions.
DUPLICATE_OBSERVATIONS_SQL = """
CREATE TABLE IF NOT EXISTS duplicate_observations (
  observation_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  left_ref TEXT NOT NULL,
  left_scope TEXT NOT NULL,
  right_ref TEXT NOT NULL,
  right_scope TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS duplicate_observations_by_left
  ON duplicate_observations(left_ref);
"""

# A content-keyed cache of perceived evidence (OCR, native text, vision
# description, layout structure) -- not a decision ledger. `evidence_id`
# already encodes content hash + producer/model/pipeline identity (see
# `identity.evidence_id_for`), so there is exactly one correct row per key and
# re-deriving the same measurement is expected to overwrite it rather than
# accumulate history the way `naming_history`/`classification_runs` do. See
# `tools/ingest/evidence/cache.py` for the cache contract this backs.
#
# Mac-side (`LocalLedger`) only in this phase, deliberately: extracted text is
# never stored in the T7 catalog (see the evidence-layer spec's privacy
# posture). This table is therefore not part of `catalog.py`'s migration
# chain.
FILE_EVIDENCE_SQL = """
CREATE TABLE IF NOT EXISTS file_evidence (
  evidence_id TEXT PRIMARY KEY,
  content_sha256 TEXT NOT NULL,
  evidence_kind TEXT NOT NULL,
  producer TEXT NOT NULL,
  producer_version TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  model_version TEXT NOT NULL DEFAULT '',
  pipeline_version TEXT NOT NULL,
  route_reason TEXT,
  status TEXT NOT NULL,
  payload TEXT NOT NULL,
  warnings TEXT NOT NULL,
  generated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS file_evidence_by_content ON file_evidence(content_sha256);
CREATE INDEX IF NOT EXISTS file_evidence_by_kind ON file_evidence(content_sha256, evidence_kind);
"""
