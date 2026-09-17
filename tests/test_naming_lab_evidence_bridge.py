"""The Lab's evidence bridge: production NamingEvidence + OCR/native-text
evidence, combined -- without ever calling a non-local backend."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.ingest.config import EvidenceConfig
from tools.naming_lab.evidence_bridge import gather_lab_evidence

from tests.ingest.evidence_fakes import FakeOCRBackend


class GatherLabEvidenceTests(unittest.TestCase):
    def test_plain_text_invoice_date_becomes_the_semantic_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.txt"
            path.write_text(
                "Invoice Date: 2025-06-15\nDue Date: 2025-07-15\nThank you for your business.\n"
            )
            evidence = gather_lab_evidence(path, evidence_config=EvidenceConfig())
            self.assertIsNotNone(evidence.semantic_date)
            self.assertEqual(evidence.semantic_date.value, "2025-06-15")
            self.assertEqual(evidence.semantic_date.kind, "invoice_date")
            self.assertEqual(evidence.semantic_date.source, "document_text_or_ocr")

    def test_evidence_disabled_yields_no_file_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.txt"
            path.write_text("Invoice Date: 2025-06-15\n")
            evidence = gather_lab_evidence(path, evidence_config=EvidenceConfig(enabled=False))
            self.assertIsNone(evidence.file_evidence)
            self.assertEqual(evidence.document_date_candidates, ())
            # No capture metadata and no machine-filename date either -- no guess.
            self.assertIsNone(evidence.semantic_date)

    def test_ocr_backend_is_never_called_for_a_plain_text_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.txt"
            path.write_text("Invoice Date: 2025-06-15\n")
            ocr = FakeOCRBackend()
            gather_lab_evidence(path, evidence_config=EvidenceConfig(), ocr_backend=ocr)
            self.assertEqual(ocr.calls, [])

    def test_naming_evidence_and_sha256_are_always_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("just some notes\n")
            evidence = gather_lab_evidence(path)
            self.assertEqual(evidence.naming_evidence.original_filename, "notes.txt")
            self.assertEqual(len(evidence.sha256), 64)

    def test_combined_text_is_empty_when_evidence_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("hello\n")
            evidence = gather_lab_evidence(path, evidence_config=EvidenceConfig(enabled=False))
            self.assertEqual(evidence.combined_text, "")


if __name__ == "__main__":
    unittest.main()
