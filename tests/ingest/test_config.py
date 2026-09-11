"""Config loading, in particular guards that must not become silent no-ops."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.ingest.config import load_config


class ConfigTests(unittest.TestCase):
    def test_defaults_load_with_no_config_file(self):
        config = load_config(None)
        self.assertEqual(config.media.heic_to_jpeg.jpeg_quality, 100)
        self.assertTrue(config.media.heic_to_jpeg.preserve_original)

    def test_preserve_original_false_is_refused_at_load_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ingest.yaml"
            path.write_text("media:\n  heic_to_jpeg:\n    preserve_original: false\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
