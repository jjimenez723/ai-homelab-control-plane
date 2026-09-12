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

`flock` is held on an open file, not on a path, which is the one way this lock
can be defeated: delete the lock file and the holder is left locking an inode
nobody will ever open again, while the next run creates its own file at the
path and takes a genuinely uncontended lock on it. That is not hypothetical --
the state directory also holds `local-ledger.sqlite`, so anything that clears
it does exactly this.

Two different things follow, and they must not be confused:

* **Prevented.** A run that opens the lock file and has it replaced before it
  manages to lock it is refused, because the file it locked is no longer the
  file at the path. That is a race `run_lock` can and does win.
* **Detected only.** A lock file removed while a run is *already holding* it
  cannot be defended at all -- nothing the holder does can stop the next
  process creating a new file at that path and locking it. The overlap really
  happens; what `run_lock` guarantees is that the run whose lock was pulled out
  from under it raises `RunLockLost` instead of reporting a clean, exclusive
  pass. The alternative designs that would prevent it (an `O_EXCL` lock file
  carrying a pid, plus a liveness check) trade this for stale locks that wedge
  the pipeline after a crash, which this one deliberately cannot do.
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


class RunLockLost(RuntimeError):
    """The lock file stopped being the file this run locked, mid-run.

    Unlike `RunLockBusy` this *is* an error: exclusivity was not held for the
    whole run, so another run may well have been touching the same sources,
    catalog and staging paths at the same time -- and by the time this is
    raised, it already has. This is detection after the fact, not prevention:
    a holder cannot stop anything from unlinking its lock file. What it can do
    is refuse to report the run as a clean, exclusive pass.
    """


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


def _is_file_at(handle: Any, path: Path) -> bool:
    """Whether the open handle and `path` still name the same file.

    (st_dev, st_ino) is the only identity that survives a rename or a delete;
    the path is just a label that can be pointed at a different file while we
    hold the old one open.
    """
    try:
        by_path = os.stat(path)
    except OSError:
        return False
    by_handle = os.fstat(handle.fileno())
    return (by_handle.st_dev, by_handle.st_ino) == (by_path.st_dev, by_path.st_ino)


@contextmanager
def run_lock(paths: RunPaths) -> Iterator[None]:
    """Hold the exclusive run lock, or raise `RunLockBusy` immediately.

    Non-blocking on purpose -- see the module docstring.

    Taking the lock is open-then-lock, and in between the file can be replaced:
    then the lock we win is on a file nothing else will ever open, while the
    run that recreated the path is holding the real one. So the handle and the
    path are compared after locking, and a mismatch is retried once -- a
    replacement is an event, not a steady state -- before refusing.

    The same comparison on the way out cannot prevent anything; it reports.
    A lock file removed while this run holds it leaves the next run free to
    take an uncontended lock on a new file at that path, and `RunLockLost`
    says that happened rather than pretending it did not. The lock file is
    never deleted or recreated here: its identity is what the lock means.
    """
    for _attempt in (1, 2):
        paths.ensure()
        handle = paths.lock_path.open("a+", encoding="utf-8")
        try:
            paths.lock_path.chmod(0o600)
        except OSError:
            pass
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RunLockBusy(
                f"another file-intelligence run is already active (lock: {paths.lock_path})"
            ) from exc
        if _is_file_at(handle, paths.lock_path):
            break
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    else:
        raise RunLockBusy(
            f"the run lock file {paths.lock_path} keeps being replaced; refusing to run unlocked"
        )

    lost = False
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()} {now_iso()}\n")
        handle.flush()
        yield
        # Checked only on the way out of a run that otherwise succeeded: if
        # the file we locked is no longer the file at the path, something
        # removed or replaced it while we were working, and whatever ran next
        # was free to take an uncontended lock on a different file. Too late to
        # stop; not too late to say.
        lost = not _is_file_at(handle, paths.lock_path)
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    if lost:
        raise RunLockLost(
            f"the run lock file {paths.lock_path} was removed or replaced during this run; "
            "another run may have been active at the same time"
        )


def write_json_atomically(path: Path, payload: Any) -> Path:
    """Write `payload` as JSON so no reader can ever see a partial file.

    A temp file made by `mkstemp` in the destination's own directory (same
    filesystem, so the `os.replace` is atomic) and removed again if anything
    fails. This lives here rather than at each call site because these are the
    only two durable human-readable records the system writes -- the run
    status and the per-asset sidecars -- and in both cases the file a plain
    write would truncate is the one that outlives what it describes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def write_status(paths: RunPaths, payload: dict[str, Any]) -> Path:
    """Publish the last-run summary atomically.

    Written via a temp file plus `os.replace` so `organize status` can never
    read a half-written record, even if a run is killed mid-write.
    """
    paths.ensure()
    write_json_atomically(paths.status_path, payload)
    paths.status_path.chmod(0o600)
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
