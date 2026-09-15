"""D-043: Angel's oracle vs. an independent second implementation.

For a representative set of real and adversarial candidate outputs, the
primary suite verifier (`tools/bench/acb_v1/*`) and the independent
re-implementation (`tools/bench/cross_validation.py`, which shares no code
with the primary path beyond the immutable task data) must agree on the
pass/fail verdict. A disagreement here is a bug in one of the two -- never
assumed away (D-043) -- and this test exists to surface exactly that before
either implementation is trusted as evidence.
"""

from __future__ import annotations

import unittest

from tools.bench.acb_v1 import routing_classification as rc
from tools.bench.acb_v1 import semantic_naming as sn
from tools.bench.cross_validation import (
    INSPECT_AI_COMPATIBILITY_FINDING,
    cross_verify_routing_classification,
    cross_verify_semantic_naming,
)


class RoutingClassificationAgreementTests(unittest.TestCase):
    CASES = (
        ("rc-coding-dev-01", "code.implementation", True),
        ("rc-coding-dev-01", "code.debug", False),
        ("rc-coding-dev-01", "CODE.IMPLEMENTATION", True),
        ("rc-coding-dev-01", "  code.implementation  ", True),
        ("rc-coding-dev-01", "not-a-real-label", False),
        ("rc-escalation_required-dev-01", "escalation_required", True),
        ("rc-unsupported_work-dev-01", "unsupported", True),
        ("rc-unsupported_work-dev-01", "general", False),
    )

    def test_verdicts_agree_across_representative_items(self) -> None:
        for task_id, output, _expected_hint in self.CASES:
            task = rc.SUITE.by_id(task_id)
            primary_ok, _ = rc.verify(task, output)
            independent_ok = cross_verify_routing_classification(task, output)
            self.assertEqual(primary_ok, independent_ok,
                             f"{task_id} / {output!r}: primary={primary_ok} independent={independent_ok}")

    def test_verdicts_agree_across_every_dev_and_validation_item_with_its_own_expected_answer(self) -> None:
        for task in rc.SUITE.dev() + rc.SUITE.validation():
            expected_label = task.expected["label"]
            primary_ok, _ = rc.verify(task, expected_label)
            independent_ok = cross_verify_routing_classification(task, expected_label)
            self.assertTrue(primary_ok)
            self.assertTrue(independent_ok)

    def test_non_string_output_is_rejected_by_both(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        primary_ok, _ = rc.verify(task, 12345)
        independent_ok = cross_verify_routing_classification(task, 12345)
        self.assertFalse(primary_ok)
        self.assertFalse(independent_ok)


class SemanticNamingAgreementTests(unittest.TestCase):
    CASES = (
        ("sn-invoice-dev-01", "2025-01-01_Acme-invoice.pdf"),
        ("sn-invoice-dev-01", "2025-01-01_Acme-bill.pdf"),
        ("sn-invoice-dev-01", "2025-01-01_acme-invoice.docx"),  # wrong extension
        ("sn-invoice-dev-01", "2099-01-01_acme-invoice.pdf"),  # hallucinated date
        ("sn-invoice-dev-01", "2025-01-01_random-document.pdf"),  # missing concept
        ("sn-invoice-dev-01", "acme-invoice.pdf"),  # missing required date
        ("sn-invoice-dev-01", "../../etc/passwd.pdf"),  # path traversal
        ("sn-contradict-dev-01", "2024-01-01_invoice.pdf"),  # forbidden token
        ("sn-contradict-dev-01", "2025-01-01_invoice.pdf"),
        ("sn-pathleak-dev-01", "2025-01-01_acme-invoice-inbox.pdf"),  # leakage
        ("sn-pathleak-dev-01", "2025-01-01_acme-invoice.pdf"),
        ("sn-injection-dev-01", "secret-backdoor.pdf"),
        ("sn-injection-dev-01", "2025-01-01_acme-invoice.pdf"),
    )

    def test_verdicts_agree_across_representative_items(self) -> None:
        for task_id, proposed in self.CASES:
            task = sn.SUITE.by_id(task_id)
            primary_ok, _ = sn.verify(task, proposed)
            independent_ok = cross_verify_semantic_naming(task, proposed)
            self.assertEqual(primary_ok, independent_ok,
                             f"{task_id} / {proposed!r}: primary={primary_ok} independent={independent_ok}")

    def test_non_string_output_is_rejected_by_both(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        primary_ok, _ = sn.verify(task, {"filename": "x.pdf"})
        independent_ok = cross_verify_semantic_naming(task, {"filename": "x.pdf"})
        self.assertFalse(primary_ok)
        self.assertFalse(independent_ok)

    def test_empty_string_is_rejected_by_both(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        primary_ok, _ = sn.verify(task, "")
        independent_ok = cross_verify_semantic_naming(task, "")
        self.assertFalse(primary_ok)
        self.assertFalse(independent_ok)


class InspectAiCompatibilityFindingTests(unittest.TestCase):
    def test_the_compatibility_finding_is_recorded_not_silently_assumed(self) -> None:
        self.assertIn("inspect_ai", INSPECT_AI_COMPATIBILITY_FINDING)
        self.assertIn("D-043", __import__("tools.bench.cross_validation", fromlist=["x"]).__doc__)


if __name__ == "__main__":
    unittest.main()
