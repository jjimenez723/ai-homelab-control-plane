"""Generic ACB-v1 capability suite: task shape, splits, contamination guard.

`docs/19-model-evaluation.md` section 2.1 defines ACB-v1 as role-specific
capability suites, not one global test, and section 2.5 requires every suite
with enough tasks to matter to define development/validation/frozen-holdout
splits. This module is the shared scaffolding both Gate-2 suites
(`tools/bench/acb_v1/routing_classification.py`,
`tools/bench/acb_v1/semantic_naming.py`) build on, so the split discipline
and contamination guard are enforced once rather than reimplemented per
suite -- the same reasoning `tools/bench/corpus.py` already applies to the
frozen Legacy-20 shape, generalised to a family of suites instead of one.

Deliberately NOT reused: `tools/bench/corpus.py::Task`/`load_corpus`, which
hard-codes the Legacy-20's exact 10/5/5/20 composition (`docs/19` section
1.2) and would reject any suite corpus with a different shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SUITE_SCHEMA_VERSION = 1

SPLITS = ("dev", "validation", "frozen_holdout")


@dataclass(frozen=True)
class SuiteTask:
    """One item in an ACB-v1 suite.

    ``input``/``expected`` are suite-specific bounded dictionaries -- a
    routing-classification item and a semantic-naming item shape them
    differently, which is why they are plain dicts here rather than a fixed
    schema; each suite module documents and validates its own shape.
    """

    id: str
    suite: str
    suite_version: str
    capability: str
    sub_capability: str
    split: str
    scenario: str
    input: dict[str, Any]
    expected: dict[str, Any]
    risk: str = "low"
    privacy: str = "local-only"
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.split not in SPLITS:
            raise ValueError(f"{self.id}: invalid split {self.split!r}; must be one of {SPLITS}")
        if not self.id or not self.scenario:
            raise ValueError("SuiteTask requires a non-empty id and scenario")


@dataclass(frozen=True)
class SuiteDefinition:
    suite_id: str
    suite_version: str
    description: str
    tasks: tuple[SuiteTask, ...]

    def __post_init__(self) -> None:
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"{self.suite_id}: duplicate task id(s): {duplicates}")
        wrong_suite = [t.id for t in self.tasks if t.suite != self.suite_id]
        if wrong_suite:
            raise ValueError(f"{self.suite_id}: task(s) declare a different suite id: {wrong_suite}")
        for split in SPLITS:
            if not any(t.split == split for t in self.tasks):
                raise ValueError(f"{self.suite_id}: split {split!r} has zero tasks")

    def by_split(self, split: str) -> tuple[SuiteTask, ...]:
        if split not in SPLITS:
            raise ValueError(f"invalid split {split!r}; must be one of {SPLITS}")
        return tuple(t for t in self.tasks if t.split == split)

    def dev(self) -> tuple[SuiteTask, ...]:
        return self.by_split("dev")

    def validation(self) -> tuple[SuiteTask, ...]:
        return self.by_split("validation")

    def frozen_holdout(self) -> tuple[SuiteTask, ...]:
        """Never call this from prompt authoring, few-shot, or threshold tuning.

        Named distinctly from `dev`/`validation` (rather than a bare
        ``by_split("frozen_holdout")`` at every call site) so a reviewer can
        grep for exactly where holdout data is used and see that it is only
        the certification/report path (`tools/campaign/`), never a suite
        module's own prompt-construction code.
        """
        return self.by_split("frozen_holdout")

    def by_id(self, task_id: str) -> SuiteTask:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(f"{self.suite_id}: no such task id: {task_id}")

    def scenarios(self) -> tuple[str, ...]:
        return tuple(sorted({t.scenario for t in self.tasks}))


class ContaminationError(ValueError):
    """Frozen-holdout content appears in material used to build prompts."""


def _normalize(text: str) -> str:
    return " ".join(str(text).split()).strip().lower()


def assert_no_holdout_leak(
    suite: SuiteDefinition,
    *,
    prompt_material: Iterable[str],
    fields: Iterable[str] = ("description",),
) -> None:
    """Refuse if any frozen_holdout item's text appears in prompt material.

    ``prompt_material`` is every string a suite's prompt construction path
    depends on (registry templates, any few-shot text, any spec text) --
    the same "checked at run time rather than trusted at authoring time"
    posture `tools.bench.ablation.assert_no_canary_contamination` uses for
    the Legacy-20 ablation, generalised to an arbitrary suite and split.
    """
    blob = _normalize("\n".join(prompt_material))
    if not blob:
        return
    offenders: list[str] = []
    for task in suite.frozen_holdout():
        for field in fields:
            value = task.input.get(field) if field in task.input else task.expected.get(field)
            if not value:
                continue
            needle = _normalize(str(value))
            if needle and needle in blob:
                offenders.append(f"{task.id}.{field}")
    if offenders:
        raise ContaminationError(
            f"{suite.suite_id}: frozen_holdout content found in prompt material "
            f"used to build prompts -- {'; '.join(offenders)}"
        )
