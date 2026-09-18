"""Config loading, in particular guards that must not become silent no-ops."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.ingest.config import MAX_VIDEO_FRAME_COUNT, load_config


class ConfigTests(unittest.TestCase):
    def test_defaults_load_with_no_config_file(self):
        config = load_config(None)
        self.assertEqual(config.media.heic_to_jpeg.jpeg_quality, 100)
        self.assertTrue(config.media.heic_to_jpeg.preserve_original)
        self.assertEqual(config.evidence.vision.video_frame_count, 5)
        self.assertTrue(config.evidence.vision.frame_dedupe)
        self.assertTrue(config.evidence.vision.frame_duration_aware)
        # Transcription is off by default and the null backend is the default
        # one: whisper.cpp is an optional system binary plus a model file the
        # repository does not ship (D-058).
        self.assertFalse(config.evidence.transcription.enabled)
        self.assertEqual(config.evidence.transcription.backend, "null")
        self.assertIsNone(config.evidence.transcription.model_path)
        self.assertEqual(config.evidence.transcription.max_audio_seconds, 600)

    def test_preserve_original_false_is_refused_at_load_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ingest.yaml"
            path.write_text("media:\n  heic_to_jpeg:\n    preserve_original: false\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)

    def test_video_frame_count_is_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ingest.yaml"
            path.write_text("evidence:\n  vision:\n    video_frame_count: 8\n", encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config.evidence.vision.video_frame_count, 8)

    def test_video_frame_count_out_of_range_is_refused(self):
        for value in (0, MAX_VIDEO_FRAME_COUNT + 1):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "ingest.yaml"
                path.write_text(
                    f"evidence:\n  vision:\n    video_frame_count: {value}\n", encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_transcription_can_be_enabled_with_an_explicit_model_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ingest.yaml"
            path.write_text(
                "evidence:\n"
                "  transcription:\n"
                "    enabled: true\n"
                "    backend: whisper-cpp\n"
                "    model_path: /models/ggml-base.en.bin\n"
                "    language: en\n"
                "    max_audio_seconds: 120\n",
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertTrue(config.evidence.transcription.enabled)
        self.assertEqual(config.evidence.transcription.backend, "whisper-cpp")
        self.assertEqual(
            config.evidence.transcription.model_path, Path("/models/ggml-base.en.bin")
        )
        self.assertEqual(config.evidence.transcription.max_audio_seconds, 120)

    def test_an_unknown_transcription_backend_is_refused_at_load_time(self):
        """A typo must not silently fall back to a backend nobody asked for --
        the same posture the OCR and vision backend guards already take."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ingest.yaml"
            path.write_text(
                "evidence:\n  transcription:\n    backend: whisperx\n", encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_config(path)

    def test_a_relative_transcription_model_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ingest.yaml"
            path.write_text(
                "evidence:\n  transcription:\n    model_path: models/ggml-base.en.bin\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(path)

    def test_transcription_bounds_are_enforced(self):
        for snippet in (
            "    max_audio_seconds: 0\n",
            "    max_audio_seconds: 100000\n",
            "    timeout_seconds: 0\n",
            "    timeout_seconds: 99999\n",
        ):
            with self.subTest(snippet=snippet.strip()), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "ingest.yaml"
                path.write_text(
                    "evidence:\n  transcription:\n" + snippet, encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
