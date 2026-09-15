"""Generic ACB-v1 suite scaffolding: split enforcement, duplicate-id guard."""

from __future__ import annotations

import unittest

from tools.bench.suite import ContaminationError, SuiteDefinition, SuiteTask, assert_no_holdout_leak


def _task(task_id: str, split: str, suite: str = "s", scenario: str = "sc") -> SuiteTask:
    return SuiteTask(
        id=task_id, suite=suite, suite_version="1", capability="c", sub_capability="c",
        split=split, scenario=scenario, input={"description": f"text for {task_id}"},
        expected={},
    )


class SuiteTaskTests(unittest.TestCase):
    def test_invalid_split_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _task("t1", "prod")

    def test_empty_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _task("", "dev")


class SuiteDefinitionTests(unittest.TestCase):
    def _full(self, **overrides) -> SuiteDefinition:
        tasks = [_task("a", "dev"), _task("b", "validation"), _task("c", "frozen_holdout")]
        tasks = overrides.pop("tasks", tasks)
        return SuiteDefinition(suite_id="s", suite_version="1", description="d", tasks=tuple(tasks))

    def test_a_well_formed_suite_loads(self) -> None:
        suite = self._full()
        self.assertEqual(len(suite.tasks), 3)

    def test_duplicate_ids_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._full(tasks=[_task("a", "dev"), _task("a", "validation"), _task("b", "frozen_holdout")])

    def test_missing_split_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._full(tasks=[_task("a", "dev"), _task("b", "dev")])

    def test_task_declaring_a_different_suite_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._full(tasks=[_task("a", "dev", suite="other"), _task("b", "validation"),
                               _task("c", "frozen_holdout")])

    def test_by_split_rejects_unknown_split(self) -> None:
        suite = self._full()
        with self.assertRaises(ValueError):
            suite.by_split("staging")

    def test_by_id_raises_for_unknown_id(self) -> None:
        suite = self._full()
        with self.assertRaises(KeyError):
            suite.by_id("nope")

    def test_frozen_holdout_accessor_is_distinct_from_dev_and_validation(self) -> None:
        suite = self._full()
        self.assertEqual([t.id for t in suite.dev()], ["a"])
        self.assertEqual([t.id for t in suite.validation()], ["b"])
        self.assertEqual([t.id for t in suite.frozen_holdout()], ["c"])


class HoldoutLeakGuardTests(unittest.TestCase):
    def test_no_leak_passes_silently(self) -> None:
        tasks = [_task("a", "dev"), _task("b", "validation"), _task("c", "frozen_holdout")]
        suite = SuiteDefinition(suite_id="s", suite_version="1", description="d", tasks=tuple(tasks))
        assert_no_holdout_leak(suite, prompt_material=["unrelated template text"])

    def test_leaked_holdout_text_is_caught(self) -> None:
        tasks = [_task("a", "dev"), _task("b", "validation"), _task("c", "frozen_holdout")]
        suite = SuiteDefinition(suite_id="s", suite_version="1", description="d", tasks=tuple(tasks))
        with self.assertRaises(ContaminationError):
            assert_no_holdout_leak(suite, prompt_material=["text for c"])

    def test_empty_prompt_material_never_raises(self) -> None:
        tasks = [_task("a", "dev"), _task("b", "validation"), _task("c", "frozen_holdout")]
        suite = SuiteDefinition(suite_id="s", suite_version="1", description="d", tasks=tuple(tasks))
        assert_no_holdout_leak(suite, prompt_material=[])


if __name__ == "__main__":
    unittest.main()
