"""Carrying a file's Mac-side history onto its durable Angel record.

The realistic life of a renamed file is two separate commands:

    organize triage downloads --apply     # IMG_6909.JPG -> 2026-05-04_photo-....jpg
    organize downloads --apply            # ...and later, into Angel

By the time the second command runs, the name the owner would recognise is
gone from the filesystem. `assets.original_filename` records the name at the
moment of transfer, which is the *new* one -- so without this, ingesting a
file the pipeline had helpfully renamed would erase the only name the owner
remembers it by. Invariant 11 forbids that.

So the lineage is read from the local ledger *before* the transfer (the source
is unlinked by it, and (device, inode) are unreadable afterwards) and written
onto the catalog row after the transfer succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.ingest.catalog import Catalog
from tools.ingest.identity import local_file_id_for
from tools.ingest.ledger import LocalLedger

LOCAL_VOLUME_ID = "mac-local"


@dataclass(frozen=True)
class LocalProvenance:
    """What the Mac-side ledger knows about a file about to be ingested."""

    local_file_id: str
    first_seen_filename: str
    naming_history: list[dict[str, Any]] = field(default_factory=list)
    classification_runs: list[dict[str, Any]] = field(default_factory=list)
    retention_recommendation: str | None = None
    retention_run_id: str | None = None


def read_local_provenance(ledger: LocalLedger, path: Path) -> LocalProvenance | None:
    """Look up `path`'s lineage, or None if this pipeline has never seen it.

    Must be called before the transfer: identity is (device, inode), and the
    transfer primitive unlinks the source.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    local_file_id = local_file_id_for(LOCAL_VOLUME_ID, stat.st_dev, stat.st_ino)
    row = ledger.get_local_file(local_file_id)
    if row is None:
        return None
    return LocalProvenance(
        local_file_id=local_file_id,
        first_seen_filename=row["first_seen_filename"] or path.name,
        naming_history=ledger.naming_history(local_file_id),
        classification_runs=ledger.classification_runs(local_file_id),
        retention_recommendation=row["retention_recommendation"],
        retention_run_id=row["retention_run_id"],
    )


def attach_local_provenance(
    catalog: Catalog,
    ledger: LocalLedger,
    provenance: LocalProvenance,
    *,
    asset_id: str,
    sidecars_dir: Path,
) -> None:
    """Write a file's pre-ingestion lineage onto its Angel asset.

    The ledger is Mac-side, rebuildable state; the catalog is durable truth and
    is what restic backs up. Losing the ledger afterwards must not cost the
    provenance of anything that actually reached Angel, so the lineage is
    copied rather than referenced.
    """
    catalog.update_asset(
        asset_id,
        first_seen_filename=provenance.first_seen_filename,
        local_file_id=provenance.local_file_id,
        retention_recommendation=provenance.retention_recommendation,
        retention_run_id=provenance.retention_run_id,
    )
    for row in provenance.naming_history:
        record = dict(row)
        record.pop("naming_id", None)
        record["asset_id"] = asset_id
        catalog.append_naming(record)
    for row in provenance.classification_runs:
        record = dict(row)
        record.pop("run_id", None)
        record["asset_id"] = asset_id
        catalog.append_classification(record)
    ledger.link_naming_to_asset(provenance.local_file_id, asset_id)
    ledger.link_classification_to_asset(provenance.local_file_id, asset_id)

    # The transfer primitive already wrote a sidecar, before any of this
    # existed on the asset row. Regenerate it, or the durable human-readable
    # record would show only the post-rename name -- which is exactly the
    # provenance this function exists to preserve.
    catalog.write_sidecar(asset_id, sidecars_dir)
