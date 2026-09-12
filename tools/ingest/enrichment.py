"""Deterministic enrichment of an asset that is already durably on Angel.

This runs *after* the transfer primitive has verified the bytes and committed
the catalog row, and it reads the destination copy rather than the source --
the source is gone by then, and the destination is what the library actually
holds.

Everything recorded here was measured: dimensions, duration, codecs, page
counts, capture timestamps. No model is involved and none is required
(PLAN Phase 1B / docs/16). The point of separating it from the transfer is
that a probe failure must never call a verified transfer into question, so
every failure here is recorded against the asset and otherwise ignored.
"""

from __future__ import annotations

from pathlib import Path

from tools.ingest.catalog import Catalog, now_iso
from tools.ingest.media.probe import probe_file


def enrich_asset(
    catalog: Catalog,
    *,
    asset_id: str,
    final_path: Path,
    sidecars_dir: Path | None = None,
    mime: str | None = None,
) -> bool:
    """Probe a committed asset and record what was measured. Never raises.

    Returns whether facts were written. A `False` is not an error -- plenty of
    file types have nothing measurable to say.
    """
    try:
        facts = probe_file(final_path, mime=mime)
    except Exception:  # noqa: BLE001 - a probe must never undo a verified ingest
        return False

    try:
        catalog.record_media_metadata(
            asset_id,
            subject_kind="asset",
            media_kind=facts.media_kind,
            mime=facts.mime or mime,
            probe=facts.probe,
            probe_version=facts.probe_version,
            facts=facts.facts,
            created_at=now_iso(),
        )
    except Exception:  # noqa: BLE001 - bookkeeping, not the transaction
        return False

    # The transfer primitive wrote this asset's sidecar before any of these
    # facts existed. Regenerate it, so the durable human-readable record and
    # the catalog agree.
    if sidecars_dir is not None:
        try:
            catalog.write_sidecar(asset_id, sidecars_dir)
        except Exception:  # noqa: BLE001
            pass
    return True
