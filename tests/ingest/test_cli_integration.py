"""End-to-end `organize` CLI tests wiring scanner + transaction + catalog +
volume detection together, with a fully injected fake volume prober so no
test ever calls real `diskutil` or touches a real disk.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.ingest.catalog import Catalog

from tests.ingest.helpers import SD_TEST_UUID, T7_TEST_UUID, UNKNOWN_SD_UUID, build_config, make_volume, write_file


class CLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.desktop = self.root / "Desktop"
        self.documents = self.root / "Documents"
        self.downloads = self.root / "Downloads"
        self.sd_mount = self.root / "SD_CARD"
        self.t7 = self.root / "T7"
        self.t7.mkdir()

    def _run(self, config, argv, prober):
        # Every test here targets an isolated `storage_root_override` root, and
        # that override now has to be requested explicitly -- a config file on
        # its own may not disable volume-UUID verification (see
        # `test_mutation_safety.DevStorageRootTests`).
        argv = list(argv) + ["--allow-dev-storage-root"]
        from tools.ingest import cli as cli_module

        original = cli_module.load_config
        cli_module.load_config = lambda _path: config
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exit_code = cli_module.main(argv, prober=prober)
            return exit_code, buf.getvalue()
        finally:
            cli_module.load_config = original

    def test_full_apply_across_desktop_documents_downloads_and_registered_sd(self):
        write_file(self.desktop / "photo.jpg", b"a" * 1000)
        write_file(self.documents / "resume.pdf", b"b" * 2000)
        write_file(self.downloads / "installer.dmg", b"c" * 3000)
        write_file(self.sd_mount / "DCIM" / "IMG_0001.JPG", b"d" * 4000)
        # A git repo on the desktop should be inventoried, not moved.
        write_file(self.desktop / "old-repo" / "main.py", b"code")
        (self.desktop / "old-repo" / ".git").mkdir(parents=True, exist_ok=True)

        config = build_config(
            desktop=self.desktop, documents=self.documents, downloads=self.downloads,
            t7_root=self.t7, sd_volume_uuids=frozenset({SD_TEST_UUID}), settle_seconds=1,
        )
        sd_volume = make_volume(SD_TEST_UUID, self.sd_mount, removable=True, label="SD_CARD")
        prober = lambda: [sd_volume]  # noqa: E731

        dry_exit, dry_output = self._run(config, ["all", "--dry-run"], prober)
        self.assertEqual(dry_exit, 0)
        self.assertIn("Desktop: 1 eligible", dry_output)
        self.assertIn("Documents: 1 eligible", dry_output)
        self.assertIn("Downloads: 1 eligible", dry_output)
        self.assertIn("SD: 1 eligible", dry_output)
        self.assertIn("Git repositories", dry_output)
        self.assertIn("Dry run. No files changed.", dry_output)
        # Confirm zero-write: nothing landed on the T7 during dry-run.
        self.assertEqual(list(self.t7.rglob("*")), [])

        apply_exit, apply_output = self._run(config, ["all", "--apply"], prober)
        self.assertEqual(apply_exit, 0)
        self.assertIn("4/4 transferred", apply_output)
        self.assertIn("4/4 verified", apply_output)
        self.assertIn("4 organized/staged", apply_output)

        self.assertFalse((self.desktop / "photo.jpg").exists())
        self.assertFalse((self.documents / "resume.pdf").exists())
        self.assertFalse((self.downloads / "installer.dmg").exists())
        self.assertFalse((self.sd_mount / "DCIM" / "IMG_0001.JPG").exists())
        # The git repo was inventoried, never moved.
        self.assertTrue((self.desktop / "old-repo" / "main.py").exists())

        with Catalog(self.t7 / "90-system" / "catalog.sqlite") as catalog:
            row_count = catalog._conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]  # noqa: SLF001
            self.assertEqual(row_count, 4)
            repo_count = catalog._conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]  # noqa: SLF001
            self.assertEqual(repo_count, 1)

    def test_unknown_sd_card_is_ignored_end_to_end(self):
        write_file(self.sd_mount / "DCIM" / "IMG_0001.JPG", b"d" * 100)
        config = build_config(desktop=self.desktop, t7_root=self.t7, sd_volume_uuids=frozenset({SD_TEST_UUID}))
        unknown_volume = make_volume(UNKNOWN_SD_UUID, self.sd_mount, removable=True, label="SD_CARD")
        prober = lambda: [unknown_volume]  # noqa: E731

        exit_code, output = self._run(config, ["sd", "--dry-run"], prober)
        self.assertEqual(exit_code, 0)
        self.assertIn("SD: no registered/mounted card detected", output)

        apply_exit, apply_output = self._run(config, ["sd", "--apply"], prober)
        self.assertEqual(apply_exit, 0)
        # Nothing was moved -- the unregistered card was never touched.
        self.assertTrue((self.sd_mount / "DCIM" / "IMG_0001.JPG").exists())

    def test_apply_refuses_when_t7_missing_and_touches_nothing(self):
        write_file(self.desktop / "photo.jpg", b"data")
        config = build_config(desktop=self.desktop, t7_volume_uuid=T7_TEST_UUID)  # no override, nothing mounted
        prober = lambda: []  # noqa: E731

        exit_code, output = self._run(config, ["desktop", "--apply"], prober)
        self.assertEqual(exit_code, 1)
        self.assertIn("not connected", output)
        self.assertTrue((self.desktop / "photo.jpg").exists())

    def test_two_sd_cards_sharing_a_generic_label_are_both_processed(self):
        sd_a_mount = self.root / "SD_CARD_A"
        sd_b_mount = self.root / "SD_CARD_B"
        write_file(sd_a_mount / "IMG_0001.JPG", b"a" * 100)
        write_file(sd_b_mount / "IMG_0001.JPG", b"b" * 100)
        uuid_b = "33333333-3333-3333-3333-333333333333"

        config = build_config(
            desktop=self.desktop, t7_root=self.t7, sd_volume_uuids=frozenset({SD_TEST_UUID, uuid_b}), settle_seconds=1
        )
        volume_a = make_volume(SD_TEST_UUID, sd_a_mount, removable=True, label="NO NAME")
        volume_b = make_volume(uuid_b, sd_b_mount, removable=True, label="NO NAME")  # same label as volume_a
        prober = lambda: [volume_a, volume_b]  # noqa: E731

        exit_code, output = self._run(config, ["sd", "--apply"], prober)
        self.assertEqual(exit_code, 0)
        self.assertIn("2/2 transferred", output)
        self.assertFalse((sd_a_mount / "IMG_0001.JPG").exists())
        self.assertFalse((sd_b_mount / "IMG_0001.JPG").exists())
        landed = list((self.t7 / "00-inbox" / "organized" / "sd").rglob("IMG_0001*.JPG"))
        self.assertEqual(len(landed), 2)  # both cards' files landed distinctly, not one clobbering the other

    def test_one_file_raising_unexpectedly_does_not_abort_the_whole_batch(self):
        write_file(self.desktop / "good1.txt", b"one")
        write_file(self.desktop / "bad.txt", b"two")
        write_file(self.desktop / "good2.txt", b"three")
        config = build_config(desktop=self.desktop, t7_root=self.t7, settle_seconds=1)

        from tools.ingest import cli as cli_module
        from tools.ingest import transaction as transaction_module

        original_transfer = transaction_module.transfer_file

        def flaky_transfer(catalog, planned, resolved_t7, **kwargs):
            if planned.source_path.name == "bad.txt":
                raise RuntimeError("simulated unexpected failure")
            return original_transfer(catalog, planned, resolved_t7, **kwargs)

        cli_module.transfer_file = flaky_transfer
        try:
            exit_code, output = self._run(config, ["desktop", "--apply"], lambda: [])
        finally:
            cli_module.transfer_file = original_transfer

        self.assertEqual(exit_code, 0)
        self.assertFalse((self.desktop / "good1.txt").exists())
        self.assertFalse((self.desktop / "good2.txt").exists())
        self.assertTrue((self.desktop / "bad.txt").exists())  # errored before anything touched it
        self.assertIn("ERROR:", output)
        self.assertIn("2/3 transferred", output)

    def test_default_mode_without_flags_is_dry_run(self):
        write_file(self.desktop / "photo.jpg", b"data")
        config = build_config(desktop=self.desktop, t7_root=self.t7)
        exit_code, output = self._run(config, ["desktop"], lambda: [])
        self.assertEqual(exit_code, 0)
        self.assertIn("Dry run. No files changed.", output)
        self.assertTrue((self.desktop / "photo.jpg").exists())

    def test_json_dry_run_returns_reviewable_plan_and_stable_fingerprint(self):
        write_file(self.sd_mount / "DCIM" / "IMG_0001.JPG", b"image")
        config = build_config(
            desktop=self.desktop,
            t7_root=self.t7,
            sd_volume_uuids=frozenset({SD_TEST_UUID}),
            settle_seconds=1,
        )
        sd_volume = make_volume(SD_TEST_UUID, self.sd_mount, removable=True, label="SD_CARD")
        prober = lambda: [sd_volume]  # noqa: E731

        first_exit, first_output = self._run(config, ["sd", "--dry-run", "--json"], prober)
        second_exit, second_output = self._run(config, ["sd", "--dry-run", "--json"], prober)

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        first = json.loads(first_output)
        second = json.loads(second_output)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["summary"]["file_count"], 1)
        self.assertEqual(first["analysis"]["status"], "not_run")
        self.assertEqual(first["files"][0]["source_class"], "sd")
        self.assertEqual(first["files"][0]["source_volume"], SD_TEST_UUID)
        self.assertEqual(first["files"][0]["proposed_filename"], "IMG_0001.JPG")
        self.assertEqual(
            first["files"][0]["landing_destination"],
            str(self.t7 / "00-inbox" / "organized" / "sd" / "DCIM" / "IMG_0001.JPG"),
        )
        self.assertTrue((self.sd_mount / "DCIM" / "IMG_0001.JPG").exists())

    def test_apply_refuses_a_changed_reviewed_plan(self):
        write_file(self.desktop / "photo.jpg", b"original")
        config = build_config(desktop=self.desktop, t7_root=self.t7, settle_seconds=1)
        prober = lambda: []  # noqa: E731

        preview_exit, preview_output = self._run(config, ["desktop", "--dry-run", "--json"], prober)
        self.assertEqual(preview_exit, 0)
        fingerprint = json.loads(preview_output)["fingerprint"]
        write_file(self.desktop / "photo.jpg", b"changed")

        apply_exit, apply_output = self._run(
            config,
            ["desktop", "--apply", "--plan-fingerprint", fingerprint],
            prober,
        )
        self.assertEqual(apply_exit, 2)
        self.assertIn("source set changed", apply_output)
        self.assertTrue((self.desktop / "photo.jpg").exists())

    def test_approved_plan_controls_destination_and_subset(self):
        write_file(self.desktop / "photo.jpg", b"original")
        write_file(self.desktop / "leave.txt", b"leave")
        config = build_config(desktop=self.desktop, t7_root=self.t7, settle_seconds=1)
        prober = lambda: []  # noqa: E731

        preview_exit, preview_output = self._run(config, ["desktop", "--dry-run", "--json"], prober)
        self.assertEqual(preview_exit, 0)
        preview = json.loads(preview_output)
        approved_path = self.root / "approved.json"
        approved_path.write_text(
            json.dumps(
                {
                    "fingerprint": preview["fingerprint"],
                    "files": [
                        {
                            "source_path": next(
                                item["source_path"]
                                for item in preview["files"]
                                if item["current_filename"] == "photo.jpg"
                            ),
                            "destination_relative_path": "reviewed/photo-renamed.jpg",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        apply_exit, apply_output = self._run(
            config,
            ["desktop", "--apply", "--plan-fingerprint", preview["fingerprint"], "--approved-plan", str(approved_path)],
            prober,
        )
        self.assertEqual(apply_exit, 0)
        self.assertIn("1/1 transferred", apply_output)
        self.assertFalse((self.desktop / "photo.jpg").exists())
        self.assertTrue((self.desktop / "leave.txt").exists())
        self.assertTrue(
            (self.t7 / "00-inbox" / "organized" / "desktop" / "reviewed" / "photo-renamed.jpg").exists()
        )

    def test_approved_plan_rejects_absolute_and_parent_destinations(self):
        write_file(self.desktop / "photo.jpg", b"original")
        config = build_config(desktop=self.desktop, t7_root=self.t7, settle_seconds=1)
        prober = lambda: []  # noqa: E731

        preview_exit, preview_output = self._run(config, ["desktop", "--dry-run", "--json"], prober)
        self.assertEqual(preview_exit, 0)
        preview = json.loads(preview_output)
        source_path = preview["files"][0]["source_path"]

        for unsafe_destination in ("../outside.txt", "/tmp/outside.txt"):
            approved_path = self.root / "approved-unsafe.json"
            approved_path.write_text(
                json.dumps(
                    {
                        "fingerprint": preview["fingerprint"],
                        "files": [
                            {
                                "source_path": source_path,
                                "destination_relative_path": unsafe_destination,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            apply_exit, apply_output = self._run(
                config,
                [
                    "desktop",
                    "--apply",
                    "--plan-fingerprint",
                    preview["fingerprint"],
                    "--approved-plan",
                    str(approved_path),
                ],
                prober,
            )
            self.assertEqual(apply_exit, 2)
            self.assertIn("invalid approved destination", apply_output)
            self.assertTrue((self.desktop / "photo.jpg").exists())


if __name__ == "__main__":
    unittest.main()
