"""The suite registry a campaign manifest may reference by id.

A small, explicit table -- the same "versioned code, not configuration a
deployment can edit unreviewed" reasoning `tools/models/task_classes.py`
already gives for its own join table. Adding a third ACB-v1 suite later
means adding one row here and one adapter function, not touching the
campaign runner.

``INPUT_ADAPTERS`` bridges a suite's task-specific `SuiteTask.input` shape
into the generic `(task_text, extra)` pair `tools.prompts.renderer.render`
needs -- the routing suite hands over one description string; the naming
suite hands over an evidence description plus the original filename as a
named template substitution.
"""

from __future__ import annotations

from typing import Any, Callable

from tools.bench.acb_v1 import routing_classification as _rc
from tools.bench.acb_v1 import semantic_naming as _sn
from tools.bench.acb_v1 import semantic_catalog as _sc
from tools.bench.suite import SuiteDefinition, SuiteTask

SUITES: dict[str, SuiteDefinition] = {
    _rc.SUITE_ID: _rc.SUITE,
    _sn.SUITE_ID: _sn.SUITE,
    _sc.SUITE_ID: _sc.SUITE,
}

VERIFIERS: dict[str, Callable[[SuiteTask, Any], tuple[bool, dict]]] = {
    _rc.SUITE_ID: _rc.verify,
    _sn.SUITE_ID: _sn.verify,
    _sc.SUITE_ID: _sc.verify,
}


def _routing_input(task: SuiteTask) -> tuple[str, dict[str, str]]:
    return task.input["description"], {}


def _naming_input(task: SuiteTask) -> tuple[str, dict[str, str]]:
    return task.input["evidence_text"], {"original_filename": task.input["original_filename"]}


def _semantic_catalog_input(task: SuiteTask) -> tuple[str, dict[str, str]]:
    return task.input["description"], {}


INPUT_ADAPTERS: dict[str, Callable[[SuiteTask], tuple[str, dict[str, str]]]] = {
    _rc.SUITE_ID: _routing_input,
    _sn.SUITE_ID: _naming_input,
    _sc.SUITE_ID: _semantic_catalog_input,
}


class UnknownSuite(KeyError):
    """A campaign manifest referenced a suite id this registry does not have."""


def suite(suite_id: str) -> SuiteDefinition:
    try:
        return SUITES[suite_id]
    except KeyError as exc:
        raise UnknownSuite(f"unknown suite id {suite_id!r}; known: {sorted(SUITES)}") from exc


def verifier(suite_id: str) -> Callable[[SuiteTask, Any], tuple[bool, dict]]:
    try:
        return VERIFIERS[suite_id]
    except KeyError as exc:
        raise UnknownSuite(f"unknown suite id {suite_id!r}; known: {sorted(VERIFIERS)}") from exc


def prompt_input(suite_id: str, task: SuiteTask) -> tuple[str, dict[str, str]]:
    try:
        adapter = INPUT_ADAPTERS[suite_id]
    except KeyError as exc:
        raise UnknownSuite(f"unknown suite id {suite_id!r}; known: {sorted(INPUT_ADAPTERS)}") from exc
    return adapter(task)
