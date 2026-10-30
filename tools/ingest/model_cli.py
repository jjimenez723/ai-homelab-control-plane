"""CLI for proposal-only model analysis and human-maintained person profiles."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from tools.ingest.archive_profile import archive_profile_for
from tools.ingest.catalog import Catalog
from tools.ingest.config import load_config
from tools.ingest.modeling import ModelError, ModelOrchestrator


def _profile_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise ValueError("profile name must contain at least one letter or number")
    return f"person-{slug}"


def cmd_analyze(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    failures = 0
    with Catalog(args.catalog) as catalog:
        orchestrator = ModelOrchestrator(config.ai, archive_profile_for(config))
        for raw_path in args.paths:
            path = raw_path.expanduser().resolve()
            asset = catalog.get_asset_by_final_path(str(path))
            if asset is None and args.asset_id:
                asset = catalog.get_asset(args.asset_id)
            if asset is None:
                print(f"ERROR: no catalog asset has final_path {path}; analysis requires a verified asset", file=sys.stderr)
                failures += 1
                continue
            try:
                result = orchestrator.analyze_asset(catalog, asset)
            except ModelError as exc:
                print(f"ERROR: {path}: {exc}", file=sys.stderr)
                failures += 1
                continue
            model = result.model or "disabled"
            print(
                f"{path}: model={model} proposals={len(result.proposal_ids)} "
                f"exact_duplicates={len(result.exact_duplicate_ids)} "
                f"people_detected={result.people_detected} "
                f"animals_detected={result.animals_detected} "
                f"profile_matches={result.people_matched}"
            )
    return 1 if failures else 0


def cmd_profile_add(args: argparse.Namespace) -> int:
    profile_id = args.profile_id or _profile_id(args.name)
    with Catalog(args.catalog) as catalog:
        catalog.upsert_person_profile(
            profile_id,
            display_name=args.name,
            scope=args.scope,
            aliases=args.alias,
            notes=args.notes,
        )
    print(f"saved profile {profile_id}: {args.name}")
    return 0


def cmd_profile_list(args: argparse.Namespace) -> int:
    with Catalog(args.catalog) as catalog:
        profiles = catalog.profile_context()
    if not profiles:
        print("No person profiles yet.")
        return 0
    for profile in profiles:
        aliases = f" aliases={','.join(profile['aliases'])}" if profile["aliases"] else ""
        print(
            f"{profile['profile_id']}: {profile['display_name']} scope={profile.get('scope', 'global')}"
            f"{aliases} observations={profile['observation_count']}"
        )
    return 0


def cmd_profile_correct(args: argparse.Namespace) -> int:
    with Catalog(args.catalog) as catalog:
        if not catalog.list_person_profiles():
            print("ERROR: create a profile before recording a correction", file=sys.stderr)
            return 1
        profile_ids = {profile["profile_id"] for profile in catalog.list_person_profiles()}
        if args.profile_id not in profile_ids:
            print(f"ERROR: unknown profile {args.profile_id}", file=sys.stderr)
            return 1
        asset = catalog.get_asset(args.asset_id)
        if asset is None:
            print(f"ERROR: unknown asset {args.asset_id}", file=sys.stderr)
            return 1
        catalog.record_correction(args.asset_id, field="person_profile_id", value=args.profile_id)
        catalog.record_person_observation(
            args.asset_id,
            profile_id=args.profile_id,
            confidence=1.0,
            source="human",
            evidence="explicit human correction",
        )
    print(f"recorded human correction: {args.asset_id} -> {args.profile_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analyze-media", description="Proposal-only local media analysis for Angel.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="analyze verified catalog assets with the configured local model")
    analyze.add_argument("paths", nargs="+", type=Path)
    analyze.add_argument("--catalog", type=Path, required=True, help="path to 90-system/catalog.sqlite")
    analyze.add_argument("--asset-id", help="fallback asset ID when a path is not the current final_path")
    analyze.add_argument("--config", type=Path, default=None)
    analyze.set_defaults(handler=cmd_analyze)

    profile = subparsers.add_parser("profile", help="manage human-maintained person profiles")
    profile_subparsers = profile.add_subparsers(dest="profile_command", required=True)

    add = profile_subparsers.add_parser("add", help="create or update a profile")
    add.add_argument("name")
    add.add_argument("--catalog", type=Path, required=True)
    add.add_argument("--profile-id")
    add.add_argument("--scope", default="global", help="privacy scope such as global or client/project")
    add.add_argument("--alias", action="append", default=[])
    add.add_argument("--notes", default="")
    add.set_defaults(handler=cmd_profile_add)

    listing = profile_subparsers.add_parser("list", help="list profiles and confirmed observations")
    listing.add_argument("--catalog", type=Path, required=True)
    listing.set_defaults(handler=cmd_profile_list)

    correct = profile_subparsers.add_parser("correct", help="record a human person/profile correction")
    correct.add_argument("asset_id")
    correct.add_argument("--profile-id", required=True)
    correct.add_argument("--catalog", type=Path, required=True)
    correct.set_defaults(handler=cmd_profile_correct)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
