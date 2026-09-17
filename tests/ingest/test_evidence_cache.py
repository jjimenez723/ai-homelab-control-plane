"""The evidence cache is keyed by content, never by path -- and proves it.

`cache.CacheKey`/`lookup`/`store` are the mechanical layer; `collect_evidence`
is what a real caller uses. This suite exercises both, against a real
`LocalLedger` in a temp directory rather than a mock, because the property
under test -- "a rename hits, different bytes at the same path never can,
and a version bump invalidates only the kind it belongs to" -- is precisely
the kind of thing a hand-rolled fake ledger could get right by accident. Two
extra properties round it out: `pipeline_version_for` folds in exactly the
settings that change what a producer sees (so `raster_dpi` invalidates and
`timeout_seconds` never does), and an unavailable backend's result is never
cached at all, so installing a missing engine is retried on the very next
run instead of reading back a stale absence.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.ingest.config import EvidenceConfig, OCRConfig, VisionConfig
from tools.ingest.evidence import cache
from tools.ingest.evidence.extract import collect_evidence, pipeline_version_for
from tools.ingest.evidence.types import (
    EvidenceSource,
    OCRBlock,
    Polygon,
    VisualEvidence,
    WARN_LOW_CONFIDENCE,
)
from tools.ingest.ledger import LocalLedger
from tools.ingest.media.probe import MEDIA_IMAGE

from tests.ingest.evidence_fakes import FakeOCRBackend, FakeVisionBackend, UnavailableOCRBackend


def _ledger(tmp: Path) -> LocalLedger:
    return LocalLedger(tmp / "local-ledger.sqlite")


class CacheKeyIdentityTests(unittest.TestCase):
    """`evidence_id` folds in every input that determines the answer."""

    def _key(self, **overrides):
        base = dict(
            content_sha256="a" * 64, kind="ocr", producer="tesseract-ocr", producer_version="1",
            model="", model_version="", pipeline_version="1",
        )
        base.update(overrides)
        return cache.CacheKey(**base)

    def test_identical_keys_produce_the_same_id(self):
        self.assertEqual(self._key().evidence_id, self._key().evidence_id)

    def test_every_field_is_load_bearing(self):
        baseline = self._key().evidence_id
        for field, value in (
            ("content_sha256", "b" * 64), ("kind", "visual"), ("producer", "other-ocr"),
            ("producer_version", "2"), ("model", "m"), ("model_version", "2"), ("pipeline_version", "2"),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(self._key(**{field: value}).evidence_id, baseline)


class RawCacheLookupStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_miss_is_reported_cleanly_not_as_an_error(self):
        with _ledger(self.root) as ledger:
            lookup = cache.lookup(ledger, cache.CacheKey(content_sha256="a" * 64, kind="ocr", producer="x", producer_version="1"))
        self.assertFalse(lookup.hit)
        self.assertIsNone(lookup.payload)

    def test_store_then_lookup_round_trips_the_payload_and_status(self):
        key = cache.CacheKey(content_sha256="a" * 64, kind="visual", producer="heuristic-vision", producer_version="1")
        with _ledger(self.root) as ledger:
            cache.store(ledger, key, payload={"visual": {"image_kind": "screenshot"}}, status="ok", now="2026-01-01T00:00:00+00:00")
            lookup = cache.lookup(ledger, key)
        self.assertTrue(lookup.hit)
        self.assertEqual(lookup.payload, {"visual": {"image_kind": "screenshot"}})
        self.assertEqual(lookup.status, "ok")

    def test_a_second_store_to_the_same_key_replaces_rather_than_accumulates(self):
        """Not a violation of the append-only rule governing naming_history:
        the key already encodes content+producer+model+pipeline identity, so
        two writes to the same key are the same measurement re-derived."""
        key = cache.CacheKey(content_sha256="a" * 64, kind="ocr", producer="tesseract-ocr", producer_version="1")
        with _ledger(self.root) as ledger:
            cache.store(ledger, key, payload={"ocr": {"blocks": []}}, status="ok", now="2026-01-01T00:00:00+00:00")
            cache.store(ledger, key, payload={"ocr": {"blocks": [1]}}, status="partial", now="2026-01-02T00:00:00+00:00")
            rows = ledger._conn.execute("SELECT COUNT(*) FROM file_evidence WHERE evidence_id = ?", (key.evidence_id,)).fetchone()[0]
            lookup = cache.lookup(ledger, key)
        self.assertEqual(rows, 1)
        self.assertEqual(lookup.status, "partial")


class PipelineVersionFingerprintTests(unittest.TestCase):
    """Only settings that change what a producer *sees* belong in the
    fingerprint (see `pipeline_version_for`'s docstring)."""

    def test_raster_dpi_change_alters_the_fingerprint(self):
        self.assertNotEqual(
            pipeline_version_for(EvidenceConfig(raster_dpi=100)),
            pipeline_version_for(EvidenceConfig(raster_dpi=300)),
        )

    def test_max_pages_raster_max_pixels_max_text_chars_and_ocr_max_blocks_are_all_load_bearing(self):
        baseline = pipeline_version_for(EvidenceConfig())
        variants = (
            EvidenceConfig(max_pages=5),
            EvidenceConfig(raster_max_pixels=1_000_000),
            EvidenceConfig(max_text_chars=500),
            EvidenceConfig(ocr=OCRConfig(max_blocks=10)),
            EvidenceConfig(vision=VisionConfig(video_frame_count=2)),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(pipeline_version_for(variant), baseline)

    def test_ocr_timeout_seconds_never_changes_the_fingerprint(self):
        """A timeout decides whether a stage gives up, not what it would have
        perceived if it had not -- so it must not cost every cached kind a
        re-derivation the way an actual behaviour change would."""
        self.assertEqual(
            pipeline_version_for(EvidenceConfig(ocr=OCRConfig(timeout_seconds=5))),
            pipeline_version_for(EvidenceConfig(ocr=OCRConfig(timeout_seconds=300))),
        )

    def test_vision_timeout_seconds_never_changes_the_fingerprint(self):
        self.assertEqual(
            pipeline_version_for(EvidenceConfig(vision=VisionConfig(timeout_seconds=5))),
            pipeline_version_for(EvidenceConfig(vision=VisionConfig(timeout_seconds=300))),
        )

    def test_enabled_flags_never_change_the_fingerprint(self):
        """Whether a stage runs at all is a routing decision, not a change to
        what a producer that *did* run would have seen."""
        self.assertEqual(
            pipeline_version_for(EvidenceConfig(ocr=OCRConfig(enabled=False), vision=VisionConfig(enabled=False))),
            pipeline_version_for(EvidenceConfig()),
        )


def _screenshot_source(path: Path, sha256: str = "a" * 64) -> EvidenceSource:
    return EvidenceSource(path=path, sha256=sha256, mime="image/png", media_kind=MEDIA_IMAGE, size=100)


def _screenshot_facts() -> dict:
    return {"is_probably_screenshot": True}


class EndToEndCacheSemanticsTests(unittest.TestCase):
    """Items 5-7 of the evidence-layer test matrix: exercised through the real
    orchestrator (`collect_evidence`) against a real `LocalLedger`, because
    the property under test is about the *system*, not about `cache.py` in
    isolation.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.png_bytes = _make_png_bytes()
        self.path = self.root / "shot.png"
        self.path.write_bytes(self.png_bytes)
        import hashlib

        self.sha256 = hashlib.sha256(self.png_bytes).hexdigest()

    def _collect(self, *, ledger, ocr, vision, config=None, path=None, sha256=None):
        return collect_evidence(
            source=_screenshot_source(path or self.path, sha256 or self.sha256),
            facts=_screenshot_facts(), screenshot_by_name=False,
            config=config or EvidenceConfig(), now="2026-01-01T00:00:00+00:00",
            ledger=ledger, ocr_backend=ocr, vision_backend=vision,
        )

    def test_renamed_identical_content_reuses_evidence(self):
        ocr = FakeOCRBackend(blocks_by_page={1: (_block("hello"),)})
        vision = FakeVisionBackend(visual=_visual())
        with _ledger(self.root) as ledger:
            first = self._collect(ledger=ledger, ocr=ocr, vision=vision)
            self.assertEqual(ocr.calls, [1])
            self.assertEqual(vision.calls, 1)

            renamed = self.root / "renamed-by-the-owner.png"
            self.path.rename(renamed)
            second = self._collect(ledger=ledger, ocr=ocr, vision=vision, path=renamed)

        self.assertEqual(ocr.calls, [1], "a rename must not cost a second OCR pass")
        self.assertEqual(vision.calls, 1, "a rename must not cost a second vision call")
        self.assertTrue(all(record.cache_hit for record in second.provenance))
        self.assertEqual(first.ocr_blocks, second.ocr_blocks)
        self.assertEqual(first.visual, second.visual)

    def test_different_bytes_at_the_same_path_cannot_reuse_evidence(self):
        ocr = FakeOCRBackend(blocks_by_page={1: (_block("hello"),)})
        vision = FakeVisionBackend(visual=_visual())
        with _ledger(self.root) as ledger:
            first = self._collect(ledger=ledger, ocr=ocr, vision=vision)

            different = _make_png_bytes(color=(9, 9, 9))
            self.path.write_bytes(different)
            import hashlib

            new_sha = hashlib.sha256(different).hexdigest()
            self.assertNotEqual(new_sha, self.sha256)
            second = self._collect(ledger=ledger, ocr=ocr, vision=vision, sha256=new_sha)

        self.assertEqual(ocr.calls, [1, 1], "different bytes at the same path must re-run OCR")
        self.assertEqual(vision.calls, 2)
        self.assertNotEqual(first.content_sha256, second.content_sha256)
        self.assertFalse(any(record.cache_hit for record in second.provenance))

    def test_bumping_the_ocr_backend_version_misses_only_ocr(self):
        ocr = FakeOCRBackend(blocks_by_page={1: (_block("hello"),)})
        vision = FakeVisionBackend(visual=_visual())
        with _ledger(self.root) as ledger:
            self._collect(ledger=ledger, ocr=ocr, vision=vision)
            ocr.version = "2"
            second = self._collect(ledger=ledger, ocr=ocr, vision=vision)

        self.assertEqual(ocr.calls, [1, 1], "the version bump must trigger a fresh OCR pass")
        self.assertEqual(vision.calls, 1, "vision must still hit its unrelated cache entry")
        by_kind = {record.kind: record for record in second.provenance}
        self.assertFalse(by_kind["ocr"].cache_hit)
        self.assertTrue(by_kind["visual"].cache_hit)

    def test_bumping_the_vision_model_version_misses_only_visual(self):
        ocr = FakeOCRBackend(blocks_by_page={1: (_block("hello"),)})
        vision = FakeVisionBackend(visual=_visual())
        with _ledger(self.root) as ledger:
            self._collect(ledger=ledger, ocr=ocr, vision=vision)
            vision.model_version = "2"
            second = self._collect(ledger=ledger, ocr=ocr, vision=vision)

        self.assertEqual(vision.calls, 2, "the model version bump must trigger a fresh vision call")
        self.assertEqual(ocr.calls, [1], "ocr must still hit its unrelated cache entry")
        by_kind = {record.kind: record for record in second.provenance}
        self.assertTrue(by_kind["ocr"].cache_hit)
        self.assertFalse(by_kind["visual"].cache_hit)

    def test_bumping_raster_dpi_invalidates_every_kind(self):
        """`raster_dpi` feeds `pipeline_version_for`, which is folded into
        every stage's cache key uniformly -- so this is deliberately
        conservative (it also invalidates kinds `raster_dpi` cannot actually
        affect) rather than selectively wrong in the unsafe direction."""
        ocr = FakeOCRBackend(blocks_by_page={1: (_block("hello"),)})
        vision = FakeVisionBackend(visual=_visual())
        with _ledger(self.root) as ledger:
            self._collect(ledger=ledger, ocr=ocr, vision=vision, config=EvidenceConfig(raster_dpi=200))
            second = self._collect(ledger=ledger, ocr=ocr, vision=vision, config=EvidenceConfig(raster_dpi=150))

        self.assertEqual(ocr.calls, [1, 1])
        self.assertEqual(vision.calls, 2)
        self.assertFalse(any(record.cache_hit for record in second.provenance))

    def test_bumping_only_ocr_timeout_seconds_changes_nothing(self):
        ocr = FakeOCRBackend(blocks_by_page={1: (_block("hello"),)})
        vision = FakeVisionBackend(visual=_visual())
        with _ledger(self.root) as ledger:
            self._collect(ledger=ledger, ocr=ocr, vision=vision, config=EvidenceConfig(ocr=OCRConfig(timeout_seconds=5)))
            second = self._collect(ledger=ledger, ocr=ocr, vision=vision, config=EvidenceConfig(ocr=OCRConfig(timeout_seconds=250)))

        self.assertEqual(ocr.calls, [1], "a timeout change is not a producer-behaviour change")
        self.assertEqual(vision.calls, 1)
        self.assertTrue(all(record.cache_hit for record in second.provenance))


class GeometryAndConfidenceSurviveCachingTests(unittest.TestCase):
    """Items 8-9: OCR geometry and confidence survive normalization and a real
    store/load round trip through the cache, including the edges (0.0, 1.0,
    and a below-threshold confidence that must still be kept, flagged, not
    dropped)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.png_bytes = _make_png_bytes()
        self.path = self.root / "shot.png"
        self.path.write_bytes(self.png_bytes)
        import hashlib

        self.sha256 = hashlib.sha256(self.png_bytes).hexdigest()

    def test_a_non_axis_aligned_polygon_survives_a_cache_round_trip(self):
        tilted = Polygon(points=((10, 0), (110, 20), (100, 70), (0, 50)))
        block = OCRBlock(text="tilted", polygon=tilted, page=1, confidence=0.42, reading_order=0, engine="e", engine_version="1")
        ocr = FakeOCRBackend(blocks_by_page={1: (block,)})
        vision = FakeVisionBackend(visual=None)
        with _ledger(self.root) as ledger:
            first = collect_evidence(
                source=_screenshot_source(self.path, self.sha256), facts=_screenshot_facts(), screenshot_by_name=False,
                config=EvidenceConfig(), now="2026-01-01T00:00:00+00:00", ledger=ledger, ocr_backend=ocr, vision_backend=vision,
            )
            second = collect_evidence(
                source=_screenshot_source(self.path, self.sha256), facts=_screenshot_facts(), screenshot_by_name=False,
                config=EvidenceConfig(), now="2026-01-01T00:00:01+00:00", ledger=ledger, ocr_backend=ocr, vision_backend=vision,
            )
        self.assertEqual(ocr.calls, [1], "the second call must be a cache hit, not a second recognition")
        self.assertEqual(first.ocr_blocks[0].polygon.points, tilted.points)
        self.assertEqual(second.ocr_blocks[0].polygon.points, tilted.points)

    def test_confidence_extremes_survive_normalization_unflagged(self):
        """0.0 and 1.0 are both legitimate confidences, not sentinels -- a mix
        including a full-confidence block must not itself trip the
        low-confidence warning (that only fires when *every* block on the
        page is below threshold; see the next test)."""
        blocks = (
            OCRBlock(text="zero", polygon=Polygon.from_bbox(0, 0, 10, 10), page=1, confidence=0.0, reading_order=0, engine="e", engine_version="1"),
            OCRBlock(text="full", polygon=Polygon.from_bbox(0, 20, 10, 30), page=1, confidence=1.0, reading_order=0, engine="e", engine_version="1"),
        )
        ocr = FakeOCRBackend(blocks_by_page={1: blocks})
        vision = FakeVisionBackend(visual=None)
        with _ledger(self.root) as ledger:
            evidence = collect_evidence(
                source=_screenshot_source(self.path, self.sha256), facts=_screenshot_facts(), screenshot_by_name=False,
                config=EvidenceConfig(), now="2026-01-01T00:00:00+00:00", ledger=ledger, ocr_backend=ocr, vision_backend=vision,
            )
        confidences = sorted(block.confidence for block in evidence.ocr_blocks)
        self.assertEqual(confidences, [0.0, 1.0])
        self.assertNotIn(WARN_LOW_CONFIDENCE, evidence.warning_codes())
        self.assertEqual({block.text for block in evidence.ocr_blocks}, {"zero", "full"})

    def test_a_uniformly_low_confidence_block_is_kept_and_flagged_not_dropped(self):
        blocks = (
            OCRBlock(text="barely legible", polygon=Polygon.from_bbox(0, 0, 10, 10), page=1, confidence=0.1, reading_order=0, engine="e", engine_version="1"),
        )
        ocr = FakeOCRBackend(blocks_by_page={1: blocks})
        vision = FakeVisionBackend(visual=None)
        with _ledger(self.root) as ledger:
            evidence = collect_evidence(
                source=_screenshot_source(self.path, self.sha256), facts=_screenshot_facts(), screenshot_by_name=False,
                config=EvidenceConfig(), now="2026-01-01T00:00:00+00:00", ledger=ledger, ocr_backend=ocr, vision_backend=vision,
            )
        self.assertEqual(len(evidence.ocr_blocks), 1, "a low-confidence block is evidence, not noise to discard")
        self.assertEqual(evidence.ocr_blocks[0].text, "barely legible")
        self.assertIn(WARN_LOW_CONFIDENCE, evidence.warning_codes())


class UnavailableBackendIsNeverCachedTests(unittest.TestCase):
    """§ D / item 12: installing a missing backend must re-ask, which is only
    true if "unavailable" was never written to the cache in the first place."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.png_bytes = _make_png_bytes()
        self.path = self.root / "shot.png"
        self.path.write_bytes(self.png_bytes)
        import hashlib

        self.sha256 = hashlib.sha256(self.png_bytes).hexdigest()

    def test_an_unavailable_ocr_backend_is_asked_again_every_run(self):
        ocr = UnavailableOCRBackend()
        vision = FakeVisionBackend(visual=None)
        with _ledger(self.root) as ledger:
            for _ in range(3):
                collect_evidence(
                    source=_screenshot_source(self.path, self.sha256), facts=_screenshot_facts(), screenshot_by_name=False,
                    config=EvidenceConfig(), now="2026-01-01T00:00:00+00:00", ledger=ledger, ocr_backend=ocr, vision_backend=vision,
                )
            key = cache.CacheKey(content_sha256=self.sha256, kind="ocr", producer=ocr.name, producer_version=ocr.version, pipeline_version=pipeline_version_for(EvidenceConfig()))
            row = ledger.get_file_evidence(key.evidence_id)
        self.assertEqual(ocr.available_calls, 3, "availability must be re-checked, never assumed from a cached row")
        self.assertIsNone(row, "an unavailable result must never be written to the cache")


def _make_png_bytes(*, color=(20, 80, 160)) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _block(text: str) -> OCRBlock:
    return OCRBlock(text=text, polygon=Polygon.from_bbox(0, 0, 50, 20), page=1, confidence=0.9, reading_order=0, engine="e", engine_version="1")


def _visual() -> VisualEvidence:
    return VisualEvidence(image_kind="screenshot", description="a window", entities=(), context="", confidence=0.6, backend="fake", model="fake", model_version="1")


if __name__ == "__main__":
    unittest.main()
