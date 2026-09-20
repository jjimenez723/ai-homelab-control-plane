"""Deterministic Gate 3 evaluation and regression comparison."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class CanaryRecord:
    asset_id: str
    expected_client_id: str | None
    expected_project_id: str | None
    expected_tags: frozenset[str]
    predicted_client_id: str | None
    predicted_project_id: str | None
    predicted_tags: frozenset[str]
    client_confidence: float = 0.0
    project_confidence: float = 0.0


@dataclass(frozen=True)
class Gate3Report:
    assets: int
    labeled_assets: int
    client_project_correct: int
    client_project_predicted: int
    client_project_precision: float
    client_project_recall: float
    coverage: float
    tag_true_positives: int
    tag_false_positives: int
    tag_false_negatives: int
    tag_precision: float
    tag_recall: float
    unknown_or_review: int
    confusion_categories: Mapping[str, int]
    per_client_project_errors: Mapping[str, int]
    tag_false_positive_counts: Mapping[str, int]
    tag_false_negative_counts: Mapping[str, int]

    def as_dict(self) -> dict:
        return {key: (dict(value) if isinstance(value, Mapping) else value) for key, value in self.__dict__.items()}


def evaluate(records: Iterable[CanaryRecord]) -> Gate3Report:
    rows = list(records)
    total = len(rows)
    cp_predicted = 0
    cp_correct = 0
    false_positive_tags: dict[str, int] = {}
    false_negative_tags: dict[str, int] = {}
    confusion: dict[str, int] = {}
    per_identity: dict[str, int] = {}
    tp = fp = fn = 0
    for row in rows:
        predicted = row.predicted_client_id is not None and row.predicted_project_id is not None
        correct = (
            predicted
            and row.predicted_client_id == row.expected_client_id
            and row.predicted_project_id == row.expected_project_id
        )
        if predicted:
            cp_predicted += 1
        if correct:
            cp_correct += 1
        elif predicted:
            key = f"{row.predicted_client_id or 'unknown'} -> {row.expected_client_id or 'unknown'} / {row.predicted_project_id or 'unknown'} -> {row.expected_project_id or 'unknown'}"
            confusion[key] = confusion.get(key, 0) + 1
            per_identity[row.expected_client_id or "unknown"] = per_identity.get(row.expected_client_id or "unknown", 0) + 1
        else:
            confusion["unknown_or_review"] = confusion.get("unknown_or_review", 0) + 1
        true = row.expected_tags
        predicted_tags = row.predicted_tags
        tp += len(true & predicted_tags)
        fp += len(predicted_tags - true)
        fn += len(true - predicted_tags)
        for tag in predicted_tags - true:
            false_positive_tags[tag] = false_positive_tags.get(tag, 0) + 1
        for tag in true - predicted_tags:
            false_negative_tags[tag] = false_negative_tags.get(tag, 0) + 1
    labeled = sum(1 for row in rows if row.predicted_client_id and row.predicted_project_id)
    return Gate3Report(
        assets=total,
        labeled_assets=labeled,
        client_project_correct=cp_correct,
        client_project_predicted=cp_predicted,
        client_project_precision=cp_correct / cp_predicted if cp_predicted else 0.0,
        client_project_recall=cp_correct / total if total else 0.0,
        coverage=cp_predicted / total if total else 0.0,
        tag_true_positives=tp,
        tag_false_positives=fp,
        tag_false_negatives=fn,
        tag_precision=tp / (tp + fp) if tp + fp else 0.0,
        tag_recall=tp / (tp + fn) if tp + fn else 0.0,
        unknown_or_review=total - cp_predicted,
        confusion_categories=confusion,
        per_client_project_errors=per_identity,
        tag_false_positive_counts=false_positive_tags,
        tag_false_negative_counts=false_negative_tags,
    )


def compare(baseline: Iterable[CanaryRecord], candidate: Iterable[CanaryRecord]) -> dict:
    """Compare two frozen-canary runs without silently dropping unknowns."""
    before = {row.asset_id: row for row in baseline}
    after = {row.asset_id: row for row in candidate}
    changed = []
    confidence_shifts = []
    for asset_id in sorted(set(before) | set(after)):
        old = before.get(asset_id)
        new = after.get(asset_id)
        if old is None or new is None or (
            old.predicted_client_id, old.predicted_project_id, old.predicted_tags
        ) != (
            new.predicted_client_id, new.predicted_project_id, new.predicted_tags
        ):
            changed.append(asset_id)
        if old is not None and new is not None:
            confidence_shifts.append({
                "asset_id": asset_id,
                "client": new.client_confidence - old.client_confidence,
                "project": new.project_confidence - old.project_confidence,
            })
    return {
        "baseline": evaluate(before.values()).as_dict(),
        "candidate": evaluate(after.values()).as_dict(),
        "changed_classifications_or_tags": changed,
        "confidence_shifts": confidence_shifts,
        "candidate_passes_gate3": (
            evaluate(after.values()).client_project_precision >= 0.95
            and evaluate(after.values()).tag_precision >= 0.80
        ),
    }
