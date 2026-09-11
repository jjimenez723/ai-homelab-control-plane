"""CLI entry points behind the small shell wrappers in ``scripts/``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools.backup import BackupError, BackupService
from tools.ingest.config import load_config
from tools.ingest.volumes import diskutil_volume_prober


def _service(config_path: Path | None) -> BackupService:
    return BackupService(load_config(config_path), diskutil_volume_prober)


def _emit(result) -> int:  # type: ignore[no-untyped-def]
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T7 angel encrypted Google Drive backups")
    parser.add_argument("command", choices=("backup", "init", "snapshots", "check", "restore", "retention"))
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--tag", default=None, help="Additional lifecycle tag, e.g. pre-organize.")
    parser.add_argument("--deep", action="store_true", help="For check: read all repository data.")
    parser.add_argument("--target", type=Path, help="Required isolated restore directory.")
    parser.add_argument("--include", action="append", default=[], help="Restore only matching snapshot path(s).")
    parser.add_argument("--apply", action="store_true", help="For retention: perform forget --prune.")
    parser.add_argument("--confirmed-after-restore", action="store_true", help="Required with retention --apply.")
    parser.add_argument("--preflight", action="store_true", help="For backup: verify prerequisites and report scope without uploading.")
    parser.add_argument("snapshot", nargs="?", help="Snapshot ID (or latest) for restore.")
    args = parser.parse_args(argv)
    try:
        service = _service(args.config)
        if args.command == "backup":
            if args.preflight:
                report = service.preflight_report()
                print("T7 backup preflight (no snapshot created):")
                for key, value in report.items():
                    print(f"  {key}: {value}")
                return 0
            return _emit(service.backup(args.tag))
        if args.command == "init":
            print(service.initialize())
            return 0
        if args.command == "snapshots":
            return _emit(service.snapshots())
        if args.command == "check":
            return _emit(service.check(deep=args.deep))
        if args.command == "restore":
            if args.snapshot is None or args.target is None:
                raise BackupError("restore requires SNAPSHOT_ID and --target /separate/recovery-directory")
            return _emit(service.restore(args.snapshot, args.target, args.include))
        if args.command == "retention":
            return _emit(service.retention(apply=args.apply, confirmed_after_restore=args.confirmed_after_restore))
        raise AssertionError("unreachable")
    except (BackupError, ValueError, FileNotFoundError) as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
