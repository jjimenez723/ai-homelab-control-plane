"""SQLite catalog + JSON sidecars (D-009): SQLite is the query/transaction
layer, sidecars are the durable human-readable record regenerated from it.

Schema is additive-compatible with the V0 design in
`reviews/ASTRA-V0-ARCHITECTURE-REVIEW.md` §11 (`volumes`, `blobs`, `assets`,
`repos`, `corrections`, `jobs`), extended with the Phase 1A transfer-state
columns from `docs/13-dump-first-ingestion.md` and a `derivatives` table for
HEIC->JPEG (and future) derivative relationships. Single writer, WAL mode.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.ingest.automation import write_json_atomically
from tools.ingest.ledger import LedgerTables
from tools.ingest.migrations import add_column_if_missing, apply_migrations, connect
from tools.ingest.schema_sql import (
    CLASSIFICATION_RUNS_SQL,
    DUPLICATE_OBSERVATIONS_SQL,
    MEDIA_METADATA_SQL,
    NAMING_HISTORY_SQL,
)
from tools.semantic.schema import SEMANTIC_SCHEMA_SQL
from tools.semantic.ids import (
    embedding_id_for,
    semantic_classification_id_for,
    semantic_correction_id_for,
)

# Sidecar schema version. V3 records the content-aware asset identity model:
# blobs are content identity; assets are source occurrences and paths may be
# reused by multiple generations of bytes.
SCHEMA_VERSION = 8

# Transfer state machine. Ordered roughly by progress; retries resume from
# wherever the previous attempt stopped instead of restarting blindly.
STATE_PENDING = "pending"
STATE_COPYING = "copying"
STATE_VERIFIED = "verified"
STATE_COMMITTED = "committed"
STATE_SOURCE_UNLINKED = "source_unlinked"
STATE_FAILED_HASH_MISMATCH = "failed_hash_mismatch"
STATE_FAILED_ERROR = "failed_error"
STATE_NEEDS_REVIEW = "needs_review"

TERMINAL_SUCCESS_STATES = {STATE_SOURCE_UNLINKED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS volumes (
  volume_id TEXT PRIMARY KEY,
  label TEXT,
  mount_point TEXT,
  fs TEXT,
  first_seen TEXT,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS blobs (
  sha256 TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  mime TEXT,
  first_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  sha256 TEXT REFERENCES blobs(sha256),
  source_volume_id TEXT NOT NULL,
  source_class TEXT NOT NULL,
  original_path TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  size INTEGER,
  mime TEXT,
  detected_at TEXT,
  transfer_started_at TEXT,
  transferred_at TEXT,
  verified_at TEXT,
  completed_at TEXT,
  transfer_state TEXT NOT NULL,
  final_path TEXT,
  final_filename TEXT,
  error_state TEXT,
  error_message TEXT,
  derived_from TEXT REFERENCES assets(asset_id),
  classifier_model TEXT,
  classifier_model_version TEXT,
  classifier_confidence REAL,
  human_reviewed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derivatives (
  parent_asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  kind TEXT NOT NULL,
  child_asset_id TEXT REFERENCES assets(asset_id),
  converter TEXT NOT NULL,
  converter_version TEXT NOT NULL,
  quality INTEGER,
  status TEXT NOT NULL,
  error_message TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(parent_asset_id, kind)
);

CREATE TABLE IF NOT EXISTS corrections (
  asset_id TEXT NOT NULL,
  field TEXT NOT NULL,
  value TEXT,
  by TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(asset_id, field)
);

CREATE TABLE IF NOT EXISTS repos (
  repo_id TEXT PRIMARY KEY,
  source_volume_id TEXT,
  original_path TEXT,
  remote_url TEXT,
  head TEXT,
  branch TEXT,
  dirty INTEGER,
  unpushed INTEGER,
  stash INTEGER,
  detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  asset_id TEXT,
  class TEXT,
  status TEXT,
  created_at TEXT,
  finished_at TEXT
);

"""


# Local-model analysis: append-only proposals, analysis/preflight runs, and
# human-maintained person profiles with their confirmed observations.
MODEL_ANALYSIS_SQL = """
-- Model output is append-only proposal data. A future model version can write
-- a new proposal without erasing provenance or a human correction.
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL,
  field TEXT NOT NULL,
  value TEXT NOT NULL,
  confidence REAL,
  model TEXT NOT NULL,
  model_version TEXT NOT NULL,
  prompt_sha TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS analysis_runs (
  asset_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  model TEXT NOT NULL,
  model_version TEXT NOT NULL,
  prompt_sha TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(asset_id, stage, input_sha256, model, model_version, prompt_sha)
);

-- Pre-ingest analysis runs against source media that has not reached T7 yet.
-- The full review record is kept so a UI/process restart can rebuild the
-- review list without rerunning the model or trusting an in-memory plan.
CREATE TABLE IF NOT EXISTS preflight_results (
  result_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_volume_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  source_size INTEGER NOT NULL,
  source_mtime_ns INTEGER NOT NULL,
  model TEXT NOT NULL,
  model_version TEXT NOT NULL,
  prompt_sha TEXT,
  status TEXT NOT NULL,
  result_json TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(source_volume_id, relative_path, source_size, source_mtime_ns, model, model_version)
);

CREATE TABLE IF NOT EXISTS person_profiles (
  profile_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL DEFAULT 'global',
  display_name TEXT NOT NULL UNIQUE,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_observations (
  asset_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  confidence REAL,
  source TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '',
  model TEXT,
  model_version TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(asset_id, profile_id),
  FOREIGN KEY(profile_id) REFERENCES person_profiles(profile_id)
);

CREATE TABLE IF NOT EXISTS correction_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL,
  field TEXT NOT NULL,
  value TEXT,
  by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

def _migration_1_phase_1a(conn: sqlite3.Connection) -> None:
    """The original Phase 1A schema.

    Every statement is `IF NOT EXISTS`, so a catalog created before migrations
    existed (which reports `user_version = 0`) is brought to version 1 without
    a single row or column being touched.
    """
    conn.executescript(SCHEMA)


def _migration_2_file_intelligence(conn: sqlite3.Connection) -> None:
    """Naming lineage, classification history, media facts, duplicates.

    Purely additive. The `assets` columns below record where an ingested asset
    came from *before* it was renamed locally -- `original_filename` keeps its
    existing meaning (the name at the moment of transfer), and
    `first_seen_filename` is the name the file had when this system first saw
    it. Both are needed: the first is what the asset_id was derived from, the
    second is what the owner would recognise.
    """
    conn.executescript(NAMING_HISTORY_SQL)
    conn.executescript(CLASSIFICATION_RUNS_SQL)
    conn.executescript(MEDIA_METADATA_SQL)
    conn.executescript(DUPLICATE_OBSERVATIONS_SQL)
    add_column_if_missing(conn, "assets", "first_seen_filename", "TEXT")
    add_column_if_missing(conn, "assets", "local_file_id", "TEXT")
    add_column_if_missing(conn, "assets", "retention_recommendation", "TEXT")
    add_column_if_missing(conn, "assets", "retention_run_id", "TEXT")


def _migration_3_generation(conn: sqlite3.Connection) -> None:
    """Carry the Mac-side generation of a lineage row onto the T7 copy.

    The catalog never scopes its own queries by generation -- its rows are
    reached by asset -- but the column has to exist here because
    `attach_local_provenance` copies ledger rows across verbatim, and dropping
    the field on the way in would lose the only marker saying which physical
    file a row is about.
    """
    add_column_if_missing(conn, "naming_history", "generation", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(conn, "classification_runs", "generation", "INTEGER NOT NULL DEFAULT 0")


def _assets_has_path_uniqueness(conn: sqlite3.Connection) -> bool:
    """Whether the legacy one-row-per-(volume,path) constraint still exists."""
    for index in conn.execute("PRAGMA index_list(assets)").fetchall():
        if not index["unique"]:
            continue
        name = str(index["name"]).replace('"', '""')
        columns = [row["name"] for row in conn.execute(f'PRAGMA index_info("{name}")').fetchall()]
        if columns == ["source_volume_id", "original_path"]:
            return True
    return False


def _migration_4_reusable_source_paths(conn: sqlite3.Connection) -> None:
    """Remove the legacy UNIQUE(source_volume_id, original_path) constraint.

    A path is a location, not a durable file identity. SQLite cannot drop a
    table-level UNIQUE constraint in place, so this is the one deliberately
    reconstructive migration. It is atomic: either the old table survives
    untouched or the replacement containing every row is committed.

    Foreign keys are disabled only for the duration of the rebuild so tables
    such as derivatives can continue to reference the replacement `assets`
    table by the same name.
    """
    if not _assets_has_path_uniqueness(conn):
        return

    columns = (
        "asset_id", "sha256", "source_volume_id", "source_class", "original_path",
        "original_filename", "size", "mime", "detected_at", "transfer_started_at",
        "transferred_at", "verified_at", "completed_at", "transfer_state",
        "final_path", "final_filename", "error_state", "error_message",
        "derived_from", "classifier_model", "classifier_model_version",
        "classifier_confidence", "human_reviewed", "created_at", "updated_at",
        "first_seen_filename", "local_file_id", "retention_recommendation",
        "retention_run_id",
    )
    column_list = ", ".join(columns)

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE assets_v4 (
              asset_id TEXT PRIMARY KEY,
              sha256 TEXT REFERENCES blobs(sha256),
              source_volume_id TEXT NOT NULL,
              source_class TEXT NOT NULL,
              original_path TEXT NOT NULL,
              original_filename TEXT NOT NULL,
              size INTEGER,
              mime TEXT,
              detected_at TEXT,
              transfer_started_at TEXT,
              transferred_at TEXT,
              verified_at TEXT,
              completed_at TEXT,
              transfer_state TEXT NOT NULL,
              final_path TEXT,
              final_filename TEXT,
              error_state TEXT,
              error_message TEXT,
              derived_from TEXT REFERENCES assets(asset_id),
              classifier_model TEXT,
              classifier_model_version TEXT,
              classifier_confidence REAL,
              human_reviewed INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              first_seen_filename TEXT,
              local_file_id TEXT,
              retention_recommendation TEXT,
              retention_run_id TEXT
            )
            """
        )
        conn.execute(
            f"INSERT INTO assets_v4 ({column_list}) SELECT {column_list} FROM assets"
        )
        conn.execute("DROP TABLE assets")
        conn.execute("ALTER TABLE assets_v4 RENAME TO assets")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migration_5_naming_audit(conn: sqlite3.Connection) -> None:
    """Carry the D-057 naming-audit columns onto the T7 copy of a lineage row.

    Same reasoning as `_migration_3_generation`: the catalog never queries
    these columns itself, but `pipeline.py::_attach_provenance` copies ledger
    rows across verbatim through `append_naming`, whose column list is
    `ledger.NAMING_HISTORY_COLUMNS`. Without the columns here that insert
    would fail on an already-ingested file the moment naming gained an audit
    trail.

    Note what this does *not* carry: the evidence text itself. Only the
    content hash, the evidence *kinds*, the prompt identity and the model's
    own raw one-line answer travel to Angel. Extracted document text stays in
    the Mac-side ledger, which is not restic-backed -- D-030's privacy posture
    is unchanged by giving naming an audit trail.
    """
    for column in ("prompt_id", "prompt_version", "prompt_sha256", "evidence_sha256",
                   "evidence_sources", "raw_output"):
        add_column_if_missing(conn, "naming_history", column, "TEXT")


def _migration_6_semantic_catalog(conn: sqlite3.Connection) -> None:
    """Phase 3 semantic state, embeddings, and derived-view audit tables."""
    conn.executescript(SEMANTIC_SCHEMA_SQL)


def _migration_7_semantic_metadata(conn: sqlite3.Connection) -> None:
    """Carry deterministic media facets used by search filters."""
    add_column_if_missing(conn, "semantic_classifications", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")


def _migration_8_model_analysis(conn: sqlite3.Connection) -> None:
    """Local-model proposals, preflight results, and person profiles.

    Catalogs created by the first model slice already have these tables but
    not `person_profiles.scope`, so the column is added when missing.
    """
    conn.executescript(MODEL_ANALYSIS_SQL)
    add_column_if_missing(conn, "person_profiles", "scope", "TEXT NOT NULL DEFAULT 'global'")


MIGRATIONS = (
    _migration_1_phase_1a,
    _migration_2_file_intelligence,
    _migration_3_generation,
    _migration_4_reusable_source_paths,
    _migration_5_naming_audit,
    _migration_6_semantic_catalog,
    _migration_7_semantic_metadata,
    _migration_8_model_analysis,
)


def now_iso() -> str:
    # Preserve sub-second ordering for filesystem safety checks. File mtimes and
    # ctimes commonly carry finer-than-second precision; truncating observations
    # to whole seconds makes an unchanged file touched earlier in the same
    # second look newer than the ledger record on the next scan.
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


@dataclass
class Asset:
    row: dict[str, Any]

    def __getattr__(self, item: str) -> Any:
        try:
            return self.row[item]
        except KeyError as exc:
            raise AttributeError(item) from exc


class Catalog(LedgerTables):
    """One connection per `organize` invocation. Single writer, WAL mode.

    Durable truth about assets that are actually on Angel. Files that have not
    been ingested are tracked in the Mac-side `LocalLedger` instead -- see
    `ledger.py` for why the two are separate databases.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect(db_path)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.schema_version = apply_migrations(self._conn, MIGRATIONS)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- volumes --------------------------------------------------------

    def upsert_volume(self, volume_id: str, *, label: str = "", mount_point: str = "", fs: str = "") -> None:
        now = now_iso()
        self._conn.execute(
            """
            INSERT INTO volumes(volume_id, label, mount_point, fs, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(volume_id) DO UPDATE SET
                label=excluded.label, mount_point=excluded.mount_point,
                fs=excluded.fs, last_seen=excluded.last_seen
            """,
            (volume_id, label, mount_point, fs, now, now),
        )

    # -- blobs ------------------------------------------------------------

    def upsert_blob(self, sha256: str, *, size: int, mime: str | None) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO blobs(sha256, size, mime, first_seen) VALUES (?, ?, ?, ?)",
            (sha256, size, mime, now_iso()),
        )

    # -- assets -----------------------------------------------------------

    def get_asset(self, asset_id: str) -> Asset | None:
        row = self._conn.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        return Asset(dict(row)) if row is not None else None

    def assets(self, *, transfer_state: str | None = None) -> list[Asset]:
        if transfer_state is None:
            rows = self._conn.execute("SELECT * FROM assets ORDER BY asset_id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM assets WHERE transfer_state = ? ORDER BY asset_id", (transfer_state,)
            ).fetchall()
        return [Asset(dict(row)) for row in rows]

    def get_asset_for_source_content(
        self, source_volume_id: str, original_path: str, sha256: str
    ) -> Asset | None:
        """Return the occurrence row for these exact bytes at this source path."""
        row = self._conn.execute(
            """
            SELECT * FROM assets
            WHERE source_volume_id = ? AND original_path = ? AND sha256 = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (source_volume_id, original_path, sha256),
        ).fetchone()
        return Asset(dict(row)) if row is not None else None

    def get_latest_asset_for_source(
        self, source_volume_id: str, original_path: str
    ) -> Asset | None:
        """Most recently recorded generation observed at a source path.

        Used only when the source is absent and therefore cannot be hashed.
        When bytes are present callers must use `get_asset_for_source_content`
        so a reused path can never impersonate an earlier file.
        """
        row = self._conn.execute(
            """
            SELECT * FROM assets
            WHERE source_volume_id = ? AND original_path = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (source_volume_id, original_path),
        ).fetchone()
        return Asset(dict(row)) if row is not None else None

    def get_asset_id_by_final_path(self, final_path: str) -> str | None:
        """Which asset (if any) currently claims this exact destination path.

        Used to tell "this path is already mine, safe to resume" apart from
        "an unrelated asset (possibly from a different source volume) already
        landed here" -- content equality alone isn't enough to distinguish
        those, since two different sources can legitimately contain
        byte-identical files at the same relative path.
        """
        row = self._conn.execute("SELECT asset_id FROM assets WHERE final_path = ?", (final_path,)).fetchone()
        return row["asset_id"] if row is not None else None

    def get_asset_by_final_path(self, final_path: str) -> Asset | None:
        row = self._conn.execute("SELECT * FROM assets WHERE final_path = ?", (final_path,)).fetchone()
        return Asset(dict(row)) if row is not None else None

    def find_assets_by_sha256(self, sha256: str, *, exclude_asset_id: str | None = None) -> list[Asset]:
        query = "SELECT * FROM assets WHERE sha256 = ?"
        values: list[Any] = [sha256]
        if exclude_asset_id is not None:
            query += " AND asset_id != ?"
            values.append(exclude_asset_id)
        rows = self._conn.execute(query, values).fetchall()
        return [Asset(dict(row)) for row in rows]

    def ensure_pending_asset(
        self,
        asset_id: str,
        *,
        source_volume_id: str,
        source_class: str,
        original_path: str,
        original_filename: str,
        size: int,
    ) -> Asset:
        """Idempotent: return the existing row if present, else insert a fresh `pending` one."""
        existing = self.get_asset(asset_id)
        if existing is not None:
            return existing
        now = now_iso()
        self._conn.execute(
            """
            INSERT INTO assets(
                asset_id, source_volume_id, source_class, original_path, original_filename,
                size, detected_at, transfer_state, human_reviewed, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (asset_id, source_volume_id, source_class, original_path, original_filename, size, now, STATE_PENDING, now, now),
        )
        return self.get_asset(asset_id)  # type: ignore[return-value]

    def upsert_derivative_asset(
        self,
        asset_id: str,
        *,
        parent_asset_id: str,
        source_volume_id: str,
        source_class: str,
        sha256: str,
        size: int,
        mime: str | None,
        final_path: Path,
    ) -> None:
        """Register a generated derivative (e.g. the HEIC->JPEG output) as its
        own asset row: its own file/hash/path, linked to the parent original.
        There is no external source to unlink, so it lands directly in the
        `committed` terminal state.
        """
        now = now_iso()
        self._conn.execute(
            """
            INSERT INTO assets(
                asset_id, sha256, source_volume_id, source_class, original_path, original_filename,
                size, mime, detected_at, transfer_started_at, transferred_at, verified_at, completed_at,
                transfer_state, final_path, final_filename, derived_from, human_reviewed, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                sha256=excluded.sha256, size=excluded.size, mime=excluded.mime,
                transferred_at=excluded.transferred_at, verified_at=excluded.verified_at,
                completed_at=excluded.completed_at, transfer_state=excluded.transfer_state,
                final_path=excluded.final_path, final_filename=excluded.final_filename,
                updated_at=excluded.updated_at
            """,
            (
                asset_id, sha256, source_volume_id, source_class, str(final_path), final_path.name,
                size, mime, now, now, now, now, now,
                STATE_COMMITTED, str(final_path), final_path.name, parent_asset_id, now, now,
            ),
        )

    def update_asset(self, asset_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [asset_id]
        self._conn.execute(f"UPDATE assets SET {columns} WHERE asset_id = ?", values)

    # -- Phase 3 semantic catalog ------------------------------------------

    def record_semantic_classification(self, asset_id: str, proposal: dict[str, Any]) -> str:
        """Append a validated semantic proposal; never replace prior truth."""
        asset = self.get_asset(asset_id)
        if asset is None:
            raise KeyError(f"unknown asset: {asset_id}")
        count = self._conn.execute(
            "SELECT COUNT(*) FROM semantic_classifications WHERE asset_id = ?", (asset_id,)
        ).fetchone()[0]
        human_locked = bool(self.semantic_corrections(asset_id))
        classification_id = semantic_classification_id_for(asset_id, count)
        self._conn.execute(
            """
            INSERT INTO semantic_classifications(
                classification_id, asset_id, sha256, client_id, client_confidence,
                project_id, project_confidence, subject, tags, tag_confidences,
                metadata_json,
                model_id, model_digest, prompt_id, prompt_version, prompt_sha256,
                evidence_sha256, validator, raw_output, human_review_state,
                human_corrected, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                classification_id, asset_id, asset.row["sha256"] or "",
                proposal.get("client_id"), float(proposal.get("client_confidence", 0.0)),
                proposal.get("project_id"), float(proposal.get("project_confidence", 0.0)),
                proposal.get("subject"), json.dumps(sorted(proposal.get("tags", []))),
                json.dumps(proposal.get("tag_confidences", {}), sort_keys=True),
                json.dumps(proposal.get("metadata", {}), sort_keys=True),
                str(proposal.get("model_id", "unknown")), proposal.get("model_digest"),
                str(proposal.get("prompt_id", "")), str(proposal.get("prompt_version", "")),
                str(proposal.get("prompt_sha256", "")), str(proposal.get("evidence_sha256", "")),
                str(proposal.get("validator", "")), str(proposal.get("raw_output", ""))[:20_000],
                str(proposal.get("human_review_state", "review_required")), int(human_locked), now_iso(),
            ),
        )
        return classification_id

    @staticmethod
    def _decode_semantic(row: dict[str, Any]) -> dict[str, Any]:
        row["tags"] = tuple(json.loads(row.get("tags") or "[]"))
        row["tag_confidences"] = json.loads(row.get("tag_confidences") or "{}")
        row["metadata"] = json.loads(row.get("metadata_json") or "{}")
        return row

    def semantic_history(self, asset_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM semantic_classifications WHERE asset_id = ? ORDER BY rowid", (asset_id,)
        ).fetchall()
        return [self._decode_semantic(dict(row)) for row in rows]

    def semantic_corrections(self, asset_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM semantic_corrections WHERE asset_id = ? ORDER BY rowid", (asset_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["new_value"] = json.loads(item["new_value"])
            except (TypeError, json.JSONDecodeError):
                pass
            try:
                item["old_value"] = json.loads(item["old_value"]) if item["old_value"] is not None else None
            except (TypeError, json.JSONDecodeError):
                pass
            result.append(item)
        return result

    def latest_semantic_classification(self, asset_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM semantic_classifications WHERE asset_id = ? ORDER BY rowid DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        if row is None:
            return None
        result = self._decode_semantic(dict(row))
        corrections = {}
        for correction in self.semantic_corrections(asset_id):
            corrections[correction["field"]] = correction["new_value"]
        for field, value in corrections.items():
            result[field] = value
        result["human_corrections"] = corrections
        result["human_corrected"] = bool(corrections)
        return result

    def record_semantic_correction(
        self, asset_id: str, *, field: str, value: Any, corrected_by: str,
        reason: str = "", created_at: str | None = None,
    ) -> str:
        allowed = {"client_id", "project_id", "subject", "tags", "placement"}
        if field not in allowed:
            raise ValueError(f"semantic corrections cannot lock field {field!r}")
        if self.get_asset(asset_id) is None:
            raise KeyError(f"unknown asset: {asset_id}")
        if not corrected_by.strip():
            raise ValueError("corrected_by is required")
        current = self.latest_semantic_classification(asset_id)
        old_value = None if current is None else current.get(field)
        if field == "tags":
            if not isinstance(value, (list, tuple, set)):
                raise ValueError("tags correction must be a sequence")
            value = tuple(sorted({str(tag).strip().lower().replace(" ", "-").replace("_", "-") for tag in value}))
        count = self._conn.execute(
            "SELECT COUNT(*) FROM semantic_corrections WHERE asset_id = ?", (asset_id,)
        ).fetchone()[0]
        correction_id = semantic_correction_id_for(asset_id, field, count)
        self._conn.execute(
            """
            INSERT INTO semantic_corrections(
                correction_id, asset_id, field, old_value, new_value,
                corrected_by, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                correction_id, asset_id, field,
                json.dumps(old_value, sort_keys=True, default=list) if old_value is not None else None,
                json.dumps(value, sort_keys=True, default=list), corrected_by, reason,
                created_at or now_iso(),
            ),
        )
        return correction_id

    def get_embedding(
        self, asset_id: str, *, input_sha256: str, model_id: str, model_version: str,
        schema_version: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM embeddings
            WHERE asset_id = ? AND input_sha256 = ? AND model_id = ?
              AND model_version = ? AND schema_version = ?
            """,
            (asset_id, input_sha256, model_id, model_version, schema_version),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["vector"] = tuple(json.loads(result.pop("vector_json")))
        return result

    def record_embedding(
        self, asset_id: str, *, content_sha256: str, input_sha256: str, model_id: str,
        model_version: str, model_digest: str | None, schema_version: str,
        vector: tuple[float, ...],
    ) -> str:
        embedding_id = embedding_id_for(asset_id, input_sha256, model_id, model_version, schema_version)
        self._conn.execute(
            """
            INSERT OR IGNORE INTO embeddings(
                embedding_id, asset_id, content_sha256, input_sha256, model_id,
                model_version, model_digest, schema_version, dimensions,
                vector_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                embedding_id, asset_id, content_sha256, input_sha256, model_id,
                model_version, model_digest, schema_version, len(vector),
                json.dumps(list(vector), separators=(",", ":")), now_iso(),
            ),
        )
        return embedding_id

    def embedding_rows(self, *, model_id: str, model_version: str, schema_version: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT e.*, a.final_path, a.final_filename, a.mime, a.size, a.sha256
            FROM embeddings e JOIN assets a ON a.asset_id = e.asset_id
            WHERE e.model_id = ? AND e.model_version = ? AND e.schema_version = ?
              AND e.rowid IN (
                SELECT MAX(rowid) FROM embeddings
                WHERE model_id = ? AND model_version = ? AND schema_version = ?
                GROUP BY asset_id
              )
            ORDER BY e.asset_id
            """,
            (model_id, model_version, schema_version, model_id, model_version, schema_version),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_view_run(self, run_id: str, *, scope: str, dry_run: bool, drift_fraction: float | None,
                        drift_aborted: bool, summary: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO organized_view_runs(
                run_id, scope, dry_run, drift_fraction, drift_aborted, summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, scope, int(dry_run), drift_fraction, int(drift_aborted),
             json.dumps(summary, sort_keys=True), now_iso()),
        )

    def record_view_proposal(self, run_id: str, *, asset_id: str, client_id: str | None,
                             project_id: str | None, relative_path: str | None,
                             tags: tuple[str, ...], action: str, reason: str,
                             estimated_bytes: int) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO organized_view_proposals(
                run_id, asset_id, client_id, project_id, relative_path, tags,
                action, reason, estimated_bytes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, asset_id, client_id, project_id, relative_path, json.dumps(list(tags)),
             action, reason, int(estimated_bytes), now_iso()),
        )

    def latest_view_proposals(self, scope: str) -> list[dict[str, Any]]:
        run = self._conn.execute(
            "SELECT run_id FROM organized_view_runs WHERE scope = ? ORDER BY rowid DESC LIMIT 1", (scope,)
        ).fetchone()
        if run is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM organized_view_proposals WHERE run_id = ? ORDER BY asset_id", (run["run_id"],)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["tags"] = tuple(json.loads(item["tags"] or "[]"))
            result.append(item)
        return result

    def record_materialization(self, *, asset_id: str, relative_path: str,
                               source_sha256: str, destination_sha256: str, bytes_count: int) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO materializations(
                asset_id, relative_path, source_sha256, destination_sha256,
                bytes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (asset_id, relative_path, source_sha256, destination_sha256, bytes_count, now_iso()),
        )


    # -- model proposals and durable profile feedback ----------------------

    def record_proposal(
        self,
        asset_id: str,
        *,
        field: str,
        value: str,
        confidence: float | None,
        model: str,
        model_version: str,
        prompt_sha: str,
        status: str = "pending",
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO proposals(
                asset_id, field, value, confidence, model, model_version,
                prompt_sha, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (asset_id, field, value, confidence, model, model_version, prompt_sha, now_iso(), status),
        )
        return int(cursor.lastrowid)

    def has_proposal(self, asset_id: str, *, field: str, value: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM proposals WHERE asset_id=? AND field=? AND value=? LIMIT 1",
            (asset_id, field, value),
        ).fetchone()
        return row is not None

    def begin_analysis_run(
        self,
        *,
        asset_id: str,
        stage: str,
        input_sha256: str,
        model: str,
        model_version: str,
        prompt_sha: str,
    ) -> bool:
        now = now_iso()
        cursor = self._conn.execute(
            """
            INSERT INTO analysis_runs(
                asset_id, stage, input_sha256, model, model_version, prompt_sha,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
            ON CONFLICT(asset_id, stage, input_sha256, model, model_version, prompt_sha)
            DO UPDATE SET status='running', updated_at=excluded.updated_at
            WHERE analysis_runs.status != 'succeeded'
            """,
            (asset_id, stage, input_sha256, model, model_version, prompt_sha, now, now),
        )
        if cursor.rowcount:
            return True
        row = self._conn.execute(
            """
            SELECT status FROM analysis_runs
            WHERE asset_id=? AND stage=? AND input_sha256=? AND model=?
              AND model_version=? AND prompt_sha=?
            """,
            (asset_id, stage, input_sha256, model, model_version, prompt_sha),
        ).fetchone()
        return row is None or row["status"] != "succeeded"

    def finish_analysis_run(
        self,
        *,
        asset_id: str,
        stage: str,
        input_sha256: str,
        model: str,
        model_version: str,
        prompt_sha: str,
        status: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE analysis_runs SET status=?, updated_at=?
            WHERE asset_id=? AND stage=? AND input_sha256=? AND model=?
              AND model_version=? AND prompt_sha=?
            """,
            (status, now_iso(), asset_id, stage, input_sha256, model, model_version, prompt_sha),
        )

    def record_preflight_result(
        self,
        *,
        source_volume_id: str,
        source_path: str,
        relative_path: str,
        source_size: int,
        source_mtime_ns: int,
        model: str,
        model_version: str,
        prompt_sha: str | None,
        status: str,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        """Persist one source review result so preflight survives UI/process loss."""
        now = now_iso()
        self._conn.execute(
            """
            INSERT INTO preflight_results(
                source_volume_id, source_path, relative_path, source_size,
                source_mtime_ns, model, model_version, prompt_sha, status,
                result_json, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                source_volume_id, relative_path, source_size, source_mtime_ns,
                model, model_version
            ) DO UPDATE SET
                source_path=excluded.source_path,
                prompt_sha=excluded.prompt_sha,
                status=excluded.status,
                result_json=excluded.result_json,
                error_message=excluded.error_message,
                updated_at=excluded.updated_at
            """,
            (
                source_volume_id,
                source_path,
                relative_path,
                source_size,
                source_mtime_ns,
                model,
                model_version,
                prompt_sha,
                status,
                json.dumps(result, sort_keys=True) if result is not None else None,
                error_message,
                now,
                now,
            ),
        )

    def get_preflight_result(
        self,
        *,
        source_volume_id: str,
        relative_path: str,
        source_size: int,
        source_mtime_ns: int,
        model: str,
        model_version: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM preflight_results
            WHERE source_volume_id=? AND relative_path=? AND source_size=?
              AND source_mtime_ns=? AND model=? AND model_version=?
            """,
            (source_volume_id, relative_path, source_size, source_mtime_ns, model, model_version),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
        return result

    def upsert_person_profile(
        self,
        profile_id: str,
        *,
        display_name: str,
        scope: str = "global",
        aliases: list[str] | None = None,
        notes: str = "",
    ) -> None:
        now = now_iso()
        aliases_json = json.dumps(sorted({alias.strip() for alias in (aliases or []) if alias.strip()}))
        self._conn.execute(
            """
            INSERT INTO person_profiles(profile_id, scope, display_name, aliases_json, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                scope=excluded.scope,
                display_name=excluded.display_name,
                aliases_json=excluded.aliases_json,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (profile_id, scope.strip() or "global", display_name.strip(), aliases_json, notes.strip(), now, now),
        )

    def list_person_profiles(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        if scope is None:
            rows = self._conn.execute("SELECT * FROM person_profiles ORDER BY display_name COLLATE NOCASE").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM person_profiles WHERE scope = ? ORDER BY display_name COLLATE NOCASE", (scope,)
            ).fetchall()
        profiles: list[dict[str, Any]] = []
        for row in rows:
            profile = dict(row)
            profile["aliases"] = json.loads(profile.pop("aliases_json") or "[]")
            profiles.append(profile)
        return profiles

    def record_person_observation(
        self,
        asset_id: str,
        *,
        profile_id: str,
        confidence: float | None,
        source: str,
        evidence: str = "",
        model: str | None = None,
        model_version: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO person_observations(
                asset_id, profile_id, confidence, source, evidence,
                model, model_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, profile_id) DO UPDATE SET
                confidence=excluded.confidence,
                source=excluded.source,
                evidence=excluded.evidence,
                model=excluded.model,
                model_version=excluded.model_version,
                created_at=excluded.created_at
            WHERE person_observations.source != 'human' OR excluded.source = 'human'
            """,
            (asset_id, profile_id, confidence, source, evidence, model, model_version, now_iso()),
        )

    def record_correction(self, asset_id: str, *, field: str, value: str, by: str = "human") -> None:
        self._conn.execute(
            "INSERT INTO correction_events(asset_id, field, value, by, created_at) VALUES (?, ?, ?, ?, ?)",
            (asset_id, field, value, by, now_iso()),
        )
        self._conn.execute(
            """
            INSERT INTO corrections(asset_id, field, value, by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, field) DO UPDATE SET
                value=excluded.value, by=excluded.by, created_at=excluded.created_at
            """,
            (asset_id, field, value, by, now_iso()),
        )
        self.update_asset(asset_id, human_reviewed=1)

    def profile_context(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        """Return compact, human-maintained profile memory for model prompts."""
        profiles = self.list_person_profiles(scope=scope)
        for profile in profiles:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count, MAX(created_at) AS last_seen
                FROM person_observations WHERE profile_id = ?
                """,
                (profile["profile_id"],),
            ).fetchone()
            profile["observation_count"] = int(row["count"] or 0)
            profile["last_seen"] = row["last_seen"]
            examples = self._conn.execute(
                """
                SELECT o.asset_id, a.original_filename, o.evidence
                FROM person_observations o
                LEFT JOIN assets a ON a.asset_id = o.asset_id
                WHERE o.profile_id = ? AND o.source = 'human'
                ORDER BY o.created_at DESC LIMIT 12
                """,
                (profile["profile_id"],),
            ).fetchall()
            profile["confirmed_examples"] = [dict(example) for example in examples]
        return profiles

    def begin_immediate(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    # -- derivatives --------------------------------------------------------

    def get_derivative(self, parent_asset_id: str, kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM derivatives WHERE parent_asset_id = ? AND kind = ?", (parent_asset_id, kind)
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_derivative(
        self,
        child_asset_id: str | None,
        *,
        parent_asset_id: str,
        kind: str,
        converter: str,
        converter_version: str,
        quality: int | None,
        status: str,
        error_message: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO derivatives(
                child_asset_id, parent_asset_id, kind, converter, converter_version,
                quality, status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(parent_asset_id, kind) DO UPDATE SET
                child_asset_id=excluded.child_asset_id, converter=excluded.converter,
                converter_version=excluded.converter_version, quality=excluded.quality,
                status=excluded.status, error_message=excluded.error_message,
                created_at=excluded.created_at
            """,
            (child_asset_id, parent_asset_id, kind, converter, converter_version, quality, status, error_message, now_iso()),
        )

    # -- repos --------------------------------------------------------------

    def record_repo(
        self,
        repo_id: str,
        *,
        source_volume_id: str,
        original_path: str,
        remote_url: str | None,
        head: str | None,
        branch: str | None,
        dirty: bool | None,
        unpushed: bool | None,
        stash: bool | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO repos(
                repo_id, source_volume_id, original_path, remote_url, head, branch,
                dirty, unpushed, stash, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id) DO UPDATE SET
                remote_url=excluded.remote_url, head=excluded.head, branch=excluded.branch,
                dirty=excluded.dirty, unpushed=excluded.unpushed, stash=excluded.stash,
                detected_at=excluded.detected_at
            """,
            (
                repo_id,
                source_volume_id,
                original_path,
                remote_url,
                head,
                branch,
                int(bool(dirty)) if dirty is not None else None,
                int(bool(unpushed)) if unpushed is not None else None,
                int(bool(stash)) if stash is not None else None,
                now_iso(),
            ),
        )

    # -- sidecars -------------------------------------------------------------

    def write_sidecar(self, asset_id: str, sidecars_dir: Path) -> Path:
        """Regenerate the durable JSON sidecar for one asset from its catalog row."""
        asset = self.get_asset(asset_id)
        if asset is None:
            raise KeyError(asset_id)
        sidecars_dir.mkdir(parents=True, exist_ok=True)
        derivative = self._conn.execute(
            "SELECT * FROM derivatives WHERE parent_asset_id = ?", (asset_id,)
        ).fetchall()
        corrections = self._conn.execute(
            "SELECT field, value, by, created_at FROM corrections WHERE asset_id = ? ORDER BY field",
            (asset_id,),
        ).fetchall()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "asset_id": asset.row["asset_id"],
            "sha256": asset.row["sha256"],
            "source_volume_id": asset.row["source_volume_id"],
            "source_class": asset.row["source_class"],
            "original_path": asset.row["original_path"],
            "original_filename": asset.row["original_filename"],
            "size": asset.row["size"],
            "mime": asset.row["mime"],
            "detected_at": asset.row["detected_at"],
            "transfer_started_at": asset.row["transfer_started_at"],
            "transferred_at": asset.row["transferred_at"],
            "verified_at": asset.row["verified_at"],
            "completed_at": asset.row["completed_at"],
            "transfer_state": asset.row["transfer_state"],
            "final_path": asset.row["final_path"],
            "final_filename": asset.row["final_filename"],
            "error_state": asset.row["error_state"],
            "error_message": asset.row["error_message"],
            "derived_from": asset.row["derived_from"],
            "classifier": {
                "model": asset.row["classifier_model"],
                "model_version": asset.row["classifier_model_version"],
                "confidence": asset.row["classifier_confidence"],
            },
            "human_reviewed": bool(asset.row["human_reviewed"]),
            "corrections": [dict(row) for row in corrections],
            "derivatives": [dict(row) for row in derivative],
            # Provenance that survives a pre-ingestion rename. `original_filename`
            # is the name at the moment of transfer; `first_seen_filename` and the
            # lineage below are how the owner finds the file they remember.
            "first_seen_filename": asset.row["first_seen_filename"] or asset.row["original_filename"],
            "local_file_id": asset.row["local_file_id"],
            "naming_history": self.naming_history_for_asset(asset_id),
            "retention": {
                "recommendation": asset.row["retention_recommendation"],
                "run_id": asset.row["retention_run_id"],
            },
            "media_metadata": self.get_media_metadata(asset_id, "asset"),
            "semantic": {
                "current": self.latest_semantic_classification(asset_id),
                "history": self.semantic_history(asset_id),
                "corrections": self.semantic_corrections(asset_id),
            },
        }
        # Atomic, like the status file: a sidecar is the durable copy of a
        # catalog row, and a process killed mid-write must not be able to
        # leave a truncated one where a complete one used to be. The primitive
        # lives in `automation` so that filesystem authority stays in the
        # modules that already hold it (see tests/ingest/test_mutation_authority).
        return write_json_atomically(sidecars_dir / f"{asset_id}.json", payload)
