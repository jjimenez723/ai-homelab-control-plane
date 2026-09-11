"""The core transactional move primitive: fault injection at every boundary
named in the spec.

Covers required scenarios: #16 interrupted copy (T7 disconnected / process
killed mid-copy), #17 injected hash mismatch, #18 database failure before
commit, #19 database failure after copy, #20 retry after partial copy, #21
duplicate invocation, #22 destination collision, #23 source unlink failure,
#24 resume after reconnect, #25 successful verified transfer, #26 original
disappears only after successful destination verification.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tools.ingest.catalog import (
    STATE_COMMITTED,
    STATE_FAILED_ERROR,
    STATE_FAILED_HASH_MISMATCH,
    STATE_SOURCE_UNLINKED,
    STATE_VERIFIED,
    Catalog,
)
from tools.ingest.hashing import sha256_file
from tools.ingest.transaction import PlannedTransfer, TransferFaultHooks, transfer_file

from tests.ingest.helpers import build_config, make_resolved_t7, write_file

SOURCE_VOLUME_ID = "local-test-volume"


class TransactionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.desktop = self.root / "Desktop"
        self.t7 = self.root / "T7"
        self.t7.mkdir()  # stand-in for an already-mounted volume's root
        self.config = build_config(desktop=self.desktop, t7_root=self.t7, settle_seconds=1)
        self.resolved_t7 = make_resolved_t7(self.config)
        self.catalog = Catalog(self.resolved_t7.catalog_path)
        self.addCleanup(self.catalog.close)

    def make_source(self, name: str, content: bytes, *, age_seconds: int = 3600) -> Path:
        return write_file(self.desktop / name, content, age_seconds=age_seconds)

    def planned(self, source_path: Path, rel_path: str | None = None, source_class: str = "desktop") -> PlannedTransfer:
        return PlannedTransfer(
            source_path=source_path,
            rel_path=rel_path or source_path.name,
            source_volume_id=SOURCE_VOLUME_ID,
            source_class=source_class,
        )

    def run_transfer(self, planned: PlannedTransfer, hooks: TransferFaultHooks | None = None):
        return transfer_file(self.catalog, planned, self.resolved_t7, settle_seconds=self.config.settle_seconds, hooks=hooks)


class SuccessfulTransferTests(TransactionTestCase):
    def test_successful_verified_transfer(self):
        source = self.make_source("report.pdf", b"important report bytes" * 100)
        source_hash = sha256_file(source)
        outcome = self.run_transfer(self.planned(source))

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.state, STATE_SOURCE_UNLINKED)
        self.assertFalse(source.exists())
        self.assertTrue(outcome.final_path.exists())
        self.assertEqual(sha256_file(outcome.final_path), source_hash)

        asset = self.catalog.get_asset(outcome.asset_id)
        self.assertEqual(asset.row["sha256"], source_hash)
        self.assertEqual(asset.row["transfer_state"], STATE_SOURCE_UNLINKED)
        self.assertIsNotNone(asset.row["verified_at"])
        self.assertIsNotNone(asset.row["completed_at"])

        sidecar_path = self.resolved_t7.sidecars_dir / f"{outcome.asset_id}.json"
        self.assertTrue(sidecar_path.exists())
        import json

        sidecar = json.loads(sidecar_path.read_text())
        self.assertEqual(sidecar["sha256"], source_hash)
        self.assertEqual(sidecar["transfer_state"], STATE_SOURCE_UNLINKED)


class InterruptedCopyTests(TransactionTestCase):
    def test_t7_disconnect_mid_copy_leaves_source_intact_and_partial_detectable(self):
        content = os.urandom(3 * 1024 * 1024)  # multiple 1 MiB chunks
        source = self.make_source("video.mov", content)
        source_hash = sha256_file(source)

        def disconnect(written: int, total: int) -> None:
            if written < total:
                raise OSError("simulated T7 disconnect")

        outcome = self.run_transfer(self.planned(source), hooks=TransferFaultHooks(mid_copy=disconnect))

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.state, STATE_FAILED_ERROR)
        # Source survives byte-for-byte.
        self.assertTrue(source.exists())
        self.assertEqual(sha256_file(source), source_hash)
        # The partial state is detectable: a .partial file exists and is short.
        partial = self.resolved_t7.staging_dir / f"{outcome.asset_id}.partial"
        self.assertTrue(partial.exists())
        self.assertLess(partial.stat().st_size, len(content))

        asset = self.catalog.get_asset(outcome.asset_id)
        self.assertEqual(asset.row["transfer_state"], STATE_FAILED_ERROR)

    def test_retry_after_interrupted_copy_succeeds(self):
        content = os.urandom(3 * 1024 * 1024)
        source = self.make_source("video.mov", content)
        source_hash = sha256_file(source)

        def disconnect(written: int, total: int) -> None:
            if written < total:
                raise OSError("simulated T7 disconnect")

        first = self.run_transfer(self.planned(source), hooks=TransferFaultHooks(mid_copy=disconnect))
        self.assertFalse(first.success)
        self.assertTrue(source.exists())

        second = self.run_transfer(self.planned(source))
        self.assertTrue(second.success)
        self.assertEqual(second.state, STATE_SOURCE_UNLINKED)
        self.assertFalse(source.exists())
        self.assertEqual(sha256_file(second.final_path), source_hash)

    def test_retry_after_stale_partial_from_a_killed_process(self):
        """Simulate a hard process kill: a `.partial` file with garbage,
        truncated content exists on disk but no exception was ever handled
        (so the catalog row is still at an early, non-terminal state)."""
        source = self.make_source("archive.zip", b"real content" * 1000)
        source_hash = sha256_file(source)
        planned = self.planned(source)

        from tools.ingest.identity import asset_id_for

        asset_id = asset_id_for(planned.source_volume_id, planned.rel_path)
        self.catalog.ensure_pending_asset(
            asset_id,
            source_volume_id=planned.source_volume_id,
            source_class=planned.source_class,
            original_path=planned.rel_path,
            original_filename=source.name,
            size=source.stat().st_size,
        )
        self.catalog.update_asset(asset_id, transfer_state="copying")
        stale_partial = self.resolved_t7.staging_dir / f"{asset_id}.partial"
        stale_partial.parent.mkdir(parents=True, exist_ok=True)
        stale_partial.write_bytes(b"garbage-from-killed-process")

        outcome = self.run_transfer(planned)

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.state, STATE_SOURCE_UNLINKED)
        self.assertEqual(sha256_file(outcome.final_path), source_hash)
        self.assertFalse(source.exists())


class HashMismatchTests(TransactionTestCase):
    def test_injected_hash_mismatch_fails_safely_and_source_survives(self):
        source = self.make_source("photo.jpg", b"original bytes")
        source_hash = sha256_file(source)

        def corrupt(staging_path: Path) -> None:
            with open(staging_path, "r+b") as fh:
                fh.seek(0)
                fh.write(b"CORRUPTED")

        outcome = self.run_transfer(self.planned(source), hooks=TransferFaultHooks(after_copy=corrupt))

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.state, STATE_FAILED_HASH_MISMATCH)
        self.assertTrue(source.exists())
        self.assertEqual(sha256_file(source), source_hash)

        asset = self.catalog.get_asset(outcome.asset_id)
        self.assertEqual(asset.row["transfer_state"], STATE_FAILED_HASH_MISMATCH)
        self.assertEqual(asset.row["error_state"], "hash_mismatch")

    def test_retry_after_hash_mismatch_succeeds(self):
        source = self.make_source("photo.jpg", b"original bytes")
        source_hash = sha256_file(source)

        def corrupt(staging_path: Path) -> None:
            with open(staging_path, "r+b") as fh:
                fh.seek(0)
                fh.write(b"CORRUPTED")

        self.run_transfer(self.planned(source), hooks=TransferFaultHooks(after_copy=corrupt))
        outcome = self.run_transfer(self.planned(source))

        self.assertTrue(outcome.success)
        self.assertEqual(sha256_file(outcome.final_path), source_hash)


class DatabaseFailureTests(TransactionTestCase):
    def test_database_failure_before_verify_commit_leaves_source_intact(self):
        source = self.make_source("notes.txt", b"content")
        source_hash = sha256_file(source)

        def boom() -> None:
            raise RuntimeError("simulated sqlite failure")

        outcome = self.run_transfer(self.planned(source), hooks=TransferFaultHooks(before_verify_commit=boom))

        self.assertFalse(outcome.success)
        self.assertTrue(source.exists())
        self.assertEqual(sha256_file(source), source_hash)
        # No asset row claims verified/committed/source_unlinked.
        from tools.ingest.identity import asset_id_for

        asset = self.catalog.get_asset(asset_id_for(SOURCE_VOLUME_ID, "notes.txt"))
        self.assertNotIn(asset.row["transfer_state"], (STATE_VERIFIED, STATE_COMMITTED, STATE_SOURCE_UNLINKED))

    def test_retry_after_database_failure_before_commit_succeeds(self):
        source = self.make_source("notes.txt", b"content")
        source_hash = sha256_file(source)

        def boom() -> None:
            raise RuntimeError("simulated sqlite failure")

        self.run_transfer(self.planned(source), hooks=TransferFaultHooks(before_verify_commit=boom))
        outcome = self.run_transfer(self.planned(source))

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.state, STATE_SOURCE_UNLINKED)
        self.assertEqual(sha256_file(outcome.final_path), source_hash)

    def test_database_failure_after_copy_before_final_commit(self):
        """Physical rename succeeds, but the final catalog commit fails --
        the verified destination file exists, source is still untouched."""
        source = self.make_source("data.bin", b"payload" * 500)
        source_hash = sha256_file(source)

        def boom() -> None:
            raise RuntimeError("simulated sqlite failure at final commit")

        outcome = self.run_transfer(self.planned(source), hooks=TransferFaultHooks(before_final_commit=boom))

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.state, STATE_VERIFIED)
        self.assertTrue(source.exists(), "source must survive a DB failure even after the dest rename")
        self.assertTrue(outcome.final_path.exists())
        self.assertEqual(sha256_file(outcome.final_path), source_hash)

        asset = self.catalog.get_asset(outcome.asset_id)
        self.assertEqual(asset.row["transfer_state"], STATE_VERIFIED)

    def test_resume_after_database_failure_after_copy_completes_and_unlinks_source(self):
        """This is also the 'resume after reconnect' scenario: everything
        needed to finish is already on disk, only bookkeeping was pending."""
        source = self.make_source("data.bin", b"payload" * 500)
        source_hash = sha256_file(source)

        def boom() -> None:
            raise RuntimeError("simulated sqlite failure at final commit")

        first = self.run_transfer(self.planned(source), hooks=TransferFaultHooks(before_final_commit=boom))
        self.assertFalse(first.success)
        self.assertTrue(source.exists())

        second = self.run_transfer(self.planned(source))
        self.assertTrue(second.success)
        self.assertEqual(second.state, STATE_SOURCE_UNLINKED)
        self.assertFalse(source.exists())
        self.assertEqual(sha256_file(second.final_path), source_hash)


class ResumeAfterReconnectTests(TransactionTestCase):
    def test_resume_when_rename_never_happened_but_staged_copy_was_verified(self):
        source = self.make_source("clip.mp4", b"video-bytes" * 200)
        source_hash = sha256_file(source)

        def boom() -> None:
            raise OSError("simulated disconnect before rename")

        first = self.run_transfer(self.planned(source), hooks=TransferFaultHooks(before_finalize_rename=boom))
        self.assertFalse(first.success)
        self.assertTrue(source.exists())
        partial = self.resolved_t7.staging_dir / f"{first.asset_id}.partial"
        self.assertTrue(partial.exists())

        second = self.run_transfer(self.planned(source))
        self.assertTrue(second.success)
        self.assertEqual(second.state, STATE_SOURCE_UNLINKED)
        self.assertEqual(sha256_file(second.final_path), source_hash)
        self.assertFalse(source.exists())
        self.assertFalse(partial.exists())


class DuplicateInvocationTests(TransactionTestCase):
    def test_duplicate_invocation_is_idempotent(self):
        source = self.make_source("dup.txt", b"same file twice")
        source_hash = sha256_file(source)

        first = self.run_transfer(self.planned(source))
        self.assertTrue(first.success)
        self.assertEqual(first.state, STATE_SOURCE_UNLINKED)

        second = self.run_transfer(self.planned(source))
        self.assertTrue(second.success)
        self.assertEqual(second.skipped_reason, "already_completed")
        self.assertEqual(second.asset_id, first.asset_id)

        # Exactly one asset row -- no duplicate created.
        row_count = self.catalog._conn.execute(  # noqa: SLF001 - direct check is appropriate in a test
            "SELECT COUNT(*) FROM assets WHERE asset_id = ?", (first.asset_id,)
        ).fetchone()[0]
        self.assertEqual(row_count, 1)
        self.assertEqual(sha256_file(first.final_path), source_hash)


class DestinationCollisionTests(TransactionTestCase):
    def test_destination_collision_with_unrelated_file_is_disambiguated(self):
        colliding_path = self.resolved_t7.inbox_dir / "desktop" / "shared-name.txt"
        colliding_path.parent.mkdir(parents=True, exist_ok=True)
        colliding_path.write_bytes(b"unrelated pre-existing content")

        source = self.make_source("shared-name.txt", b"our new content")
        source_hash = sha256_file(source)

        outcome = self.run_transfer(self.planned(source))

        self.assertTrue(outcome.success)
        self.assertNotEqual(outcome.final_path, colliding_path)
        self.assertEqual(colliding_path.read_bytes(), b"unrelated pre-existing content")
        self.assertEqual(sha256_file(outcome.final_path), source_hash)

    def test_collision_with_our_own_already_committed_content_is_reused(self):
        target_path = self.resolved_t7.inbox_dir / "desktop" / "note.txt"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source = self.make_source("note.txt", b"identical bytes")
        source_hash = sha256_file(source)
        target_path.write_bytes(b"identical bytes")  # pre-seeded with matching content

        outcome = self.run_transfer(self.planned(source))
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.final_path, target_path)
        self.assertEqual(sha256_file(target_path), source_hash)


class SourceUnlinkFailureTests(TransactionTestCase):
    def test_source_unlink_failure_keeps_transfer_committed_but_flags_error(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits are not enforced for root")
        source = self.make_source("locked-dir-file.txt", b"content")
        source_hash = sha256_file(source)
        os.chmod(self.desktop, 0o500)
        try:
            outcome = self.run_transfer(self.planned(source))
        finally:
            os.chmod(self.desktop, 0o700)

        self.assertTrue(outcome.success)
        self.assertTrue(outcome.source_unlink_failed)
        self.assertEqual(outcome.state, STATE_COMMITTED)
        self.assertTrue(source.exists())
        self.assertEqual(sha256_file(outcome.final_path), source_hash)

        asset = self.catalog.get_asset(outcome.asset_id)
        self.assertEqual(asset.row["error_state"], "source_unlink_failed")
        self.assertEqual(asset.row["transfer_state"], STATE_COMMITTED)

    def test_retry_after_source_unlink_failure_completes(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits are not enforced for root")
        source = self.make_source("locked-dir-file.txt", b"content")
        os.chmod(self.desktop, 0o500)
        try:
            first = self.run_transfer(self.planned(source))
        finally:
            os.chmod(self.desktop, 0o700)
        self.assertTrue(first.source_unlink_failed)

        second = self.run_transfer(self.planned(source))
        self.assertTrue(second.success)
        self.assertFalse(second.source_unlink_failed)
        self.assertEqual(second.state, STATE_SOURCE_UNLINKED)
        self.assertFalse(source.exists())

    def test_source_already_moved_is_treated_as_a_safe_noop(self):
        """If the source vanishes after commit but before this run's unlink
        attempt (e.g. a previous run's unlink actually succeeded but crashed
        before updating the catalog), retry must not error."""
        source = self.make_source("ghost.txt", b"content")
        planned = self.planned(source)

        def boom() -> None:
            raise OSError("simulated crash right after commit, before unlink")

        first = self.run_transfer(planned, hooks=TransferFaultHooks(before_unlink=boom))
        self.assertTrue(first.success)  # destination already verified+committed
        self.assertTrue(first.source_unlink_failed)
        self.assertTrue(source.exists())  # the injected fault fired before unlink was attempted

        source.unlink()  # now simulate that the source is gone by the time we retry

        second = self.run_transfer(planned)
        self.assertTrue(second.success)
        self.assertEqual(second.state, STATE_SOURCE_UNLINKED)


if __name__ == "__main__":
    unittest.main()
