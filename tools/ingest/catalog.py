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
from tools.ingest.migrations import add_column_if_missing, apply_migrations
from tools.ingest.schema_sql import (
    CLASSIFICATION_RUNS_SQL,
    DUPLICATE_OBSERVATIONS_SQL,
    MEDIA_METADATA_SQL,
    NAMING_HISTORY_SQL,
)

# Bumped to 2 when naming lineage, classification runs, media facts, and
# duplicate observations were added. Sidecars carry this so an old sidecar is
# still identifiable as having been written by the pre-naming pipeline.
SCHEMA_VERSION = 2

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
  updated_at TEXT NOT NULL,
  UNIQUE(source_volume_id, original_path)
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


MIGRATIONS = (_migration_1_phase_1a, _migration_2_file_intelligence, _migration_3_generation)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


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
        self._conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
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
        }
        # Atomic, like the status file: a sidecar is the durable copy of a
        # catalog row, and a process killed mid-write must not be able to
        # leave a truncated one where a complete one used to be. The primitive
        # lives in `automation` so that filesystem authority stays in the
        # modules that already hold it (see tests/ingest/test_mutation_authority).
        return write_json_atomically(sidecars_dir / f"{asset_id}.json", payload)
