"""`run_candidate` keeps raw compliance and end-to-end success independently
scored, and an extension mismatch never silently suppresses an otherwise
passing end-to-end result."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.ingest.evidence.router import EvidenceRoute
from tools.ingest.evidence.types import FileEvidence, VisualEvidence
from tools.naming_lab.dates import SemanticDateCandidate
from tools.naming_lab.evidence_bridge import LabEvidence
from tools.naming_lab.runner import build_task_text, run_candidate
from tools.naming_lab.seeds import SeedRecord, new_seed
from tools.ingest.naming.evidence import NamingEvidence
from tools.prompts.library import SEMANTIC_NAMING_V1

_SEMANTIC_DATE = SemanticDateCandidate(
    value="2025-06-15", kind="invoice_date", source="document_text_or_ocr", confidence="high",
)


def _lab_evidence(original_filename: str = "IMG_scan_9.pdf") -> LabEvidence:
    naming_evidence = NamingEvidence(
        source_class="naming-lab", original_filename=original_filename, extension=".pdf",
        size=1024, mime="application/pdf", media_kind="document",
        name_quality="poor", name_quality_reason="machine-generated stem",
    )
    return LabEvidence(
        sha256="a" * 64, naming_evidence=naming_evidence, file_evidence=None,
        semantic_date=_SEMANTIC_DATE, document_date_candidates=(_SEMANTIC_DATE,),
    )


def _seed() -> SeedRecord:
    return new_seed(
        sha256="a" * 64, original_filename="IMG_scan_9.pdf", media_kind="document",
        extension=".pdf", semantic_date=_SEMANTIC_DATE,
        accepted_filenames=("25_0615_northwind-invoice.pdf",),
    )


class FakeProvider:
    """A stand-in for `OllamaTextProvider`, shaped like its `.generate()` contract."""

    def __init__(self, endpoint: str, model: str, **_kwargs):
        self.endpoint = endpoint
        self.model = model

    def generate(self, prompt: str, *, timeout_seconds=None, **_kwargs):
        return {
            "output": self.__class__.OUTPUT, "finish_reason": "stop",
            "input_tokens": 100, "output_tokens": 8,
        }


class RunCandidateTests(unittest.TestCase):
    def test_wrong_raw_extension_is_a_raw_compliance_failure_but_not_an_end_to_end_one(self):
        FakeProvider.OUTPUT = "northwind-invoice.jpeg"  # wrong extension
        with patch("tools.naming_lab.runner.OllamaTextProvider", FakeProvider):
            result = run_candidate(_seed(), _lab_evidence(), prompt=SEMANTIC_NAMING_V1, model_name="fake-model")
        self.assertFalse(result.raw_compliance.passed)
        self.assertFalse(result.raw_compliance.extension_matches)
        self.assertTrue(result.end_to_end.passed)
        self.assertTrue(result.end_to_end.filename.endswith(".pdf"))

    def test_well_formed_output_passes_both(self):
        FakeProvider.OUTPUT = "25_0615_northwind-invoice.pdf"
        with patch("tools.naming_lab.runner.OllamaTextProvider", FakeProvider):
            result = run_candidate(_seed(), _lab_evidence(), prompt=SEMANTIC_NAMING_V1, model_name="fake-model")
        self.assertTrue(result.raw_compliance.passed)
        self.assertTrue(result.end_to_end.passed)
        self.assertTrue(result.semantic.exact_accepted_match)

    def test_provider_connection_error_is_recorded_not_raised(self):
        class ExplodingProvider:
            def __init__(self, endpoint, model, **_kwargs):
                pass

            def generate(self, *args, **kwargs):
                raise ConnectionError("Ollama unavailable")

        with patch("tools.naming_lab.runner.OllamaTextProvider", ExplodingProvider):
            result = run_candidate(_seed(), _lab_evidence(), prompt=SEMANTIC_NAMING_V1, model_name="fake-model")
        self.assertIsNotNone(result.error)
        self.assertIsNone(result.raw_output)
        self.assertIsNone(result.raw_compliance)
        self.assertIsNone(result.end_to_end)

    def test_empty_raw_output_fails_end_to_end_safely(self):
        FakeProvider.OUTPUT = ""
        with patch("tools.naming_lab.runner.OllamaTextProvider", FakeProvider):
            result = run_candidate(_seed(), _lab_evidence(), prompt=SEMANTIC_NAMING_V1, model_name="fake-model")
        self.assertFalse(result.end_to_end.passed)
        self.assertIsNotNone(result.end_to_end.error)

    def test_both_results_persist_independently_on_the_result_object(self):
        FakeProvider.OUTPUT = "northwind-invoice.txt"  # both wrong extension AND otherwise fine stem
        with patch("tools.naming_lab.runner.OllamaTextProvider", FakeProvider):
            result = run_candidate(_seed(), _lab_evidence(), prompt=SEMANTIC_NAMING_V1, model_name="fake-model")
        # Neither field is derived from the other -- both are independently present.
        self.assertIsNotNone(result.raw_compliance)
        self.assertIsNotNone(result.end_to_end)
        self.assertNotEqual(result.raw_compliance.passed, True)  # wrong extension
        self.assertEqual(result.end_to_end.passed, True)  # deterministic normalization fixed it


class BuildTaskTextIncludesVisualDescriptionTests(unittest.TestCase):
    """A video's (or photo's) vision-backend description must reach the
    naming prompt -- perceiving it and then never telling the model about it
    would make the vision integration pointless."""

    def test_visual_description_and_entities_appear_in_the_task_text(self):
        naming_evidence = NamingEvidence(
            source_class="naming-lab", original_filename="IMG_clip.mp4", extension=".mp4",
            size=2048, mime="video/mp4", media_kind="video",
            name_quality="poor", name_quality_reason="machine-generated stem",
        )
        route = EvidenceRoute(native_text=False, ocr=False, vision=True, reason="video: vision only", rules=("r14-video-frame",))
        visual = VisualEvidence(
            image_kind="ordinary_photo", description="a hawk perched on a wooden fence at dusk",
            entities=("hawk", "fence"), context="outdoor wildlife shot",
            confidence=0.8, backend="ollama", model="qwen3-vl:8b", model_version="1",
        )
        file_evidence = FileEvidence(
            content_sha256="a" * 64, mime="video/mp4", media_kind="video",
            facts={}, route=route, visual=visual,
        )
        lab_evidence = LabEvidence(
            sha256="a" * 64, naming_evidence=naming_evidence, file_evidence=file_evidence,
            semantic_date=None, document_date_candidates=(),
        )
        text = build_task_text(lab_evidence)
        self.assertIn("a hawk perched on a wooden fence at dusk", text)
        self.assertIn("hawk, fence", text)
        self.assertIn("outdoor wildlife shot", text)

    def test_no_visual_evidence_omits_the_section_without_crashing(self):
        naming_evidence = NamingEvidence(
            source_class="naming-lab", original_filename="notes.txt", extension=".txt",
            size=10, mime="text/plain", media_kind="text", name_quality="good", name_quality_reason="fine",
        )
        lab_evidence = LabEvidence(
            sha256="b" * 64, naming_evidence=naming_evidence, file_evidence=None,
            semantic_date=None, document_date_candidates=(),
        )
        text = build_task_text(lab_evidence)
        self.assertNotIn("visual_description", text)


if __name__ == "__main__":
    unittest.main()
