"""Safe launcher for the local-only Streamlit operator dashboard."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

APP_PATH = Path(__file__).with_name("app.py")
REPO_ROOT = APP_PATH.parents[2]
DEFAULT_CLIENTS_ROOT = REPO_ROOT / "20-clients"
BIND_ADDRESS = "127.0.0.1"


def build_command(
    state: Path,
    corpus_id: str,
    *,
    clients_root: Path = DEFAULT_CLIENTS_ROOT,
    python: str = sys.executable,
) -> list[str]:
    return [
        python, "-m", "streamlit", "run", str(APP_PATH),
        "--server.address", BIND_ADDRESS,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--",
        "--state", str(state.expanduser().resolve()),
        "--corpus-id", corpus_id,
        "--clients-root", str(clients_root.expanduser().resolve()),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="angel-operator-dashboard")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--clients-root", type=Path, default=DEFAULT_CLIENTS_ROOT)
    args = parser.parse_args(argv)
    return subprocess.call(build_command(args.state, args.corpus_id, clients_root=args.clients_root))


if __name__ == "__main__":
    raise SystemExit(main())
