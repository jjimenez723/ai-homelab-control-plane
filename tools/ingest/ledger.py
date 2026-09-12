"""Append-only naming/classification bookkeeping, and the Mac-side ledger.

`LedgerTables` is mixed into both databases so a file's record has the same
shape before and after ingestion. `LocalLedger` is the Mac-side database that
tracks files which have *not* been ingested.

Why a second database at all: naming is not retention (D-021). A screenshot
that will never enter Angel still deserves a useful filename, and that has to
keep working with the T7 unplugged. Putting the naming ledger on the T7 would
have quietly made every naming decision depend on the drive being connected.

The local ledger is rebuildable state, not truth. Losing it costs re-derivable
bookkeeping (and, at worst, one redundant re-evaluation of a file's name); it
never costs a file. Durable truth about ingested assets stays in the T7
catalog, which is what restic backs up.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.ingest.migrations import apply_migrations
from tools.ingest.schema_sql import (
    CLASSIFICATION_RUNS_SQL,
    DUPLICATE_OBSERVATIONS_SQL,
    MEDIA_METADATA_SQL,
    NAMING_HISTORY_SQL,
)
from tools.ingest.identity import (
    classification_run_id_for,
    duplicate_observation_id_for,
    local_file_id_for,
    naming_id_for,
)

# Naming lineage decisions. Only `renamed` ever corresponds to a filesystem
# mutation; every other value records a deliberate decision *not* to touch the
# file, which is exactly what makes repeat runs cheap and auditable.
DECISION_KEPT = "kept"  # existing name is already useful
DECISION_RENAMED = "renamed"
DECISION_PROPOSED = "proposed"  # dry run: a rename would have happened
DECISION_NO_PROPOSAL = "no_proposal"
DECISION_LOW_CONFIDENCE = "low_confidence"
DECISION_REJECTED_POLICY = "rejected_policy"
DECISION_HUMAN_LOCKED = "skipped_human_corrected"
DECISION_ALREADY_NAMED = "already_named"
DECISION_RENAME_FAILED = "rename_failed"

NAMING_STATE_UNEVALUATED = "unevaluated"
NAMING_STATE_KEPT = "kept"
NAMING_STATE_NAMED = "named"
NAMING_STATE_REVIEW = "needs_review"
NAMING_STATE_HUMAN = "human_named"

RETENTION_IGNORE = "ignore"
RETENTION_REVIEW = "review"
RETENTION_INGEST = "ingest"
RETENTION_STATES = (RETENTION_IGNORE, RETENTION_REVIEW, RETENTION_INGEST)


class LedgerTables:
    """Shared accessors for the append-only tables. Expects `self._conn`."""

    _conn: sqlite3.Connection

    # -- naming lineage -----------------------------------------------------

    def naming_history(self, local_file_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM naming_history WHERE local_file_id = ? ORDER BY created_at, rowid",
            (local_file_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def naming_history_for_asset(self, asset_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM naming_history WHERE asset_id = ? ORDER BY created_at, rowid",
            (asset_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def append_naming(self, record: dict[str, Any]) -> str:
        """Append one lineage row. Never updates an earlier one.

        The row id is derived from (local_file_id, sequence), so re-running the
        exact same scan over an unchanged tree reuses ids instead of growing the
        table forever -- `INSERT OR REPLACE` on an identical id is the only
        case where a row is rewritten, and then only by a re-run of the same
        step.
        """
        sequence = self._conn.execute(
            "SELECT COUNT(*) FROM naming_history WHERE local_file_id = ?", (record["local_file_id"],)
        ).fetchone()[0]
        naming_id = naming_id_for(record["local_file_id"], sequence)
        columns = (
            "naming_id", "local_file_id", "asset_id", "source_volume_id", "source_class",
            "directory", "sha256", "original_filename", "proposed_filename", "applied_filename",
            "name_quality", "decision", "reason", "model", "model_version", "confidence",
            "approved_by", "human_corrected", "policy_outcome", "created_at",
        )
        values = [naming_id] + [record.get(column) for column in columns[1:]]
        placeholders = ", ".join("?" for _ in columns)
        self._conn.execute(
            f"INSERT OR REPLACE INTO naming_history({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        return naming_id

    def link_naming_to_asset(self, local_file_id: str, asset_id: str) -> None:
        """Attach an asset_id to an existing lineage once the file is ingested.

        This is the one permitted mutation of lineage rows: it adds the Angel
        identity to history that was recorded before Angel had ever seen the
        file. No naming decision, name, model, or timestamp is altered.
        """
        self._conn.execute(
            "UPDATE naming_history SET asset_id = ? WHERE local_file_id = ? AND asset_id IS NULL",
            (asset_id, local_file_id),
        )

    def has_human_naming(self, local_file_id: str) -> bool:
        """True once a person has named or corrected this file (invariant 12)."""
        row = self._conn.execute(
            """
            SELECT 1 FROM naming_history
            WHERE local_file_id = ? AND (human_corrected = 1 OR approved_by = 'human')
            LIMIT 1
            """,
            (local_file_id,),
        ).fetchone()
        return row is not None

    def known_names(self, local_file_id: str) -> set[str]:
        """Every filename this system has already settled on for this file.

        Used for idempotency: if the name on disk is one we previously applied
        (or deliberately kept), there is nothing left to decide.
        """
        rows = self._conn.execute(
            """
            SELECT applied_filename, original_filename, decision FROM naming_history
            WHERE local_file_id = ?
            """,
            (local_file_id,),
        ).fetchall()
        names: set[str] = set()
        for row in rows:
            if row["applied_filename"]:
                names.add(row["applied_filename"])
            if row["decision"] in (DECISION_KEPT, DECISION_HUMAN_LOCKED):
                names.add(row["original_filename"])
        return names

    def evaluated_by(self, local_file_id: str, filename: str, model_version: str) -> bool:
        """Whether this exact (file, name, proposer version) was already judged.

        A proposal that was declined, rejected, or scored too low is recorded so
        the next run does not pay for it again -- while a proposer *upgrade*
        (new model_version) deliberately re-opens the question.
        """
        row = self._conn.execute(
            """
            SELECT 1 FROM naming_history
            WHERE local_file_id = ? AND original_filename = ?
              AND COALESCE(model_version, '') = ?
              AND decision IN (?, ?, ?, ?)
            LIMIT 1
            """,
            (
                local_file_id, filename, model_version,
                DECISION_NO_PROPOSAL, DECISION_LOW_CONFIDENCE,
                DECISION_REJECTED_POLICY, DECISION_KEPT,
            ),
        ).fetchone()
        return row is not None

    # -- classification / retention ----------------------------------------

    def latest_classification(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM classification_runs
            WHERE COALESCE(local_file_id, asset_id, '') = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (key,),
        ).fetchone()
        return dict(row) if row is not None else None

    def append_classification(self, record: dict[str, Any]) -> str:
        """Append a classification run, unless it would restate the last one.

        History is the point of this table, but an hourly agent re-deciding the
        same file the same way is not history -- it is a row per file per run,
        for as long as the file sits in Downloads. Appending only when the
        verdict actually changes keeps the ledger a record of decisions rather
        than of scans, and keeps the sequence scan below bounded.
        """
        key = record.get("local_file_id") or record.get("asset_id") or ""
        previous = self.latest_classification(key)
        if previous is not None and all(
            previous.get(field) == record.get(field)
            for field in ("recommendation", "reason", "sha256", "policy_outcome", "human_corrected")
        ):
            return previous["run_id"]
        sequence = self._conn.execute(
            "SELECT COUNT(*) FROM classification_runs WHERE COALESCE(local_file_id, asset_id, '') = ?",
            (key,),
        ).fetchone()[0]
        run_id = classification_run_id_for(key, sequence)
        columns = (
            "run_id", "local_file_id", "asset_id", "source_volume_id", "source_class", "sha256",
            "recommendation", "confidence", "reason", "client_proposal", "project_proposal",
            "tags", "content_description", "canonical_filename_proposal", "destination_proposal",
            "model", "model_version", "policy_outcome", "human_state", "human_corrected", "created_at",
        )
        values = [run_id] + [record.get(column) for column in columns[1:]]
        placeholders = ", ".join("?" for _ in columns)
        self._conn.execute(
            f"INSERT OR REPLACE INTO classification_runs({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        return run_id

    def classification_runs(self, local_file_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM classification_runs WHERE local_file_id = ? ORDER BY created_at, rowid",
            (local_file_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def link_classification_to_asset(self, local_file_id: str, asset_id: str) -> None:
        self._conn.execute(
            "UPDATE classification_runs SET asset_id = ? WHERE local_file_id = ? AND asset_id IS NULL",
            (asset_id, local_file_id),
        )

    def has_human_classification(self, local_file_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM classification_runs WHERE local_file_id = ? AND human_corrected = 1 LIMIT 1",
            (local_file_id,),
        ).fetchone()
        return row is not None

    # -- deterministic media facts -----------------------------------------

    def record_media_metadata(
        self,
        subject_id: str,
        *,
        subject_kind: str,
        media_kind: str,
        mime: str | None,
        probe: str,
        probe_version: str,
        facts: dict[str, Any],
        created_at: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO media_metadata(
                subject_id, subject_kind, media_kind, mime, probe, probe_version, facts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_id, subject_kind) DO UPDATE SET
                media_kind=excluded.media_kind, mime=excluded.mime, probe=excluded.probe,
                probe_version=excluded.probe_version, facts=excluded.facts,
                created_at=excluded.created_at
            """,
            (subject_id, subject_kind, media_kind, mime, probe, probe_version, json.dumps(facts, sort_keys=True), created_at),
        )

    def get_media_metadata(self, subject_id: str, subject_kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM media_metadata WHERE subject_id = ? AND subject_kind = ?",
            (subject_id, subject_kind),
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["facts"] = json.loads(record["facts"])
        return record

    # -- duplicates (observed, never acted on) ------------------------------

    def record_duplicate(
        self,
        *,
        kind: str,
        left_ref: str,
        left_scope: str,
        right_ref: str,
        right_scope: str,
        detail: str | None,
        created_at: str,
    ) -> str:
        observation_id = duplicate_observation_id_for(kind, left_ref, right_ref)
        self._conn.execute(
            """
            INSERT INTO duplicate_observations(
                observation_id, kind, left_ref, left_scope, right_ref, right_scope, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(observation_id) DO UPDATE SET detail=excluded.detail
            """,
            (observation_id, kind, left_ref, left_scope, right_ref, right_scope, detail, created_at),
        )
        return observation_id

    def duplicate_observations(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM duplicate_observations ORDER BY kind, created_at"
        ).fetchall()
        return [dict(row) for row in rows]


LOCAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_files (
  local_file_id TEXT PRIMARY KEY,
  source_volume_id TEXT NOT NULL,
  source_class TEXT NOT NULL,
  sha256 TEXT,
  size INTEGER,
  mime TEXT,
  media_kind TEXT,
  first_seen_path TEXT NOT NULL,
  first_seen_filename TEXT NOT NULL,
  current_path TEXT NOT NULL,
  current_filename TEXT NOT NULL,
  name_quality TEXT,
  naming_state TEXT NOT NULL,
  retention_recommendation TEXT,
  retention_confidence REAL,
  retention_reason TEXT,
  retention_run_id TEXT,
  ingested_asset_id TEXT,
  generation INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS local_files_by_sha ON local_files(sha256);
CREATE INDEX IF NOT EXISTS local_files_by_class ON local_files(source_class);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  trigger TEXT NOT NULL,
  targets TEXT NOT NULL,
  apply INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  summary TEXT,
  error_message TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_started ON runs(started_at);
"""


def _local_migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript(LOCAL_SCHEMA)
    conn.executescript(NAMING_HISTORY_SQL)
    conn.executescript(CLASSIFICATION_RUNS_SQL)
    conn.executescript(MEDIA_METADATA_SQL)
    conn.executescript(DUPLICATE_OBSERVATIONS_SQL)


LOCAL_MIGRATIONS = (_local_migration_1,)


@dataclass(frozen=True)
class ObservedFile:
    """What a scan measured about a file on the Mac, before any decision."""

    local_file_id: str
    path: Path
    source_class: str
    source_volume_id: str
    size: int
    sha256: str
    mime: str | None
    media_kind: str
    is_new_generation: bool


class LocalLedger(LedgerTables):
    """The Mac-side database. Holds no authority over any file."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.schema_version = apply_migrations(self._conn, LOCAL_MIGRATIONS)
        try:
            db_path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LocalLedger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- local file state ---------------------------------------------------

    def get_local_file(self, local_file_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM local_files WHERE local_file_id = ?", (local_file_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def observe_file(
        self,
        *,
        path: Path,
        source_class: str,
        source_volume_id: str,
        device: int,
        inode: int,
        size: int,
        sha256: str,
        mime: str | None,
        media_kind: str,
        now: str,
    ) -> ObservedFile:
        """Record that a file exists, returning its stable local identity.

        Inode reuse is the one way (device, inode) can lie: the file we knew is
        deleted and an unrelated file lands on the same inode. The signature of
        that is unmistakable -- different content *and* the path we last saw is
        gone -- and it must reset the lineage, because inheriting the old
        record would let a stranger's file inherit a human naming lock.

        Different content at a path that still exists is the opposite case: the
        owner edited the file in place. That keeps its lineage and its lock.
        """
        local_file_id = local_file_id_for(source_volume_id, device, inode)
        existing = self.get_local_file(local_file_id)
        is_new_generation = False
        generation = 0

        if existing is not None:
            generation = int(existing["generation"])
            content_changed = bool(existing["sha256"]) and existing["sha256"] != sha256
            previous_path = Path(existing["current_path"])
            if content_changed and not previous_path.exists() and previous_path != path:
                is_new_generation = True
                generation += 1

        if existing is None or is_new_generation:
            self._conn.execute(
                """
                INSERT INTO local_files(
                    local_file_id, source_volume_id, source_class, sha256, size, mime, media_kind,
                    first_seen_path, first_seen_filename, current_path, current_filename,
                    naming_state, generation, first_seen_at, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_file_id) DO UPDATE SET
                    source_class=excluded.source_class, sha256=excluded.sha256, size=excluded.size,
                    mime=excluded.mime, media_kind=excluded.media_kind,
                    first_seen_path=excluded.first_seen_path,
                    first_seen_filename=excluded.first_seen_filename,
                    current_path=excluded.current_path, current_filename=excluded.current_filename,
                    name_quality=NULL, naming_state=excluded.naming_state,
                    retention_recommendation=NULL, retention_confidence=NULL,
                    retention_reason=NULL, retention_run_id=NULL, ingested_asset_id=NULL,
                    generation=excluded.generation, last_seen_at=excluded.last_seen_at,
                    updated_at=excluded.updated_at
                """,
                (
                    local_file_id, source_volume_id, source_class, sha256, size, mime, media_kind,
                    str(path), path.name, str(path), path.name,
                    NAMING_STATE_UNEVALUATED, generation, now, now, now,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE local_files SET
                    source_class = ?, sha256 = ?, size = ?, mime = ?, media_kind = ?,
                    current_path = ?, current_filename = ?, last_seen_at = ?, updated_at = ?
                WHERE local_file_id = ?
                """,
                (source_class, sha256, size, mime, media_kind, str(path), path.name, now, now, local_file_id),
            )

        return ObservedFile(
            local_file_id=local_file_id,
            path=path,
            source_class=source_class,
            source_volume_id=source_volume_id,
            size=size,
            sha256=sha256,
            mime=mime,
            media_kind=media_kind,
            is_new_generation=is_new_generation,
        )

    def update_local_file(self, local_file_id: str, *, now: str, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        fields["updated_at"] = now
        columns = ", ".join(f"{key} = ?" for key in fields)
        self._conn.execute(
            f"UPDATE local_files SET {columns} WHERE local_file_id = ?",
            list(fields.values()) + [local_file_id],
        )

    def local_files(self, source_class: str | None = None) -> list[dict[str, Any]]:
        if source_class is None:
            rows = self._conn.execute("SELECT * FROM local_files ORDER BY current_path").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM local_files WHERE source_class = ? ORDER BY current_path",
                (source_class,),
            ).fetchall()
        return [dict(row) for row in rows]

    def local_files_with_sha(self, sha256: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM local_files WHERE sha256 = ? ORDER BY first_seen_at", (sha256,)
        ).fetchall()
        return [dict(row) for row in rows]

    def record_human_name(self, local_file_id: str, *, filename: str, directory: str, now: str, note: str = "") -> None:
        """Record an owner-authored name. Nothing automatic overrides this."""
        record = self.get_local_file(local_file_id) or {}
        self.append_naming(
            {
                "local_file_id": local_file_id,
                "asset_id": record.get("ingested_asset_id"),
                "source_volume_id": record.get("source_volume_id", ""),
                "source_class": record.get("source_class", ""),
                "directory": directory,
                "sha256": record.get("sha256"),
                "original_filename": record.get("current_filename", filename),
                "proposed_filename": None,
                "applied_filename": filename,
                "name_quality": "human",
                "decision": DECISION_RENAMED,
                "reason": note or "owner-authored name",
                "approved_by": "human",
                "human_corrected": 1,
                "policy_outcome": "human_authority",
                "created_at": now,
            }
        )
        self.update_local_file(
            local_file_id,
            now=now,
            current_filename=filename,
            current_path=str(Path(directory) / filename),
            naming_state=NAMING_STATE_HUMAN,
        )

    # -- run records --------------------------------------------------------

    def start_run(self, run_id: str, *, kind: str, trigger: str, targets: str, apply: bool, now: str) -> None:
        self._conn.execute(
            """
            INSERT INTO runs(run_id, kind, trigger, targets, apply, started_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'running')
            ON CONFLICT(run_id) DO UPDATE SET
                started_at=excluded.started_at, status='running', finished_at=NULL, error_message=NULL
            """,
            (run_id, kind, trigger, targets, int(apply), now),
        )

    def finish_run(self, run_id: str, *, status: str, summary: dict[str, Any] | None, now: str, error_message: str | None = None) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, summary = ?, error_message = ? WHERE run_id = ?",
            (now, status, json.dumps(summary or {}, sort_keys=True), error_message, run_id),
        )

    def last_run(self, kind: str | None = None) -> dict[str, Any] | None:
        if kind is None:
            row = self._conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE kind = ? ORDER BY started_at DESC LIMIT 1", (kind,)
            ).fetchone()
        return dict(row) if row is not None else None

    def last_failed_run(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE status NOT IN ('ok', 'running') ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None
