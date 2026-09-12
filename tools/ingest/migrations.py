"""Additive, idempotent SQLite migrations shared by both catalogs.

There are two databases in this system and they are deliberately different
things:

* The **T7 catalog** (`90-system/catalog.sqlite`) is durable truth about assets
  that have actually been ingested into Angel. It only exists when Angel is
  connected.
* The **local ledger** (`.state/file-intelligence/local-ledger.sqlite`) is
  Mac-side bookkeeping about files that have *not* been ingested: what they are
  called, what we proposed calling them, and what retention triage decided.
  Naming is not retention (D-021), so naming has to keep working with Angel
  unplugged -- which means its ledger cannot live on Angel.

Both use the same machinery here. Default rules for migrations:

* prefer additive changes: `CREATE TABLE IF NOT EXISTS` / guarded
  `ALTER TABLE ... ADD COLUMN`;
* every migration is idempotent;
* existing data must survive unchanged.

Catalog migration 4 is the deliberate exception to "additive only": SQLite
cannot drop a table-level UNIQUE constraint in place, so that migration rebuilds
`assets` inside one explicit transaction and copies every row byte-for-byte.

`user_version` is the applied-version marker. A database created before
migrations existed reports `user_version = 0`; migration 1 is written so that
re-running it over that database changes nothing.
"""

from __future__ import annotations

import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Sequence

Migration = Callable[[sqlite3.Connection], None]

# Every database this system opens is opened by more than one process: the
# hourly agent and whatever the owner runs by hand. These govern what happens
# when they collide.
BUSY_TIMEOUT_MS = 30_000
LOCK_RETRY_ATTEMPTS = 12
LOCK_RETRY_BASE_DELAY = 0.02
LOCK_RETRY_MAX_DELAY = 0.25


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _backoff(attempt: int) -> None:
    """Wait a little longer each time, but not in lockstep with the others.

    Several connections that collided once will collide again if they all wake
    at the same moment, so the delay is jittered.
    """
    delay = min(LOCK_RETRY_BASE_DELAY * (2 ** attempt), LOCK_RETRY_MAX_DELAY)
    time.sleep(random.uniform(delay / 2, delay))


def retry_while_locked(operation: Callable[[], Any], *, attempts: int = LOCK_RETRY_ATTEMPTS) -> Any:
    """Run `operation`, retrying only while SQLite says the database is locked.

    Safe here, and only here, because everything it is used on is idempotent:
    re-running a migration step over the parts that already exist is the rule
    this module is built on. Any other error is raised immediately.
    """
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc) or attempt == attempts - 1:
                raise
            _backoff(attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def enable_wal(conn: sqlite3.Connection) -> str:
    """Put the database into WAL mode, tolerating another opener doing the same.

    `PRAGMA journal_mode` takes a brief exclusive lock and returns SQLITE_BUSY
    *immediately* instead of honouring `busy_timeout` -- measured at 0.0s
    against a 30s timeout -- so two commands opening the same database at the
    same moment left one of them raising `database is locked` out of the
    constructor, before it had done anything at all. `timeout=` cannot fix
    that; retrying can.

    Retrying is enough because the journal mode belongs to the *file*, not to
    the connection: the winner sets it, and by the time a loser retries the
    pragma is usually a no-op that reads back `wal`. And if every attempt
    loses, carrying on is still right -- a connection left in the default
    rollback journal is correct, merely less concurrent, and the database ends
    up in WAL as soon as anyone wins. A command that refuses to start would be
    the worse outcome by far.
    """
    mode = ""
    for attempt in range(LOCK_RETRY_ATTEMPTS):
        try:
            mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
        else:
            if mode == "wal":
                return mode
        _backoff(attempt)
    return mode


def connect(db_path: Path) -> sqlite3.Connection:
    """Open one of this system's databases, configured the same way every time.

    Both the T7 catalog and the local ledger are opened concurrently by
    separate processes, so the settings that decide what happens under
    contention belong in one place rather than in two constructors that drift:

    * autocommit (`isolation_level=None`); the code that needs a transaction
      asks for one explicitly with `BEGIN IMMEDIATE`, and says why;
    * `busy_timeout` stated as a pragma rather than implied by `timeout=`, so
      the number that governs waiting for a lock is readable as one;
    * rows addressable by column name, which every accessor assumes;
    * WAL where it can be had -- see `enable_wal`.
    """
    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    enable_wal(conn)
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """`ALTER TABLE ADD COLUMN`, skipped when the column is already there.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, and a failed ALTER inside a
    migration would abort the rest of it, so the check has to be explicit.

    The check and the ALTER cannot be one transaction (see `apply_migrations`
    for why migrations run without one), so two processes opening the same
    database for the first time after an upgrade can both see the column
    missing and both try to add it. Losing that race is not a failure: the
    column exists, which is the entire promise of this function. Treating it
    as one killed the whole invocation from inside `Catalog.__init__`.
    """
    if column in _columns(conn, table):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def apply_migrations(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> int:
    """Bring `conn` up to `len(migrations)`. Returns the resulting version.

    The framework does not wrap steps in a transaction. Most migrations use
    `executescript`, which commits implicitly, so pretending there is one outer
    transaction would be misleading. A migration that needs atomic replacement
    may open its own explicit transaction as catalog migration 4 does.

    The invariant is idempotence: `user_version` advances only after a step
    returns. A crash leaves the version unchanged and the next opener safely
    re-applies that step.
    """
    current = int(retry_while_locked(lambda: conn.execute("PRAGMA user_version").fetchone()[0]))
    target = len(migrations)
    if current > target:
        raise RuntimeError(
            f"database schema version {current} is newer than this code supports ({target}); "
            "upgrade the control plane checkout rather than downgrading the database"
        )
    for index in range(current, target):
        # The other process opening this database at the same moment is holding
        # the write lock while it applies the very same step. Waiting for it and
        # re-applying is harmless -- see the idempotence rule above -- whereas
        # failing here takes down the whole invocation from inside a constructor.
        retry_while_locked(lambda step=migrations[index]: step(conn))
        # PRAGMA user_version does not accept a bound parameter.
        retry_while_locked(lambda version=index + 1: conn.execute(f"PRAGMA user_version = {version}"))
    return target
