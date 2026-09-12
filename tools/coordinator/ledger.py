"""Durable local job/attempt telemetry for the V0 coordinator.

This database is intentionally independent of the T7 asset catalog: a model
request must continue to work while Angel is detached.  The schema is
append-friendly and is suitable for the future Phase 4 coordinator API.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.ingest.migrations import add_column_if_missing, apply_migrations, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  idempotency_key TEXT,
  task_class TEXT NOT NULL,
  request_json TEXT NOT NULL,
  privacy TEXT NOT NULL DEFAULT 'local_only',
  risk TEXT NOT NULL DEFAULT 'low',
  preferred_execution TEXT,
  selected_tier TEXT,
  selection_reason TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  max_attempts INTEGER NOT NULL DEFAULT 3,
  retry_count INTEGER NOT NULL DEFAULT 0,
  escalated INTEGER NOT NULL DEFAULT 0,
  escalation_reason TEXT,
  final_disposition TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_by_idempotency ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS job_attempts (
  attempt_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  attempt_number INTEGER NOT NULL,
  tier TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  model_version TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  input_tokens INTEGER,
  output_tokens INTEGER,
  wall_ms REAL,
  prefill_ms REAL,
  generation_ms REAL,
  estimated_cost_usd REAL,
  estimated_energy_wh REAL,
  energy_method TEXT,
  verifier_result TEXT,
  error_reason TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(job_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS attempts_by_job ON job_attempts(job_id, attempt_number);
CREATE TABLE IF NOT EXISTS job_events (
  event_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  attempt_id TEXT REFERENCES job_attempts(attempt_id),
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_job ON job_events(job_id, created_at);
"""


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _migration_2(conn: sqlite3.Connection) -> None:
    """Add request deduplication and retry policy to pre-Phase-2 databases."""
    add_column_if_missing(conn, "jobs", "idempotency_key", "TEXT")
    add_column_if_missing(conn, "jobs", "max_attempts", "INTEGER NOT NULL DEFAULT 3")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_by_idempotency ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL")


MIGRATIONS = (_migration_1, _migration_2)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class JobLedger:
    """SQLite persistence owned by the Mac-local coordinator process."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._conn = connect(self.db_path)
        self.schema_version = apply_migrations(self._conn, MIGRATIONS)
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JobLedger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def create_job(self, task_class: str, request: dict[str, Any], *, privacy: str = "local_only", risk: str = "low", preferred_execution: str | None = None, selected_tier: str | None = None, selection_reason: str | None = None, idempotency_key: str | None = None, max_attempts: int = 3, job_id: str | None = None) -> str:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if idempotency_key:
            existing = self._conn.execute("SELECT job_id FROM jobs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if existing:
                return str(existing[0])
        job_id = job_id or str(uuid.uuid4())
        self._conn.execute("INSERT INTO jobs(job_id, idempotency_key, task_class, request_json, privacy, risk, preferred_execution, selected_tier, selection_reason, max_attempts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (job_id, idempotency_key, task_class, _json(request), privacy, risk, preferred_execution, selected_tier, selection_reason, max_attempts, _now()))
        return job_id

    def start_attempt(self, job_id: str, *, tier: str, provider: str | None = None, model: str | None = None, model_version: str | None = None) -> str:
        row = self._conn.execute("SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM job_attempts WHERE job_id = ?", (job_id,)).fetchone()
        number = int(row[0])
        attempt_id = str(uuid.uuid4())
        self._conn.execute("INSERT INTO job_attempts(attempt_id, job_id, attempt_number, tier, provider, model, model_version, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (attempt_id, job_id, number, tier, provider, model, model_version, _now()))
        self._conn.execute("UPDATE jobs SET status = 'running' WHERE job_id = ?", (job_id,))
        return attempt_id

    def finish_attempt(self, attempt_id: str, *, status: str, verifier_result: str | None = None, error_reason: str | None = None, **metrics: Any) -> None:
        allowed = {"input_tokens", "output_tokens", "wall_ms", "prefill_ms", "generation_ms", "estimated_cost_usd", "estimated_energy_wh", "energy_method"}
        values = {key: metrics[key] for key in metrics if key in allowed}
        values.update(status=status, verifier_result=verifier_result, error_reason=error_reason, finished_at=_now())
        clause = ", ".join(f"{key} = ?" for key in values)
        self._conn.execute(f"UPDATE job_attempts SET {clause} WHERE attempt_id = ?", [*values.values(), attempt_id])

    def finish_job(self, job_id: str, *, disposition: str, status: str = "completed", escalated: bool = False, escalation_reason: str | None = None) -> None:
        self._conn.execute("UPDATE jobs SET status = ?, final_disposition = ?, escalated = ?, escalation_reason = ?, retry_count = COALESCE((SELECT MAX(attempt_number)-1 FROM job_attempts WHERE job_id = ?), 0), finished_at = ? WHERE job_id = ?", (status, disposition, int(escalated), escalation_reason, job_id, _now(), job_id))

    def add_event(self, job_id: str, event_type: str, payload: dict[str, Any], *, attempt_id: str | None = None) -> str:
        event_id = str(uuid.uuid4())
        self._conn.execute("INSERT INTO job_events(event_id, job_id, attempt_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", (event_id, job_id, attempt_id, event_type, _json(payload), _now()))
        return event_id

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def attempts(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute("SELECT * FROM job_attempts WHERE job_id = ? ORDER BY attempt_number", (job_id,)).fetchall()]

    def events(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM job_events WHERE job_id = ? ORDER BY created_at, rowid", (job_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result
