"""Read-only aggregation for the Mac-local coordinator ledger."""
from __future__ import annotations
import datetime as dt
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None

def report(db: str | Path | sqlite3.Connection, *, days: int = 7, now: dt.datetime | None = None) -> dict[str, Any]:
    """Summarise telemetry without changing state; absent/empty DB is valid."""
    if days <= 0: raise ValueError("days must be positive")
    conn = sqlite3.connect(str(db)) if not isinstance(db, sqlite3.Connection) else db
    conn.row_factory = sqlite3.Row
    cutoff = ((now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=days)).isoformat()
    try:
        jobs = conn.execute("SELECT * FROM jobs WHERE created_at >= ?", (cutoff,)).fetchall()
        attempts = conn.execute("SELECT a.*, j.task_class FROM job_attempts a JOIN jobs j ON j.job_id=a.job_id WHERE j.created_at >= ?", (cutoff,)).fetchall()
    except sqlite3.OperationalError:
        jobs, attempts = [], []
    verified = [row for row in attempts if row["tier"] in ("deterministic", "small_local") and row["verifier_result"] in ("passed", "failed")]
    known_costs = [row["estimated_cost_usd"] for row in attempts if row["estimated_cost_usd"] is not None]
    unknown_cost = any(row["estimated_cost_usd"] is None and row["provider"] not in (None, "deterministic") for row in attempts)
    known_energy = [row["estimated_energy_wh"] for row in attempts if row["estimated_energy_wh"] is not None]
    unknown_energy = any(row["estimated_energy_wh"] is None and row["tier"] != "deterministic" for row in attempts)
    retries = sum(1 for row in attempts if row["attempt_number"] > 1)
    return {
        "period_days": days,
        "jobs": len(jobs), "attempts": len(attempts), "retries": retries,
        "jobs_by_tier": dict(Counter(str(row["selected_tier"] or "unselected") for row in jobs)),
        "jobs_by_task_class": dict(Counter(str(row["task_class"]) for row in jobs)),
        "success_rate": _ratio(sum(row["final_disposition"] == "success" for row in jobs), len(jobs)),
        "deterministic_verification_success_rate": _ratio(sum(row["verifier_result"] == "passed" for row in verified), len(verified)),
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
