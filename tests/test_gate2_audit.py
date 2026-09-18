"""Gate-2 contract audit: expose oracle/prompt mismatches without changing them."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.bench.gate2_audit import generate_gate2_audit, to_markdown, write_gate2_audit


class Gate2AuditTests(unittest.TestCase):
    def test_default_audit_excludes_frozen_holdout_content(self) -> None:
        audit = generate_gate2_audit()
        self.assertEqual(audit["splits"], ["dev", "validation"])
        self.assertFalse(audit["frozen_holdout_content_included"])
        self.assertEqual(audit["summary"]["routing_items"], 38)
        self.assertEqual(audit["summary"]["naming_items"], 28)
        self.assertFalse(any(item["split"] == "frozen_holdout" for item in audit["routing_items"]))
        self.assertFalse(any(item["split"] == "frozen_holdout" for item in audit["naming_items"]))

    def test_audit_still_detects_a_prompt_vs_oracle_date_requirement_mismatch(self) -> None:
        # The check that found the 1.0.0 defect must remain live, so pairing an
        # old prompt with the suite is reported rather than silently tolerated.
        # It is exercised directly against the 1.0.0 text rather than against
        # whichever version is active now (D-059 froze a prompt that agrees).
        from tools.bench.acb_v1 import semantic_naming as naming
        from tools.bench.gate2_audit import _audit_naming_contract
        from tools.prompts.library import SEMANTIC_NAMING_V1

        findings = _audit_naming_contract(list(naming.SUITE.validation()), SEMANTIC_NAMING_V1)
        checks = {finding.check for finding in findings}
        self.assertIn("prompt_date_requirement_mismatch", checks)

    def test_audit_no_longer_finds_evidence_supported_dates_the_verifier_forbids(self) -> None:
        # D-059 fix 1: allowed_dates is derived from the evidence, so this
        # class of finding cannot occur for any item in any reviewed split.
        audit = generate_gate2_audit()
        matches = [
            finding for finding in audit["findings"]
            if finding["check"] == "evidence_date_not_allowed"
        ]
        self.assertEqual(matches, [])

    def test_audit_reports_no_contract_errors_for_the_active_configuration(self) -> None:
        audit = generate_gate2_audit()
        errors = [finding for finding in audit["findings"] if finding["severity"] == "error"]
        self.assertEqual(errors, [], f"unexpected contract errors: {errors}")

    def test_audit_probes_the_oracle_for_false_negatives_and_false_positives(self) -> None:
        audit = generate_gate2_audit()
        checks = set(audit["summary"]["checks_run"])
        # These checks produce findings only when something is wrong, so their
        # absence is the assertion; the probes themselves are proven to run by
        # the deliberate-defect tests below.
        self.assertNotIn("semantic_false_negative", checks)
        self.assertNotIn("semantic_false_positive", checks)
        self.assertNotIn("extension_conflated_with_semantics", checks)
        self.assertNotIn("concept_set_not_evidence_grounded", checks)
        self.assertNotIn("forbidden_token_contradicts_required_concept", checks)

    def test_the_false_negative_probe_fires_on_an_unsatisfiable_item(self) -> None:
        from tools.bench.gate2_audit import _audit_naming_oracle_behaviour
        from tools.bench.suite import SuiteTask

        impossible = SuiteTask(
            id="sn-probe-01", suite="acb-v1/semantic-naming", suite_version="1.1.0",
            capability="semantic_naming", sub_capability="invoice", split="dev",
            scenario="invoice",
            input={"original_filename": "scan.pdf",
                   "evidence_text": "An invoice from Acme Corp dated 2025-01-01.",
                   "vocabulary": ()},
            # Requires a date while permitting none: no answer can pass.
            expected={"required_concept_sets": (("acme",),), "forbidden_tokens": (),
                      "allowed_dates": (), "require_date": True,
                      "min_evidence_words": 0, "forbidden_substrings": (),
                      "max_filename_length": 80},
        )
        findings = _audit_naming_oracle_behaviour([impossible])
        self.assertIn("semantic_false_negative", {finding.check for finding in findings})

    def test_audit_records_the_contamination_guard_verdict(self) -> None:
        audit = generate_gate2_audit()
        clean = [finding for finding in audit["findings"]
                 if finding["check"] == "frozen_holdout_guard_clean"]
        self.assertEqual(len(clean), 2)

    def test_audit_reports_scenario_and_label_balance(self) -> None:
        audit = generate_gate2_audit()
        checks = set(audit["summary"]["checks_run"])
        self.assertIn("scenario_imbalance", checks)
        self.assertIn("label_frequency_skew", checks)

    def test_markdown_keeps_actual_task_language_and_expected_properties(self) -> None:
        audit = generate_gate2_audit(splits=("validation",))
        markdown = to_markdown(audit)
        self.assertIn("Something is off in the chunking function", markdown)
        self.assertIn("PXL_20250611_140233812.jpg", markdown)
        self.assertIn('"required_concept_sets"', markdown)
        self.assertIn("Exact model-facing prompt definitions", markdown)

    def test_write_audit_produces_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_path, markdown_path = write_gate2_audit(out_dir=Path(tmp), splits=("dev",))
            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())
            self.assertIn("Gate-2 evaluator contract audit", markdown_path.read_text(encoding="utf-8"))

    def test_unknown_split_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            generate_gate2_audit(splits=("not-a-split",))


if __name__ == "__main__":
    unittest.main()
