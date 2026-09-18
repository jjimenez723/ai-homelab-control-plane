"""Unit tests for the Angel knowledge-export volume-identity boundary.

Mirrors tests/test_backup.py's discipline: resolution is tested against a
fake prober and temp directories only, never a real /Volumes mount, and the
refusal paths (no UUID configured, UUID not mounted, mounted read-only, dev
override) are proven before any copy is attempted.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.ingest.helpers import T7_TEST_UUID, build_config, fake_prober, make_volume
from tools.export_angel_knowledge import ExportError, export_knowledge, resolve_angel_root


class ResolveAngelRootTests(unittest.TestCase):
    def test_refuses_when_no_volume_uuid_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = build_config(desktop=Path(tmp) / "Desktop")
            with self.assertRaisesRegex(ExportError, "no t7.volume_uuid configured"):
                resolve_angel_root(config, fake_prober([]))

    def test_refuses_storage_root_override_even_if_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(desktop=root / "Desktop", t7_root=root, t7_volume_uuid=T7_TEST_UUID)
            with self.assertRaisesRegex(ExportError, "storage_root_override"):
                resolve_angel_root(config, fake_prober([]))

    def test_refuses_when_configured_uuid_is_not_mounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = build_config(desktop=Path(tmp) / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            with self.assertRaisesRegex(ExportError, "is not mounted"):
                resolve_angel_root(config, fake_prober([]))

    def test_refuses_when_mounted_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            angel_root = Path(tmp) / "angel"
            angel_root.mkdir()
            config = build_config(desktop=Path(tmp) / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            volume = make_volume(T7_TEST_UUID, angel_root, writable=False)
            with self.assertRaisesRegex(ExportError, "read-only or locked"):
                resolve_angel_root(config, fake_prober([volume]))

    def test_resolves_to_the_matching_mount_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            angel_root = Path(tmp) / "angel"
            angel_root.mkdir()
            config = build_config(desktop=Path(tmp) / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            volume = make_volume(T7_TEST_UUID, angel_root)
            resolved = resolve_angel_root(config, fake_prober([volume]))
            self.assertEqual(resolved, angel_root.resolve())


class ExportKnowledgeTests(unittest.TestCase):
    def test_copies_a_file_and_a_directory_into_the_leaf_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            angel_root = tmp_path / "angel"
            angel_root.mkdir()

            source_doc = tmp_path / "source-doc.md"
            source_doc.write_text("hello knowledge\n", encoding="utf-8")
            source_dir = tmp_path / "source-profiles"
            (source_dir).mkdir()
            (source_dir / "one.yaml").write_text("a: 1\n", encoding="utf-8")

            config = build_config(desktop=tmp_path / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            volume = make_volume(T7_TEST_UUID, angel_root)

            # export_knowledge joins each source against REPO_ROOT; pathlib's
            # `/` operator returns the right-hand side unchanged when it is
            # already absolute, so passing absolute temp-dir paths here
            # exercises the same code path a relative repo path would.
            result = export_knowledge(
                config,
                fake_prober([volume]),
                exports=((source_doc, "doc.md"), (source_dir, "profiles")),
            )

            expected_root = (angel_root / "50-research" / "knowledge" / "backend-baselines").resolve()
            self.assertEqual(result.destination_root, expected_root)
            self.assertTrue((expected_root / "doc.md").is_file())
            self.assertEqual((expected_root / "doc.md").read_text(encoding="utf-8"), "hello knowledge\n")
            self.assertTrue((expected_root / "profiles" / "one.yaml").is_file())

    def test_does_not_disturb_existing_siblings_under_knowledge(self):
        """Creating only the leaf: an existing 50-research/knowledge/other-thing/ must survive untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            angel_root = tmp_path / "angel"
            other = angel_root / "50-research" / "knowledge" / "other-workstream"
            other.mkdir(parents=True)
            (other / "marker.txt").write_text("pre-existing\n", encoding="utf-8")

            source_doc = tmp_path / "source-doc.md"
            source_doc.write_text("x\n", encoding="utf-8")

            config = build_config(desktop=tmp_path / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            volume = make_volume(T7_TEST_UUID, angel_root)

            export_knowledge(config, fake_prober([volume]), exports=((source_doc, "doc.md"),))

            self.assertTrue((other / "marker.txt").is_file())
            self.assertEqual((other / "marker.txt").read_text(encoding="utf-8"), "pre-existing\n")

    def test_missing_source_raises_before_any_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            angel_root = tmp_path / "angel"
            angel_root.mkdir()
            config = build_config(desktop=tmp_path / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            volume = make_volume(T7_TEST_UUID, angel_root)
            missing = tmp_path / "does-not-exist.md"
            with self.assertRaises(ExportError):
                export_knowledge(config, fake_prober([volume]), exports=((missing, "doc.md"),))


if __name__ == "__main__":
    unittest.main()
