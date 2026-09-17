"""Persistence for run results and human review decisions.

Append-only JSONL for results, mirroring the spirit of
`tools.campaign.store.ResultRecord` (schema-v2: raw output and finish
reason both kept, per spec section 11) but scoped to one Lab corpus rather
than a whole campaign. Human decisions are appended to their own log for a
full audit trail, and also folded onto the relevant `SeedRecord` so the UI
only has to read current state, not replay history.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Literal

from tools.naming_lab.runner import NamingLabResult, result_from_json, result_to_json
from tools.naming_lab.seeds import SeedRecord, SeedStore

Decision = Literal["accept", "accept_alternate", "reject"]


@dataclasses.dataclass(frozen=True)
class HumanDecision:
    seed_sha256: str
    decision: Decision
    filename: str | None  # the filename being accepted/rejected, if any
    notes: str | None
    created_at: str


class ResultStore:
    """One corpus's run results, under `.state/naming-lab/<corpus_id>/results.jsonl`."""

    def __init__(self, corpus_root: Path):
        self.corpus_root = Path(corpus_root)
        self.corpus_root.mkdir(parents=True, exist_ok=True)
        self.results_path = self.corpus_root / "results.jsonl"

    def append(self, result: NamingLabResult) -> None:
        with self.results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result_to_json(result), sort_keys=True) + "\n")

    def append_all(self, results: list[NamingLabResult]) -> None:
        for result in results:
            self.append(result)

    def all(self) -> tuple[NamingLabResult, ...]:
        if not self.results_path.exists():
            return ()
        results = []
        for line in self.results_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                results.append(result_from_json(json.loads(line)))
        return tuple(results)

    def for_seed(self, sha256: str) -> tuple[NamingLabResult, ...]:
        return tuple(result for result in self.all() if result.seed_sha256 == sha256)


class DecisionStore:
    """The full human-review audit trail, under
    `.state/naming-lab/<corpus_id>/decisions.jsonl`."""

    def __init__(self, corpus_root: Path):
        self.corpus_root = Path(corpus_root)
        self.corpus_root.mkdir(parents=True, exist_ok=True)
        self.decisions_path = self.corpus_root / "decisions.jsonl"

    def append(self, decision: HumanDecision) -> None:
        with self.decisions_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dataclasses.asdict(decision), sort_keys=True) + "\n")

    def for_seed(self, sha256: str) -> tuple[HumanDecision, ...]:
        if not self.decisions_path.exists():
            return ()
        decisions = []
        for line in self.decisions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                decisions.append(HumanDecision(**json.loads(line)))
        return tuple(d for d in decisions if d.seed_sha256 == sha256)


def record_human_decision(
    seed_store: SeedStore,
    decision_store: DecisionStore,
    seed: SeedRecord,
    *,
    decision: Decision,
    filename: str | None,
    notes: str | None = None,
) -> SeedRecord:
    """Apply one human review decision: log it, and fold `accept`/`accept_alternate`
    onto the seed's `accepted_filenames` (spec section 6: "Accept a
    model-generated filename as another valid alternative"). A `reject`
    never adds a filename -- only the notes, for the review trail.
    """
    record = HumanDecision(
        seed_sha256=seed.sha256, decision=decision, filename=filename,
        notes=notes, created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    decision_store.append(record)

    updated = seed
    if decision in ("accept", "accept_alternate") and filename:
        updated = updated.with_accepted_filename(filename)
    if notes is not None:
        updated = updated.with_notes(notes)
    if updated is not seed:
        updated = seed_store.save(updated)
    return updated
