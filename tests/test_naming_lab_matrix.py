"""Aggregate matrix rates: explicit numerator/denominator, no opaque score."""

from __future__ import annotations

import unittest

from tools.naming_lab.dates import SemanticDateCandidate
from tools.naming_lab.grammar import EndToEndResult, RawComplianceResult
from tools.naming_lab.matrix import build_matrix
from tools.naming_lab.runner import HallucinationFlags, NamingLabResult, SemanticResult
from tools.naming_lab.seeds import new_seed

_SEMANTIC_DATE = SemanticDateCandidate(
    value="2025-06-15", kind="invoice_date", source="document_text_or_ocr", confidence="high",
)


def _raw(passed: bool, no_leakage: bool = True) -> RawComplianceResult:
    return RawComplianceResult(
        passed=passed, single_line=True, no_prose=True, no_leakage=no_leakage,
        extension_matches=True, grammar_matches=passed, reasons=() if passed else ("bad",),
    )


def _e2e(filename: str | None, passed: bool = True) -> EndToEndResult:
    return EndToEndResult(passed=passed, filename=filename, changed=True, policy_reason="ok", error=None)


def _semantic(exact: bool, passed: bool = True) -> SemanticResult:
    return SemanticResult(
        exact_accepted_match=exact, approved_alternate_match=exact,
        satisfies_required_semantics=passed, violates_forbidden_semantics=False, passed=passed,
    )


def _result(seed_sha, filename, *, exact=True, e2e_ok=True, raw_ok=True, error=None, finish_reason="stop") -> NamingLabResult:
    return NamingLabResult(
        seed_sha256=seed_sha, prompt_id="p", prompt_version="1", model_name="m",
        raw_output=filename, finish_reason=finish_reason, input_tokens=1, output_tokens=1,
        raw_compliance=_raw(raw_ok), deterministic_filename=filename,
        end_to_end=_e2e(filename, e2e_ok), semantic=_semantic(exact, e2e_ok),
        hallucination=HallucinationFlags(unsupported_date=False, unsupported_entities=()),
        error=error, created_at="2026-01-01T00:00:00+00:00",
    )


class BuildMatrixTests(unittest.TestCase):
    def test_rates_have_correct_numerator_and_denominator(self):
        seed = new_seed(
            sha256="a" * 64, original_filename="x.pdf", media_kind="document", extension=".pdf",
            semantic_date=_SEMANTIC_DATE, accepted_filenames=("25_0615_x.pdf",),
        )
        results = (
            _result("a" * 64, "25_0615_x.pdf", exact=True, e2e_ok=True, raw_ok=True),
            _result("a" * 64, "25_0615_y.pdf", exact=False, e2e_ok=True, raw_ok=True),
            _result("a" * 64, None, exact=False, e2e_ok=False, raw_ok=False),
        )
        matrix = build_matrix((seed,), results)
        self.assertEqual(len(matrix.cells), 3)

        exact_rate = matrix.rates["exact_human_accepted_rate"]
        self.assertEqual((exact_rate.numerator, exact_rate.denominator), (1, 3))

        e2e_rate = matrix.rates["production_policy_success_rate"]
        self.assertEqual((e2e_rate.numerator, e2e_rate.denominator), (2, 3))

    def test_date_selection_accuracy_excludes_non_applicable_results(self):
        seed = new_seed(
            sha256="a" * 64, original_filename="x.pdf", media_kind="document",
            extension=".pdf", semantic_date=_SEMANTIC_DATE,
        )
        results = (
            _result("a" * 64, "25_0615_x.pdf", e2e_ok=True),  # correct prefix
            _result("a" * 64, None, e2e_ok=False),  # not applicable -- excluded
        )
        matrix = build_matrix((seed,), results)
        rate = matrix.rates["date_selection_accuracy"]
        self.assertEqual((rate.numerator, rate.denominator), (1, 1))

    def test_empty_results_yield_zero_denominator_not_a_crash(self):
        matrix = build_matrix((), ())
        for rate in matrix.rates.values():
            self.assertEqual(rate.denominator, 0)
            self.assertIsNone(rate.value)

    def test_provider_error_is_counted(self):
        seed = new_seed(sha256="a" * 64, original_filename="x.pdf", media_kind="document", extension=".pdf")
        results = (_result("a" * 64, None, e2e_ok=False, error="Ollama unavailable"),)
        matrix = build_matrix((seed,), results)
        rate = matrix.rates["provider_error_rate"]
        self.assertEqual((rate.numerator, rate.denominator), (1, 1))


if __name__ == "__main__":
    unittest.main()
