"""Content-keyed cache for perceived evidence, backed by the local ledger.

Running OCR or a vision backend over every file on every scan would make the
hourly automation slow and, for anything battery- or thermally-sensitive,
wasteful for no reason: a file's bytes do not change between runs, so what an
extractor or backend says about them should not need to be re-derived every
time either. This module is the seam between "here is the evidence key for
one (content, producer, model, pipeline) combination" and the one table that
persists the answer -- `file_evidence`, added by
`tools/ingest/ledger.py`'s local migration 3.

The cache is keyed by content hash plus producer identity, **never by path**:
a rename with identical bytes hits the cache, and different bytes at the same
path can never collide, because the SHA is part of the key. Bumping a
backend's version, a model's version, or `EVIDENCE_PIPELINE_VERSION` misses
the cache for that kind only -- every other cached kind for the same bytes is
addressed by a different key and is untouched.

A cached row with `status="unavailable"` (a backend that could not run at
all) is a legitimate thing to store: it stops an hourly job from retrying a
broken backend on every file, every run. That is only safe because
`producer_version` is part of the key and is defined, by the backend
contract in `tools/ingest/evidence/backends/__init__.py`, to change identity
whenever the backend's availability changes -- so installing a previously
missing engine changes the version string, which changes the key, which
re-asks the question instead of reading back a stale "unavailable" answer.
This module trusts that contract; it does no availability-specific logic of
its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from tools.ingest.evidence.types import (
    EVIDENCE_PIPELINE_VERSION,
    EvidenceWarning,
    evidence_warning_from_json,
    evidence_warning_to_json,
)
from tools.ingest.identity import evidence_id_for


class EvidenceLedger(Protocol):
    """The slice of `LedgerTables` this module needs. See `tools/ingest/ledger.py`."""

    def get_file_evidence(self, evidence_id: str) -> dict[str, Any] | None: ...

    def record_file_evidence(
        self,
        *,
        evidence_id: str,
        content_sha256: str,
        evidence_kind: str,
        producer: str,
        producer_version: str,
        model: str,
        model_version: str,
        pipeline_version: str,
        route_reason: str | None,
        status: str,
        payload: dict[str, Any],
        warnings: list[dict[str, Any]],
        created_at: str,
    ) -> None: ...


@dataclass(frozen=True)
class CacheKey:
    """Every input that determines whether a cached answer is still valid."""

    content_sha256: str
    kind: str
    producer: str
    producer_version: str
    model: str = ""
    model_version: str = ""
    pipeline_version: str = EVIDENCE_PIPELINE_VERSION

    @property
    def evidence_id(self) -> str:
        return evidence_id_for(
            self.content_sha256,
            self.kind,
            self.producer,
            self.producer_version,
            self.model,
            self.model_version,
            self.pipeline_version,
        )


@dataclass(frozen=True)
class CacheLookup:
    hit: bool
    payload: dict[str, Any] | None
    warnings: tuple[EvidenceWarning, ...] = ()
    status: str = "ok"
    generated_at: str | None = None


def lookup(ledger: EvidenceLedger, key: CacheKey) -> CacheLookup:
    """Return the cached result for `key`, or a miss.

    A miss is not an error and not itself worth a warning -- it simply means
    this exact (content, producer, model, pipeline) combination has not been
    measured yet, which is the normal state for a brand-new file.
    """
    row = ledger.get_file_evidence(key.evidence_id)
    if row is None:
        return CacheLookup(hit=False, payload=None, warnings=(), status="", generated_at=None)
    warnings = tuple(evidence_warning_from_json(item) for item in row["warnings"])
    return CacheLookup(
        hit=True,
        payload=row["payload"],
        warnings=warnings,
        status=row["status"],
        generated_at=row["generated_at"],
    )


def store(
    ledger: EvidenceLedger,
    key: CacheKey,
    *,
    payload: dict[str, Any],
    warnings: tuple[EvidenceWarning, ...] = (),
    status: str = "ok",
    route_reason: str | None = None,
    now: str,
) -> None:
    """Persist one measurement under `key`, replacing any prior row for it.

    Replacing is correct, not a loss of history: `key.evidence_id` already
    encodes every component that could make the answer different (see
    `identity.evidence_id_for`), so a second write to the same id is by
    construction a re-derivation of the same question, not a competing
    decision that needs to be kept alongside the first. See
    `LedgerTables.record_file_evidence` for why that is not a violation of the
    append-only rule that governs `naming_history`/`classification_runs`.
    """
    ledger.record_file_evidence(
        evidence_id=key.evidence_id,
        content_sha256=key.content_sha256,
        evidence_kind=key.kind,
        producer=key.producer,
        producer_version=key.producer_version,
        model=key.model,
        model_version=key.model_version,
        pipeline_version=key.pipeline_version,
        route_reason=route_reason,
        status=status,
        payload=payload,
        warnings=[evidence_warning_to_json(warning) for warning in warnings],
        created_at=now,
    )
