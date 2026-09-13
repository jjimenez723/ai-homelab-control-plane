"""AI model-cache placement must be UUID-gated and never fall back locally."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tools.ingest.volumes import VolumeError
from tools.models.storage import cache_environment, resolve_ai_storage
from tests.ingest.helpers import T7_TEST_UUID, build_config, fake_prober, make_volume


class AIStorageTests(unittest.TestCase):
    def test_missing_t7_does_not_create_a_name_based_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_angel = root / "Volumes" / "Angel"
            config = build_config(desktop=root / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            with self.assertRaisesRegex(VolumeError, "not mounted"):
                resolve_ai_storage(config, fake_prober([]), create=True)
            self.assertFalse(fake_angel.exists())

    def test_wrong_uuid_refuses_before_creating_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Angel"
            root.mkdir()
            config = build_config(desktop=root.parent / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            wrong = make_volume("wrong-volume-uuid", root, label="Angel")
            with self.assertRaisesRegex(VolumeError, "not mounted"):
                resolve_ai_storage(config, fake_prober([wrong]), create=True)
            self.assertFalse((root / "40-ai").exists())

    def test_preflight_requires_initialized_tree_but_init_creates_only_ai_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Angel"
            root.mkdir()
            config = build_config(desktop=root.parent / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            volume = make_volume(T7_TEST_UUID, root, label="Angel")
            with self.assertRaisesRegex(VolumeError, "not initialized"):
                resolve_ai_storage(config, fake_prober([volume]))
            storage = resolve_ai_storage(config, fake_prober([volume]), create=True)
            self.assertTrue(storage.ollama.is_dir())
            self.assertTrue(storage.whisper.is_dir())
            self.assertEqual(cache_environment(storage)["OLLAMA_MODELS"], str(root / "40-ai/model-cache/ollama"))
            self.assertEqual(resolve_ai_storage(config, fake_prober([volume])).volume_uuid, T7_TEST_UUID)

    def test_read_only_volume_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Angel"
            root.mkdir()
            config = build_config(desktop=root.parent / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            volume = make_volume(T7_TEST_UUID, root, writable=False, label="Angel")
            with self.assertRaisesRegex(VolumeError, "read-only or locked"):
                resolve_ai_storage(config, fake_prober([volume]), create=True)

    def test_backup_default_excludes_only_regenerable_model_cache(self):
        config = build_config(desktop=Path("/tmp/Desktop"))
        patterns = config.backup.exclude_patterns
        self.assertIn("40-ai/model-cache/**", patterns)
        self.assertNotIn("40-ai/**", patterns)


if __name__ == "__main__":
    unittest.main()
