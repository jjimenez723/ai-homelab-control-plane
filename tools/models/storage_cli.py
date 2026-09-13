"""Operator CLI for initializing and inspecting UUID-verified AI storage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.ingest.config import load_config
from tools.ingest.volumes import diskutil_volume_prober
from tools.models.storage import cache_environment, resolve_ai_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage UUID-verified Angel AI cache paths")
    parser.add_argument("command", choices=("preflight", "init", "env"))
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        storage = resolve_ai_storage(load_config(args.config), diskutil_volume_prober, create=args.command == "init")
    except Exception as exc:  # Do not print a usable fallback path.
        print(f"AI storage unavailable: {type(exc).__name__}: {exc}")
        return 2
    if args.command == "env":
        print(json.dumps(dict(cache_environment(storage)), sort_keys=True))
    else:
        print(json.dumps({"status": "initialized" if args.command == "init" else "ready", "volume_uuid": storage.volume_uuid, "root": str(storage.root), "cache_root": str(storage.cache_root)}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
