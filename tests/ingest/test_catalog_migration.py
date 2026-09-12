"""Migrating a catalog that already holds the owner's data.

The T7 catalog is durable truth about files whose Mac originals have already
been deleted. A migration that loses a row loses the only record of where
something came from, so these tests are about one thing: an existing catalog
survives, unchanged, no matter how many times the code runs over it.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tools.ingest.catalog import MIGRATIONS, SCHEMA, Catalog
from tools.ingest.ledger import LOCAL_MIGRATIONS, LocalLedger
from tools.ingest.migrations import add_column_if_missing, apply_migrations


def make_v1_catalog(path: Path) -> str:
    """A catalog exactly as Phase 1A left it: the old schema, no version marker.

    Pre-migration databases report `user_version = 0`, which is what makes
    migration 1 have to be a no-op over an already-populated tree.
    """
    connection = sqlite3.connect(str(path))
    connection.executescript(SCHEMA)
    connection.execute(
        """
        INSERT INTO assets(
            asset_id, sha256, source_volume_id, source_class, original_path, original_filename,
            size, mime, transfer_state, final_path, final_filename, human_reviewed, created_at, updated_at
        ) VALUES (
            'asset-1', 'abc123', 'vol-1', 'desktop', 'holiday.jpg', 'holiday.jpg',
            4096, 'image/jpeg', 'source_unlinked', '/T7/00-inbox/organized/desktop/holiday.jpg',
            'holiday.jpg', 0, '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00'
        )
        """
    )
    connection.execute(
        "INSERT INTO blobs(sha256, size, mime, first_seen) VALUES ('abc123', 4096, 'image/jpeg', '2026-09-01T00:00:00+00:00')"
    )
    connection.execute(
        """
        INSERT INTO repos(repo_id, source_volume_id, original_path, remote_url, head, branch, dirty, unpushed, stash, detected_at)
        VALUES ('vol-1:code/app', 'vol-1', 'code/app', 'git@example.com:me/app.git', 'deadbeef', 'main', 1, 1, 0, '2026-09-01T00:00:00+00:00')
        """
    )
    connection.commit()
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()
    return version


class CatalogMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "catalog.sqlite"

    def test_a_phase_1a_catalog_reports_version_zero(self):
        self.assertEqual(make_v1_catalog(self.path), 0)

    def test_existing_rows_survive_the_migration(self):
        make_v1_catalog(self.path)
        with Catalog(self.path) as catalog:
            self.assertEqual(catalog.schema_version, len(MIGRATIONS))
            asset = catalog.get_asset("asset-1")
            self.assertEqual(asset.row["original_filename"], "holiday.jpg")
            self.assertEqual(asset.row["sha256"], "abc123")
            self.assertEqual(asset.row["transfer_state"], "source_unlinked")
            blobs = catalog._conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
            repos = catalog._conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        self.assertEqual((blobs, repos), (1, 1))

    def test_the_new_columns_are_added_as_null_not_backfilled_with_a_guess(self):
        make_v1_catalog(self.path)
        with Catalog(self.path) as catalog:
            asset = catalog.get_asset("asset-1")
        # We do not know what this file was first called; claiming to would be
        # a fabricated provenance record.
        self.assertIsNone(asset.row["first_seen_filename"])
        self.assertIsNone(asset.row["local_file_id"])
        self.assertIsNone(asset.row["retention_recommendation"])

    def test_migration_is_idempotent(self):
        make_v1_catalog(self.path)
        for _ in range(4):
            with Catalog(self.path) as catalog:
                self.assertEqual(catalog.schema_version, len(MIGRATIONS))
        with Catalog(self.path) as catalog:
            rows = catalog._conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            self.assertEqual(rows, 1)

    def test_a_fresh_catalog_reaches_the_current_version(self):
        with Catalog(self.path) as catalog:
            self.assertEqual(catalog.schema_version, len(MIGRATIONS))
            tables = {
                row[0]
                for row in catalog._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        for expected in ("assets", "blobs", "naming_history", "classification_runs", "media_metadata", "duplicate_observations"):
            self.assertIn(expected, tables)

    def test_a_newer_database_is_refused_rather_than_downgraded(self):
        """A checkout older than the disk must stop, not silently misread it."""
        connection = sqlite3.connect(str(self.path))
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
        connection.close()
        with self.assertRaises(RuntimeError) as caught:
            Catalog(self.path)
        self.assertIn("newer than this code supports", str(caught.exception))

    def test_naming_and_classification_history_survive_a_later_migration(self):
        """History is the point of these tables; a future migration must not
        be able to quietly discard it."""
        with Catalog(self.path) as catalog:
            catalog.append_naming(
                {
                    "local_file_id": "local-1",
                    "asset_id": "asset-1",
                    "source_volume_id": "vol-1",
                    "source_class": "downloads",
                    "directory": "/Users/me/Downloads",
                    "sha256": "abc123",
                    "original_filename": "IMG_6909.JPG",
                    "applied_filename": "2026-09-12_hawk.jpg",
                    "name_quality": "poor",
                    "decision": "renamed",
                    "approved_by": "automatic",
                    "human_corrected": 0,
                    "created_at": "2026-09-12T00:00:00+00:00",
                }
            )
            catalog.append_classification(
                {
                    "local_file_id": "local-1",
                    "source_volume_id": "vol-1",
                    "source_class": "downloads",
                    "recommendation": "review",
                    "policy_outcome": "review",
                    "created_at": "2026-09-12T00:00:00+00:00",
                }
            )

        # A hypothetical migration 3 runs; prior rows must be untouched.
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        connection.row_factory = sqlite3.Row

        def migration_3(conn: sqlite3.Connection) -> None:
            add_column_if_missing(conn, "assets", "future_column", "TEXT")

        apply_migrations(connection, tuple(MIGRATIONS) + (migration_3,))
        naming = connection.execute("SELECT * FROM naming_history").fetchall()
        runs = connection.execute("SELECT * FROM classification_runs").fetchall()
        connection.close()

        self.assertEqual(len(naming), 1)
        self.assertEqual(naming[0]["original_filename"], "IMG_6909.JPG")
        self.assertEqual(naming[0]["applied_filename"], "2026-09-12_hawk.jpg")
        self.assertEqual(len(runs), 1)


class LocalLedgerMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state" / "local-ledger.sqlite"

    def test_the_ledger_creates_its_directory_and_reaches_the_current_version(self):
        with LocalLedger(self.path) as ledger:
            self.assertEqual(ledger.schema_version, len(LOCAL_MIGRATIONS))
        self.assertTrue(self.path.exists())

    def test_reopening_the_ledger_preserves_its_rows(self):
        with LocalLedger(self.path) as ledger:
            ledger.append_naming(
                {
                    "local_file_id": "local-1",
                    "source_volume_id": "mac-local",
                    "source_class": "screenshots",
                    "directory": "/tmp",
                    "original_filename": "Screenshot.png",
                    "name_quality": "poor",
                    "decision": "renamed",
                    "applied_filename": "2026-09-12_thing.png",
                    "approved_by": "automatic",
                    "human_corrected": 0,
                    "created_at": "2026-09-12T00:00:00+00:00",
                }
            )
        for _ in range(3):
            with LocalLedger(self.path) as ledger:
                history = ledger.naming_history("local-1")
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["applied_filename"], "2026-09-12_thing.png")

    def test_the_ledger_file_is_owner_only(self):
        with LocalLedger(self.path):
            pass
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()


class LedgerGrowthTests(unittest.TestCase):
    """An hourly agent must not grow the ledger by a row per file per run.

    History is the point of these tables, but a re-decision that reaches the
    same verdict is not history -- it is a record of having scanned, and at one
    row per file per hour it buries the decisions that did change.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = LocalLedger(Path(self._tmp.name) / "ledger.sqlite")
        self.addCleanup(self.ledger.close)

    def _classify(self, recommendation="review", reason="unchanged", when="2026-09-12T00:00:00+00:00"):
        return self.ledger.append_classification(
            {
                "local_file_id": "file-1",
                "source_volume_id": "mac-local",
                "source_class": "downloads",
                "sha256": "a" * 64,
                "recommendation": recommendation,
                "reason": reason,
                "policy_outcome": recommendation,
                "human_corrected": 0,
                "created_at": when,
            }
        )

    def test_an_unchanged_verdict_does_not_append_a_row(self):
        for _ in range(10):
            self._classify()
        self.assertEqual(len(self.ledger.classification_runs("file-1")), 1)

    def test_a_changed_verdict_does_append(self):
        self._classify(recommendation="review")
        self._classify(recommendation="ingest", reason="now looks worth keeping")
        self._classify(recommendation="ingest", reason="now looks worth keeping")
        runs = self.ledger.classification_runs("file-1")
        self.assertEqual([row["recommendation"] for row in runs], ["review", "ingest"])

    def test_the_subject_lookup_is_indexed(self):
        """Without an index this is a full scan, once per file per run."""
        plan = self.ledger._conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM classification_runs
            WHERE COALESCE(local_file_id, asset_id, '') = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            ("file-1",),
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        self.assertNotIn("SCAN classification_runs", detail, f"unindexed subject lookup: {detail}")


class ConcurrentOpenTests(unittest.TestCase):
    """Two commands opening the same catalog at the same moment.

    That is not a contrived case: the hourly agent and a hand-run `organize`
    overlap by design, and the first time either one meets a catalog written by
    an older checkout, both run the same migration. `add_column_if_missing` is
    check-then-ALTER with no transaction around it -- SQLite cannot give
    migrations one, because `executescript` commits -- so the loser used to get
    `duplicate column name` thrown straight out of `Catalog.__init__`, killing
    the whole invocation.
    """

    OPENERS = 6
    TRIALS = 15  # the original bug landed on roughly half of these

    def _open_concurrently(self, path: Path) -> list[BaseException]:
        ready = threading.Barrier(self.OPENERS, timeout=30)
        failures: list[BaseException] = []

        def open_once() -> None:
            try:
                ready.wait()
                with Catalog(path) as catalog:
                    self.assertEqual(catalog.schema_version, len(MIGRATIONS))
            except BaseException as exc:  # noqa: BLE001 - collected, then reported
                failures.append(exc)

        threads = [threading.Thread(target=open_once) for _ in range(self.OPENERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        return failures

    def _assert_schema_is_complete(self, path: Path) -> None:
        with Catalog(path) as catalog:
            columns = {row[1] for row in catalog._conn.execute("PRAGMA table_info(assets)")}
            version = catalog._conn.execute("PRAGMA user_version").fetchone()[0]
        for column in ("first_seen_filename", "local_file_id", "retention_recommendation", "retention_run_id"):
            self.assertIn(column, columns)
        self.assertEqual(version, len(MIGRATIONS))

    def test_a_v1_catalog_survives_concurrent_first_opens(self):
        for trial in range(self.TRIALS):
            with self.subTest(trial=trial), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "catalog.sqlite"
                make_v1_catalog(path)
                failures = self._open_concurrently(path)
                self.assertEqual(failures, [], f"opening an upgraded catalog raced: {failures}")
                self._assert_schema_is_complete(path)

    def test_a_fresh_catalog_survives_concurrent_creation(self):
        for trial in range(self.TRIALS):
            with self.subTest(trial=trial), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "catalog.sqlite"
                failures = self._open_concurrently(path)
                self.assertEqual(failures, [], f"creating a catalog raced: {failures}")
                self._assert_schema_is_complete(path)

    def test_the_local_ledger_survives_the_same_race(self):
        """Same machinery, and the ledger is opened by every command there is."""
        for trial in range(self.TRIALS):
            with self.subTest(trial=trial), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "local-ledger.sqlite"
                ready = threading.Barrier(self.OPENERS, timeout=30)
                failures: list[BaseException] = []

                def open_once() -> None:
                    try:
                        ready.wait()
                        with LocalLedger(path) as ledger:
                            self.assertEqual(ledger.schema_version, len(LOCAL_MIGRATIONS))
                    except BaseException as exc:  # noqa: BLE001 - collected, then reported
                        failures.append(exc)

                threads = [threading.Thread(target=open_once) for _ in range(self.OPENERS)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(60)
                self.assertEqual(failures, [], f"opening the ledger raced: {failures}")

