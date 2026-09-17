"""The Lab must never read or reuse a frozen_holdout task.

`tools.bench.acb_v1.semantic_naming`/`routing_classification` interleave
frozen-holdout tasks inline with dev/validation tasks (no separate file to
exclude by path) -- these tests prove the code-level guard actually works
against the real suites, not just a synthetic fixture.
"""

from __future__ import annotations

import unittest

from tools.bench.acb_v1.routing_classification import SUITE as ROUTING_SUITE
from tools.bench.acb_v1.semantic_naming import SUITE as SEMANTIC_NAMING_SUITE
from tools.bench.suite import SuiteDefinition, SuiteTask
from tools.naming_lab.holdout_guard import FrozenHoldoutTouched, forbid_frozen_holdout, usable_tasks

_DEV_TASK = SuiteTask(
    id="t-dev-01", suite="fake-suite", suite_version="1", capability="naming.semantic",
    sub_capability="x", split="dev", scenario="dev-example", input={}, expected={},
)
_HOLDOUT_TASK = SuiteTask(
    id="t-fro-01", suite="fake-suite", suite_version="1", capability="naming.semantic",
    sub_capability="x", split="frozen_holdout", scenario="holdout-example", input={}, expected={},
)


class ForbidFrozenHoldoutTests(unittest.TestCase):
    def test_dev_task_is_allowed(self):
        forbid_frozen_holdout(_DEV_TASK)  # must not raise

    def test_frozen_holdout_task_raises(self):
        with self.assertRaises(FrozenHoldoutTouched):
            forbid_frozen_holdout(_HOLDOUT_TASK)


class UsableTasksTests(unittest.TestCase):
    def test_never_returns_a_frozen_holdout_task(self):
        definition = SuiteDefinition(
            suite_id="fake-suite", suite_version="1", description="",
            tasks=(
                _DEV_TASK, _HOLDOUT_TASK,
                SuiteTask(
                    id="t-val-01", suite="fake-suite", suite_version="1", capability="naming.semantic",
                    sub_capability="x", split="validation", scenario="val-example", input={}, expected={},
                ),
            ),
        )
        usable = usable_tasks(definition)
        self.assertNotIn(_HOLDOUT_TASK, usable)
        self.assertEqual({task.split for task in usable}, {"dev", "validation"})


class RealGate2SuitesTests(unittest.TestCase):
    """Proves the guard works against the actual Gate-2 suites, without ever
    printing, storing, or otherwise surfacing their frozen_holdout content."""

    def test_semantic_naming_suite_usable_tasks_exclude_holdout(self):
        usable = usable_tasks(SEMANTIC_NAMING_SUITE)
        self.assertTrue(all(task.split != "frozen_holdout" for task in usable))
        self.assertTrue(any(task.split == "dev" for task in usable))

    def test_semantic_naming_suite_holdout_tasks_all_raise(self):
        for task in SEMANTIC_NAMING_SUITE.frozen_holdout():
            with self.assertRaises(FrozenHoldoutTouched):
                forbid_frozen_holdout(task)

    def test_routing_classification_suite_usable_tasks_exclude_holdout(self):
        usable = usable_tasks(ROUTING_SUITE)
        self.assertTrue(all(task.split != "frozen_holdout" for task in usable))


if __name__ == "__main__":
    unittest.main()
