"""vNext grammar: raw compliance vs. end-to-end kept separate; extension
authority; no path-traversal/unsafe-filename regression."""

from __future__ import annotations

import unittest

from tools.naming_lab.dates import SemanticDateCandidate
from tools.naming_lab.grammar import check_raw_compliance, compose_filename, validate_end_to_end

_DATE = SemanticDateCandidate(
    value="2025-06-15", kind="invoice_date", source="document_text_or_ocr", confidence="high",
)


class ComposeFilenameTests(unittest.TestCase):
    def test_with_semantic_date(self):
        self.assertEqual(compose_filename(_DATE, "northwind invoice", ".pdf"), "25_0615_northwind-invoice.pdf")

    def test_without_semantic_date(self):
        self.assertEqual(compose_filename(None, "mountain sunset", ".jpg"), "mountain-sunset.jpg")

    def test_source_extension_is_authoritative_over_a_model_supplied_one(self):
        # The model's raw text carries a `.txt`-shaped tail, but the real
        # extension supplied by the caller (`.pdf`, from the source file) wins.
        filename = compose_filename(_DATE, "northwind-invoice.txt", ".pdf")
        self.assertTrue(filename.endswith(".pdf"))
        self.assertNotIn(".txt", filename)

    def test_strips_a_legacy_grammar_date_prefix_the_model_emitted(self):
        filename = compose_filename(_DATE, "2025-06-15_northwind-invoice", ".pdf")
        self.assertEqual(filename, "25_0615_northwind-invoice.pdf")

    def test_strips_a_vnext_shaped_date_prefix_the_model_emitted(self):
        # Trusted code computes the prefix itself; a model-emitted one is
        # discarded rather than trusted or duplicated.
        filename = compose_filename(_DATE, "25_0615_northwind-invoice", ".pdf")
        self.assertEqual(filename, "25_0615_northwind-invoice.pdf")

    def test_empty_stem_yields_empty_string(self):
        self.assertEqual(compose_filename(_DATE, "   ", ".pdf"), "")

    def test_path_leakage_in_model_text_is_stripped_to_the_basename(self):
        filename = compose_filename(None, "../../etc/passwd", ".txt")
        self.assertNotIn("..", filename)
        self.assertNotIn("/", filename)


class RawComplianceTests(unittest.TestCase):
    def test_well_formed_vnext_output_passes(self):
        result = check_raw_compliance("25_0615_northwind-invoice.pdf", expected_extension=".pdf")
        self.assertTrue(result.passed)
        self.assertEqual(result.reasons, ())

    def test_no_date_output_passes(self):
        result = check_raw_compliance("mountain-sunset.jpg", expected_extension=".jpg")
        self.assertTrue(result.passed)

    def test_wrong_extension_fails_raw_compliance(self):
        result = check_raw_compliance("northwind-invoice.jpeg", expected_extension=".pdf")
        self.assertFalse(result.passed)
        self.assertFalse(result.extension_matches)

    def test_prose_fails_raw_compliance(self):
        result = check_raw_compliance(
            "Sure! Here is a filename: northwind-invoice.pdf", expected_extension=".pdf",
        )
        self.assertFalse(result.passed)
        self.assertFalse(result.no_prose)

    def test_multiline_output_fails_raw_compliance(self):
        result = check_raw_compliance("northwind-invoice.pdf\nextra line", expected_extension=".pdf")
        self.assertFalse(result.passed)
        self.assertFalse(result.single_line)

    def test_path_separator_fails_as_leakage(self):
        result = check_raw_compliance("some/path/invoice.pdf", expected_extension=".pdf")
        self.assertFalse(result.passed)
        self.assertFalse(result.no_leakage)

    def test_prompt_echo_fails_as_leakage(self):
        result = check_raw_compliance("Policy: northwind-invoice.pdf", expected_extension=".pdf")
        self.assertFalse(result.no_leakage)

    def test_empty_output_fails(self):
        result = check_raw_compliance("", expected_extension=".pdf")
        self.assertFalse(result.passed)


class ExtensionAuthoritySeparationTests(unittest.TestCase):
    """A wrong model-supplied extension is a raw-compliance failure but does
    not automatically fail end-to-end, because `compose_filename` always uses
    the real source extension (spec section 4)."""

    def test_wrong_raw_extension_does_not_block_end_to_end_success(self):
        raw_output = "northwind-invoice.jpeg"  # model guessed the wrong extension
        true_extension = ".pdf"
        raw_compliance = check_raw_compliance(raw_output, expected_extension=true_extension)
        self.assertFalse(raw_compliance.passed)  # visible as a raw-compliance failure

        deterministic = compose_filename(_DATE, raw_output, true_extension)
        end_to_end = validate_end_to_end(
            deterministic, original_filename="IMG_scan_9.pdf", max_filename_length=120, semantic_date=_DATE,
        )
        self.assertTrue(end_to_end.passed)  # not automatically an end-to-end failure
        self.assertTrue(end_to_end.filename.endswith(".pdf"))


class ValidateEndToEndTests(unittest.TestCase):
    def test_valid_proposal_passes(self):
        result = validate_end_to_end(
            "25_0615_northwind-invoice.pdf", original_filename="IMG_scan_9.pdf",
            max_filename_length=120, semantic_date=_DATE,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.filename, "25_0615_northwind-invoice.pdf")

    def test_vnext_underscore_survives_without_a_semantic_date_hint(self):
        # Without `semantic_date`, the bridge cannot apply -- this documents
        # (rather than hides) that policy.py's separator normalization still
        # folds an un-bridged `YY_MMDD_`-shaped prefix's underscore to a
        # hyphen. Callers that have a `SemanticDateCandidate` (every real
        # Lab run does) always pass it; this is the honest fallback shape.
        result = validate_end_to_end(
            "25_0615_northwind-invoice.pdf", original_filename="IMG_scan_9.pdf", max_filename_length=120,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.filename, "25-0615-northwind-invoice.pdf")

    def test_empty_proposal_fails_safely(self):
        result = validate_end_to_end("", original_filename="IMG_scan_9.pdf", max_filename_length=120)
        self.assertFalse(result.passed)
        self.assertIsNone(result.filename)
        self.assertIsNotNone(result.error)

    def test_path_traversal_proposal_is_rejected(self):
        result = validate_end_to_end(
            "../../etc/passwd", original_filename="IMG_scan_9.pdf", max_filename_length=120,
        )
        self.assertFalse(result.passed)

    def test_reserved_windows_device_stem_is_not_rejected_outright(self):
        # `policy.validate_proposal` prefixes reserved stems rather than
        # refusing them; this proves the Lab did not weaken that behavior.
        result = validate_end_to_end("con", original_filename="notes.txt", max_filename_length=120)
        self.assertTrue(result.passed)
        self.assertTrue(result.filename.startswith("file-con"))


if __name__ == "__main__":
    unittest.main()
