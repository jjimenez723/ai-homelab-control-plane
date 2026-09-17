"""The prompt x model x file matrix and its aggregate metrics.

Every rate is returned with an explicit numerator and denominator (spec
section 9: "Show denominators clearly" / "Do not reduce this to one opaque
score"). A predicate that is not applicable to a given result (e.g. date
selection accuracy when there was no final filename at all) is excluded
from both, rather than counted as a failure.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Callable

from tools.naming_lab.dates import format_date_prefix
from tools.naming_lab.runner import NamingLabResult
from tools.naming_lab.seeds import SeedRecord

_DATE_PREFIX_RE = re.compile(r"^(\d{2}_\d{4})_")


@dataclasses.dataclass(frozen=True)
class MatrixCell:
    seed_sha256: str
    prompt_id: str
    prompt_version: str
    model_name: str
    exact_accepted_match: bool
    human_approved_alternate: bool
    deterministic_semantic_pass: bool
    raw_compliance_pass: bool
    end_to_end_pass: bool
    error: str | None


@dataclasses.dataclass(frozen=True)
class Rate:
    label: str
    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        return None if self.denominator == 0 else self.numerator / self.denominator


@dataclasses.dataclass(frozen=True)
class LabMatrix:
    cells: tuple[MatrixCell, ...]
    rates: dict[str, Rate]


def _date_prefix_of(filename: str | None) -> str | None:
    if not filename:
        return None
    match = _DATE_PREFIX_RE.match(filename)
    return match.group(1) if match else None


def _date_selection_correct(seed: SeedRecord | None, result: NamingLabResult) -> bool | None:
    if seed is None or result.end_to_end is None or result.end_to_end.filename is None:
        return None
    expected = format_date_prefix(seed.semantic_date.value) if seed.semantic_date else None
    return expected == _date_prefix_of(result.end_to_end.filename)


def _rate(
    label: str,
    results: tuple[NamingLabResult, ...],
    predicate: Callable[[NamingLabResult], bool | None],
) -> Rate:
    applicable = [predicate(result) for result in results]
    applicable = [value for value in applicable if value is not None]
    return Rate(label=label, numerator=sum(1 for value in applicable if value), denominator=len(applicable))


def build_matrix(seeds: tuple[SeedRecord, ...], results: tuple[NamingLabResult, ...]) -> LabMatrix:
    seed_by_sha = {seed.sha256: seed for seed in seeds}

    cells = tuple(
        MatrixCell(
            seed_sha256=result.seed_sha256,
            prompt_id=result.prompt_id,
            prompt_version=result.prompt_version,
            model_name=result.model_name,
            exact_accepted_match=bool(result.semantic and result.semantic.exact_accepted_match),
            human_approved_alternate=bool(result.semantic and result.semantic.approved_alternate_match),
            deterministic_semantic_pass=bool(result.semantic and result.semantic.passed),
            raw_compliance_pass=bool(result.raw_compliance and result.raw_compliance.passed),
            end_to_end_pass=bool(result.end_to_end and result.end_to_end.passed),
            error=result.error,
        )
        for result in results
    )

    rates = {
        "exact_human_accepted_rate": _rate(
            "exact-human-accepted", results,
            lambda r: bool(r.semantic and r.semantic.exact_accepted_match),
        ),
        "human_acceptance_rate": _rate(
            "human-acceptance (exact or approved alternate)", results,
            lambda r: bool(r.semantic and (r.semantic.exact_accepted_match or r.semantic.approved_alternate_match)),
        ),
        "deterministic_semantic_success_rate": _rate(
            "deterministic-semantic-success", results,
            lambda r: bool(r.semantic and r.semantic.passed),
        ),
        "production_policy_success_rate": _rate(
            "production-policy-success (end-to-end)", results,
            lambda r: bool(r.end_to_end and r.end_to_end.passed),
        ),
        "raw_format_compliance_rate": _rate(
            "raw-format-compliance", results,
            lambda r: bool(r.raw_compliance and r.raw_compliance.passed),
        ),
        "hallucinated_date_rate": _rate(
            "hallucinated-date (heuristic)", results,
            lambda r: bool(r.hallucination and r.hallucination.unsupported_date),
        ),
        "hallucinated_entity_rate": _rate(
            "hallucinated-entity (heuristic)", results,
            lambda r: bool(r.hallucination and r.hallucination.unsupported_entities),
        ),
        "date_selection_accuracy": _rate(
            "date-selection-accuracy", results,
            lambda r: _date_selection_correct(seed_by_sha.get(r.seed_sha256), r),
        ),
        "path_or_prompt_leakage_rate": _rate(
            "path-or-prompt-leakage", results,
            lambda r: (not r.raw_compliance.no_leakage) if r.raw_compliance else None,
        ),
        "provider_error_rate": _rate(
            "provider-error", results, lambda r: r.error is not None,
        ),
        "truncation_rate": _rate(
            "truncation (finish_reason == length)", results,
            lambda r: r.finish_reason == "length",
        ),
    }
    return LabMatrix(cells=cells, rates=rates)
