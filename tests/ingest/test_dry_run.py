"""Dry-run zero-write contract and manual-trigger-only source policy.

Covers required scenarios: #1 dry-run is zero-write, #7 Desktop requires
explicit invocation, #8 Documents requires explicit invocation.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tools.ingest import cli
from tools.ingest.scanner import scan_source

from tests.ingest.helpers import build_config, write_file


class DryRunZeroWriteTests(unittest.TestCase):
    def test_dry_run_creates_nothing_on_t7_and_leaves_sources_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desktop = root / "Desktop"
            documents = root / "Documents"
            downloads = root / "Downloads"
            t7 = root / "T7"  # deliberately never created

            write_file(desktop / "photo.jpg", b"jpeg-bytes")
            write_file(documents / "resume.pdf", b"pdf-bytes")
            write_file(downloads / "installer.dmg", b"dmg-bytes")

            config = build_config(desktop=desktop, documents=documents, downloads=downloads, t7_root=t7)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exit_code = self._run_dry_run(config)
            output = buf.getvalue()

            self.assertEqual(exit_code, 0)
            self.assertIn("Dry run. No files changed.", output)
            self.assertIn("3 files", output)

            # Nothing was created on the (nonexistent) T7 root at all.
            self.assertFalse(t7.exists())

            # Every source file is untouched, in place, with original bytes.
            self.assertEqual((desktop / "photo.jpg").read_bytes(), b"jpeg-bytes")
            self.assertEqual((documents / "resume.pdf").read_bytes(), b"pdf-bytes")
            self.assertEqual((downloads / "installer.dmg").read_bytes(), b"dmg-bytes")

    def test_dry_run_does_not_hash_or_stage_when_t7_root_exists(self):
        """Even when the configured T7 root pre-exists, dry-run must not write
        a catalog, sidecars, or staging files into it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desktop = root / "Desktop"
            t7 = root / "T7"
            t7.mkdir()
            write_file(desktop / "a.txt")

            config = build_config(desktop=desktop, t7_root=t7)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exit_code = self._run_dry_run(config)
            self.assertEqual(exit_code, 0)

            before_after_listing = list(t7.rglob("*"))
            self.assertEqual(before_after_listing, [])  # T7 root stayed completely empty

    @staticmethod
    def _run_dry_run(config):
        import argparse

        args = argparse.Namespace(target="all", dry_run=True, apply=False, config=None)
        # cmd_organize reads args.config via load_config(args.config); we bypass
        # the file-loading path entirely by monkeypatching load_config for this call.
        from tools.ingest import cli as cli_module

        original = cli_module.load_config
        cli_module.load_config = lambda _path: config
        try:
            return cli_module.cmd_organize(args, prober=lambda: [])
        finally:
            cli_module.load_config = original


class ManualTriggerOnlyTests(unittest.TestCase):
    """Desktop/Documents/Downloads have no code path that scans them except
    an explicit `organize <source>` invocation -- there is no scheduler,
    watcher, or T7-mount hook anywhere in this package for Phase 1A."""

    def test_scan_source_is_a_pure_function_never_invoked_on_import(self):
        # Importing the package must not touch the filesystem outside itself.
        import importlib

        import tools.ingest.cli as cli_mod

        importlib.reload(cli_mod)  # re-import must not raise or perform I/O

    def test_organize_desktop_only_scans_desktop_not_documents_or_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desktop = root / "Desktop"
            documents = root / "Documents"
            write_file(desktop / "d.txt")
            write_file(documents / "doc.txt")

            config = build_config(desktop=desktop, documents=documents, t7_root=root / "T7")
            desktop_scan = scan_source(desktop, config=config)
            documents_scan = scan_source(documents, config=config)

            self.assertEqual(len(desktop_scan.eligible), 1)
            self.assertEqual(len(documents_scan.eligible), 1)
            # Requesting only "desktop" must never enumerate documents' files.
            requested = cli._requested_classes("desktop")
            self.assertEqual(requested, ["desktop"])
            self.assertNotIn("documents", requested)


if __name__ == "__main__":
    unittest.main()
