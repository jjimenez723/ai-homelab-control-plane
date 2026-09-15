"""ACB-v1 routing/classification suite: schema, splits, contamination, oracle."""

from __future__ import annotations

import unittest

from tools.bench import suite as suite_mod
from tools.bench.acb_v1 import routing_classification as rc
from tools.prompts.library import ROUTING_CLASSIFICATION_LABELS


class SuiteSchemaTests(unittest.TestCase):
    def test_suite_id_matches_every_task(self) -> None:
        for task in rc.SUITE.tasks:
            self.assertEqual(task.suite, rc.SUITE_ID)

    def test_task_ids_are_unique(self) -> None:
        ids = [t.id for t in rc.SUITE.tasks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_split_present_and_nonempty(self) -> None:
        for split in ("dev", "validation", "frozen_holdout"):
            self.assertGreater(len(rc.SUITE.by_split(split)), 0)

    def test_splits_are_disjoint(self) -> None:
        dev_ids = {t.id for t in rc.SUITE.dev()}
        val_ids = {t.id for t in rc.SUITE.validation()}
        hold_ids = {t.id for t in rc.SUITE.frozen_holdout()}
        self.assertEqual(dev_ids & val_ids, set())
        self.assertEqual(dev_ids & hold_ids, set())
        self.assertEqual(val_ids & hold_ids, set())

    def test_every_expected_label_is_in_the_closed_taxonomy(self) -> None:
        labels = set(ROUTING_CLASSIFICATION_LABELS)
        for task in rc.SUITE.tasks:
            self.assertIn(task.expected["label"], labels)

    def test_required_scenarios_are_all_covered(self) -> None:
        required = {
            "straightforward_routing", "semantic_paraphrase", "ambiguous_resolvable",
            "deterministic_work", "model_assisted_work", "perception_ocr",
            "semantic_naming", "structured_extraction", "coding", "orchestration",
            "general_generation", "unsupported_work", "escalation_required",
            "adversarial_irrelevant", "prompt_injection",
        }
        self.assertEqual(required, set(rc.SUITE.scenarios()))

    def test_every_scenario_is_represented_in_every_split(self) -> None:
        for split in ("dev", "validation", "frozen_holdout"):
            split_scenarios = {t.scenario for t in rc.SUITE.by_split(split)}
            self.assertEqual(split_scenarios, set(rc.SUITE.scenarios()),
                              f"split {split!r} is missing scenario coverage")

    def test_bad_split_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            suite_mod.SuiteTask(
                id="x", suite="s", suite_version="1", capability="c", sub_capability="c",
                split="production", scenario="x", input={}, expected={},
            )

    def test_unknown_label_is_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            rc._task("bad", split="dev", scenario="x", description="d", label="not-a-real-label")


class OracleTests(unittest.TestCase):
    def test_exact_correct_label_passes(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        ok, _ = rc.verify(task, "code.implementation")
        self.assertTrue(ok)

    def test_case_insensitive_match_passes(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        ok, _ = rc.verify(task, "  CODE.IMPLEMENTATION  ")
        self.assertTrue(ok)

    def test_wrong_label_fails(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        ok, detail = rc.verify(task, "general")
        self.assertFalse(ok)
        self.assertEqual(detail["expected"], "code.implementation")

    def test_label_outside_closed_set_fails(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        ok, detail = rc.verify(task, "definitely-not-a-label")
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "label_not_in_closed_set")

    def test_non_string_output_fails_closed(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        ok, detail = rc.verify(task, {"label": "code.implementation"})
        self.assertFalse(ok)
        self.assertEqual(detail["failure"], "non_string_output")

    def test_unsupported_work_is_labeled_unsupported_not_a_task_class(self) -> None:
        for task in rc.SUITE.tasks:
            if task.scenario == "unsupported_work":
                self.assertEqual(task.expected["label"], "unsupported")

    def test_escalation_required_scenario_is_labeled_correctly(self) -> None:
        for task in rc.SUITE.tasks:
            if task.scenario == "escalation_required":
                self.assertEqual(task.expected["label"], "escalation_required")

    def test_prompt_injection_tasks_are_not_labeled_as_the_injected_instruction(self) -> None:
        # None of the injection scenario's expected labels are "unsupported"
        # purely because of the injected text -- the correct label reflects
        # the real underlying job, proving the taxonomy doesn't reward
        # collapsing an injection attempt into an escalation/refusal by default.
        injection_tasks = [t for t in rc.SUITE.tasks if t.scenario == "prompt_injection"]
        self.assertTrue(injection_tasks)
        for task in injection_tasks:
            self.assertIn(task.expected["label"],
                           {"classification", "extraction.structured", "naming.semantic"})


class ContaminationTests(unittest.TestCase):
    """Frozen holdout must never leak into prompt-construction material."""

    def test_holdout_descriptions_do_not_appear_in_the_active_prompt_templates(self) -> None:
        from tools.prompts.registry import load_default_registry
        prompt = load_default_registry().active("acb-v1-routing-classification")
        material = [prompt.policy_template, prompt.role_template,
                    prompt.task_template, prompt.output_schema]
        suite_mod.assert_no_holdout_leak(rc.SUITE, prompt_material=material, fields=("description",))

    def test_a_deliberately_leaked_holdout_item_is_caught(self) -> None:
        holdout_task = rc.SUITE.frozen_holdout()[0]
        leaked_material = [f"Example: {holdout_task.input['description']}"]
        with self.assertRaises(suite_mod.ContaminationError):
            suite_mod.assert_no_holdout_leak(rc.SUITE, prompt_material=leaked_material,
                                              fields=("description",))

    def test_dev_and_validation_are_not_flagged_by_the_holdout_guard(self) -> None:
        # The guard only ever inspects frozen_holdout content; dev/validation
        # descriptions appearing in prompt material is expected and fine.
        dev_task = rc.SUITE.dev()[0]
        material = [dev_task.input["description"]]
        suite_mod.assert_no_holdout_leak(rc.SUITE, prompt_material=material, fields=("description",))


if __name__ == "__main__":
    unittest.main()
