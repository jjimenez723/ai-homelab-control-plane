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

Both use the same machinery here. Rules for every migration:

* never drop or rewrite a column, only add;
* `CREATE TABLE IF NOT EXISTS` / guarded `ALTER TABLE ... ADD COLUMN`, so
  running an old database forward twice is a no-op;
* an existing row is never modified by a migration.

`user_version` is the applied-version marker. A database created before
migrations existed reports `user_version = 0`; migration 1 is written so that
re-running it over that database changes nothing.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Sequence

Migration = Callable[[sqlite3.Connection], None]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """`ALTER TABLE ADD COLUMN`, skipped when the column is already there.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, and a failed ALTER inside a
    migration would abort the rest of it, so the check has to be explicit.
    """
    if column in _columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def apply_migrations(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> int:
    """Bring `conn` up to `len(migrations)`. Returns the resulting version.

    No step is wrapped in an explicit transaction, on purpose. SQLite commits
    implicitly around `executescript`, so a hand-rolled BEGIN/COMMIT here would
    be a lie about atomicity rather than a guarantee of it. Instead the rule is
    the one stated at the top of this module: **every migration is idempotent
    and additive**, and `user_version` is only advanced once the step has
    returned. A crash mid-migration therefore leaves the version unchanged and
    the next run simply re-applies the whole step over the parts that already
    exist. Nothing is dropped or rewritten, so re-application cannot lose data.
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    target = len(migrations)
    if current > target:
        raise RuntimeError(
            f"database schema version {current} is newer than this code supports ({target}); "
            "upgrade the control plane checkout rather than downgrading the database"
        )
    for index in range(current, target):
        migrations[index](conn)
        # PRAGMA user_version does not accept a bound parameter.
        conn.execute(f"PRAGMA user_version = {index + 1}")
    return target
