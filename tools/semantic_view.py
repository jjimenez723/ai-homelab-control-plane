#!/usr/bin/env python3
"""Plan/apply the derived organized client view.

The default is a dry run. Applying requires an explicit ``--apply`` and a
root supplied by the operator after the normal T7 UUID preflight.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.ingest.catalog import Catalog
from tools.ingest.config import load_config
from tools.ingest.t7 import resolve_t7
from tools.ingest.volumes import diskutil_volume_prober
from tools.semantic.materializer import build_placement_proposal, materialize, plan_materialization


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan/apply Angel's derived organized view")
    parser.add_argument("--catalog", type=Path, default=Path("90-system/catalog.sqlite"))
    parser.add_argument("--root", type=Path, required=True, help="verified 20-clients view root")
    parser.add_argument("--config", type=Path, default=None, help="ingest config used for --apply T7 preflight")
    parser.add_argument(
        "--allow-dev-storage-root",
        action="store_true",
        help="explicitly allow the configured development storage override",
    )
    parser.add_argument("--client")
    parser.add_argument("--project")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-free-bytes", type=int, default=0)
    args = parser.parse_args()
    scope = "/".join(item for item in (args.client, args.project) if item) or "all"
    verified_t7 = None
    view_root = args.root.expanduser().resolve()
    if args.apply:
        verified_t7 = resolve_t7(
            load_config(args.config),
            diskutil_volume_prober,
            for_apply=True,
            allow_storage_root_override=args.allow_dev_storage_root,
        )
        trusted_root = verified_t7.root.resolve()
        try:
            view_root.relative_to(trusted_root)
        except ValueError as exc:
            raise SystemExit("refusing --apply: view root is outside the UUID-verified T7 root") from exc
    with Catalog(args.catalog) as catalog:
        proposals = []
        for asset in catalog.assets():
            semantic = catalog.latest_semantic_classification(asset.asset_id)
            if semantic is None:
                continue
            if args.client and semantic.get("client_id") != args.client:
                continue
            if args.project and semantic.get("project_id") != args.project:
                continue
            proposals.append(build_placement_proposal(
                asset.row, semantic, canonical_filename=asset.row.get("final_filename") or asset.row.get("original_filename"),
            ))
        previous = []
        for row in catalog.latest_view_proposals(scope):
            asset = catalog.get_asset(row["asset_id"])
            if asset is None:
                continue
            previous.append(build_placement_proposal(
                asset.row, row, canonical_filename=Path(row["relative_path"]).name if row.get("relative_path") else None,
            ))
        plan = plan_materialization(args.root, proposals, scope=scope, previous=previous, min_free_bytes=args.min_free_bytes)
        catalog.record_view_run(plan.run_id, scope=scope, dry_run=not args.apply,
                                drift_fraction=plan.drift.fraction, drift_aborted=plan.drift.abort,
                                summary=plan.summary())
        for item in plan.proposals:
            catalog.record_view_proposal(
                plan.run_id, asset_id=item.asset_id, client_id=item.client_id,
                project_id=item.project_id, relative_path=item.relative_path,
                tags=item.tags, action="materialize" if item in plan.materializable else "review",
                reason=item.reason, estimated_bytes=item.size,
            )
        print(json.dumps({**plan.summary(), "proposed_paths": [item.relative_path for item in plan.proposals if item.relative_path],
                          "conflicts": list(plan.conflicts), "drift_changed_assets": list(plan.drift.changed_assets)}, indent=2, sort_keys=True))
        if args.apply:
            result = materialize(
                view_root,
                plan,
                min_free_bytes=args.min_free_bytes,
                canonical_root=verified_t7.root if verified_t7 is not None else None,
            )
            for item in plan.materializable:
                catalog.record_materialization(
                    asset_id=item.asset_id, relative_path=item.relative_path or "",
                    source_sha256=item.source_sha256, destination_sha256=item.source_sha256,
                    bytes_count=item.size,
                )
            print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
