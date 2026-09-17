"""`angel-eval`: the deterministic, unattended evaluation-campaign command.

    ./scripts/angel-eval campaign --profile gate2 --dry-run
    ./scripts/angel-eval campaign --profile gate2 --smoke
    ./scripts/angel-eval campaign --profile gate2
    ./scripts/angel-eval campaign --profile gate2 --resume
    ./scripts/angel-eval report <campaign-id>
    ./scripts/angel-eval audit
    ./scripts/angel-eval analyze <campaign-id>

No step here requires an AI coding agent to supervise it -- see
`docs/22-evaluation-campaign.md` for the full operational writeup.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.bench.gate2_audit import DEFAULT_SPLITS, generate_gate2_audit, to_markdown as audit_to_markdown
from tools.bench.gate2_failure_analysis import write_failure_analysis
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


def _cmd_audit(args: argparse.Namespace) -> int:
    splits = tuple(args.split or DEFAULT_SPLITS)
    if "frozen_holdout" in splits and not args.include_frozen_holdout:
        print(
            "ERROR: refusing to render frozen-holdout content without "
            "--include-frozen-holdout; keep holdout out of prompt/threshold tuning",
            file=sys.stderr,
        )
        return 2

    audit = generate_gate2_audit(splits=splits)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "gate2-contract-audit.json"
    markdown_path = out_dir / "gate2-contract-audit.md"
    json_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(audit_to_markdown(audit), encoding="utf-8")

    summary = audit["summary"]
    print(f"wrote {json_path}")
    print(f"wrote {markdown_path}")
    print(
        f"audit: {summary['errors']} contract error(s), {summary['warnings']} warning(s); "
        f"{summary['routing_items']} routing and {summary['naming_items']} naming items rendered"
    )
    return 1 if args.strict and summary["errors"] else 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else state_dir(
        args.campaign_id, root=Path(args.state_root)
    ) / "failure-analysis"
    try:
        json_path, markdown_path = write_failure_analysis(
            args.campaign_id,
            root=Path(args.state_root),
            out_dir=out_dir,
            candidates=args.candidate,
            suites=args.suite,
        )
    except CorruptResultRecord as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    print(f"wrote {json_path}")
    print(f"wrote {markdown_path}")
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

    audit = subparsers.add_parser(
        "audit",
        help="render and self-check the Gate-2 prompt/oracle contract without running inference",
    )
    audit.add_argument(
        "--split",
        action="append",
        choices=("dev", "validation", "frozen_holdout"),
        help="split to render; repeat for multiple splits (default: dev + validation)",
    )
    audit.add_argument(
        "--include-frozen-holdout",
        action="store_true",
        help="explicitly acknowledge rendering frozen-holdout task content",
    )
    audit.add_argument(
        "--out",
        default=".state/evaluation-audits/gate2",
        help="output directory for audit JSON+Markdown",
    )
    audit.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when deterministic contract errors are detected",
    )
    audit.set_defaults(func=_cmd_audit)

    analyze = subparsers.add_parser(
        "analyze", parents=[common],
        help="summarize existing campaign failures without re-running inference",
    )
    analyze.add_argument("campaign_id")
    analyze.add_argument(
        "--candidate", action="append",
        help="candidate name to include; repeat for multiple (default: all)",
    )
    analyze.add_argument(
        "--suite", action="append",
        help="suite id to include; repeat for multiple (default: all)",
    )
    analyze.add_argument(
        "--out",
        help="output directory (default: <state-root>/<campaign-id>/failure-analysis)",
    )
    analyze.set_defaults(func=_cmd_analyze)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
