"""`angel-eval`: the deterministic, unattended evaluation-campaign command.

    ./scripts/angel-eval campaign --profile gate2 --dry-run
    ./scripts/angel-eval campaign --profile gate2 --smoke
    ./scripts/angel-eval campaign --profile gate2
    ./scripts/angel-eval campaign --profile gate2 --resume
    ./scripts/angel-eval report <campaign-id>

No step here requires an AI coding agent to supervise it -- see
`docs/22-evaluation-campaign.md` for the full operational writeup.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.campaign.manifest import ManifestError, load_manifest
from tools.campaign.profiles import UnknownProfile, manifest_path_for_profile
from tools.campaign.report import generate_report, write_report
from tools.campaign.runner import DEFAULT_STATE_ROOT, ManifestDrift, dry_run, run_campaign, state_dir
from tools.campaign.store import CorruptResultRecord


def _resolve_manifest(args: argparse.Namespace):
    if args.manifest:
        return load_manifest(Path(args.manifest))
    return load_manifest(manifest_path_for_profile(args.profile))


def _cmd_campaign(args: argparse.Namespace) -> int:
    try:
        manifest = _resolve_manifest(args)
    except (ManifestError, UnknownProfile, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        report = dry_run(manifest, root=Path(args.state_root))
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return 0

    try:
        summary = run_campaign(
            manifest, root=Path(args.state_root), resume=not args.no_resume,
            smoke=args.smoke, strict_candidates=args.strict, max_workers=args.workers,
            allow_manifest_change=args.allow_manifest_change,
            auto_pull=not args.no_auto_pull,
        )
    except ManifestDrift as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except CorruptResultRecord as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 5

    print(f"campaign {summary.campaign_id}: {summary.executed} executed, "
          f"{summary.skipped_already_done} already done, {summary.total_work_units} total work units")
    for status, count in sorted(summary.status_counts.items()):
        print(f"  {status}: {count}")
    if summary.pulled_candidates:
        print(f"  auto-pulled: {', '.join(summary.pulled_candidates)}")
    if summary.skipped_candidates:
        print(f"  skipped (unavailable/lifecycle failure): {', '.join(summary.skipped_candidates)}")
    if summary.memory_budget_bytes is not None:
        gib = summary.memory_budget_bytes / (1024 ** 3)
        print(f"  campaign model-memory budget: {gib:.1f} GiB; max parallel candidates: {summary.max_parallel_candidates}")
    infra_statuses = {"timeout", "provider_unavailable", "provider_error", "sandbox_error", "verifier_error"}
    had_infra_failure = any(summary.status_counts.get(s, 0) for s in infra_statuses)
    return 1 if had_infra_failure and args.strict else 0


def _cmd_report(args: argparse.Namespace) -> int:
    manifest = None
    if args.manifest:
        manifest = load_manifest(Path(args.manifest))
    elif args.profile:
        try:
            manifest = load_manifest(manifest_path_for_profile(args.profile))
        except UnknownProfile:
            manifest = None
    report = generate_report(args.campaign_id, root=Path(args.state_root), manifest=manifest)
    out_dir = Path(args.out) if args.out else state_dir(args.campaign_id, root=Path(args.state_root)) / "report"
    json_path, md_path = write_report(report, out_dir=out_dir)
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT),
                        help="directory holding per-campaign checkpoints/results (default: .state/campaigns)")

    parser = argparse.ArgumentParser(prog="angel-eval", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    campaign = subparsers.add_parser("campaign", parents=[common],
                                     help="run (or dry-run/smoke-test) an evaluation campaign")
    campaign.add_argument("--profile", help="named campaign profile, e.g. gate2 (config/campaigns/<profile>.yaml)")
    campaign.add_argument("--manifest", help="explicit path to a campaign manifest YAML file")
    campaign.add_argument("--dry-run", action="store_true", help="enumerate work without running any inference")
    campaign.add_argument("--smoke", action="store_true",
                          help="run one item per (candidate, suite) pair to validate the harness")
    campaign.add_argument("--resume", action="store_true", help="accepted for clarity; resuming is always the default")
    campaign.add_argument("--no-resume", action="store_true",
                          help="ignore prior results for this campaign_id and recompute everything")
    campaign.add_argument("--strict", action="store_true",
                          help="fail rather than skip when a candidate model is unavailable, and exit "
                               "non-zero if any infrastructure failures occurred")
    campaign.add_argument("--workers", type=int, default=None,
                          help="override the manifest's maximum simultaneous candidate batches")
    campaign.add_argument("--no-auto-pull", action="store_true",
                          help="do not auto-pull missing manifest-approved Ollama candidates")
    campaign.add_argument("--allow-manifest-change", action="store_true",
                          help="acknowledge that this campaign_id's manifest changed since its last run")
    campaign.set_defaults(func=_cmd_campaign)

    report = subparsers.add_parser("report", parents=[common],
                                   help="regenerate JSON+Markdown reports from raw campaign records")
    report.add_argument("campaign_id")
    report.add_argument("--profile", help="named profile, used only to attach qualification thresholds")
    report.add_argument("--manifest", help="explicit manifest path, used only to attach qualification thresholds")
    report.add_argument("--out", help="output directory (default: <state-root>/<campaign-id>/report)")
    report.set_defaults(func=_cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
