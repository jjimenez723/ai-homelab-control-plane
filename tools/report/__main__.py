from __future__ import annotations
import argparse
from . import format_report, report

def main() -> int:
    parser = argparse.ArgumentParser(description="Report local model jobs")
    parser.add_argument("database", nargs="?", default=".state/coordinator/jobs.sqlite")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    print(format_report(report(args.database, days=args.days)))
    return 0

if __name__ == "__main__": raise SystemExit(main())
