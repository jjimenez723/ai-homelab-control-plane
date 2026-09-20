from __future__ import annotations
import argparse
from pathlib import Path

from . import format_report, report

DEFAULT_DB = ".state/coordinator/jobs.sqlite"


def main() -> int:
    parser = argparse.ArgumentParser(description="Report local model jobs (read-only)")
    parser.add_argument("database", nargs="?", default=DEFAULT_DB)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--snapshot-dir", default=None, metavar="DIR",
        help=("Also write a durable telemetry snapshot into DIR. The coordinator "
              "ledger is gitignored, Mac-local, and outside the restic source, so "
              "without this the measurement history has exactly one copy. Point "
              "this at verified Angel 40-ai/evaluation/telemetry to get it backed up."),
    )
    parser.add_argument(
        "--phase3-catalog", default=None, metavar="PATH",
        help="Also print read-only Phase 3 semantic catalog metrics from PATH.",
    )
    args = parser.parse_args()

    value = report(args.database, days=args.days)
    print(format_report(value))

    if args.snapshot_dir:
        from tools.coordinator.snapshot import build_snapshot, write_snapshot

        written = write_snapshot(build_snapshot(args.database, days=args.days), Path(args.snapshot_dir))
        print(f"\nsnapshot written: {written}")
    if args.phase3_catalog:
        from tools.ingest.catalog import Catalog
        from tools.semantic.report import phase3_report
        with Catalog(Path(args.phase3_catalog)) as catalog:
            import json
            print("\nphase3:")
            print(json.dumps(phase3_report(catalog), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
