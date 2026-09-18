"""Video-frame selection: `backends/ffmpeg_video.py` and the `r14-video-frame`
route, end to end through `collect_evidence`.

Uses real, tiny, synthetically-generated `.mp4` files (ffmpeg's own `lavfi`
test-pattern source, or a Pillow-drawn PNG sequence when a test needs frames
that genuinely differ) so this proves the actual subprocess path works, not
just a mocked shape. Every test that needs ffmpeg is skipped, not failed, on
a machine without it -- matching how the rest of this package treats optional
tooling (`tesseract`, `pypdfium2`).

Three distinct claims live here, and the fixtures are chosen to keep them
apart:

* **Sampling** -- how many frames, at which timestamps. Those tests pass
  `dedupe=False` deliberately: they are about `_sample_timestamps` and
  `frame_count_for`, and ffmpeg's `testsrc` pattern is near-static in
  average-hash terms (a small moving element on a fixed chart), so leaving
  deduplication on would make a sampling assertion silently measure
  deduplication instead.
* **Deduplication** -- `average_hash`/`deduplicate_frames`, exercised
  against a deliberately static clip (one image looped) and a deliberately
  changing one (a sequence of visibly different images).
* **Routing and end-to-end** -- `collect_evidence` over a real file, with
  fake backends counting the calls.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.ingest.config import (
    MAX_VIDEO_FRAME_COUNT,
    EvidenceConfig,
    OCRConfig,
    VisionConfig,
)
from tools.ingest.evidence.backends import ffmpeg_video
from tools.ingest.evidence.extract import collect_evidence
from tools.ingest.evidence.types import WARN_DEDUPLICATED, EvidenceSource, VisualEvidence
from tools.ingest.media.probe import MEDIA_VIDEO, probe_file

from tests.ingest.evidence_fakes import FakeOCRBackend, FakeVisionBackend
from tests.ingest.media_fixtures import make_changing_video, make_static_video

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
                video_path, source_sha256="d" * 64, count=5, duration_seconds=6.0,
                timeout_seconds=15, dedupe=False,
            )
            self.assertEqual(warnings, ())
            self.assertEqual(len(frames), 5)
            self.assertEqual([frame.page for frame in frames], [1, 2, 3, 4, 5])
            for frame in frames:
                self.assertTrue(frame.png_bytes.startswith(b"\x89PNG"))
                self.assertEqual(frame.source_sha256, "d" * 64)

    def test_count_is_respected_when_lower(self):
        # 3.0s is under SHORT_VIDEO_SECONDS, but the requested count (2) is
        # already at what the duration-aware rule would allow, so this
        # measures the count and nothing else.
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4", duration=3.0)
            frames, _warnings = ffmpeg_video.extract_frames(
                video_path, source_sha256="e" * 64, count=2, duration_seconds=3.0,
                timeout_seconds=15, dedupe=False,
            )
            self.assertEqual(len(frames), 2)

    def test_unknown_duration_still_samples_count_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4", duration=8.0)
            frames, _warnings = ffmpeg_video.extract_frames(
                video_path, source_sha256="f" * 64, count=4, duration_seconds=None,
                timeout_seconds=15, dedupe=False,
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
        # A clip whose frames genuinely differ, so the default configuration
        # (`frame_dedupe: true`) keeps every sampled frame and this measures
        # "one backend call per sampled frame" rather than deduplication.
        with tempfile.TemporaryDirectory() as tmp:
            video_path = make_changing_video(Path(tmp) / "clip.mp4", frames=6)
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
            video_path = make_changing_video(Path(tmp) / "clip.mp4", frames=6)
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
            video_path = make_changing_video(Path(tmp) / "clip.mp4", frames=6)
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


class FrameCountSelectionTests(unittest.TestCase):
    """Step 1 of the selection algorithm: how many frames. Pure, no ffmpeg."""

    def test_the_requested_count_is_honoured_for_a_long_clip(self):
        self.assertEqual(ffmpeg_video.frame_count_for(30.0, 5), 5)

    def test_a_hard_ceiling_applies_however_much_is_asked_for(self):
        self.assertEqual(ffmpeg_video.frame_count_for(600.0, 999), MAX_VIDEO_FRAME_COUNT)
        self.assertEqual(MAX_VIDEO_FRAME_COUNT, 12)

    def test_a_short_clip_is_sampled_less(self):
        self.assertEqual(
            ffmpeg_video.frame_count_for(2.0, 5), ffmpeg_video.SHORT_VIDEO_FRAMES
        )

    def test_a_very_short_clip_gets_exactly_one_frame(self):
        self.assertEqual(ffmpeg_video.frame_count_for(0.4, 5), 1)

    def test_the_duration_aware_rule_is_optional(self):
        self.assertEqual(ffmpeg_video.frame_count_for(0.4, 5, duration_aware=False), 5)

    def test_an_unknown_duration_is_treated_as_long(self):
        """Refusing to sample is worse than sampling a short clip redundantly."""
        self.assertEqual(ffmpeg_video.frame_count_for(None, 5), 5)
        self.assertEqual(ffmpeg_video.frame_count_for(0.0, 5), 5)

    def test_a_nonsense_request_still_yields_at_least_one_frame(self):
        self.assertEqual(ffmpeg_video.frame_count_for(30.0, 0), 1)
        self.assertEqual(ffmpeg_video.frame_count_for(30.0, -3), 1)


class AverageHashTests(unittest.TestCase):
    """Step 3's primitive, in isolation: no ffmpeg, no video."""

    def _png(self, color, size=(64, 64)) -> bytes:
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", size, color).save(buffer, format="PNG")
        return buffer.getvalue()

    def _split_png(self, left, right, size=(64, 64)) -> bytes:
        from io import BytesIO

        from PIL import Image

        image = Image.new("RGB", size, left)
        for x in range(size[0] // 2, size[0]):
            for y in range(size[1]):
                image.putpixel((x, y), right)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_identical_bytes_hash_identically(self):
        png = self._png((10, 120, 200))
        self.assertEqual(ffmpeg_video.average_hash(png), ffmpeg_video.average_hash(png))

    def test_two_visibly_different_pictures_hash_differently(self):
        left = ffmpeg_video.average_hash(self._split_png((0, 0, 0), (255, 255, 255)))
        right = ffmpeg_video.average_hash(self._split_png((255, 255, 255), (0, 0, 0)))
        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        self.assertGreater(
            bin(left ^ right).count("1"), ffmpeg_video.DEDUPE_HAMMING_THRESHOLD
        )

    def test_undecodable_bytes_have_no_hash_rather_than_a_wrong_one(self):
        self.assertIsNone(ffmpeg_video.average_hash(b"not a png at all"))

    def test_a_frame_that_cannot_be_hashed_is_kept_not_dropped(self):
        """"We could not compare this" must never become "this was a duplicate"."""
        from tools.ingest.evidence.types import RasterImage

        broken = RasterImage(png_bytes=b"junk", width=1, height=1, page=1)
        also_broken = RasterImage(png_bytes=b"junk", width=1, height=1, page=2)
        kept, dropped = ffmpeg_video.deduplicate_frames([broken, also_broken])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 0)


@unittest.skipUnless(_FFMPEG_AVAILABLE, "ffmpeg not installed on this machine")
class FrameDeduplicationTests(unittest.TestCase):
    def test_a_static_clip_collapses_to_one_representative_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = make_static_video(Path(tmp) / "static.mp4", duration=6.0)
            frames, warnings = ffmpeg_video.extract_frames(
                video_path, source_sha256="a" * 64, count=5, duration_seconds=6.0,
                timeout_seconds=20,
            )
        self.assertEqual(len(frames), 1, "five identical frames are one picture")
        codes = [warning.code for warning in warnings]
        self.assertIn(WARN_DEDUPLICATED, codes)
        detail = next(w.detail for w in warnings if w.code == WARN_DEDUPLICATED)
        self.assertIn("4 near-identical", detail)

    def test_the_kept_frame_keeps_its_original_sampling_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = make_static_video(Path(tmp) / "static.mp4", duration=6.0)
            frames, _warnings = ffmpeg_video.extract_frames(
                video_path, source_sha256="a" * 64, count=5, duration_seconds=6.0,
                timeout_seconds=20,
            )
        self.assertEqual([frame.page for frame in frames], [1])

    def test_a_changing_clip_keeps_its_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = make_changing_video(Path(tmp) / "changing.mp4", frames=6)
            frames, warnings = ffmpeg_video.extract_frames(
                video_path, source_sha256="b" * 64, count=5, duration_seconds=6.0,
                timeout_seconds=20,
            )
        self.assertEqual(len(frames), 5)
        self.assertNotIn(WARN_DEDUPLICATED, [warning.code for warning in warnings])

    def test_deduplication_can_be_turned_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = make_static_video(Path(tmp) / "static.mp4", duration=6.0)
            frames, warnings = ffmpeg_video.extract_frames(
                video_path, source_sha256="a" * 64, count=4, duration_seconds=6.0,
                timeout_seconds=20, dedupe=False,
            )
        self.assertEqual(len(frames), 4)
        self.assertNotIn(WARN_DEDUPLICATED, [warning.code for warning in warnings])

    def test_deduplication_is_deterministic_across_runs(self):
        """Cached evidence is keyed by content hash, so the same bytes must
        always produce the same frames -- otherwise a cache hit answers a
        question that changed between runs."""
        with tempfile.TemporaryDirectory() as tmp:
            video_path = make_changing_video(Path(tmp) / "changing.mp4", frames=6)
            first, _ = ffmpeg_video.extract_frames(
                video_path, source_sha256="c" * 64, count=5, duration_seconds=6.0,
                timeout_seconds=20,
            )
            second, _ = ffmpeg_video.extract_frames(
                video_path, source_sha256="c" * 64, count=5, duration_seconds=6.0,
                timeout_seconds=20,
            )
        self.assertEqual([frame.page for frame in first], [frame.page for frame in second])
        self.assertEqual(
            [frame.png_bytes for frame in first], [frame.png_bytes for frame in second]
        )


@unittest.skipUnless(_FFMPEG_AVAILABLE, "ffmpeg not installed on this machine")
class DeduplicationReachesTheBackendsTests(unittest.TestCase):
    """The point of collapsing frames is fewer backend calls, not a tidier list."""

    def _visual(self) -> VisualEvidence:
        return VisualEvidence(
            image_kind="ordinary_photo", description="a flat still image",
            entities=(), context="", confidence=0.4,
            backend="fake", model="fake", model_version="1",
        )

    def test_a_static_video_costs_one_vision_and_one_ocr_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = make_static_video(Path(tmp) / "static.mp4", duration=6.0)
            facts = probe_file(video_path).facts
            ocr = FakeOCRBackend()
            vision = FakeVisionBackend(visual=self._visual())
            source = EvidenceSource(
                path=video_path, sha256="d" * 64, mime="video/mp4",
                media_kind=MEDIA_VIDEO, size=video_path.stat().st_size,
            )
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=False, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )

        self.assertEqual(vision.calls, 1)
        self.assertEqual(ocr.calls, [1])
        self.assertEqual(len(evidence.visual_frames), 1)
        self.assertIn(WARN_DEDUPLICATED, evidence.warning_codes())

    def test_dedupe_disabled_in_config_restores_every_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = make_static_video(Path(tmp) / "static.mp4", duration=6.0)
            facts = probe_file(video_path).facts
            ocr = FakeOCRBackend()
            vision = FakeVisionBackend(visual=self._visual())
            source = EvidenceSource(
                path=video_path, sha256="e" * 64, mime="video/mp4",
                media_kind=MEDIA_VIDEO, size=video_path.stat().st_size,
            )
            config = EvidenceConfig(vision=VisionConfig(video_frame_count=3, frame_dedupe=False))
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=False, config=config,
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )

        self.assertEqual(vision.calls, 3)
        self.assertEqual(ocr.calls, [1, 2, 3])
        self.assertEqual(len(evidence.visual_frames), 3)


if __name__ == "__main__":
    unittest.main()
