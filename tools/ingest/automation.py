"""Run serialization and the operator-visible run record.

An hourly launchd job plus an owner running `organize` by hand means two
processes can reach the same files at the same time. Both would hash, both
would decide, and both could try to rename the same file -- so runs take an
exclusive lock and a second run exits cleanly rather than queuing behind the
first. Skipping is correct here: the next hourly firing picks the work up, and
a backlog of blocked scans is worse than a missed one.

The lock is `flock` on a file in the checkout's state directory, matching how
`tools/backup.py` serializes restic jobs. It is released by the kernel if the
process dies, so a crash cannot leave the pipeline permanently wedged.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

LOCK_FILENAME = "file-intelligence.lock"
STATUS_FILENAME = "status.json"


class RunLockBusy(RuntimeError):
    """Another file-intelligence run holds the lock. Not an error condition."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class RunPaths:
    state_dir: Path
    lock_path: Path
    status_path: Path
    log_dir: Path

    @classmethod
    def for_state_dir(cls, state_dir: Path) -> "RunPaths":
        return cls(
            state_dir=state_dir,
            lock_path=state_dir / LOCK_FILENAME,
            status_path=state_dir / STATUS_FILENAME,
            log_dir=state_dir / "logs",
        )

    def ensure(self) -> None:
        for directory in (self.state_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except OSError:
                pass


@contextmanager
def run_lock(paths: RunPaths) -> Iterator[None]:
    """Hold the exclusive run lock, or raise `RunLockBusy` immediately.

    Non-blocking on purpose -- see the module docstring.
    """
    paths.ensure()
    with paths.lock_path.open("a+", encoding="utf-8") as handle:
        try:
            paths.lock_path.chmod(0o600)
        except OSError:
            pass
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunLockBusy(
                f"another file-intelligence run is already active (lock: {paths.lock_path})"
            ) from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()} {now_iso()}\n")
            handle.flush()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_status(paths: RunPaths, payload: dict[str, Any]) -> Path:
    """Publish the last-run summary atomically.

    Written via a temp file plus `os.replace` so `organize status` can never
    read a half-written record, even if a run is killed mid-write.
    """
    paths.ensure()
    fd, temporary = tempfile.mkstemp(prefix="status-", suffix=".json", dir=str(paths.state_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, paths.status_path)
        paths.status_path.chmod(0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return paths.status_path


def read_status(paths: RunPaths) -> dict[str, Any] | None:
    try:
        return json.loads(paths.status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lock_is_held(paths: RunPaths) -> bool:
    """Whether some other process currently holds the run lock.

    Used only for reporting. Acquiring and releasing to test is the only way to
    ask this question with `flock`, which is harmless: a reader that wins the
    lock momentarily cannot disturb a run, because a run that wanted it would
    have held it.
    """
    if not paths.lock_path.exists():
        return False
    try:
        with paths.lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False
