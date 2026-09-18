"""`naming-lab`: launches the local-only Streamlit Naming Lab UI.

    ./scripts/angel-eval naming-lab --corpus /path/to/my/naming-seeds

Binds `127.0.0.1` only, unconditionally -- there is no flag that changes
this (spec section 5: "must default to localhost only" / "do not expose
this publicly" is treated as an absolute rule, not a default).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

APP_PATH = Path(__file__).resolve().parent / "app.py"
BIND_ADDRESS = "127.0.0.1"


def build_streamlit_command(
    corpus: Path, *, python: str = sys.executable, app_path: Path = APP_PATH,
) -> list[str]:
    """The exact subprocess argv used to launch the UI. Pure, so it is testable
    without actually starting a server.

    `--server.address` is a literal `BIND_ADDRESS`, not derived from any
    parameter this function accepts -- there is no argument that can make
    it bind anywhere but loopback.
    """
    return [
        python, "-m", "streamlit", "run", str(app_path),
        "--server.address", BIND_ADDRESS,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--",
        "--corpus", str(Path(corpus).expanduser().resolve()),
    ]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="angel-eval")
    subparsers = parser.add_subparsers(dest="command")
    naming_lab = subparsers.add_parser("naming-lab", help="launch the local Naming Lab UI")
    naming_lab.add_argument("--corpus", required=True, type=Path, help="directory of sample files to review")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.command != "naming-lab":
        parser.print_help()
        return 2

    corpus = args.corpus.expanduser().resolve()
    if not corpus.is_dir():
        print(f"ERROR: --corpus is not a directory: {corpus}", file=sys.stderr)
        return 2

    command = build_streamlit_command(corpus)
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
