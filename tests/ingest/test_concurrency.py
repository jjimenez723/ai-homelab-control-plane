"""Two runs at once: the hourly agent and the owner, on the same files.

An hourly LaunchAgent plus a hand-run `organize` is the normal state of this
system, not an edge case, so every one of these started as a reproduction
against real code rather than a hypothetical:

* two writers appending to the append-only audit table destroyed each other's
  rows, because both derived a row id from the same `SELECT COUNT(*)`;
* `organize ... --apply` -- the destructive command -- took no run lock at all;
* the run lock was defeated by removing the file it is held on.

Everything here runs against temp directories only.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tools.ingest.automation import RunLockBusy, RunLockLost, RunPaths, run_lock
from tools.ingest.cli import cmd_organize
from tools.ingest.ledger import LocalLedger

from tests.ingest.fi_helpers import fi_config, make_blob

LOCAL_FILE_ID = "local-file-under-test"


class _OrganizeArgs:
    """The argparse namespace `cmd_organize` consumes."""

    def __init__(self, target: str, *, apply: bool):
        self.target = target
        self.apply = apply
        self.dry_run = not apply
        self.config = None
        self.allow_dev_storage_root = True


class AppendOnlyUnderConcurrencyTests(unittest.TestCase):
    """The lineage tables promise that nothing rewrites a prior row.

    Both append paths derive the row id from a count of the rows already there.
    Two writers that each counted first derive the same id, and `INSERT OR
    REPLACE` then quietly destroys one of the two decisions -- in the table
    whose only purpose is to still be true a year from now.
    """

    WRITERS = 6
    ROUNDS = 4

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "local-ledger.sqlite"
        with LocalLedger(self.db_path):
            pass  # create the schema before the writers race for it

    def _in_parallel(self, work) -> list[BaseException]:
        """Run `work(index)` in `WRITERS` threads that start together.

        Each thread opens its own `LocalLedger`, because the thing under test
        is two *connections* writing, which is what two processes are.
        """
        ready = threading.Barrier(self.WRITERS, timeout=10)
        errors: list[BaseException] = []

        def run(index: int) -> None:
            try:
                with LocalLedger(self.db_path) as ledger:
                    ready.wait()
                    work(ledger, index)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(self.WRITERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        return errors

    def test_concurrent_naming_appends_all_survive(self):
        def append(ledger: LocalLedger, index: int) -> None:
            ledger.append_naming(
                {
                    "local_file_id": LOCAL_FILE_ID,
                    "source_volume_id": "mac-local",
                    "source_class": "downloads",
                    "directory": "/tmp/downloads",
                    "original_filename": f"IMG_{index:04d}.JPG",
                    "name_quality": "poor",
                    "decision": "proposed",
                    "proposed_filename": f"2026-09-12_thing-{index}.jpg",
                    "approved_by": "automatic",
                    "human_corrected": 0,
                    "created_at": "2026-09-12T00:00:00+00:00",
                }
            )

        for round_number in range(1, self.ROUNDS + 1):
            errors = self._in_parallel(append)
            self.assertEqual(errors, [], f"a writer failed: {errors}")
            with LocalLedger(self.db_path) as ledger:
                rows = ledger.naming_history(LOCAL_FILE_ID)
            self.assertEqual(
                len(rows), round_number * self.WRITERS,
                "a concurrent append overwrote an earlier row of the append-only table",
            )
            self.assertEqual(
                len({row["naming_id"] for row in rows}), len(rows),
                "two rows were given the same derived id",
            )

    def test_concurrent_classification_appends_all_survive(self):
        def append(ledger: LocalLedger, index: int) -> None:
            ledger.append_classification(
                {
                    "local_file_id": LOCAL_FILE_ID,
                    "source_volume_id": "mac-local",
                    "source_class": "downloads",
                    "recommendation": "review",
                    # A distinct reason per writer: an unchanged verdict is
                    # deliberately *not* appended, and that behaviour is
                    # covered by LedgerGrowthTests.
                    "reason": f"writer {index}",
                    "policy_outcome": "review",
                    "human_corrected": 0,
                    "created_at": "2026-09-12T00:00:00+00:00",
                }
            )

        errors = self._in_parallel(append)
        self.assertEqual(errors, [], f"a writer failed: {errors}")
        with LocalLedger(self.db_path) as ledger:
            rows = ledger.classification_runs(LOCAL_FILE_ID)
        self.assertEqual(len(rows), self.WRITERS, "a concurrent append overwrote an earlier run")
        self.assertEqual(len({row["run_id"] for row in rows}), len(rows))
        self.assertEqual({row["reason"] for row in rows}, {f"writer {index}" for index in range(self.WRITERS)})


class OrganizeRunLockTests(unittest.TestCase):
    """`organize --apply` is the destructive command; it was the unlocked one.

    `triage --apply` now transfers files too, so an hourly run and a hand-run
    ingest would otherwise share the sources, the catalog, and the same
    `staging/{asset_id}.partial` path.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.downloads = self.root / "Downloads"
        self.t7 = self.root / "T7"
        for directory in (self.downloads, self.t7):
            directory.mkdir()
        self.config = fi_config(self.root, downloads=self.downloads, t7_root=self.t7)
        self.paths = RunPaths.for_state_dir(self.config.state_dir)

    def _organize(self, *, apply: bool) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_organize(_OrganizeArgs("downloads", apply=apply), prober=lambda: [], config=self.config)
        return code, out.getvalue()

    def test_apply_refuses_while_another_run_holds_the_lock(self):
        source = make_blob(self.downloads / "quarterly-report.txt")
        with run_lock(self.paths):
            code, output = self._organize(apply=True)

        self.assertEqual(code, 0, "a skipped run is not a failure")
        self.assertIn("Skipped", output)
        self.assertTrue(source.exists(), "the refused run must not touch a source file")
        self.assertFalse(
            (self.t7 / "90-system" / "catalog.sqlite").exists(),
            "the refused run must not even open the catalog the other run is writing",
        )

    def test_a_dry_run_is_not_blocked_by_a_running_agent(self):
        make_blob(self.downloads / "quarterly-report.txt")
        with run_lock(self.paths):
            code, output = self._organize(apply=False)

        self.assertEqual(code, 0)
        self.assertIn("Dry run", output, "a read-only report must still answer while a run is active")

    def test_apply_runs_normally_when_nothing_holds_the_lock(self):
        source = make_blob(self.downloads / "quarterly-report.txt")
        code, output = self._organize(apply=True)
        self.assertEqual(code, 0)
        self.assertNotIn("Skipped", output)
        self.assertFalse(source.exists(), "the verified transfer removes the source")


class RunLockIdentityTests(unittest.TestCase):
    """`flock` is held on an open file, not on a path.

    Replace the file at the path and the holder is left locking something
    nobody will open again, while the next run takes an uncontended lock on the
    replacement. The state directory holds `local-ledger.sqlite`, so anything
    that clears it does exactly this.

    Two of these assert a race that is *prevented*; two assert one that is only
    *detected*, and they are deliberately named for which. A run that has its
    lock file pulled out from under it cannot stop the next run from starting,
    so the guarantee there is that it refuses to report a clean pass -- not
    that the overlap did not happen.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = RunPaths.for_state_dir(Path(self._tmp.name) / "state")

    def _acquire_in_another_thread(self) -> str:
        """Try to take the run lock the way a second process would."""
        outcome: list[str] = []

        def attempt() -> None:
            try:
                with run_lock(self.paths):
                    outcome.append("acquired")
            except RunLockBusy:
                outcome.append("refused")

        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(10)
        return outcome[0]

    def test_a_lock_file_replaced_before_we_lock_it_is_not_a_lock(self):
        """The window between opening the lock file and locking it.

        A rival run that deletes the path and creates its own file in that
        window leaves us holding an exclusive lock on an unreachable inode --
        which is not exclusion, it is the illusion of it.
        """
        self.paths.ensure()
        self.paths.lock_path.touch()
        real_flock = fcntl.flock
        rival: list[object] = []

        def racing_flock(fd, operation):
            if operation & fcntl.LOCK_EX and not rival:
                self.paths.lock_path.unlink()
                handle = self.paths.lock_path.open("a+", encoding="utf-8")
                real_flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                rival.append(handle)
            return real_flock(fd, operation)

        with mock.patch("tools.ingest.automation.fcntl.flock", racing_flock):
            with self.assertRaises(RunLockBusy):
                with run_lock(self.paths):
                    self.fail("acquired a lock on a file the path no longer names")

        real_flock(rival[0].fileno(), fcntl.LOCK_UN)
        rival[0].close()

    def test_unlinking_the_lock_file_mid_run_is_detected_not_prevented(self):
        """The second run really does start. That is the part to be honest about.

        Nothing can stop a process unlinking the file this run holds, and the
        next run's own lock on the file it creates at that path is genuinely
        uncontended -- so it acquires. Both halves are asserted, because a test
        that only checked the raise would let a future change quietly stop
        detecting the overlap while still "passing". If this is ever made
        preventable, the expectation below should change to "refused" rather
        than keep passing on the weaker behaviour.
        """
        second: list[str] = []
        with self.assertRaises(RunLockLost):
            with run_lock(self.paths):
                self.paths.lock_path.unlink()
                second.append(self._acquire_in_another_thread())
        self.assertEqual(second, ["acquired"], "the overlap is real; only the report is guaranteed")

    def test_deleting_the_state_dir_mid_run_is_detected_not_prevented(self):
        second: list[str] = []
        with self.assertRaises(RunLockLost):
            with run_lock(self.paths):
                shutil.rmtree(self.paths.state_dir)
                second.append(self._acquire_in_another_thread())
        self.assertEqual(second, ["acquired"], "a wiped state directory leaves nothing to contend on")

    def test_an_undisturbed_run_reports_nothing(self):
        """The check must not cry wolf on the ordinary path."""
        with run_lock(self.paths):
            pass
        with run_lock(self.paths):
            pass

    def test_a_second_run_is_still_refused(self):
        """The ordinary case, which *is* prevention: nobody touched the file."""
        started = threading.Event()
        release = threading.Event()
        outcome: list[object] = []

        def hold():
            with run_lock(self.paths):
                started.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join, 5)
        try:
            self.assertTrue(started.wait(5))
            try:
                with run_lock(self.paths):
                    outcome.append("acquired")
            except RunLockBusy as exc:
                outcome.append(exc)
        finally:
            release.set()

        self.assertIsInstance(outcome[0], RunLockBusy)


if __name__ == "__main__":
    unittest.main()
