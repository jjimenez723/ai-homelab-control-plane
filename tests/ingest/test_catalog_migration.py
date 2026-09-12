"""Schema migrations must never cost an existing catalog anything.

The T7 catalog is the durable record of what was ingested and where it came
from. A migration that dropped a row, or that failed halfway and left the
database unusable, would be worse than the feature it was adding.

So the rules under test are: forward-only, additive, idempotent, and every
pre-existing row survives byte-for-byte.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.ingest.catalog import MIGRATIONS, SCHEMA, Catalog
from tools.ingest.ledger import LocalLedger
from tools.ingest.migrations import add_column_if_missing, apply_migrations

# The exact Phase 1A schema as shipped in PR #6, reproduced here rather than
# imported: the point is to prove that a database created by *that* code opens
# under today's code. Importing the current SCHEMA would test nothing.
LEGACY_ASSET_COLUMNS = (
    "asset_id", "sha256", "source_volume_id", "source_class", "original_path",
    "original_filename", "size", "mime", "detected_at", "transfer_started_at",
    "transferred_at", "verified_at", "completed_at", "transfer_state", "final_path",
    "final_filename", "error_state", "error_message", "derived_from",
    "classifier_model", "classifier_model_version", "classifier_confidence",
    "human_reviewed", "created_at", "updated_at",
)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _legacy_catalog(self) -> Path:
        """A database exactly as the pre-migration code would have left it."""
        path = self.root / "catalog.sqlite"
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.executescript(SCHEMA)
        conn.execute(
            """
            INSERT INTO assets(
                asset_id, sha256, source_volume_id, source_class, original_path,
                original_filename, size, transfer_state, human_reviewed, created_at, updated_at
            ) VALUES ('legacy-1', 'abc123', 'vol-1', 'desktop', 'photo.jpg', 'photo.jpg',
                      4096, 'source_unlinked', 0, '2026-09-11T00:00:00+00:00', '2026-09-11T00:00:00+00:00')
            """
        )
        conn.execute("INSERT INTO blobs(sha256, size, mime, first_seen) VALUES ('abc123', 4096, 'image/jpeg', '2026-09-11T00:00:00+00:00')")
        # user_version stays 0, as it was before migrations existed.
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        conn.close()
        return path

    def test_a_pre_migration_catalog_opens_and_keeps_its_rows(self):
        path = self._legacy_catalog()
        with Catalog(path) as catalog:
            self.assertEqual(catalog.schema_version, len(MIGRATIONS))
            asset = catalog.get_asset("legacy-1")
            self.assertIsNotNone(asset)
            self.assertEqual(asset.row["original_filename"], "photo.jpg")
            self.assertEqual(asset.row["sha256"], "abc123")
            self.assertEqual(asset.row["transfer_state"], "source_unlinked")

    def test_every_legacy_column_survives(self):
        path = self._legacy_catalog()
        with Catalog(path) as catalog:
            columns = {row[1] for row in catalog._conn.execute("PRAGMA table_info(assets)")}
        for column in LEGACY_ASSET_COLUMNS:
            self.assertIn(column, columns, f"migration dropped {column}")

    def test_migration_is_idempotent(self):
        path = self._legacy_catalog()
        for _ in range(3):
            with Catalog(path) as catalog:
                version = catalog.schema_version
                count = catalog._conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            self.assertEqual(version, len(MIGRATIONS))
            self.assertEqual(count, 1)

    def test_new_tables_exist_after_migration(self):
        path = self._legacy_catalog()
        with Catalog(path) as catalog:
            tables = {
                row[0]
                for row in catalog._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        for table in ("naming_history", "classification_runs", "media_metadata", "duplicate_observations"):
            self.assertIn(table, tables)

    def test_a_newer_database_is_refused_rather_than_downgraded(self):
        path = self.root / "future.sqlite"
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.execute("PRAGMA user_version = 99")
        conn.close()
        conn = sqlite3.connect(str(path), isolation_level=None)
        with self.assertRaises(RuntimeError):
            apply_migrations(conn, MIGRATIONS)
        conn.close()

    def test_add_column_if_missing_is_safe_to_repeat(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.execute("INSERT INTO t(a) VALUES ('keep me')")
        for _ in range(3):
            add_column_if_missing(conn, "t", "b", "TEXT")
        columns = [row[1] for row in conn.execute("PRAGMA table_info(t)")]
        self.assertEqual(columns, ["a", "b"])
        self.assertEqual(conn.execute("SELECT a FROM t").fetchone()[0], "keep me")
        conn.close()


class LedgerHistoryTests(unittest.TestCase):
    """Naming and classification are ledgers, not mutable fields."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = LocalLedger(Path(self._tmp.name) / "ledger.sqlite")
        self.addCleanup(self.ledger.close)

    def _naming(self, **overrides):
        record = {
            "local_file_id": "file-1",
            "source_volume_id": "mac-local",
            "source_class": "downloads",
            "directory": "/tmp",
            "original_filename": "IMG_1.png",
            "name_quality": "poor",
            "decision": "renamed",
            "approved_by": "automatic",
            "human_corrected": 0,
            "created_at": "2026-09-12T00:00:00+00:00",
        }
        record.update(overrides)
        return record

    def test_naming_history_accumulates_rather_than_overwrites(self):
        self.ledger.append_naming(self._naming(applied_filename="first.png"))
        self.ledger.append_naming(self._naming(original_filename="first.png", applied_filename="second.png"))
        history = self.ledger.naming_history("file-1")
        self.assertEqual(len(history), 2)
        self.assertEqual([row["applied_filename"] for row in history], ["first.png", "second.png"])

    def test_reclassification_appends_a_new_run(self):
        for recommendation in ("review", "ingest", "ignore"):
            self.ledger.append_classification(
                {
                    "local_file_id": "file-1",
                    "source_volume_id": "mac-local",
                    "source_class": "downloads",
                    "recommendation": recommendation,
                    "policy_outcome": recommendation,
                    "created_at": "2026-09-12T00:00:00+00:00",
                }
            )
        runs = self.ledger.classification_runs("file-1")
        self.assertEqual([row["recommendation"] for row in runs], ["review", "ingest", "ignore"])

    def test_a_human_row_is_recognised_permanently(self):
        self.assertFalse(self.ledger.has_human_naming("file-1"))
        self.ledger.append_naming(self._naming(approved_by="human", human_corrected=1, applied_filename="mine.png"))
        self.assertTrue(self.ledger.has_human_naming("file-1"))
        # A later automatic row does not clear the lock.
        self.ledger.append_naming(self._naming(applied_filename="machine.png"))
        self.assertTrue(self.ledger.has_human_naming("file-1"))

    def test_inode_reuse_starts_a_new_lineage_rather_than_inheriting_a_lock(self):
        """A recycled inode must not let a stranger's file inherit a human lock."""
        tmp = Path(self._tmp.name)
        original = tmp / "original.png"
        original.write_bytes(b"first file")
        stat = original.stat()

        first = self.ledger.observe_file(
            path=original, source_class="downloads", source_volume_id="mac-local",
            device=stat.st_dev, inode=stat.st_ino, size=10, sha256="a" * 64,
            mime="image/png", media_kind="image", now="2026-09-12T00:00:00+00:00",
        )
        self.ledger.record_human_name(
            first.local_file_id, filename="original.png", directory=str(tmp), now="2026-09-12T00:00:00+00:00"
        )
        self.assertTrue(self.ledger.has_human_naming(first.local_file_id))

        original.unlink()  # the tracked path is gone
        replacement = tmp / "replacement.png"
        replacement.write_bytes(b"an entirely different file")

        second = self.ledger.observe_file(
            path=replacement, source_class="downloads", source_volume_id="mac-local",
            device=stat.st_dev, inode=stat.st_ino,  # inode reused
            size=20, sha256="b" * 64, mime="image/png", media_kind="image",
            now="2026-09-12T01:00:00+00:00",
        )
        self.assertTrue(second.is_new_generation)
        row = self.ledger.get_local_file(second.local_file_id)
        self.assertEqual(row["first_seen_filename"], "replacement.png")
        self.assertEqual(row["naming_state"], "unevaluated")

    def test_an_in_place_edit_keeps_its_lineage(self):
        """Same path, changed bytes: the owner edited their file."""
        tmp = Path(self._tmp.name)
        path = tmp / "notes.txt"
        path.write_bytes(b"v1")
        stat = path.stat()

        first = self.ledger.observe_file(
            path=path, source_class="downloads", source_volume_id="mac-local",
            device=stat.st_dev, inode=stat.st_ino, size=2, sha256="a" * 64,
            mime="text/plain", media_kind="text", now="2026-09-12T00:00:00+00:00",
        )
        self.ledger.record_human_name(
            first.local_file_id, filename="notes.txt", directory=str(tmp), now="2026-09-12T00:00:00+00:00"
        )

        path.write_bytes(b"v2 much longer now")
        second = self.ledger.observe_file(
            path=path, source_class="downloads", source_volume_id="mac-local",
            device=stat.st_dev, inode=stat.st_ino, size=18, sha256="c" * 64,
            mime="text/plain", media_kind="text", now="2026-09-12T01:00:00+00:00",
        )
        self.assertFalse(second.is_new_generation)
        self.assertTrue(self.ledger.has_human_naming(second.local_file_id))


if __name__ == "__main__":
    unittest.main()
