"""Screenshots, Downloads triage, and the pipeline that joins them.

The requirements these encode, in one sentence each:

* A screenshot gets a useful name whether or not it is ever archived.
* `ignore` never means "delete from the Mac".
* An unattended run waits the watcher's settle window, not the manual one.
* Nothing reaches the transfer primitive that policy did not approve, and
  nothing reaches it at all while Angel is absent.
"""

from __future__ import annotations

import dataclasses
import os
import json
import tempfile
import threading
import unittest
from pathlib import Path

from tools.ingest.automation import RunLockBusy, RunPaths, run_lock
from tools.ingest.catalog import Catalog
from tools.ingest.cli import cmd_decide, cmd_organize
from tools.ingest.ledger import (
    DECISION_KEPT,
    DECISION_RENAMED,
    RETENTION_IGNORE,
    RETENTION_INGEST,
    RETENTION_REVIEW,
    LocalLedger,
)
from tools.ingest.naming.proposer import NameProposal
from tools.ingest.pipeline import run_pipeline, run_source
from tools.ingest.provenance import read_local_provenance
from tools.ingest.sources import ScreenshotsNotConfigured, resolve_screenshots_root
from tools.ingest.t7 import resolve_t7

from tests.ingest.fi_helpers import (
    LOCAL_VOLUME_ID,
    aged,
    fi_config,
    fresh,
    make_blob,
    make_camera_jpeg,
    make_png,
    make_zip,
)


class _OrganizeArgs:
    """The argparse namespace `cmd_organize` consumes."""

    def __init__(self, target: str, *, apply: bool):
        self.target = target
        self.apply = apply
        self.dry_run = not apply
        self.config = None
        self.allow_dev_storage_root = True


class _DecideArgs:
    """The argparse namespace `cmd_decide` consumes."""

    def __init__(self, command: str, path: Path, reason: str = ""):
        self.command = command
        self.path = path
        self.reason = reason
        self.config = None
        self.allow_dev_storage_root = True


class _TitleProposer:
    """Stands in for a proposer that can read a document's real title.

    The deterministic proposer needs `pypdf` to get a PDF title, and that is an
    optional dependency -- so the *pipeline* behavior under test here is pinned
    to a fixed proposal rather than to whether the optional probe is installed.
    """

    model = "test-title-proposer"
    model_version = "1"

    def __init__(self, filename: str):
        self._filename = filename

    def propose(self, evidence):
        return NameProposal(
            proposed_filename=self._filename,
            confidence=0.9,
            model=self.model,
            model_version=self.model_version,
            reason="document title",
        )


class FileIntelligenceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.downloads = self.root / "Downloads"
        self.screenshots = self.root / "Screenshots"
        self.t7 = self.root / "T7"
        for directory in (self.downloads, self.screenshots, self.t7):
            directory.mkdir()
        self.config = fi_config(
            self.root, downloads=self.downloads, screenshots=self.screenshots, t7_root=self.t7
        )

    def resolved_t7(self):
        return resolve_t7(self.config, prober=lambda: [], for_apply=True, allow_storage_root_override=True)

    def run_pipeline(self, *, targets=("downloads", "screenshots"), apply=True, automated=False, with_t7=False, config=None):
        config = config or self.config
        resolved = self.resolved_t7() if with_t7 else None
        catalog = Catalog(resolved.catalog_path) if resolved is not None else None
        try:
            with LocalLedger(config.local_ledger_path) as ledger:
                return run_pipeline(
                    config,
                    targets=list(targets),
                    apply=apply,
                    automated=automated,
                    run_id="test-run",
                    ledger=ledger,
                    catalog=catalog,
                    resolved_t7=resolved,
                )
        finally:
            if catalog is not None:
                catalog.close()

    def source(self, report, source_class):
        return next(s for s in report.sources if s.source_class == source_class)

    def outcome_for(self, report, source_class, filename):
        for outcome in self.source(report, source_class).outcomes:
            if outcome.original_filename == filename:
                return outcome
        return None


class ScreenshotSourceTests(FileIntelligenceTestCase):
    def test_a_configured_screenshots_folder_is_discovered(self):
        make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png")
        report = self.run_pipeline(targets=("screenshots",), apply=False)
        self.assertEqual(self.source(report, "screenshots").discovered, 1)

    def test_an_unconfigured_screenshots_source_fails_safely(self):
        """Never a fallback to somewhere plausible -- the macOS default is the
        Desktop, and scanning it by accident is the failure to avoid."""
        config = dataclasses.replace(
            self.config,
            sources=dataclasses.replace(
                self.config.sources,
                screenshots=dataclasses.replace(self.config.sources.screenshots, path=None, enabled=False),
            ),
        )
        with self.assertRaises(ScreenshotsNotConfigured):
            resolve_screenshots_root(config)

        report = self.run_pipeline(targets=("screenshots",), apply=True, config=config)
        source = self.source(report, "screenshots")
        self.assertFalse(source.available)
        self.assertEqual(source.discovered, 0)

    def test_a_screenshots_path_that_does_not_exist_fails_safely(self):
        config = dataclasses.replace(
            self.config,
            sources=dataclasses.replace(
                self.config.sources,
                screenshots=dataclasses.replace(self.config.sources.screenshots, path=self.root / "nope"),
            ),
        )
        report = self.run_pipeline(targets=("screenshots",), apply=True, config=config)
        self.assertFalse(self.source(report, "screenshots").available)

    def test_an_unsettled_screenshot_is_skipped_for_now(self):
        path = make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", settled=False)
        fresh(path)
        report = self.run_pipeline(targets=("screenshots",), apply=True)
        source = self.source(report, "screenshots")
        self.assertEqual(source.evaluated, 0)
        self.assertTrue(path.exists(), "an unsettled file is skipped, never touched")

    def test_every_settled_screenshot_is_named_even_when_it_is_not_archived(self):
        """Invariants 4 and 5, together: renamed, and still on the Mac."""
        path = make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(2560, 1440))
        report = self.run_pipeline(targets=("screenshots",), apply=True, with_t7=True)
        outcome = self.outcome_for(report, "screenshots", path.name)

        self.assertEqual(outcome.naming_decision, DECISION_RENAMED)
        self.assertNotEqual(outcome.retention, RETENTION_INGEST)
        self.assertFalse(outcome.ingested)
        self.assertFalse(path.exists(), "the old name is gone")
        renamed = next(p for p in self.screenshots.iterdir())
        self.assertTrue(renamed.is_file(), "the renamed screenshot is still on the Mac")

    def test_an_ignored_screenshot_is_not_deleted(self):
        """Invariant 9: ignore-for-Angel is not delete-from-Mac."""
        tiny = make_png(self.screenshots / "Screenshot 2026-09-12 at 09.00.00 AM.png", size=(8, 8))
        report = self.run_pipeline(targets=("screenshots",), apply=True, with_t7=True)
        outcome = self.outcome_for(report, "screenshots", tiny.name)
        self.assertEqual(outcome.retention, RETENTION_IGNORE)
        survivors = list(self.screenshots.iterdir())
        self.assertEqual(len(survivors), 1, "an ignored screenshot must stay on disk")

    def test_a_screenshots_folder_shared_with_the_desktop_only_sees_screenshots(self):
        """Pointing this at the macOS default must not put the Desktop under
        automatic evaluation."""
        desktop = self.root / "Desktop"
        desktop.mkdir(exist_ok=True)
        config = dataclasses.replace(
            self.config,
            sources=dataclasses.replace(
                self.config.sources,
                screenshots=dataclasses.replace(self.config.sources.screenshots, path=desktop),
            ),
        )
        make_png(desktop / "Screenshot 2026-09-12 at 10.14.32 AM.png")
        private = make_camera_jpeg(desktop / "IMG_7777.JPG")
        tax = make_blob(desktop / "IMG_8888.JPG")

        report = self.run_pipeline(targets=("screenshots",), apply=True, config=config)

        self.assertEqual(self.source(report, "screenshots").discovered, 1)
        self.assertTrue(private.exists(), "a Desktop photo must not be renamed by the screenshots source")
        self.assertTrue(tax.exists())


class DownloadsTriageTests(FileIntelligenceTestCase):
    def test_an_installer_is_ignored_without_being_moved_or_renamed(self):
        installer = make_blob(self.downloads / "GoogleChrome.dmg", b"D" * 300_000)
        report = self.run_pipeline(targets=("downloads",), apply=True, with_t7=True)
        outcome = self.outcome_for(report, "downloads", "GoogleChrome.dmg")

        self.assertEqual(outcome.retention, RETENTION_IGNORE)
        self.assertFalse(outcome.ingested)
        self.assertTrue(installer.exists(), "an ignored installer stays exactly where it is")

    def test_a_partial_download_is_never_touched(self):
        partial = self.downloads / "big-file.crdownload"
        partial.write_bytes(b"half a file")
        report = self.run_pipeline(targets=("downloads",), apply=True, with_t7=True)
        self.assertEqual(self.source(report, "downloads").evaluated, 0)
        self.assertTrue(partial.exists())

    def test_a_candidate_can_be_renamed_without_being_ingested(self):
        """Naming and retention are answered separately (invariant 4)."""
        make_zip(self.downloads / "download (4).zip", ["fine-line-kitchen-renders/a.png", "fine-line-kitchen-renders/b.png"])
        report = self.run_pipeline(targets=("downloads",), apply=True, with_t7=True)
        outcome = self.outcome_for(report, "downloads", "download (4).zip")

        self.assertEqual(outcome.naming_decision, DECISION_RENAMED)
        self.assertEqual(outcome.retention, RETENTION_REVIEW)
        self.assertFalse(outcome.ingested)
        self.assertIn("fine-line-kitchen-renders", outcome.applied_filename)

    def test_a_useful_existing_filename_survives_triage(self):
        document = make_blob(self.downloads / "FineLine_signed_contract.pdf", b"%PDF-1.4 " + b"z" * 60_000)
        report = self.run_pipeline(targets=("downloads",), apply=True, with_t7=True)
        outcome = self.outcome_for(report, "downloads", "FineLine_signed_contract.pdf")

        self.assertEqual(outcome.naming_decision, DECISION_KEPT)
        self.assertEqual(outcome.retention, RETENTION_INGEST)
        self.assertTrue(document.exists(), "auto_ingest is off, so an ingest candidate still stays put")

    def test_being_old_does_not_by_itself_mean_it_belongs_in_angel(self):
        """Invariant 6. Settling makes a file safe to touch, not worth keeping."""
        make_blob(self.downloads / "GoogleChrome.dmg", b"D" * 300_000)
        make_png(self.downloads / "IMG_6909.JPG")
        report = self.run_pipeline(targets=("downloads",), apply=True, with_t7=True)
        self.assertEqual(self.source(report, "downloads").ingested, 0)
        self.assertEqual(len(list(self.downloads.iterdir())), 2)

    def test_an_automated_run_uses_the_watcher_settle_window(self):
        """A 20-minute unattended window, never the 30-second manual one."""
        self.assertEqual(self.config.effective_settle_seconds(automated=False), 1)
        self.assertEqual(
            self.config.effective_settle_seconds(automated=True),
            self.config.downloads_watcher_min_settle_seconds,
        )
        report = self.run_pipeline(targets=("downloads",), apply=True, automated=True)
        self.assertEqual(report.settle_seconds, 1200)

    def test_lowering_settle_seconds_cannot_shorten_the_unattended_window(self):
        config = dataclasses.replace(self.config, settle_seconds=1, downloads_watcher_min_settle_seconds=1200)
        self.assertEqual(config.effective_settle_seconds(automated=True), 1200)
        generous = dataclasses.replace(config, settle_seconds=3600)
        self.assertEqual(generous.effective_settle_seconds(automated=True), 3600)

    def test_a_recently_downloaded_file_is_skipped_by_an_automated_run(self):
        recent = make_blob(self.downloads / "still-downloading-report.pdf", b"%PDF" + b"z" * 1000)
        fresh(recent)
        report = self.run_pipeline(targets=("downloads",), apply=True, automated=True)
        self.assertEqual(self.source(report, "downloads").evaluated, 0)
        self.assertTrue(recent.exists())


class AngelAbsentTests(FileIntelligenceTestCase):
    def test_naming_still_works_with_angel_disconnected(self):
        """The reason the naming ledger lives on the Mac (D-021)."""
        make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(2560, 1440))
        report = self.run_pipeline(targets=("screenshots",), apply=True, with_t7=False)
        self.assertFalse(report.t7_connected)
        self.assertEqual(self.source(report, "screenshots").renamed, 1)

    def test_no_source_file_is_removed_while_angel_is_absent(self):
        """Invariant: T7 missing means no source deletion, full stop."""
        config = dataclasses.replace(
            self.config, triage=dataclasses.replace(self.config.triage, auto_ingest=True, min_ingest_confidence=0.1)
        )
        document = make_blob(self.downloads / "FineLine_signed_contract.pdf", b"%PDF-1.4 " + b"z" * 60_000)
        report = self.run_pipeline(targets=("downloads",), apply=True, with_t7=False, config=config)

        outcome = self.outcome_for(report, "downloads", "FineLine_signed_contract.pdf")
        self.assertFalse(outcome.ingested)
        self.assertTrue(document.exists(), "no source file may be removed with Angel unavailable")


class AutoIngestTests(FileIntelligenceTestCase):
    def test_auto_ingest_off_means_nothing_moves(self):
        make_blob(self.downloads / "FineLine_signed_contract.pdf", b"%PDF-1.4 " + b"z" * 60_000)
        report = self.run_pipeline(targets=("downloads",), apply=True, with_t7=True)
        self.assertEqual(self.source(report, "downloads").ingested, 0)

    def test_auto_ingest_on_routes_through_the_verified_transfer_primitive(self):
        config = dataclasses.replace(
            self.config, triage=dataclasses.replace(self.config.triage, auto_ingest=True, min_ingest_confidence=0.5)
        )
        document = make_blob(self.downloads / "FineLine_signed_contract.pdf", b"%PDF-1.4 " + b"z" * 60_000)
        expected = document.read_bytes()

        report = self.run_pipeline(targets=("downloads",), apply=True, with_t7=True, config=config)
        outcome = self.outcome_for(report, "downloads", "FineLine_signed_contract.pdf")

        self.assertTrue(outcome.ingested)
        self.assertFalse(document.exists(), "the source is removed only after verified transfer")
        landed = list((self.resolved_t7().inbox_dir / "downloads").iterdir())
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0].read_bytes(), expected)

    def test_triage_judges_the_name_the_owner_gave_it_not_the_one_we_generated(self):
        """A deliberate asymmetry, worth stating explicitly.

        Name quality is evidence about value precisely because a person chose
        to type it. Re-reading it after *we* improved it would be circular --
        the pipeline would bootstrap its own files into the library by renaming
        them. So triage sees the filename as it was found.
        """
        config = dataclasses.replace(
            self.config, triage=dataclasses.replace(self.config.triage, auto_ingest=True, min_ingest_confidence=0.5)
        )
        badly_named = make_blob(self.downloads / "download (7).pdf", b"%PDF-1.4 " + b"z" * 60_000)

        resolved = self.resolved_t7()
        catalog = Catalog(resolved.catalog_path)
        self.addCleanup(catalog.close)
        with LocalLedger(config.local_ledger_path) as ledger:
            report = run_source(
                config, ledger,
                source_class="downloads",
                source_volume_id="mac-local",
                apply=True,
                settle_seconds=1,
                resolved_t7=resolved,
                catalog=catalog,
                proposer=_TitleProposer("2026-09-12_fine-line-group-scope-of-work"),
            )

        outcome = next(o for o in report.outcomes if o.original_filename == "download (7).pdf")
        self.assertEqual(outcome.naming_decision, DECISION_RENAMED)
        self.assertEqual(outcome.retention, RETENTION_REVIEW)
        self.assertFalse(outcome.ingested)
        self.assertFalse(badly_named.exists())
        self.assertTrue((self.downloads / "2026-09-12_fine-line-group-scope-of-work.pdf").exists())


class ProvenanceAcrossCommandsTests(FileIntelligenceTestCase):
    """Invariant 11, over the route a renamed file actually takes to Angel.

    `organize triage` renames it and leaves it in Downloads; `organize
    downloads --apply` ingests it later. By then the name the owner recognises
    exists nowhere on the filesystem, so the catalog has to carry it.
    """

    def test_the_original_name_survives_a_rename_then_a_manual_ingest(self):
        source = make_camera_jpeg(self.downloads / "IMG_6909.JPG")

        with LocalLedger(self.config.local_ledger_path) as ledger:
            report = run_source(
                self.config, ledger,
                source_class="downloads",
                source_volume_id="mac-local",
                apply=True,
                settle_seconds=1,
                resolved_t7=None,
            )
        outcome = report.outcomes[0]
        self.assertEqual(outcome.naming_decision, DECISION_RENAMED)
        renamed = outcome.path
        self.assertFalse(source.exists())
        self.assertTrue(renamed.exists())

        args = _OrganizeArgs("downloads", apply=True)
        self.assertEqual(cmd_organize(args, prober=lambda: [], config=self.config), 0)

        self.assertFalse(renamed.exists(), "the manual ingest removed the source after verifying it")
        with Catalog(self.resolved_t7().catalog_path) as catalog:
            rows = catalog._conn.execute("SELECT * FROM assets WHERE final_path IS NOT NULL").fetchall()
            self.assertEqual(len(rows), 1)
            asset_id = rows[0]["asset_id"]
            self.assertEqual(rows[0]["original_filename"], renamed.name)
            self.assertEqual(
                rows[0]["first_seen_filename"], "IMG_6909.JPG",
                "the name the owner remembers must survive into Angel",
            )
            history = catalog.naming_history_for_asset(asset_id)
            self.assertIn("IMG_6909.JPG", {row["original_filename"] for row in history})
            self.assertIn(renamed.name, {row["applied_filename"] for row in history if row["applied_filename"]})

            sidecar = json.loads((self.resolved_t7().sidecars_dir / f"{asset_id}.json").read_text())
        self.assertEqual(sidecar["first_seen_filename"], "IMG_6909.JPG")
        self.assertTrue(sidecar["naming_history"], "the sidecar must render the lineage, not just the catalog")

    def test_ingesting_a_file_the_pipeline_never_saw_still_works(self):
        """No ledger row is an ordinary case, not an error."""
        make_blob(self.downloads / "unseen-report.pdf", b"%PDF-1.4 " + b"z" * 1000)
        args = _OrganizeArgs("downloads", apply=True)
        self.assertEqual(cmd_organize(args, prober=lambda: [], config=self.config), 0)
        with Catalog(self.resolved_t7().catalog_path) as catalog:
            row = catalog._conn.execute("SELECT * FROM assets WHERE final_path IS NOT NULL").fetchone()
        self.assertEqual(row["original_filename"], "unseen-report.pdf")


class LocalGenerationLineageTests(FileIntelligenceTestCase):
    """Different bytes must not inherit the previous content's lineage.

    `local_file_id` is (volume, device, inode), which survives renames but not
    necessarily byte identity. `observe_file` starts a new generation on a
    verified hash change, and the lineage accessors have to agree because
    `provenance.attach_local_provenance` copies whatever `naming_history`
    returns onto the asset's durable record in Angel. A record that asserts a
    filename the file never had is worse than no record at all.
    """

    def _observe(self, ledger, path, *, identity, sha256, now):
        return ledger.observe_file(
            path=path,
            source_class="downloads",
            source_volume_id=LOCAL_VOLUME_ID,
            device=identity.st_dev,
            inode=identity.st_ino,
            size=path.stat().st_size,
            sha256=sha256,
            mime="application/octet-stream",
            media_kind="document",
            now=now,
        )

    def test_a_reused_inode_inherits_nothing_from_the_file_before_it(self):
        # The identity both files share is the *stranger's* real (device,
        # inode), so `read_local_provenance` -- which stats the file on disk --
        # reaches the same row the owner's file was recorded under. That is
        # what inode reuse looks like from this system's side.
        stranger = make_blob(self.downloads / "IMG_0001.JPG", b"bytes from somewhere else" * 200)
        identity = stranger.stat()
        owned = make_blob(self.downloads / "tax-return-2025-signed.pdf", b"the tax return" * 200)

        with LocalLedger(self.config.local_ledger_path) as ledger:
            first = self._observe(ledger, owned, identity=identity, sha256="a" * 64, now="2026-09-01T00:00:00+00:00")
            ledger.record_human_name(
                first.local_file_id,
                filename=owned.name,
                directory=str(self.downloads),
                now="2026-09-01T00:00:00+00:00",
            )
            ledger.append_classification(
                {
                    "local_file_id": first.local_file_id,
                    "source_volume_id": LOCAL_VOLUME_ID,
                    "source_class": "downloads",
                    "recommendation": RETENTION_INGEST,
                    "reason": "the owner said keep",
                    "policy_outcome": "human_authority",
                    "human_corrected": 1,
                    "created_at": "2026-09-01T00:00:00+00:00",
                }
            )
            self.assertTrue(ledger.has_human_naming(first.local_file_id))

            # The owner deletes their file; the inode is freed and reused.
            owned.unlink()
            second = self._observe(
                ledger, stranger, identity=identity, sha256="b" * 64, now="2026-09-02T00:00:00+00:00"
            )
            self.assertTrue(second.is_new_generation, "this test is meaningless unless the reset fires")
            self.assertEqual(second.local_file_id, first.local_file_id)

            self.assertFalse(
                ledger.has_human_naming(second.local_file_id),
                "a stranger's file must not inherit a human naming lock",
            )
            self.assertEqual(ledger.known_names(second.local_file_id), set())
            self.assertEqual(ledger.naming_history(second.local_file_id), [])
            self.assertEqual(ledger.classification_runs(second.local_file_id), [])

            # The superseded rows are real history of a real file. The bug was
            # attributing them to the new file, not keeping them.
            kept = ledger._conn.execute("SELECT COUNT(*) FROM naming_history").fetchone()[0]
            self.assertEqual(kept, 1, "the previous file's lineage must still be on record")

            provenance = read_local_provenance(ledger, stranger)

        self.assertIsNotNone(provenance)
        self.assertEqual(
            provenance.naming_history, [],
            "Angel's durable record would have asserted a filename this file never had",
        )
        self.assertEqual(provenance.classification_runs, [])
        self.assertEqual(provenance.first_seen_filename, "IMG_0001.JPG")

    def test_changed_bytes_at_the_same_inode_and_path_start_a_new_generation(self):
        """An in-place save and an unrelated overwrite are indistinguishable.

        The safe choice is to keep the filename on disk but not let different
        bytes inherit model or human decisions made about the prior content.
        """
        owned = make_blob(self.downloads / "notes.md", b"first draft" * 100)
        identity = owned.stat()
        with LocalLedger(self.config.local_ledger_path) as ledger:
            first = self._observe(
                ledger, owned, identity=identity, sha256="a" * 64,
                now="2026-09-01T00:00:00+00:00",
            )
            ledger.record_human_name(
                first.local_file_id,
                filename=owned.name,
                directory=str(self.downloads),
                now="2026-09-01T00:00:00+00:00",
            )
            ledger.append_classification(
                {
                    "local_file_id": first.local_file_id,
                    "source_volume_id": LOCAL_VOLUME_ID,
                    "source_class": "downloads",
                    "recommendation": RETENTION_INGEST,
                    "reason": "decision about the first draft",
                    "policy_outcome": "human_authority",
                    "human_corrected": 1,
                    "created_at": "2026-09-01T00:00:00+00:00",
                }
            )

            owned.write_bytes(b"completely different bytes" * 100)
            again = self._observe(
                ledger, owned, identity=identity, sha256="c" * 64,
                now="2026-09-03T00:00:00+00:00",
            )

            self.assertTrue(again.is_new_generation)
            self.assertEqual(again.local_file_id, first.local_file_id)
            self.assertEqual(ledger.get_local_file(again.local_file_id)["generation"], 1)
            self.assertTrue(
                ledger.has_human_naming(again.local_file_id),
                "explicit owner naming authority should survive an in-place content generation",
            )
            current_history = ledger.naming_history(again.local_file_id)
            self.assertEqual(len(current_history), 1)
            self.assertEqual(current_history[0]["approved_by"], "human")
            self.assertEqual(current_history[0]["applied_filename"], "notes.md")
            self.assertEqual(
                ledger.classification_runs(again.local_file_id), [],
                "retention authority is content-specific and must not carry forward",
            )
            self.assertEqual(owned.name, "notes.md", "generation reset must never rename by itself")

            # The earlier naming decision remains audit history, alongside the
            # explicit carried-forward lock for the new generation.
            self.assertEqual(
                ledger._conn.execute(
                    "SELECT COUNT(*) FROM naming_history WHERE local_file_id = ?",
                    (first.local_file_id,),
                ).fetchone()[0],
                2,
            )

    def test_unchanged_bytes_do_not_start_a_new_generation(self):
        owned = make_blob(self.downloads / "stable.md", b"same bytes" * 100)
        identity = owned.stat()
        with LocalLedger(self.config.local_ledger_path) as ledger:
            first = self._observe(
                ledger, owned, identity=identity, sha256="a" * 64,
                now="2026-09-01T00:00:00+00:00",
            )
            again = self._observe(
                ledger, owned, identity=identity, sha256="a" * 64,
                now="2026-09-03T00:00:00+00:00",
            )
            self.assertFalse(again.is_new_generation)
            self.assertEqual(
                ledger.get_local_file(first.local_file_id)["generation"], 0
            )


class ConcurrencyTests(FileIntelligenceTestCase):
    def test_two_runs_cannot_overlap(self):
        paths = RunPaths.for_state_dir(self.config.state_dir)
        started = threading.Event()
        release = threading.Event()
        second: list[object] = []

        def hold():
            with run_lock(paths):
                started.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join, 5)
        try:
            self.assertTrue(started.wait(5))
            try:
                with run_lock(paths):
                    second.append("acquired")
            except RunLockBusy as exc:
                second.append(exc)
        finally:
            release.set()

        self.assertIsInstance(second[0], RunLockBusy, "a second concurrent run must be refused, not queued")


if __name__ == "__main__":
    unittest.main()


class OwnerDecisionTests(FileIntelligenceTestCase):
    """`review` has to be actionable, or it is just a list nobody can clear.

    The owner's decision is the semantic layer this phase deliberately does not
    build: a person can tell a receipt from a meme, and `keep` is how they say
    so. It outranks the model's recommendation permanently, and it works with
    `auto_ingest` off -- that switch governs what the *system* does unattended.
    """

    def _ledger_id(self, path: Path) -> str:
        from tools.ingest.identity import local_file_id_for

        stat = path.stat()
        return local_file_id_for("mac-local", stat.st_dev, stat.st_ino)

    def test_keeping_a_screenshot_ingests_it_without_enabling_auto_ingest(self):
        shot = make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(2560, 1440))
        first = self.run_pipeline(targets=("screenshots",), apply=True, with_t7=True)
        outcome = first.sources[0].outcomes[0]
        self.assertEqual(outcome.retention, RETENTION_REVIEW)
        self.assertFalse(outcome.ingested)

        renamed = outcome.path
        expected = renamed.read_bytes()
        args = _DecideArgs("keep", renamed)
        self.assertEqual(cmd_decide(args, config=self.config), 0)

        self.assertFalse(self.config.triage.auto_ingest)
        second = self.run_pipeline(targets=("screenshots",), apply=True, with_t7=True)
        kept = second.sources[0].outcomes[0]
        self.assertEqual(kept.retention, RETENTION_INGEST)
        self.assertTrue(kept.ingested)
        self.assertFalse(renamed.exists(), "the source is removed only after verified transfer")

        landed = list((self.resolved_t7().inbox_dir / "screenshots").iterdir())
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0].read_bytes(), expected)
        self.assertEqual(landed[0].name, renamed.name)
        self.assertFalse(shot.exists())

    def test_ignoring_a_file_never_deletes_it(self):
        """Invariant 9, stated by the owner rather than inferred."""
        document = make_blob(self.downloads / "FineLine_signed_contract.pdf", b"%PDF-1.4 " + b"z" * 60_000)
        config = dataclasses.replace(
            self.config, triage=dataclasses.replace(self.config.triage, auto_ingest=True, min_ingest_confidence=0.5)
        )
        self.run_pipeline(targets=("downloads",), apply=False, config=config)

        self.assertEqual(cmd_decide(_DecideArgs("ignore", document), config=config), 0)
        report = self.run_pipeline(targets=("downloads",), apply=True, with_t7=True, config=config)
        outcome = report.sources[0].outcomes[0]

        self.assertEqual(outcome.retention, RETENTION_IGNORE)
        self.assertFalse(outcome.ingested)
        self.assertTrue(document.exists(), "an ignored file stays on the Mac")

    def test_an_owner_decision_outranks_every_later_model_run(self):
        document = make_blob(self.downloads / "FineLine_signed_contract.pdf", b"%PDF-1.4 " + b"z" * 60_000)
        config = dataclasses.replace(
            self.config, triage=dataclasses.replace(self.config.triage, auto_ingest=True, min_ingest_confidence=0.5)
        )
        self.run_pipeline(targets=("downloads",), apply=False, config=config)
        cmd_decide(_DecideArgs("ignore", document), config=config)

        for _ in range(3):
            report = self.run_pipeline(targets=("downloads",), apply=True, with_t7=True, config=config)
            self.assertEqual(report.sources[0].outcomes[0].retention, RETENTION_IGNORE)
            self.assertTrue(document.exists())

    def test_deciding_on_an_untracked_file_is_refused_rather_than_guessed(self):
        stray = make_blob(self.downloads / "never-scanned.pdf", b"%PDF")
        self.run_pipeline(targets=("screenshots",), apply=False)  # creates the ledger, not this row
        self.assertEqual(cmd_decide(_DecideArgs("keep", stray), config=self.config), 1)
        self.assertTrue(stray.exists())

    def test_the_decision_is_appended_with_its_reason_not_silently_overwritten(self):
        make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(2560, 1440))
        report = self.run_pipeline(targets=("screenshots",), apply=True)
        renamed = report.sources[0].outcomes[0].path
        cmd_decide(_DecideArgs("keep", renamed, reason="client feedback on the kitchen layout"), config=self.config)

        with LocalLedger(self.config.local_ledger_path) as ledger:
            runs = ledger.classification_runs(self._ledger_id(renamed))
        self.assertGreaterEqual(len(runs), 2, "the model's earlier opinion must survive alongside the owner's")
        self.assertEqual(runs[-1]["recommendation"], RETENTION_INGEST)
        self.assertEqual(runs[-1]["human_corrected"], 1)
        self.assertIn("kitchen layout", runs[-1]["reason"])
        self.assertEqual(runs[0]["human_corrected"], 0)


class RescanCostTests(FileIntelligenceTestCase):
    """An hourly agent must not read every byte of every file, every hour.

    The risk of caching is deciding against stale bytes, so these check both
    halves: an untouched file is not re-read, and a file that changed *is*.
    """

    def _run_counting_reads(self, **kwargs):
        from unittest import mock

        import tools.ingest.pipeline as pipeline_module

        reads = []
        real = pipeline_module.sha256_file

        def counting(path, *args, **kw):
            reads.append(Path(path).name)
            return real(path, *args, **kw)

        with mock.patch.object(pipeline_module, "sha256_file", counting):
            report = self.run_pipeline(**kwargs)
        return report, reads

    def test_an_untouched_file_is_not_rehashed_on_the_next_run(self):
        make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(1600, 900))
        first, first_reads = self._run_counting_reads(targets=("screenshots",), apply=True)
        self.assertEqual(len(first_reads), 1, "the first run must read the file")

        _, second_reads = self._run_counting_reads(targets=("screenshots",), apply=True)
        self.assertEqual(second_reads, [], "an unchanged file must not be read again")

    def test_a_file_that_changed_is_read_again(self):
        path = make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(1600, 900))
        report, _ = self._run_counting_reads(targets=("screenshots",), apply=True)
        renamed = report.sources[0].outcomes[0].path

        # Grow the file, then let it settle again -- a download that resumed
        # and finished. A file whose mtime is still fresh is skipped by the
        # settle check long before the cache is consulted, which would make
        # this test pass for the wrong reason.
        renamed.write_bytes(renamed.read_bytes() + b"appended bytes")
        aged(renamed)

        _, reads = self._run_counting_reads(targets=("screenshots",), apply=True)
        self.assertEqual(reads, [renamed.name], "a changed file must be read again")

    def test_same_size_rewrite_with_restored_mtime_is_read_again(self):
        """ctime must invalidate the hash cache when mtime is forged/restored."""
        path = make_blob(
            self.downloads / "client-meeting-notes.pdf",
            b"A" * 60_000,
        )
        original_stat = path.stat()
        _, first_reads = self._run_counting_reads(targets=("downloads",), apply=True)
        self.assertEqual(first_reads, [path.name])

        # Rewrite in place with the exact same size, then restore the old mtime
        # as rsync/curl-style workflows can. The bytes changed; only ctime tells
        # the cache that the old SHA-256 is stale.
        path.write_bytes(b"B" * 60_000)
        os.utime(
            path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

        _, second_reads = self._run_counting_reads(targets=("downloads",), apply=True)
        self.assertEqual(
            second_reads,
            [path.name],
            "a same-size rewrite must not inherit the previous cached hash",
        )

    def test_a_file_moved_to_a_new_path_is_read_again(self):
        path = make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(1600, 900))
        report, _ = self._run_counting_reads(targets=("screenshots",), apply=True)
        renamed = report.sources[0].outcomes[0].path

        moved = renamed.parent / "moved-by-the-owner.png"
        renamed.rename(moved)

        _, reads = self._run_counting_reads(targets=("screenshots",), apply=True)
        self.assertEqual(reads, [moved.name], "the same inode at a new path must be re-read")

    def test_the_cached_verdict_matches_a_full_read(self):
        """The fast path must not change any decision, only its cost."""
        make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(1600, 900))
        first = self.run_pipeline(targets=("screenshots",), apply=True)
        second = self.run_pipeline(targets=("screenshots",), apply=True)

        a = first.sources[0].outcomes[0]
        b = second.sources[0].outcomes[0]
        self.assertEqual(a.sha256, b.sha256)
        self.assertEqual(a.retention, b.retention)
