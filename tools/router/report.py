"""Read-only aggregation for the Mac-local coordinator ledger.

Two honesty rules shape what this reports, both of them consequences of
D-048:

* **"Nobody checked" is not "verified".** An attempt where no deterministic
  verifier ran records `verifier_result = NULL`, is counted as
  `unverified_attempts`, and is excluded from every verification rate. The
  previous version wrote `"passed"` whenever a verifier was absent, which
  made a job that returned nonsense indistinguishable from one a verifier
  approved -- in the single telemetry store that model certification and
  the Phase 6 hardware decision both read.
* **An empty history is a valid answer.** A machine that has never routed a
  job reports zeros, not a stack trace.
"""
from __future__ import annotations
import datetime as dt
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

# Dispositions that mean the job produced output. Only the first is a claim
# about correctness; the second is a claim about completion.
VERIFIED_DISPOSITIONS = ("success",)
UNVERIFIED_DISPOSITIONS = ("unverified",)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _empty(days: int) -> dict[str, Any]:
    return {
        "period_days": days, "jobs": 0, "attempts": 0, "retries": 0,
        "jobs_by_tier": {}, "jobs_by_task_class": {}, "jobs_by_disposition": {},
        "verified_success_rate": None, "completion_rate": None,
        "unverified_attempts": 0, "unverified_share_of_attempts": None,
        "verification_coverage": None, "verification_success_rate": None,
        "quality_by_task_class": {},
        "authorization_blocked": 0, "refused": 0,
        "escalation_rate": None, "input_tokens": 0, "output_tokens": 0,
        "model_provider_usage": {}, "wall_time_seconds": 0.0,
        "estimated_cost_usd": None, "estimated_energy_wh": None,
        "energy_method": "unknown (no direct sensor measurement)",
    }


def report(db: str | Path | sqlite3.Connection, *, days: int = 7, now: dt.datetime | None = None) -> dict[str, Any]:
    """Summarise telemetry without changing state; an absent database is valid."""
    if days <= 0: raise ValueError("days must be positive")

    if not isinstance(db, sqlite3.Connection):
        path = Path(db)
        # sqlite3.connect() creates a missing *file*, but raises on a missing
        # parent *directory* -- which is the shipped default's state on any
        # machine that has not routed a job yet. Reporting "no jobs" is the
        # truthful answer there, and it is what `python -m tools.report` does
        # on a clean checkout.
        if not path.exists() and not path.parent.is_dir():
            return _empty(days)
        conn = sqlite3.connect(str(path))
    else:
        conn = db
    conn.row_factory = sqlite3.Row
    cutoff = ((now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=days)).isoformat()
    try:
        jobs = conn.execute("SELECT * FROM jobs WHERE created_at >= ?", (cutoff,)).fetchall()
        attempts = conn.execute(
            "SELECT a.*, j.task_class FROM job_attempts a JOIN jobs j ON j.job_id=a.job_id "
            "WHERE j.created_at >= ?", (cutoff,)).fetchall()
    except sqlite3.OperationalError:
        jobs, attempts = [], []

    if not jobs and not attempts:
        return _empty(days)

    # A verifier ran iff the column is non-NULL. Everything else is silence.
    checked = [row for row in attempts if row["verifier_result"] in ("passed", "failed")]
    unchecked = [row for row in attempts if row["verifier_result"] is None]

    known_costs = [row["estimated_cost_usd"] for row in attempts if row["estimated_cost_usd"] is not None]
    unknown_cost = any(row["estimated_cost_usd"] is None and row["provider"] not in (None, "deterministic") for row in attempts)
    known_energy = [row["estimated_energy_wh"] for row in attempts if row["estimated_energy_wh"] is not None]
    unknown_energy = any(row["estimated_energy_wh"] is None and row["tier"] != "deterministic" for row in attempts)
    retries = sum(1 for row in attempts if row["attempt_number"] > 1)

    # Capability-specific quality, which Gate 2 asks reporting to surface.
    # Reported per task class and only over attempts a verifier actually
    # judged, so an uncovered class shows as coverage 0 rather than as a
    # perfect score.
    by_class: dict[str, dict[str, Any]] = {}
    for row in attempts:
        entry = by_class.setdefault(str(row["task_class"]), {"attempts": 0, "verified_pass": 0, "verified_fail": 0, "unverified": 0})
        entry["attempts"] += 1
        if row["verifier_result"] == "passed": entry["verified_pass"] += 1
        elif row["verifier_result"] == "failed": entry["verified_fail"] += 1
        else: entry["unverified"] += 1
    for entry in by_class.values():
        judged = entry["verified_pass"] + entry["verified_fail"]
        entry["verification_coverage"] = _ratio(judged, entry["attempts"])
        entry["verified_success_rate"] = _ratio(entry["verified_pass"], judged)

    return {
        "period_days": days,
        "jobs": len(jobs), "attempts": len(attempts), "retries": retries,
        "jobs_by_tier": dict(Counter(str(row["selected_tier"] or "unselected") for row in jobs)),
        "jobs_by_task_class": dict(Counter(str(row["task_class"]) for row in jobs)),
        "jobs_by_disposition": dict(Counter(str(row["final_disposition"] or "open") for row in jobs)),
        # Verified success is the only rate that claims correctness.
        "verified_success_rate": _ratio(
            sum(row["final_disposition"] in VERIFIED_DISPOSITIONS for row in jobs), len(jobs)),
        # Completion counts verified and unverified output together, and is
        # labelled so it is never mistaken for a quality measure.
        "completion_rate": _ratio(
            sum(row["final_disposition"] in VERIFIED_DISPOSITIONS + UNVERIFIED_DISPOSITIONS for row in jobs), len(jobs)),
        "unverified_attempts": len(unchecked),
        "unverified_share_of_attempts": _ratio(len(unchecked), len(attempts)),
        "verification_coverage": _ratio(len(checked), len(attempts)),
        "verification_success_rate": _ratio(sum(row["verifier_result"] == "passed" for row in checked), len(checked)),
        "quality_by_task_class": by_class,
        "authorization_blocked": sum(row["final_disposition"] == "authorization_required" for row in jobs),
        "refused": sum(row["final_disposition"] == "refused" for row in jobs),
        "escalation_rate": _ratio(sum(bool(row["escalated"]) for row in jobs), len(jobs)),
        "input_tokens": sum(row["input_tokens"] or 0 for row in attempts),
        "output_tokens": sum(row["output_tokens"] or 0 for row in attempts),
        "model_provider_usage": dict(Counter(f"{row['provider'] or 'unknown'}/{row['model'] or 'unknown'}" for row in attempts)),
        "wall_time_seconds": sum((row["wall_ms"] or 0) / 1000 for row in attempts),
        "estimated_cost_usd": None if not attempts or unknown_cost else sum(known_costs),
        "estimated_energy_wh": None if not attempts or unknown_energy else sum(known_energy),
        "energy_method": "configured estimate; not direct sensor measurement" if known_energy else "unknown (no direct sensor measurement)",
    }


def format_report(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True)
