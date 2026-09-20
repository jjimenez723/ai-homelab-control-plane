"""Read-only Phase 3 catalog reporting."""

from __future__ import annotations

from typing import Any

from tools.ingest.catalog import Catalog


def phase3_report(catalog: Catalog) -> dict[str, Any]:
    assets = catalog.assets()
    classified = 0
    reviewed = 0
    unknown = 0
    corrections = 0
    for asset in assets:
        row = catalog.latest_semantic_classification(asset.asset_id)
        if row is None:
            continue
        classified += 1
        if row.get("human_review_state") == "review_required" or row.get("human_corrected"):
            reviewed += 1
        if not row.get("client_id") or not row.get("project_id"):
            unknown += 1
        corrections += len(catalog.semantic_corrections(asset.asset_id))
    embeddings = catalog._conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]  # read-only report seam
    materialized = catalog._conn.execute("SELECT COUNT(*) FROM materializations").fetchone()[0]
    latest_view = catalog._conn.execute(
        "SELECT summary FROM organized_view_runs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return {
        "assets_total": len(assets),
        "assets_classified": classified,
        "assets_reviewed": reviewed,
        "unknown_rate": unknown / classified if classified else 0.0,
        "human_correction_count": corrections,
        "embedding_rows": embeddings,
        "materialization_rows": materialized,
        "latest_view": latest_view[0] if latest_view else None,
        "quality": "model_completed_and_validated; gate3_canary_required" if classified else "no_classifications",
    }
