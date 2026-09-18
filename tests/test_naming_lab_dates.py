"""`YYYY-MM-DD -> YY_MMDD`, semantic-date precedence, malformed/absent input."""

from __future__ import annotations

import unittest

from tools.naming_lab.dates import (
    SemanticDateCandidate,
    capture_date_candidate,
    derive_semantic_date,
    format_date_prefix,
    machine_filename_date,
)


class FormatDatePrefixTests(unittest.TestCase):
    def test_ordinary_date(self):
        self.assertEqual(format_date_prefix("2025-06-15"), "25_0615")

    def test_century_boundary_year_2000(self):
        self.assertEqual(format_date_prefix("2000-01-01"), "00_0101")

    def test_late_century_year(self):
        self.assertEqual(format_date_prefix("1999-12-31"), "99_1231")

    def test_single_digit_month_and_day_are_zero_padded(self):
        self.assertEqual(format_date_prefix("2025-01-05"), "25_0105")

    def test_year_past_2099(self):
        self.assertEqual(format_date_prefix("2101-03-04"), "01_0304")

    def test_none_is_none(self):
        self.assertIsNone(format_date_prefix(None))

    def test_empty_string_is_none(self):
        self.assertIsNone(format_date_prefix(""))

    def test_malformed_month_is_none(self):
        self.assertIsNone(format_date_prefix("2025-13-01"))

    def test_malformed_day_is_none(self):
        self.assertIsNone(format_date_prefix("2025-02-30"))

    def test_wrong_shape_is_none(self):
        self.assertIsNone(format_date_prefix("06/15/2025"))
        self.assertIsNone(format_date_prefix("2025-6-15"))
        self.assertIsNone(format_date_prefix("not a date"))

    def test_partial_date_is_none(self):
        self.assertIsNone(format_date_prefix("2025"))
        self.assertIsNone(format_date_prefix("2025-06"))


class SemanticDateCandidateTests(unittest.TestCase):
    def test_rejects_invalid_value(self):
        with self.assertRaises(ValueError):
            SemanticDateCandidate(value="not-a-date", kind="invoice_date", source="x", confidence="high")


class MachineFilenameDateTests(unittest.TestCase):
    def test_recognized_prefix_with_valid_embedded_date(self):
        candidate = machine_filename_date("IMG_20250615_120000.jpg")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.value, "2025-06-15")
        self.assertEqual(candidate.source, "machine_filename")

    def test_unrecognized_prefix_yields_no_candidate(self):
        self.assertIsNone(machine_filename_date("vacation-20250615.jpg"))

    def test_recognized_prefix_but_invalid_date_yields_no_candidate(self):
        self.assertIsNone(machine_filename_date("IMG_99999999_x.jpg"))

    def test_recognized_prefix_with_no_embedded_date(self):
        self.assertIsNone(machine_filename_date("IMG_family_photo.jpg"))


class CaptureDateCandidateTests(unittest.TestCase):
    def test_valid_capture_date(self):
        candidate = capture_date_candidate("2025-06-15")
        self.assertEqual(candidate.kind, "capture_date")
        self.assertEqual(candidate.source, "capture_metadata")

    def test_none_capture_date(self):
        self.assertIsNone(capture_date_candidate(None))

    def test_malformed_capture_date(self):
        self.assertIsNone(capture_date_candidate("bogus"))


class DeriveSemanticDateTests(unittest.TestCase):
    def test_document_date_beats_capture_metadata(self):
        document = SemanticDateCandidate(
            value="2025-06-15", kind="invoice_date", source="document_text_or_ocr", confidence="high",
        )
        result = derive_semantic_date(
            document_candidates=(document,), capture_date="2025-01-01", original_filename="IMG_20250101_1.jpg",
        )
        self.assertEqual(result, document)

    def test_capture_metadata_beats_machine_filename_date(self):
        result = derive_semantic_date(
            document_candidates=(), capture_date="2025-06-15", original_filename="IMG_20250101_1.jpg",
        )
        self.assertEqual(result.value, "2025-06-15")
        self.assertEqual(result.source, "capture_metadata")

    def test_machine_filename_date_is_last_resort(self):
        result = derive_semantic_date(document_candidates=(), capture_date=None, original_filename="IMG_20250615_1.jpg")
        self.assertEqual(result.value, "2025-06-15")
        self.assertEqual(result.source, "machine_filename")

    def test_no_evidence_at_all_yields_no_prefix_rather_than_a_guess(self):
        result = derive_semantic_date(document_candidates=(), capture_date=None, original_filename="vacation-photo.jpg")
        self.assertIsNone(result)

    def test_filesystem_modified_date_is_never_a_parameter(self):
        # `derive_semantic_date` structurally has no `modified_date` parameter --
        # a caller cannot accidentally feed filesystem mtime into it.
        import inspect

        parameters = inspect.signature(derive_semantic_date).parameters
        self.assertNotIn("modified_date", parameters)


if __name__ == "__main__":
    unittest.main()
