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

    def test_audit_detects_prompt_vs_oracle_date_requirement_mismatch(self) -> None:
        audit = generate_gate2_audit()
        checks = {finding["check"] for finding in audit["findings"]}
        self.assertIn("prompt_date_requirement_mismatch", checks)

    def test_audit_detects_evidence_supported_date_that_verifier_forbids(self) -> None:
        audit = generate_gate2_audit()
        matches = [
            finding for finding in audit["findings"]
            if finding["check"] == "evidence_date_not_allowed"
            and finding["task_id"] == "sn-photograph-val-01"
        ]
        self.assertEqual(len(matches), 1)
        self.assertIn("2025-06-11", matches[0]["detail"])

    def test_audit_flags_routing_taxonomy_definition_gap(self) -> None:
        audit = generate_gate2_audit()
        matches = [
            finding for finding in audit["findings"]
            if finding["check"] == "routing_taxonomy_undefined_in_prompt"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["severity"], "warning")

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
