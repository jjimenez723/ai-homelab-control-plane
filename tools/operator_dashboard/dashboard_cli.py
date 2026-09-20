"""Safe launcher for the local-only Streamlit operator dashboard."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

APP_PATH = Path(__file__).with_name("app.py")
BIND_ADDRESS = "127.0.0.1"


def build_command(state: Path, corpus_id: str, *, python: str = sys.executable) -> list[str]:
    return [python, "-m", "streamlit", "run", str(APP_PATH), "--server.address", BIND_ADDRESS,
            "--server.headless", "true", "--browser.gatherUsageStats", "false", "--",
            "--state", str(state.expanduser().resolve()), "--corpus-id", corpus_id]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="angel-operator-dashboard")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--corpus-id", required=True)
    args = parser.parse_args(argv)
    return subprocess.call(build_command(args.state, args.corpus_id))


if __name__ == "__main__":
    raise SystemExit(main())
