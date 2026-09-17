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

from tools.ingest.config import EvidenceConfig, OCRConfig, VisionConfig
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


@unittest.skipUnless(_FFMPEG_AVAILABLE, "ffmpeg not installed on this machine")
class ExtractFramesTests(unittest.TestCase):
    def test_samples_the_requested_count_evenly_spaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4", duration=6.0)
            frames, warnings = ffmpeg_video.extract_frames(
                video_path, source_sha256="d" * 64, count=5, duration_seconds=6.0, timeout_seconds=15,
            )
            self.assertEqual(warnings, ())
            self.assertEqual(len(frames), 5)
            self.assertEqual([frame.page for frame in frames], [1, 2, 3, 4, 5])
            for frame in frames:
                self.assertTrue(frame.png_bytes.startswith(b"\x89PNG"))
                self.assertEqual(frame.source_sha256, "d" * 64)

    def test_count_is_respected_when_lower(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4", duration=3.0)
            frames, _warnings = ffmpeg_video.extract_frames(
                video_path, source_sha256="e" * 64, count=2, duration_seconds=3.0, timeout_seconds=15,
            )
            self.assertEqual(len(frames), 2)

    def test_unknown_duration_still_samples_count_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4", duration=8.0)
            frames, _warnings = ffmpeg_video.extract_frames(
                video_path, source_sha256="f" * 64, count=4, duration_seconds=None, timeout_seconds=15,
            )
            self.assertEqual(len(frames), 4)

    def test_nonexistent_file_yields_no_frames_and_warnings_not_an_exception(self):
        frames, warnings = ffmpeg_video.extract_frames(
            Path("/no/such/video.mp4"), source_sha256="0" * 64, count=5, duration_seconds=5.0, timeout_seconds=5,
        )
        self.assertEqual(frames, [])
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

    def test_unavailable_ffmpeg_degrades_extract_frames_too(self):
        original = ffmpeg_video.available
        ffmpeg_video.available = lambda: False  # type: ignore[assignment]
        try:
            frames, warnings = ffmpeg_video.extract_frames(
                Path("/irrelevant.mp4"), source_sha256="e" * 64, count=5, duration_seconds=1.0, timeout_seconds=5,
            )
        finally:
            ffmpeg_video.available = original  # type: ignore[assignment]
        self.assertEqual(frames, [])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].code, "backend_unavailable")


@unittest.skipUnless(_FFMPEG_AVAILABLE, "ffmpeg not installed on this machine")
class CollectEvidenceRoutesVideoToVisionTests(unittest.TestCase):
    def _visual(self, description: str) -> VisualEvidence:
        return VisualEvidence(
            image_kind="ordinary_photo", description=description,
            entities=(), context="", confidence=0.7, backend="fake", model="fake", model_version="1",
        )

    def test_a_real_mp4_gets_vision_and_ocr_on_the_default_frame_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4", duration=4.0)
            facts = probe_file(video_path).facts
            self.assertEqual(probe_file(video_path).media_kind, MEDIA_VIDEO)

            ocr = FakeOCRBackend()
            vision = FakeVisionBackend(visual=self._visual("a synthetic test pattern"))
            source = EvidenceSource(
                path=video_path, sha256="e" * 64, mime="video/mp4",
                media_kind=MEDIA_VIDEO, size=video_path.stat().st_size,
            )
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=False, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )

        default_frame_count = EvidenceConfig().vision.video_frame_count
        self.assertEqual(evidence.route.rules, ("r14-video-frame",))
        self.assertEqual(len(ocr.calls), default_frame_count, "OCR must run once per sampled frame")
        self.assertEqual(vision.calls, default_frame_count, "vision must run once per sampled frame")
        self.assertIsNotNone(evidence.visual)
        self.assertEqual(len(evidence.visual_frames), default_frame_count)
        self.assertEqual(evidence.visual, evidence.visual_frames[0])
        # Frame indices are 1..N, shared identically between OCR pages and
        # vision frames -- both stages saw exactly the same sampled frames.
        self.assertEqual(ocr.calls, list(range(1, default_frame_count + 1)))

    def test_frame_count_is_the_configurable_roi_knob(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4", duration=4.0)
            facts = probe_file(video_path).facts

            ocr = FakeOCRBackend()
            vision = FakeVisionBackend(visual=self._visual("a synthetic test pattern"))
            source = EvidenceSource(
                path=video_path, sha256="f" * 64, mime="video/mp4",
                media_kind=MEDIA_VIDEO, size=video_path.stat().st_size,
            )
            config = EvidenceConfig(vision=VisionConfig(video_frame_count=2))
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=False, config=config,
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )

        self.assertEqual(vision.calls, 2)
        self.assertEqual(len(ocr.calls), 2)
        self.assertEqual(len(evidence.visual_frames), 2)

    def test_ocr_disabled_still_gets_vision_on_every_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4", duration=4.0)
            facts = probe_file(video_path).facts

            ocr = FakeOCRBackend()
            vision = FakeVisionBackend(visual=self._visual("a synthetic test pattern"))
            source = EvidenceSource(
                path=video_path, sha256="a1" * 32, mime="video/mp4",
                media_kind=MEDIA_VIDEO, size=video_path.stat().st_size,
            )
            config = EvidenceConfig(vision=VisionConfig(video_frame_count=3), ocr=OCRConfig(enabled=False))
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=False, config=config,
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )

        self.assertEqual(ocr.calls, [], "OCR must not run when disabled")
        self.assertEqual(vision.calls, 3)
        self.assertEqual(len(evidence.visual_frames), 3)


if __name__ == "__main__":
    unittest.main()
