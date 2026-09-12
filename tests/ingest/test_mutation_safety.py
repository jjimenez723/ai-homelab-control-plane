"""Regressions for three ways the Phase 1A pipeline could destroy a user file.

All three were reachable on `main` at 8d45635 and all three share a shape: a
source file is unlinked, or a destination is overwritten, on the strength of a
check that was never actually made. Each test here fails against that code.

The governing rule these encode: **the source is deleted only once the exact
bytes are provably present at the destination.** Not "a catalog row says so",
not "something exists at that path" -- the bytes, verified.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from tools.ingest.catalog import (
    STATE_COMMITTED,
    STATE_NEEDS_REVIEW,
    STATE_SOURCE_UNLINKED,
    STATE_VERIFIED,
    Catalog,
)
from tools.ingest.cli import cmd_organize
from tools.ingest.hashing import sha256_file
from tools.ingest.transaction import PlannedTransfer, transfer_file

from tests.ingest.helpers import build_config, make_resolved_t7, write_file
from tests.ingest.heic_fixtures import make_heic

SOURCE_VOLUME_ID = "local-test-volume"


def aged_heic(path: Path, **kwargs) -> Path:
    """A HEIC fixture whose mtime is old enough to count as settled."""
    make_heic(path, **kwargs)
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path


class _Args:
    """Stand-in for the argparse namespace `cmd_organize` consumes."""

    def __init__(self, target: str, *, apply: bool, config: Path | None = None):
        self.target = target
        self.apply = apply
        self.dry_run = not apply
        self.config = config
        self.allow_dev_storage_root = True


class DerivativeCollisionTests(unittest.TestCase):
    """A HEIC derivative must never land on another asset's file.

    `IMG_1234.HEIC` and `IMG_1234.jpg` are an entirely ordinary pair to find
    side by side -- a phone shooting HEIC plus an exported JPEG of something
    unrelated. The derivative's output path was computed as
    `parent.with_suffix('.jpg')` with no collision check at all, so ingesting
    both meant the owner's JPEG was overwritten on the T7 *after* the tool had
    already deleted it from the Mac. Both copies gone, and the run reported
    success.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.desktop = self.root / "Desktop"
        self.t7 = self.root / "T7"
        self.t7.mkdir()
        self.config = build_config(desktop=self.desktop, t7_root=self.t7, settle_seconds=1)
        self.resolved_t7 = make_resolved_t7(self.config)

    def _organize(self):
        return cmd_organize(_Args("desktop", apply=True), prober=lambda: [], config=self.config)

    def test_unrelated_jpeg_survives_a_heic_derivative_with_the_same_stem(self):
        jpeg = write_file(self.desktop / "IMG_1234.jpg", b"the owner's own photograph" * 50)
        jpeg_hash = sha256_file(jpeg)
        aged_heic(self.desktop / "IMG_1234.HEIC", color=(254, 0, 0))

        self.assertEqual(self._organize(), 0)

        inbox = self.resolved_t7.inbox_dir / "desktop"
        landed = {path.name: path for path in inbox.iterdir() if path.is_file()}
        surviving = [path for path in landed.values() if sha256_file(path) == jpeg_hash]
        self.assertEqual(
            len(surviving), 1,
            f"the owner's JPEG must still exist on the T7; found {sorted(landed)}",
        )
        # ...and it must still be reachable under the catalog row that claims it.
        with Catalog(self.resolved_t7.catalog_path) as catalog:
            rows = catalog._conn.execute(
                "SELECT final_path, COUNT(*) AS n FROM assets WHERE final_path IS NOT NULL GROUP BY final_path HAVING n > 1"
            ).fetchall()
            self.assertEqual([dict(row) for row in rows], [], "two assets must never claim one final path")

    def test_jpeg_ingested_by_an_earlier_run_survives_a_later_heic(self):
        """The collision does not need both files in one run to be dangerous."""
        jpeg = write_file(self.desktop / "IMG_9000.jpg", b"an earlier day's export" * 50)
        jpeg_hash = sha256_file(jpeg)
        self.assertEqual(self._organize(), 0)

        aged_heic(self.desktop / "IMG_9000.HEIC", color=(0, 254, 0))
        self.assertEqual(self._organize(), 0)

        inbox = self.resolved_t7.inbox_dir / "desktop"
        present = [sha256_file(path) for path in inbox.iterdir() if path.is_file()]
        self.assertIn(jpeg_hash, present, "a later HEIC must not overwrite an already-ingested JPEG")


class ResumeVerificationTests(unittest.TestCase):
    """Resuming an interrupted transfer must re-prove the destination."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.desktop = self.root / "Desktop"
        self.t7 = self.root / "T7"
        self.t7.mkdir()
        self.config = build_config(desktop=self.desktop, t7_root=self.t7, settle_seconds=1)
        self.resolved_t7 = make_resolved_t7(self.config)
        self.catalog = Catalog(self.resolved_t7.catalog_path)
        self.addCleanup(self.catalog.close)

    def _planned(self, source: Path) -> PlannedTransfer:
        return PlannedTransfer(
            source_path=source,
            rel_path=source.name,
            source_volume_id=SOURCE_VOLUME_ID,
            source_class="desktop",
        )

    def _transfer(self, planned: PlannedTransfer, **kwargs):
        return transfer_file(self.catalog, planned, self.resolved_t7, settle_seconds=1, **kwargs)

    def assertSourceOnlyGoneOnceDestinationProvesIt(self, source: Path, expected_sha: str, outcome) -> None:
        """The invariant under test, stated once.

        A resumed transfer is allowed to delete the source -- that is the whole
        point of the pipeline -- but *only* after the destination has been
        re-proved to hold those exact bytes. Redoing the copy is a perfectly
        good way to earn that right; trusting a stale catalog row is not.
        """
        if source.exists():
            return
        self.assertIsNotNone(outcome.final_path, "source deleted with no destination recorded")
        final_path = Path(outcome.final_path)
        self.assertTrue(final_path.is_file(), f"source deleted but {final_path} does not exist")
        self.assertEqual(
            sha256_file(final_path), expected_sha,
            "source deleted while the destination held different bytes",
        )

    def test_committed_resume_does_not_unlink_when_the_destination_is_gone(self):
        """A `committed` row is a claim about the past, not proof about now.

        Reaching `committed` and then failing to unlink is an ordinary outcome
        (read-only SD card, locked file). If the destination is subsequently
        lost, the next run used to delete the source anyway on the strength of
        the stale row.
        """
        source = write_file(self.desktop / "contract.pdf", b"the only copy" * 200)
        expected = sha256_file(source)
        from tools.ingest.transaction import TransferFaultHooks

        def refuse_unlink() -> None:
            raise OSError("source volume is read-only")

        outcome = self._transfer(self._planned(source), hooks=TransferFaultHooks(before_unlink=refuse_unlink))
        self.assertEqual(outcome.state, STATE_COMMITTED)
        self.assertTrue(source.exists())

        Path(outcome.final_path).unlink()  # destination lost after the fact

        resumed = self._transfer(self._planned(source))
        self.assertSourceOnlyGoneOnceDestinationProvesIt(source, expected, resumed)

    def test_committed_resume_does_not_unlink_when_the_destination_was_replaced(self):
        source = write_file(self.desktop / "invoice.pdf", b"original invoice bytes" * 100)
        expected = sha256_file(source)
        from tools.ingest.transaction import TransferFaultHooks

        outcome = self._transfer(
            self._planned(source),
            hooks=TransferFaultHooks(before_unlink=lambda: (_ for _ in ()).throw(OSError("locked"))),
        )
        self.assertEqual(outcome.state, STATE_COMMITTED)
        Path(outcome.final_path).write_bytes(b"something else entirely")

        resumed = self._transfer(self._planned(source))
        self.assertSourceOnlyGoneOnceDestinationProvesIt(source, expected, resumed)

    def test_committed_resume_with_no_source_and_no_destination_needs_review(self):
        """Nothing left to re-copy: the run must report, not invent success."""
        source = write_file(self.desktop / "gone.pdf", b"both copies lost" * 50)
        from tools.ingest.transaction import TransferFaultHooks

        outcome = self._transfer(
            self._planned(source),
            hooks=TransferFaultHooks(before_unlink=lambda: (_ for _ in ()).throw(OSError("locked"))),
        )
        Path(outcome.final_path).unlink()
        source.unlink()

        resumed = self._transfer(self._planned(source))
        self.assertFalse(resumed.success)
        self.assertEqual(resumed.state, STATE_NEEDS_REVIEW)

    def test_verified_resume_does_not_trust_a_foreign_file_at_the_destination(self):
        """`verified` + "a file exists there" was accepted as a finished rename.

        The sibling branch hashes the staging partial before trusting it; this
        one checked existence only, so any unrelated file sitting at the final
        path authorised deleting the source.
        """
        source = write_file(self.desktop / "photos.zip", b"irreplaceable archive" * 300)
        expected = sha256_file(source)
        planned = self._planned(source)
        from tools.ingest.identity import asset_id_for

        asset_id = asset_id_for(SOURCE_VOLUME_ID, planned.rel_path, expected)
        final_path = self.resolved_t7.inbox_dir / "desktop" / "photos.zip"

        from tools.ingest.transaction import TransferFaultHooks

        def stop_before_rename() -> None:
            raise RuntimeError("crash between verify and finalize")

        with self.assertRaises(RuntimeError):
            self._transfer(planned, hooks=TransferFaultHooks(before_finalize_rename=stop_before_rename))

        self.assertEqual(self.catalog.get_asset(asset_id).row["transfer_state"], STATE_VERIFIED)
        # The staging partial is lost, and something unrelated occupies the path.
        for partial in self.resolved_t7.staging_dir.iterdir():
            partial.unlink()
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(b"an unrelated file that happens to be here")

        resumed = self._transfer(planned)
        self.assertSourceOnlyGoneOnceDestinationProvesIt(source, expected, resumed)
        self.assertNotEqual(
            final_path.read_bytes(), b"an unrelated file that happens to be here",
            "the run reported success while the destination still held the foreign file",
        )


class DevStorageRootTests(unittest.TestCase):
    """`storage_root_override` disables UUID verification, so it must not be
    reachable from a plain production invocation (invariant 2)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.desktop = self.root / "Desktop"
        self.t7 = self.root / "T7"
        self.t7.mkdir()
        self.config = build_config(desktop=self.desktop, t7_root=self.t7, settle_seconds=1)

    def test_apply_refuses_a_dev_storage_root_without_the_explicit_flag(self):
        source = write_file(self.desktop / "keepme.txt", b"user data")
        args = _Args("desktop", apply=True)
        args.allow_dev_storage_root = False

        self.assertEqual(cmd_organize(args, prober=lambda: [], config=self.config), 1)
        self.assertTrue(source.exists(), "a refused run must not touch a source file")


if __name__ == "__main__":
    unittest.main()


class SourceIdentityTests(unittest.TestCase):
    """A reused pathname must create a new content-aware asset safely.

    The path is provenance, not identity. If `invoice.pdf` is replaced by
    different bytes, the new file may be ingested and removed only after its
    *own* destination is verified; the previously archived asset must remain
    untouched.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.desktop = self.root / "Desktop"
        self.t7 = self.root / "T7"
        self.t7.mkdir()
        self.config = build_config(desktop=self.desktop, t7_root=self.t7, settle_seconds=1)
        self.resolved_t7 = make_resolved_t7(self.config)
        self.catalog = Catalog(self.resolved_t7.catalog_path)
        self.addCleanup(self.catalog.close)

    def _planned(self, source: Path) -> PlannedTransfer:
        return PlannedTransfer(
            source_path=source, rel_path=source.name,
            source_volume_id=SOURCE_VOLUME_ID, source_class="desktop",
        )

    def _transfer(self, planned, **kwargs):
        return transfer_file(self.catalog, planned, self.resolved_t7, settle_seconds=1, **kwargs)

    def _commit_without_unlinking(self, path: Path):
        from tools.ingest.transaction import TransferFaultHooks

        outcome = self._transfer(
            self._planned(path),
            hooks=TransferFaultHooks(before_unlink=lambda: (_ for _ in ()).throw(OSError("locked"))),
        )
        self.assertEqual(outcome.state, STATE_COMMITTED)
        self.assertTrue(path.exists())
        return outcome

    def test_a_different_file_at_a_committed_path_gets_its_own_verified_asset(self):
        """A failed unlink on generation one must not block generation two."""
        first = write_file(self.desktop / "invoice.pdf", b"the real invoice" * 200)
        first_hash = sha256_file(first)
        first_outcome = self._commit_without_unlinking(first)
        first_final = Path(first_outcome.final_path)

        first.unlink()  # the owner dealt with it themselves
        second = write_file(self.desktop / "invoice.pdf", b"an entirely different document" * 200)
        second_hash = sha256_file(second)

        second_outcome = self._transfer(self._planned(second))

        self.assertTrue(second_outcome.success)
        self.assertEqual(second_outcome.state, STATE_SOURCE_UNLINKED)
        self.assertFalse(second.exists())
        self.assertNotEqual(second_outcome.asset_id, first_outcome.asset_id)
        self.assertNotEqual(Path(second_outcome.final_path), first_final)
        self.assertEqual(sha256_file(first_final), first_hash)
        self.assertEqual(sha256_file(Path(second_outcome.final_path)), second_hash)

    def test_the_archived_file_is_not_overwritten_by_the_new_generation(self):
        """Generation two gets a collision-safe destination; generation one stays immutable."""
        first = write_file(self.desktop / "report.pdf", b"the archived report" * 200)
        archived = sha256_file(first)
        first_outcome = self._commit_without_unlinking(first)
        first_final = Path(first_outcome.final_path)

        first.unlink()
        second = write_file(self.desktop / "report.pdf", b"a newer unrelated report" * 200)
        second_hash = sha256_file(second)
        second_outcome = self._transfer(self._planned(second))

        self.assertTrue(second_outcome.success)
        self.assertEqual(second_outcome.state, STATE_SOURCE_UNLINKED)
        self.assertFalse(second.exists())
        self.assertEqual(sha256_file(first_final), archived, "the archived asset must survive untouched")
        self.assertNotEqual(Path(second_outcome.final_path), first_final)
        self.assertEqual(sha256_file(Path(second_outcome.final_path)), second_hash)

    def test_a_new_file_at_a_completed_path_is_ingested_not_silently_skipped(self):
        """A completed generation must not make later bytes at that path invisible."""
        first = write_file(self.desktop / "scan.pdf", b"the first scan" * 200)
        first_hash = sha256_file(first)
        first_outcome = self._transfer(self._planned(first))
        self.assertEqual(first_outcome.state, STATE_SOURCE_UNLINKED)
        self.assertFalse(first.exists())

        second = write_file(self.desktop / "scan.pdf", b"a different scan entirely" * 200)
        second_hash = sha256_file(second)
        second_outcome = self._transfer(self._planned(second))

        self.assertTrue(second_outcome.success)
        self.assertEqual(second_outcome.state, STATE_SOURCE_UNLINKED)
        self.assertFalse(second.exists())
        self.assertNotEqual(second_outcome.asset_id, first_outcome.asset_id)
        self.assertNotEqual(Path(second_outcome.final_path), Path(first_outcome.final_path))
        self.assertEqual(sha256_file(Path(first_outcome.final_path)), first_hash)
        self.assertEqual(sha256_file(Path(second_outcome.final_path)), second_hash)

    def test_the_same_file_reappearing_unchanged_still_resumes_normally(self):
        """The guard must not break the ordinary retry it sits in front of."""
        source = write_file(self.desktop / "photo.jpg", b"unchanged bytes" * 200)
        self._commit_without_unlinking(source)

        outcome = self._transfer(self._planned(source))

        self.assertEqual(outcome.state, STATE_SOURCE_UNLINKED)
        self.assertTrue(outcome.success)
        self.assertFalse(source.exists(), "the genuine resume must still complete")


class SourceIdentityAtUnlinkTests(ResumeVerificationTests):
    """`_finish_unlink` is the only function that removes a user's file, so it
    re-proves its own precondition rather than trusting the caller.

    Both findings here came from an adversarial review of this branch. Neither
    needs a crash: one failed unlink is enough to set up the first, and a
    writer that preserves mtime is enough for the second.
    """

    def test_a_verified_resume_will_not_delete_a_different_file_at_the_same_path(self):
        """The `verified` branch verified the staging partial and the
        destination, and nothing at all about the source."""
        from tools.ingest.transaction import TransferFaultHooks

        first = write_file(self.desktop / "invoice.pdf", b"FIRST invoice" * 500)
        planned = self._planned(first)

        with self.assertRaises(RuntimeError):
            self._transfer(
                planned,
                hooks=TransferFaultHooks(
                    before_finalize_rename=lambda: (_ for _ in ()).throw(RuntimeError("crash after verify"))
                ),
            )
        from tools.ingest.identity import asset_id_for

        first_hash = sha256_file(first)
        asset_id = asset_id_for(SOURCE_VOLUME_ID, "invoice.pdf", first_hash)
        first_asset = self.catalog.get_asset(asset_id)
        self.assertEqual(first_asset.row["transfer_state"], STATE_VERIFIED)
        first_claimed_final = Path(first_asset.row["final_path"])

        # The owner deals with that file; later an unrelated invoice takes the name.
        first.unlink()
        second = write_file(self.desktop / "invoice.pdf", b"SECOND, entirely different invoice" * 500)
        second_hash = sha256_file(second)

        ingested = self._transfer(self._planned(second))

        self.assertTrue(ingested.success)
        self.assertEqual(ingested.state, STATE_SOURCE_UNLINKED)
        self.assertFalse(second.exists())
        self.assertNotEqual(ingested.asset_id, asset_id)
        self.assertNotEqual(Path(ingested.final_path), first_claimed_final)
        self.assertEqual(sha256_file(Path(ingested.final_path)), second_hash)
        self.assertEqual(self.catalog.get_asset(asset_id).row["transfer_state"], STATE_VERIFIED)

    def test_a_source_that_grows_after_it_was_read_is_not_deleted(self):
        """A writer that preserves mtime -- `curl -R`, `rsync -a`, a sync client
        -- makes a half-written file look settled. Both hashes then agree on
        the truncated prefix, and the completed file would be unlinked."""
        import os

        source = write_file(self.desktop / "big-download.zip", b"first chunk" * 100)

        def resume_the_download() -> None:
            stat = source.stat()
            with open(source, "ab") as handle:
                handle.write(b"the rest of the download" * 500)
            # The writer preserves the original timestamp, as several do.
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        from tools.ingest.transaction import TransferFaultHooks

        # The window is between reading the bytes and deleting the source, so
        # the writer resumes there rather than at the unlink itself.
        outcome = self._transfer(
            self._planned(source), hooks=TransferFaultHooks(before_final_commit=resume_the_download)
        )

        self.assertTrue(source.exists(), "the completed download must survive")
        self.assertNotEqual(outcome.state, STATE_SOURCE_UNLINKED)
        self.assertIn("changed after it was read", (outcome.error_message or ""))

    def test_an_unchanged_source_is_still_deleted_normally(self):
        """The guard must not break the ordinary successful case."""
        source = write_file(self.desktop / "ordinary-report.pdf", b"content" * 500)
        outcome = self._transfer(self._planned(source))
        self.assertEqual(outcome.state, STATE_SOURCE_UNLINKED)
        self.assertFalse(source.exists())
        self.assertTrue(Path(outcome.final_path).exists())
