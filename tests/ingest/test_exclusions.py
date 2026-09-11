"""Hard exclusions applied by the scanner.

Covers required scenarios: #9 file still changing, #10 incomplete browser
download, #11 excluded Git repository, #12 excluded nested Git repository,
#13 node_modules exclusion, #14 symlink escape, #15 keep-local exclusion.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tools.ingest.exclusions import ExclusionReason
from tools.ingest.scanner import scan_source

from tests.ingest.helpers import build_config, touch_fresh, write_file


class ExclusionTests(unittest.TestCase):
    def test_file_still_changing_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            fresh = write_file(desktop / "growing.mov", age_seconds=0)
            touch_fresh(fresh)
            config = build_config(desktop=desktop, settle_seconds=300)

            scan = scan_source(desktop, config=config)
            self.assertEqual(scan.eligible, [])
            self.assertEqual(scan.exclusion_counts.get(ExclusionReason.STILL_CHANGING), 1)

    def test_settled_file_becomes_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            write_file(desktop / "done.mov", age_seconds=3600)
            config = build_config(desktop=desktop, settle_seconds=5)

            scan = scan_source(desktop, config=config)
            self.assertEqual(len(scan.eligible), 1)

    def test_incomplete_browser_download_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "Downloads"
            write_file(downloads / "bigfile.zip.crdownload")
            write_file(downloads / "other.download")
            write_file(downloads / "partial.part")
            config = build_config(desktop=Path(tmp) / "Desktop", downloads=downloads)

            scan = scan_source(downloads, config=config)
            self.assertEqual(scan.eligible, [])
            self.assertEqual(scan.exclusion_counts.get(ExclusionReason.INCOMPLETE_DOWNLOAD), 3)

    def test_top_level_git_repo_is_excluded_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            repo = desktop / "old-project"
            (repo / ".git").mkdir(parents=True)
            write_file(repo / "main.py")
            write_file(desktop / "unrelated.txt")
            config = build_config(desktop=desktop)

            scan = scan_source(desktop, config=config)
            self.assertEqual(len(scan.eligible), 1)
            self.assertEqual(scan.eligible[0].rel_path, "unrelated.txt")
            self.assertEqual(len(scan.git_repos), 1)
            self.assertEqual(scan.git_repos[0].rel_path, "old-project")

    def test_nested_git_repo_is_excluded_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            nested_repo = desktop / "projects" / "deep" / "nested-repo"
            (nested_repo / ".git").mkdir(parents=True)
            write_file(nested_repo / "app.py")
            write_file(desktop / "projects" / "notes.txt")
            config = build_config(desktop=desktop)

            scan = scan_source(desktop, config=config)
            rel_paths = {f.rel_path for f in scan.eligible}
            self.assertIn(os.path.join("projects", "notes.txt"), rel_paths)
            self.assertFalse(any("nested-repo" in p for p in rel_paths))
            self.assertEqual(len(scan.git_repos), 1)
            self.assertTrue(scan.git_repos[0].rel_path.endswith("nested-repo"))

    def test_git_worktree_file_marker_is_also_detected(self):
        """A worktree/submodule has a `.git` *file*, not a directory."""
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            worktree = desktop / "worktree-checkout"
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: /somewhere/else\n")
            write_file(worktree / "file.py")
            config = build_config(desktop=desktop)

            scan = scan_source(desktop, config=config)
            self.assertEqual(scan.eligible, [])
            self.assertEqual(len(scan.git_repos), 1)

    def test_node_modules_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            write_file(desktop / "app" / "node_modules" / "leftpad" / "index.js")
            write_file(desktop / "app" / "package.json")
            config = build_config(desktop=desktop)

            scan = scan_source(desktop, config=config)
            rel_paths = {f.rel_path for f in scan.eligible}
            self.assertIn(os.path.join("app", "package.json"), rel_paths)
            self.assertFalse(any("node_modules" in p for p in rel_paths))
            self.assertEqual(scan.exclusion_counts.get(ExclusionReason.DIR_NAME), 1)

    def test_excluded_dir_names_match_case_insensitively(self):
        """macOS's default APFS is case-insensitive-but-preserving, so
        `Node_Modules` and `node_modules` must be treated as the same name."""
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            write_file(desktop / "app" / "Node_Modules" / "leftpad" / "index.js")
            write_file(desktop / "app" / "package.json")
            config = build_config(desktop=desktop)

            scan = scan_source(desktop, config=config)
            rel_paths = {f.rel_path for f in scan.eligible}
            self.assertIn(os.path.join("app", "package.json"), rel_paths)
            self.assertFalse(any("Node_Modules" in p for p in rel_paths))

    def test_venv_directories_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            write_file(desktop / "proj" / ".venv" / "lib" / "site.py")
            write_file(desktop / "proj" / "venv" / "lib" / "site.py")
            write_file(desktop / "proj" / "main.py")
            config = build_config(desktop=desktop)

            scan = scan_source(desktop, config=config)
            rel_paths = {f.rel_path for f in scan.eligible}
            self.assertEqual(rel_paths, {os.path.join("proj", "main.py")})

    def test_bundle_directory_is_not_descended_into(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            write_file(desktop / "MyApp.app" / "Contents" / "MacOS" / "MyApp")
            write_file(desktop / "keep.txt")
            config = build_config(desktop=desktop)

            scan = scan_source(desktop, config=config)
            rel_paths = {f.rel_path for f in scan.eligible}
            self.assertEqual(rel_paths, {"keep.txt"})
            self.assertEqual(scan.exclusion_counts.get(ExclusionReason.BUNDLE), 1)

    def test_symlink_escaping_source_root_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            outside = Path(tmp) / "Outside"
            desktop.mkdir(parents=True)
            outside.mkdir(parents=True)
            target = write_file(outside / "secret.txt")
            link = desktop / "escape-link.txt"
            os.symlink(target, link)
            config = build_config(desktop=desktop)

            scan = scan_source(desktop, config=config)
            self.assertEqual(scan.eligible, [])
            self.assertEqual(scan.exclusion_counts.get(ExclusionReason.SYMLINK_ESCAPE), 1)

    def test_symlink_within_source_root_is_excluded_but_not_flagged_as_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            target = write_file(desktop / "real.txt")
            link = desktop / "link.txt"
            os.symlink(target, link)
            config = build_config(desktop=desktop)

            scan = scan_source(desktop, config=config)
            rel_paths = {f.rel_path for f in scan.eligible}
            self.assertEqual(rel_paths, {"real.txt"})
            self.assertEqual(scan.exclusion_counts.get(ExclusionReason.SYMLINK), 1)
            self.assertNotIn(ExclusionReason.SYMLINK_ESCAPE, scan.exclusion_counts)

    def test_keep_local_marker_excludes_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            protected = desktop / "do-not-touch"
            write_file(protected / "important.txt")
            (protected / ".keep-local").write_text("")
            write_file(desktop / "movable.txt")
            config = build_config(desktop=desktop)

            scan = scan_source(desktop, config=config)
            rel_paths = {f.rel_path for f in scan.eligible}
            self.assertEqual(rel_paths, {"movable.txt"})
            self.assertEqual(scan.exclusion_counts.get(ExclusionReason.KEEP_LOCAL), 1)

    def test_self_repository_and_destination_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            t7 = Path(tmp) / "T7"
            t7.mkdir()
            nested_t7_copy = desktop / "T7-inside-desktop"  # simulate T7 appearing under a source root
            write_file(nested_t7_copy / "already-there.txt")
            write_file(desktop / "normal.txt")
            config = build_config(desktop=desktop, t7_root=t7)

            scan = scan_source(desktop, config=config, extra_exclude_paths=(config.repo_root, nested_t7_copy))
            rel_paths = {f.rel_path for f in scan.eligible}
            self.assertEqual(rel_paths, {"normal.txt"})


if __name__ == "__main__":
    unittest.main()
