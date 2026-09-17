"""Defensive guard: the Lab may never read or reuse frozen-holdout content.

The frozen holdout for the Gate-2 suites has no dedicated file or directory
-- it is interleaved inline with dev/validation tasks in
`tools/bench/acb_v1/semantic_naming.py` and
`tools/bench/acb_v1/routing_classification.py`, distinguished only by
`SuiteTask.split == "frozen_holdout"`. The only reliable way to keep it out
of the Lab is a code-level rule, checked at the one place the Lab would ever
touch a `SuiteTask` (if an operator chooses to seed the Lab's corpus from an
existing Gate-2 dev-split example). This module is that rule.

Mirrors `tools.bench.suite.assert_no_holdout_leak`'s "checked at run time,
not trusted at authoring time" posture, scoped to the Lab's own
prompt-construction path.
"""

from __future__ import annotations

from tools.bench.suite import SuiteDefinition, SuiteTask

FROZEN_HOLDOUT_SPLIT = "frozen_holdout"


class FrozenHoldoutTouched(ValueError):
    """The Lab attempted to read or use a frozen-holdout task."""


def forbid_frozen_holdout(task: SuiteTask) -> None:
    """Raise if `task` is a frozen-holdout task. Call before using it as a seed."""
    if task.split == FROZEN_HOLDOUT_SPLIT:
        raise FrozenHoldoutTouched(
            f"{task.suite}:{task.id} is a frozen_holdout task and may not be used to "
            "seed, prompt, or otherwise feed the Naming Lab"
        )


def usable_tasks(suite: SuiteDefinition) -> tuple[SuiteTask, ...]:
    """Every task in `suite` that is safe for the Lab to borrow: dev + validation only.

    Never returns a frozen_holdout task, regardless of what the suite
    contains -- the filter is structural, not an assertion the caller must
    remember to apply.
    """
    return tuple(task for task in suite.tasks if task.split != FROZEN_HOLDOUT_SPLIT)
