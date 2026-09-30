"""Local-only launch command for the operator dashboard companion API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.operator_dashboard.service import CompanionService, OperatorAuthorizer, ServicePolicy, LOOPBACK_HOST


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="angel-operator")
    parser.add_argument("--operators", required=True, type=Path, help="JSON object with allowed Firebase subject IDs")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    raw = json.loads(args.operators.read_text(encoding="utf-8"))
    subjects = frozenset(raw.get("operator_subjects", ()))
    policy = ServicePolicy(allowed_origins=frozenset(raw.get("allowed_origins", ())), operator_subjects=subjects)
    # Deliberately refuses to pretend an opaque browser token is validated. A
    # deployment must inject a Firebase-ID-token verifier before this command
    # is enabled for a hosted origin.
    def unavailable_verifier(_token: str) -> str:
        raise PermissionError("no Firebase ID-token verifier configured")
    service = CompanionService(LOOPBACK_HOST, args.port, authorizer=OperatorAuthorizer(policy=policy, verify_bearer=unavailable_verifier), overview=lambda: {"status": "local companion ready"})
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
