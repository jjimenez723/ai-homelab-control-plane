"""`python -m tools.appspec` -- validate, scaffold, and brief application specs.

    python -m tools.appspec validate <file>
    python -m tools.appspec new --class <backend-profile-id> --out <file>
    python -m tools.appspec agent-brief <file> [--task-class <class>]

See `docs/28-application-spec.md` for the pipeline this supports.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from tools.appspec.brief import IMPLEMENTATION_TASK_CLASSES, generate_all_briefs, generate_brief
from tools.appspec.schema import REPO_ROOT
from tools.appspec.validate import known_backend_profile_ids, load_spec, validate_spec

EXAMPLES_DIR = REPO_ROOT / "templates" / "app-spec" / "examples"

# Maps a backend-profile id (tools.appspec.validate.KNOWN_BACKEND_PROFILE_IDS)
# to the representative example spec built for that class.
CLASS_TO_EXAMPLE = {
    "social-behavior-change": "squad-quit.yaml",
    "nursing-shift-productivity": "nurse-shift.yaml",
    "single-player-game": "single-player-game.yaml",
    "multiplayer-game": "multiplayer-game.yaml",
}


def _cmd_validate(args: argparse.Namespace) -> int:
    report = validate_spec(args.file)
    print(("VALID: " if report.valid else "INVALID: ") + str(args.file))
    for error in report.errors:
        print(f"  error: {error}")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    return 0 if report.valid else 1


def _cmd_new(args: argparse.Namespace) -> int:
    example_name = CLASS_TO_EXAMPLE.get(args.klass)
    if example_name is None:
        print(f"error: --class must be one of {sorted(CLASS_TO_EXAMPLE)}, got {args.klass!r}", file=sys.stderr)
        return 2
    source = EXAMPLES_DIR / example_name
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, out_path)
    print(
        f"wrote {out_path} (scaffolded from {source.relative_to(REPO_ROOT)}; "
        "edit app_id, name, and every section before treating this as a real spec)"
    )
    return 0


def _cmd_agent_brief(args: argparse.Namespace) -> int:
    spec = load_spec(args.file)
    if args.task_class:
        print(generate_brief(spec, args.task_class))
    else:
        print(generate_all_briefs(spec))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.appspec", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="Validate a spec structurally and semantically.")
    p_validate.add_argument("file", help="Path to a YAML or JSON spec file.")
    p_validate.set_defaults(func=_cmd_validate)

    p_new = sub.add_parser("new", help="Scaffold a new spec from the matching representative example.")
    p_new.add_argument(
        "--class", dest="klass", required=True, choices=sorted(CLASS_TO_EXAMPLE),
        help=f"Known backend profile id, also documented in {known_backend_profile_ids()!r}.",
    )
    p_new.add_argument("--out", required=True, help="Path to write the scaffolded spec to.")
    p_new.set_defaults(func=_cmd_new)

    p_brief = sub.add_parser(
        "agent-brief", help="Print a bounded implementation brief per implementation task class."
    )
    p_brief.add_argument("file", help="Path to a YAML or JSON spec file.")
    p_brief.add_argument(
        "--task-class", choices=IMPLEMENTATION_TASK_CLASSES, default=None,
        help="Print only this task class's brief instead of all four.",
    )
    p_brief.set_defaults(func=_cmd_agent_brief)

    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
