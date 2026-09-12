"""End-to-end evidence collection: the right stages run, and only those.

`collect_evidence` is where routing, perception, caching, and normalization
meet. This suite proves the four routing behaviours the spec calls out by
name (embedded-text PDFs never reach OCR; a scanned PDF does; a screenshot
gets both OCR and vision; an ordinary photo gets vision and nothing else) by
counting backend calls rather than trusting the route alone -- a router bug
that still happened to route correctly would not be caught by asserting on
`route.rules` alone. It also proves the harder claim in invariant 3: nothing
a backend can hand back -- an unavailable engine, an engine that raises, one
that times out, one that returns structurally garbage data -- can turn one
bad file into a crashed batch, and `run_source` keeps going over a directory
that mixes truncated, corrupt, and zero-byte files with good ones.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.ingest.config import EvidenceConfig
from tools.ingest.evidence.extract import collect_evidence
from tools.ingest.evidence.raster import pdfium_available
from tools.ingest.evidence.types import (
    IMAGE_KIND_ORDINARY_PHOTO,
    IMAGE_KIND_SCREENSHOT,
    WARN_BACKEND_UNAVAILABLE,
    WARN_MALFORMED,
    WARN_TRUNCATED,
    EvidenceSource,
    VisualEvidence,
)
from tools.ingest.ledger import LocalLedger
from tools.ingest.media.probe import MEDIA_DOCUMENT, MEDIA_IMAGE, probe_file
from tools.ingest.pipeline import run_source

from tests.ingest.evidence_fakes import (
    ExplodingOCRBackend,
    FakeOCRBackend,
    FakeVisionBackend,
    GarbageOCRBackend,
    make_docx,
    make_scanned_pdf,
    make_text_pdf,
)
from tests.ingest.fi_helpers import fi_config, make_blob, make_camera_jpeg, make_png


def _visual(kind: str) -> VisualEvidence:
    return VisualEvidence(image_kind=kind, description="", entities=(), context="", confidence=0.6, backend="fake", model="fake", model_version="1")


class EmbeddedTextPdfAvoidsOcrTests(unittest.TestCase):
    """Item 1."""

    def test_a_pdf_with_a_real_text_layer_routes_native_only_and_never_calls_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_text_pdf(
                Path(tmp) / "letter.pdf",
                [
                    "Dear Alex,",
                    "The kitchen renders are attached for your review ahead of Friday.",
                    "Let me know if the cabinet finish needs another pass.",
                    "Best, Sam",
                ],
            )
            facts = probe_file(path).facts
            self.assertTrue(facts.get("text_available"))

            ocr = FakeOCRBackend()
            source = EvidenceSource(path=path, sha256="x" * 64, mime="application/pdf", media_kind=MEDIA_DOCUMENT, size=path.stat().st_size)
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=False, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr,
            )

        self.assertEqual(evidence.route.rules, ("r3-pdf-embedded-text",))
        self.assertFalse(evidence.route.ocr)
        self.assertEqual(ocr.calls, [], "an embedded-text pdf must never be sent to OCR")
        self.assertIsNotNone(evidence.native_text)
        self.assertIn("kitchen renders", evidence.native_text.text)
        self.assertEqual(len(evidence.native_text.sections), 1)


class OfficeDocumentIsNativeOnlyTests(unittest.TestCase):
    """r5-office-document: a `.docx` gets its own XML text reader, never OCR."""

    def test_a_docx_extracts_text_and_headings_without_ever_calling_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_docx(
                Path(tmp) / "scope-of-work.docx",
                ["The vendor will deliver the following by the agreed date."],
                headings=[("Scope of Work", 1)],
            )
            facts = probe_file(path).facts
            ocr = FakeOCRBackend()
            source = EvidenceSource(path=path, sha256="x" * 64, mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", media_kind=MEDIA_DOCUMENT, size=path.stat().st_size)
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=False, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr,
            )

        self.assertEqual(evidence.route.rules, ("r5-office-document",))
        self.assertEqual(ocr.calls, [], "an office document must never be sent to OCR")
        self.assertIn("vendor will deliver", evidence.native_text.text)
        self.assertTrue(evidence.structure.headings)
        self.assertEqual(evidence.structure.headings[0].text, "Scope of Work")


class ScannedPdfRoutesToOcrTests(unittest.TestCase):
    """Item 2."""

    @unittest.skipUnless(pdfium_available(), "requires pypdfium2 to rasterize the pdf")
    def test_an_image_only_pdf_routes_to_ocr_and_the_backend_is_called(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_scanned_pdf(Path(tmp) / "scan.pdf")
            facts = probe_file(path).facts
            self.assertFalse(facts.get("text_available", False))

            ocr = FakeOCRBackend(blocks_by_page={1: (_ocr_block("scanned text"),)})
            source = EvidenceSource(path=path, sha256="x" * 64, mime="application/pdf", media_kind=MEDIA_DOCUMENT, size=path.stat().st_size)
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=False, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr,
            )

        self.assertEqual(evidence.route.rules, ("r4-pdf-no-embedded-text",))
        self.assertTrue(evidence.route.ocr)
        self.assertEqual(ocr.calls, [1])
        self.assertTrue(evidence.ocr_blocks)

    def test_without_pdfium_rasterization_degrades_to_a_warning_not_an_exception(self):
        """The graceful-degradation path, forced regardless of whether
        pypdfium2 happens to be installed on the machine running this test."""
        import tools.ingest.evidence.raster as raster_module

        with tempfile.TemporaryDirectory() as tmp:
            path = make_scanned_pdf(Path(tmp) / "scan.pdf")
            facts = probe_file(path).facts
            ocr = FakeOCRBackend(blocks_by_page={1: (_ocr_block("scanned text"),)})
            source = EvidenceSource(path=path, sha256="x" * 64, mime="application/pdf", media_kind=MEDIA_DOCUMENT, size=path.stat().st_size)

            original = raster_module.pdfium_available
            raster_module.pdfium_available = lambda: False
            try:
                evidence = collect_evidence(
                    source=source, facts=facts, screenshot_by_name=False, config=EvidenceConfig(),
                    now="2026-01-01T00:00:00+00:00", ocr_backend=ocr,
                )
            finally:
                raster_module.pdfium_available = original

        self.assertEqual(evidence.ocr_blocks, ())
        self.assertEqual(ocr.calls, [], "no pages to recognize means the backend is never called")
        self.assertIn(WARN_BACKEND_UNAVAILABLE, evidence.warning_codes())


class ScreenshotRoutesToOcrAndVisionTests(unittest.TestCase):
    """Item 3."""

    def test_a_screenshot_calls_both_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_png(Path(tmp) / "Screenshot 2026-09-12 at 10.14.32 AM.png")
            facts = probe_file(path).facts
            ocr = FakeOCRBackend(blocks_by_page={1: (_ocr_block("menu bar"),)})
            vision = FakeVisionBackend(visual=_visual(IMAGE_KIND_SCREENSHOT))
            source = EvidenceSource(path=path, sha256="x" * 64, mime="image/png", media_kind=MEDIA_IMAGE, size=path.stat().st_size)
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=True, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )

        self.assertEqual(ocr.calls, [1])
        self.assertEqual(vision.calls, 1)
        self.assertEqual(evidence.visual.image_kind, IMAGE_KIND_SCREENSHOT)
        self.assertTrue(evidence.ocr_blocks)


class OrdinaryPhotoIsVisionOnlyTests(unittest.TestCase):
    """Item 4."""

    def test_a_camera_photo_calls_vision_and_never_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_camera_jpeg(Path(tmp) / "IMG_1234.JPG")
            facts = probe_file(path).facts
            self.assertIn("camera_make", facts)
            ocr = FakeOCRBackend()
            vision = FakeVisionBackend(visual=_visual(IMAGE_KIND_ORDINARY_PHOTO))
            source = EvidenceSource(path=path, sha256="x" * 64, mime="image/jpeg", media_kind=MEDIA_IMAGE, size=path.stat().st_size)
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=False, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )

        self.assertEqual(ocr.calls, [], "a camera photo must never reach OCR")
        self.assertEqual(vision.calls, 1)
        self.assertEqual(evidence.visual.image_kind, IMAGE_KIND_ORDINARY_PHOTO)


class BackendDegradationTests(unittest.TestCase):
    """Item 12: nothing a backend does can escape as an exception."""

    def _source(self, tmp: Path) -> tuple[EvidenceSource, dict]:
        path = make_png(tmp / "Screenshot 2026-09-12 at 10.14.32 AM.png")
        facts = probe_file(path).facts
        return EvidenceSource(path=path, sha256="x" * 64, mime="image/png", media_kind=MEDIA_IMAGE, size=path.stat().st_size), facts

    def test_null_backends_degrade_to_a_recorded_unavailable_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, facts = self._source(Path(tmp))
            before = source.path.read_bytes()
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=True, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00",
                # No ocr_backend/vision_backend injected: the real defaults
                # (null OCR, heuristic vision) are used.
            )
            self.assertEqual(source.path.read_bytes(), before, "the owner file must never be touched")
        self.assertIn(WARN_BACKEND_UNAVAILABLE, evidence.warning_codes())
        statuses = {record.kind: record.status for record in evidence.provenance}
        self.assertEqual(statuses.get("ocr"), "unavailable")

    def test_a_backend_whose_available_call_itself_raises_degrades_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, facts = self._source(Path(tmp))
            ocr = FakeOCRBackend(available_raises=True)
            vision = FakeVisionBackend(visual=None)
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=True, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )
        self.assertIn(WARN_BACKEND_UNAVAILABLE, evidence.warning_codes())
        self.assertEqual(ocr.calls, [], "recognize() must never be attempted once availability itself blew up")

    def test_a_backend_that_raises_out_of_recognize_degrades_to_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, facts = self._source(Path(tmp))
            ocr = ExplodingOCRBackend()
            vision = FakeVisionBackend(visual=None)
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=True, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )
        self.assertIn(WARN_MALFORMED, evidence.warning_codes())
        statuses = {record.kind: record.status for record in evidence.provenance}
        self.assertEqual(statuses.get("ocr"), "partial")  # one bad page; see extract.py's _ocr_stage

    def test_a_backend_that_times_out_degrades_to_a_warning_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, facts = self._source(Path(tmp))
            ocr = ExplodingOCRBackend(exc=TimeoutError("ocr backend timed out"))
            vision = FakeVisionBackend(visual=None)
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=True, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )
        self.assertIn(WARN_MALFORMED, evidence.warning_codes())

    def test_garbage_blocks_with_a_valid_page_number_degrade_to_clamped_values(self):
        """Wrong types, NaN confidence, negative/degenerate geometry, and 10k
        blocks all land safely: text is coerced to a string, confidence
        clamps to 0.0, geometry falls back to the page bounds, and the block
        count is capped with a truncation warning."""
        with tempfile.TemporaryDirectory() as tmp:
            source, facts = self._source(Path(tmp))
            ocr = GarbageOCRBackend(page_value=1)
            vision = FakeVisionBackend(visual=None)
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=True, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )
        self.assertLessEqual(len(evidence.ocr_blocks), EvidenceConfig().ocr.max_blocks)
        self.assertIn(WARN_TRUNCATED, evidence.warning_codes())
        for block in evidence.ocr_blocks:
            self.assertEqual(block.confidence, 0.0)
            self.assertTrue(block.polygon.is_valid)

    def test_a_non_numeric_page_value_falls_back_to_the_raster_page_without_raising(self):
        """`_normalize_ocr_blocks`/`_page_number` (tools/ingest/evidence/extract.py)
        treat a backend's own `page` attribute as a convenience, honoured only
        when it parses to a real 1-based integer; anything else -- a string,
        `None`, zero, negative -- falls back to the page this package actually
        handed the backend, the same defensive posture confidence gets via
        `_clamp` and geometry gets via `Polygon.is_valid`."""
        with tempfile.TemporaryDirectory() as tmp:
            source, facts = self._source(Path(tmp))
            ocr = GarbageOCRBackend(page_value="not-a-number", block_count=1)
            vision = FakeVisionBackend(visual=None)
            evidence = collect_evidence(
                source=source, facts=facts, screenshot_by_name=True, config=EvidenceConfig(),
                now="2026-01-01T00:00:00+00:00", ocr_backend=ocr, vision_backend=vision,
            )
        self.assertEqual(len(evidence.ocr_blocks), 1)
        self.assertEqual(evidence.ocr_blocks[0].page, 1)


class BatchResilienceTests(unittest.TestCase):
    """Item 13: one corrupt file must not abort a scan over the rest."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.downloads = self.root / "Downloads"
        self.downloads.mkdir()
        self.config = fi_config(self.root, downloads=self.downloads)

    def test_a_mixed_batch_of_corrupt_and_good_files_completes_without_raising(self):
        make_blob(self.downloads / "truncated.pdf", b"%PDF-1.4 this is not a real pdf" + b"\x00" * 40)
        make_blob(self.downloads / "corrupt.png", b"\x89PNG\r\n\x1a\nnot actually png data" + b"\xff" * 40)
        make_blob(self.downloads / "empty.bin", b"")
        good_text = make_blob(self.downloads / "meeting-notes.txt", b"Notes from the kickoff call today." * 20)
        good_image = make_png(self.downloads / "diagram.png")

        with LocalLedger(self.config.local_ledger_path) as ledger:
            report = run_source(
                self.config, ledger, source_class="downloads", source_volume_id="mac-local",
                apply=True, settle_seconds=1, resolved_t7=None,
            )

        self.assertEqual(report.discovered, 5)
        self.assertEqual(len(report.outcomes), 5, "naming/triage must still evaluate every file")
        by_name = {outcome.original_filename: outcome for outcome in report.outcomes}

        self.assertIn("meeting-notes.txt", by_name)
        self.assertTrue(by_name["meeting-notes.txt"].evidence_kinds, "the good text file must get evidence")
        self.assertIn("diagram.png", by_name)
        self.assertTrue(by_name["diagram.png"].evidence_kinds, "the good image must get evidence")

        self.assertTrue(good_text.exists())
        self.assertTrue(good_image.exists())


def _ocr_block(text: str):
    from tools.ingest.evidence.types import OCRBlock, Polygon

    return OCRBlock(text=text, polygon=Polygon.from_bbox(0, 0, 50, 20), page=1, confidence=0.9, reading_order=0, engine="e", engine_version="1")


if __name__ == "__main__":
    unittest.main()
