"""ACB-v1 semantic-naming suite: schema, splits, contamination, property oracle."""

from __future__ import annotations

import unittest

from tools.bench import suite as suite_mod
from tools.bench.acb_v1 import semantic_naming as sn


class SuiteSchemaTests(unittest.TestCase):
    def test_suite_id_matches_every_task(self) -> None:
        for task in sn.SUITE.tasks:
            self.assertEqual(task.suite, sn.SUITE_ID)

    def test_task_ids_are_unique(self) -> None:
        ids = [t.id for t in sn.SUITE.tasks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_split_present_and_nonempty(self) -> None:
        for split in ("dev", "validation", "frozen_holdout"):
            self.assertGreater(len(sn.SUITE.by_split(split)), 0)

    def test_splits_are_disjoint(self) -> None:
        dev_ids = {t.id for t in sn.SUITE.dev()}
        val_ids = {t.id for t in sn.SUITE.validation()}
        hold_ids = {t.id for t in sn.SUITE.frozen_holdout()}
        self.assertEqual(dev_ids & val_ids, set())
        self.assertEqual(dev_ids & hold_ids, set())
        self.assertEqual(val_ids & hold_ids, set())

    def test_required_categories_are_covered(self) -> None:
        required = {
            "invoice", "receipt", "contract", "screenshot", "photograph",
            "spreadsheet", "client_document", "recording", "client_media",
            "ambiguous_asset", "contradictory_metadata", "distracting_text",
            "prompt_injection", "source_path_leakage",
        }
        self.assertEqual(required, set(sn.SUITE.scenarios()))

    def test_every_scenario_is_represented_in_every_split(self) -> None:
        for split in ("dev", "validation", "frozen_holdout"):
            split_scenarios = {t.scenario for t in sn.SUITE.by_split(split)}
            self.assertEqual(split_scenarios, set(sn.SUITE.scenarios()),
                              f"split {split!r} is missing scenario coverage")


class MultipleValidNamesTests(unittest.TestCase):
    """A semantically good filename must not fail merely for differing from one string."""

    def test_two_different_but_valid_filenames_both_pass(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        ok_a, _ = sn.verify(task, "2025-01-01_Acme-invoice.pdf")
        ok_b, _ = sn.verify(task, "2025-01-01_Acme-bill-payment.pdf")
        self.assertTrue(ok_a)
        self.assertTrue(ok_b)
        self.assertNotEqual("2025-01-01_Acme-invoice.pdf", "2025-01-01_Acme-bill-payment.pdf")

    def test_synonym_for_a_required_concept_passes(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        ok, _ = sn.verify(task, "2025-01-01_Acme-bill.pdf")
        self.assertTrue(ok)


class DeterministicPropertyTests(unittest.TestCase):
    def test_unsafe_path_traversal_filename_fails(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        ok, detail = sn.verify(task, "../../etc/passwd.pdf")
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "policy_rejected")

    def test_wrong_extension_fails(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        ok, detail = sn.verify(task, "2025-01-01_acme-invoice.docx")
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "wrong_extension")

    def test_hallucinated_date_fails(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        ok, detail = sn.verify(task, "2099-01-01_acme-invoice.pdf")
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "unsupported_or_hallucinated_date")

    def test_missing_required_date_fails_when_evidence_supports_one(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        ok, detail = sn.verify(task, "acme-invoice.pdf")
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "missing_required_date")

    def test_hallucinated_entity_fails(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        ok, detail = sn.verify(task, "2025-01-01_random-document.pdf")
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "missing_required_concept")

    def test_contradicted_filename_metadata_is_forbidden_not_trusted(self) -> None:
        task = sn.SUITE.by_id("sn-contradict-dev-01")
        ok, detail = sn.verify(task, "2024-01-01_invoice.pdf")
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "forbidden_token_present")

    def test_correct_answer_for_contradicted_metadata_passes(self) -> None:
        task = sn.SUITE.by_id("sn-contradict-dev-01")
        ok, _ = sn.verify(task, "2025-01-01_invoice.pdf")
        self.assertTrue(ok)

    def test_distracting_text_does_not_leak_into_a_passing_filename(self) -> None:
        task = sn.SUITE.by_id("sn-distract-dev-01")
        ok, detail = sn.verify(task, "2025-01-01_acme-invoice-coffee.pdf")
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "forbidden_token_present")

    def test_prompt_injection_in_extracted_text_is_not_obeyed(self) -> None:
        task = sn.SUITE.by_id("sn-injection-dev-01")
        ok, detail = sn.verify(task, "secret-backdoor.pdf")
        self.assertFalse(ok)
        ok_good, _ = sn.verify(task, "2025-01-01_acme-invoice.pdf")
        self.assertTrue(ok_good)

    def test_source_path_leakage_fails(self) -> None:
        task = sn.SUITE.by_id("sn-pathleak-dev-01")
        ok, detail = sn.verify(task, "2025-01-01_acme-invoice-inbox-unsorted.pdf")
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "leakage")

    def test_reserved_device_stem_is_handled_via_shared_policy_validator(self) -> None:
        # Delegated to tools.ingest.naming.policy.validate_proposal, the same
        # function production naming uses -- no parallel implementation here.
        task = sn.SUITE.by_id("sn-ambiguous-dev-01")
        ok, detail = sn.verify(task, "con.pdf")
        self.assertFalse(ok)  # fails on missing required concept, not a crash

    def test_non_string_output_fails_closed(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        ok, detail = sn.verify(task, {"filename": "2025-01-01_acme-invoice.pdf"})
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "non_string_output")

    def test_empty_proposal_fails_closed(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        ok, detail = sn.verify(task, "")
        self.assertFalse(ok)

    def test_overlength_proposal_fails(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        ok, detail = sn.verify(task, "2025-01-01_" + ("acme-" * 60) + ".pdf")
        self.assertFalse(ok)


class ContaminationTests(unittest.TestCase):
    def test_holdout_evidence_does_not_appear_in_the_active_prompt_templates(self) -> None:
        from tools.prompts.registry import load_default_registry
        prompt = load_default_registry().active("acb-v1-semantic-naming")
        material = [prompt.policy_template, prompt.role_template,
                    prompt.task_template, prompt.output_schema]
        suite_mod.assert_no_holdout_leak(sn.SUITE, prompt_material=material, fields=("evidence_text",))

    def test_a_deliberately_leaked_holdout_item_is_caught(self) -> None:
        holdout_task = sn.SUITE.frozen_holdout()[0]
        leaked_material = [f"Example evidence: {holdout_task.input['evidence_text']}"]
        with self.assertRaises(suite_mod.ContaminationError):
            suite_mod.assert_no_holdout_leak(sn.SUITE, prompt_material=leaked_material,
                                              fields=("evidence_text",))


if __name__ == "__main__":
    unittest.main()
