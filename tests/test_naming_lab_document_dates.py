"""Labeled date extraction from document text: precedence, malformed input,
and the "never invent a partial date" rule."""

from __future__ import annotations

import unittest

from tools.naming_lab.dates import KIND_DOCUMENT_DATE, KIND_DUE_DATE, KIND_INVOICE_DATE
from tools.naming_lab.document_dates import extract_document_dates, preferred_document_date


class ExtractDocumentDatesTests(unittest.TestCase):
    def test_invoice_date_beats_due_date(self):
        text = "Invoice Date: 2025-06-15\nDue Date: 2025-07-15\n"
        candidates = extract_document_dates(text)
        preferred = preferred_document_date(candidates)
        self.assertEqual(preferred.kind, KIND_INVOICE_DATE)
        self.assertEqual(preferred.value, "2025-06-15")

    def test_due_date_alone_is_still_usable(self):
        text = "Due Date: 2025-07-15\n"
        preferred = preferred_document_date(extract_document_dates(text))
        self.assertEqual(preferred.kind, KIND_DUE_DATE)
        self.assertEqual(preferred.value, "2025-07-15")

    def test_generic_date_label_is_lower_priority_than_named_kinds(self):
        text = "Date: 2025-01-01\nMeeting Date: 2025-06-15\n"
        preferred = preferred_document_date(extract_document_dates(text))
        self.assertEqual(preferred.kind, "meeting_date")

    def test_generic_date_label_still_extracted_alone(self):
        text = "Date: 2025-06-15\n"
        preferred = preferred_document_date(extract_document_dates(text))
        self.assertEqual(preferred.kind, KIND_DOCUMENT_DATE)
        self.assertEqual(preferred.value, "2025-06-15")

    def test_month_name_literal(self):
        text = "Effective Date: June 15, 2025\n"
        preferred = preferred_document_date(extract_document_dates(text))
        self.assertEqual(preferred.value, "2025-06-15")

    def test_slash_literal(self):
        text = "Receipt Date: 6/15/2025\n"
        preferred = preferred_document_date(extract_document_dates(text))
        self.assertEqual(preferred.value, "2025-06-15")

    def test_malformed_date_is_not_a_candidate(self):
        text = "Invoice Date: 2025-13-40\n"
        self.assertEqual(extract_document_dates(text), ())

    def test_bare_year_never_becomes_a_full_date(self):
        text = "Fiscal Year: 2024\nSummary of activity.\n"
        self.assertEqual(extract_document_dates(text), ())

    def test_quarter_never_becomes_a_full_date(self):
        text = "Q1 2025 performance summary.\n"
        self.assertEqual(extract_document_dates(text), ())

    def test_month_and_year_alone_never_becomes_a_full_date(self):
        text = "Prepared March 2025 for the board.\n"
        self.assertEqual(extract_document_dates(text), ())

    def test_no_text_yields_no_candidates(self):
        self.assertEqual(extract_document_dates(""), ())

    def test_no_date_like_text_yields_no_candidates(self):
        self.assertEqual(extract_document_dates("Just some ordinary prose with no dates at all."), ())

    def test_preferred_document_date_of_empty_is_none(self):
        self.assertIsNone(preferred_document_date(()))


if __name__ == "__main__":
    unittest.main()
