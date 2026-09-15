"""Export coordinator telemetry somewhere a backup can actually see it.

`.state/coordinator/jobs.sqlite` is deliberately Mac-local and independent
of Angel (D-033) so that routing keeps working with the drive detached.
That was right, and it had an unnoticed consequence: `.state/` is
gitignored, the restic source is pinned to the T7 root, and the ledger is
therefore in **no** backup at all. The telemetry that model certification
(D-042) and the Phase 6 "measure before buying hardware" decision
(`PRINCIPLES.md` §12) both depend on existed in exactly one copy, on one
internal SSD.

This module does not move the ledger. It writes a normalized, derived
snapshot into the UUID-verified Angel `40-ai/evaluation/` tree, which restic
already backs up (`40-ai/model-cache/**` is the only exclusion, D-037). The
live ledger stays local and authoritative; the snapshot is a durable copy of
what it measured.

Three properties kept deliberately:

* it is **derived data, not state** -- losing every snapshot costs history,
  never correctness, and nothing reads a snapshot back into the ledger;
* it goes through the same UUID gate as everything else on Angel, so a
  name-matched `/Volumes/Angel` is not a destination;
* it contains aggregates and job metadata, never prompt or response text,
  which stays local under D-030/D-035.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from tools.router.report import report

SNAPSHOT_SCHEMA_VERSION = 1


def build_snapshot(db: str | Path, *, days: int = 30, now: dt.datetime | None = None,
                   host: str | None = None) -> dict[str, Any]:
    """A self-describing telemetry snapshot. Aggregates only, never content."""
    moment = now or dt.datetime.now(dt.timezone.utc)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "kind": "coordinator-telemetry-snapshot",
        "captured_at": moment.isoformat(),
        "window_days": days,
        "host": host or os.uname().nodename,
        "source": str(db),
        "note": (
            "Derived aggregate of the Mac-local coordinator ledger. Not a "
            "restore source: the live ledger is authoritative and stays "
            "local (D-033). Contains no prompt or response text (D-030)."
        ),
        "report": report(db, days=days, now=moment),
    }


def write_snapshot(snapshot: dict[str, Any], destination_dir: Path) -> Path:
    """Write one snapshot atomically, named by capture time.

    Atomic because a backup may run concurrently: a reader must never see a
    half-written file and treat it as the record.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(snapshot["captured_at"]).replace(":", "").replace("-", "")[:15]
    target = destination_dir / f"coordinator-telemetry-{stamp}Z.json"
    handle, temporary = tempfile.mkstemp(dir=str(destination_dir), suffix=".partial")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def export_to_angel(db: str | Path, config, prober, *, days: int = 30,
                    now: dt.datetime | None = None) -> Path:
    """Write a snapshot into verified Angel storage, or refuse.

    `resolve_ai_storage` raises `VolumeError` when the configured T7 is
    absent, locked, read-only, or uninitialised. That exception is allowed
    to propagate: an export that silently fell back to the internal disk
    would recreate the single-copy problem this module exists to close.
    """
    from tools.models.storage import resolve_ai_storage

    storage = resolve_ai_storage(config, prober, create=False)
    return write_snapshot(build_snapshot(db, days=days, now=now), storage.evaluation / "telemetry")
