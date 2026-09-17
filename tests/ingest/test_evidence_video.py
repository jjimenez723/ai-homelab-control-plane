"""Video-frame sampling: `backends/ffmpeg_video.py` and the `r14-video-frame`
route, end to end through `collect_evidence`.

Uses a real, tiny, synthetically-generated `.mp4` (via ffmpeg's own `lavfi`
test-pattern source, no external asset needed) so this proves the actual
subprocess path works, not just a mocked shape. Every test that needs
ffmpeg is skipped, not failed, on a machine without it -- matching how the
rest of this package treats optional tooling (`tesseract`, `pypdfium2`).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.ingest.config import EvidenceConfig
from tools.ingest.evidence.backends import ffmpeg_video
from tools.ingest.evidence.extract import collect_evidence
from tools.ingest.evidence.types import EvidenceSource, VisualEvidence
from tools.ingest.media.probe import MEDIA_VIDEO, probe_file

from tests.ingest.evidence_fakes import FakeOCRBackend, FakeVisionBackend

_FFMPEG_AVAILABLE = ffmpeg_video.available()


def _make_test_video(path: Path, *, duration: float = 2.0, size: str = "64x64") -> Path:
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate=5",
            str(path),
        ],
        check=True, timeout=30, capture_output=True,
    )
    return path


@unittest.skipUnless(_FFMPEG_AVAILABLE, "ffmpeg not installed on this machine")
class ExtractFrameTests(unittest.TestCase):
    def test_extracts_a_downscaled_frame_from_a_real_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4")
            raster, warnings = ffmpeg_video.extract_frame(
                video_path, source_sha256="a" * 64, duration_seconds=2.0, timeout_seconds=15,
            )
            self.assertEqual(warnings, ())
            self.assertIsNotNone(raster)
            self.assertEqual(raster.source_sha256, "a" * 64)
            self.assertGreater(raster.width, 0)
            self.assertGreater(raster.height, 0)
            self.assertLessEqual(max(raster.width, raster.height), ffmpeg_video.MAX_FRAME_EDGE_PIXELS)
            self.assertTrue(raster.png_bytes.startswith(b"\x89PNG"))

    def test_unknown_duration_falls_back_to_a_default_timestamp_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4", duration=3.0)
            raster, warnings = ffmpeg_video.extract_frame(
                video_path, source_sha256="b" * 64, duration_seconds=None, timeout_seconds=15,
            )
            self.assertEqual(warnings, ())
            self.assertIsNotNone(raster)

    def test_nonexistent_file_degrades_to_a_warning_not_an_exception(self):
        raster, warnings = ffmpeg_video.extract_frame(
            Path("/no/such/video.mp4"), source_sha256="c" * 64, duration_seconds=5.0, timeout_seconds=5,
        )
        self.assertIsNone(raster)
        self.assertTrue(warnings)


class FfmpegUnavailableTests(unittest.TestCase):
    def test_unavailable_ffmpeg_degrades_to_a_warning(self):
        original = ffmpeg_video.available
        ffmpeg_video.available = lambda: False  # type: ignore[assignment]
        try:
            raster, warnings = ffmpeg_video.extract_frame(
                Path("/irrelevant.mp4"), source_sha256="d" * 64, duration_seconds=1.0, timeout_seconds=5,
            )
        finally:
            ffmpeg_video.available = original  # type: ignore[assignment]
        self.assertIsNone(raster)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].code, "backend_unavailable")


@unittest.skipUnless(_FFMPEG_AVAILABLE, "ffmpeg not installed on this machine")
class CollectEvidenceRoutesVideoToVisionTests(unittest.TestCase):
    def test_a_real_mp4_gets_a_vision_description_and_never_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4")
            facts = probe_file(video_path).facts
            self.assertEqual(probe_file(video_path).media_kind, MEDIA_VIDEO)

            ocr = FakeOCRBackend()
            vision = FakeVisionBackend(visual=VisualEvidence(
                image_kind="ordinary_photo", description="a synthetic test pattern",
                entities=(), context="", confidence=0.7, backend="fake", model="fake", model_version="1",
            ))
            source = EvidenceSource(
                path=video_path, sha256="e" * 64, mime="video/mp4",
                media_kind=MEDIA_VIDEO, size=video_path.stat().st_size,
            )
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=False, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )

        self.assertEqual(evidence.route.rules, ("r14-video-frame",))
        self.assertEqual(ocr.calls, [], "video must never be sent to OCR")
        self.assertEqual(vision.calls, 1)
        self.assertIsNotNone(evidence.visual)


if __name__ == "__main__":
    unittest.main()
