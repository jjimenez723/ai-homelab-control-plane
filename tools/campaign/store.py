"""Append-only, resumable result storage for a campaign run.

One JSONL file per campaign (`.state/campaigns/<campaign_id>/results.jsonl`,
gitignored runtime state -- D-035's split between raw artifacts and
committed aggregates applies here exactly as it does to ablation runs).
Every completed work-unit attempt is one appended line; existing lines are
never rewritten, matching the "raw result records should be append-only"
requirement and giving crash-safety for free: a process killed mid-write
loses at most the one in-flight line (open with line-buffering + `fsync`),
never a previously recorded measurement.

Result-record schema v2 adds the model's raw text output and provider finish
reason. Both fields are optional so schema-v1 campaign files remain readable;
old rows simply expose ``None`` for those fields. Raw output remains local,
gitignored campaign state rather than a committed artifact.

Resumability policy, from the "infrastructure failure is not model-quality
failure" invariant this whole task is built around:

* ``TERMINAL_STATUSES`` reflect a genuine completed model attempt and its
  verdict -- ``verified_pass``, ``verified_fail``, ``truncated``,
  ``invalid_output``, ``unverified``. A work unit with a terminal record is
  done; resuming never re-runs it, even if a later resume would prefer a
  cleaner result -- re-running a real measurement on every resume would let
  a flaky success get silently re-rolled.
* Everything else (``timeout``, ``provider_unavailable``, ``provider_error``,
  ``sandbox_error``, ``verifier_error``, ``cancelled``) is retryable: the
  model was never actually measured, so resuming retries it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

RESULT_STATUSES = frozenset({
    "verified_pass", "verified_fail", "timeout", "truncated",
    "provider_unavailable", "provider_error", "sandbox_error",
    "verifier_error", "invalid_output", "cancelled", "unverified",
})

TERMINAL_STATUSES = frozenset({
    "verified_pass", "verified_fail", "truncated", "invalid_output", "unverified",
})

RETRYABLE_STATUSES = RESULT_STATUSES - TERMINAL_STATUSES


@dataclass(frozen=True)
class ResultRecord:
    schema_version: int
    work_unit_id: str
    campaign_id: str
    manifest_fingerprint: str
    candidate_name: str
    configuration_fingerprint: str
    suite_id: str
    suite_version: str
    split: str
    item_id: str
    scenario: str
    sample_index: int
    prompt_id: str
    prompt_version: str
    prompt_sha256: str
    tool_schema_version: str
    status: str
    passed: bool | None
    detail: dict[str, Any]
    seed: int | None
    wall_seconds: float
    input_tokens: int | None
    output_tokens: int | None
    started_at: str
    host: str
    error: str | None = None
    raw_output: str | None = None
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in RESULT_STATUSES:
            raise ValueError(f"invalid result status: {self.status!r}")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class CorruptResultRecord(ValueError):
    """A stored result line could not be parsed. Fails clearly, never silently."""

    def __init__(self, path: Path, line_number: int, reason: str):
        super().__init__(f"{path}:{line_number}: corrupt result record ({reason})")
        self.path = path
        self.line_number = line_number
        self.reason = reason


RESULT_RECORD_SCHEMA_VERSION = 2


class ResultStore:
    """One campaign's append-only result log."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read_all(self) -> list[ResultRecord]:
        if not self.path.exists():
            return []
        records: list[ResultRecord] = []
        with self.path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CorruptResultRecord(self.path, line_number, f"invalid JSON: {exc}") from exc
                if not isinstance(payload, dict):
                    raise CorruptResultRecord(self.path, line_number, "line is not a JSON object")
                try:
                    records.append(ResultRecord(**payload))
                except TypeError as exc:
                    raise CorruptResultRecord(self.path, line_number, f"unexpected record shape: {exc}") from exc
                except ValueError as exc:
                    raise CorruptResultRecord(self.path, line_number, str(exc)) from exc
        return records

    def completed_work_unit_ids(self) -> set[str]:
        """Work units that need no further attempts on resume."""
        return {r.work_unit_id for r in self.read_all() if r.is_terminal}

    def append(self, record: ResultRecord) -> None:
        line = json.dumps(record.as_dict(), sort_keys=True, default=str)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
