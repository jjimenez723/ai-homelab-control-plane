"""Focused regression tests for backup recovery and reconstruction safety."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from tests.ingest.helpers import T7_TEST_UUID, build_config, fake_prober
from tools.backup import BackupError, BackupService, CommandResult, REPOSITORY_ID
from tools.ingest.config import BackupRetentionConfig


class RecoveryRunner:
    def __init__(self, *, restored_content: bytes = b"expected") -> None:
        self.calls: list[list[str]] = []
        self.restored_content = restored_content

    def __call__(self, args, env):  # type: ignore[no-untyped-def]
        command = list(args)
        self.calls.append(command)
        if command[0] == "rclone":
            return CommandResult(0)
        if command[0] == "security":
            return CommandResult(0, "test-only-secret\n")
        if command[-2:] == ["cat", "config"]:
            return CommandResult(0, f'{{"id":"{REPOSITORY_ID}"}}\n')
        if command[0] == "restic" and command[3] == "restore":
            if "--include" not in command:
                return CommandResult(0)
            target = Path(command[command.index("--target") + 1])
            included = Path(command[command.index("--include") + 1])
            restored = target / included.relative_to(included.anchor)
            restored.parent.mkdir(parents=True, exist_ok=True)
            restored.write_bytes(self.restored_content)
            return CommandResult(0)
        if command[0] == "restic":
            return CommandResult(0)
        raise AssertionError(command)


class BackupRecoverySafetyTests(unittest.TestCase):
    def make_service(self, root: Path, runner: RecoveryRunner, drill_path: str | None) -> BackupService:
        config = build_config(desktop=root / "Desktop", t7_root=root, t7_volume_uuid=T7_TEST_UUID)
        config = replace(
            config,
            backup=replace(config.backup, restore_drill_relative_path=drill_path),
        )
        return BackupService(
            config,
            fake_prober([]),
            runner=runner,
            state_dir=root.parent / f"{root.name}-state",
            lock_path=root.parent / f"{root.name}-machine-state" / "restic.lock",
            allow_test_source_override=True,
        )

    def test_retention_is_disabled_in_code_and_tracked_example(self):
        self.assertFalse(BackupRetentionConfig().enabled)
        example = yaml.safe_load(Path("config/ingest.example.yaml").read_text(encoding="utf-8"))
        self.assertIs(example["backup"]["retention"]["enabled"], False)
        self.assertEqual(example["t7"]["volume_uuid"], "REPLACE-WITH-T7-VOLUME-UUID")

    def test_restore_drill_rejects_absolute_and_parent_paths_before_external_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            for index, unsafe in enumerate(("../outside", "nested/../../outside", "/tmp/outside")):
                root = parent / f"source-{index}"
                root.mkdir()
                runner = RecoveryRunner()
                service = self.make_service(root, runner, unsafe)
                with self.assertRaisesRegex(BackupError, "safe path relative"):
                    service.restore_drill()
                self.assertEqual(runner.calls, [])
                self.assertEqual(service.status()["restore_drill_status"], "failure")

    def test_restore_drill_hash_mismatch_fails_and_removes_private_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "marker.txt").write_bytes(b"expected")
            service = self.make_service(root, RecoveryRunner(restored_content=b"wrong"), "marker.txt")
            with self.assertRaisesRegex(BackupError, "hash verification failed"):
                service.restore_drill()
            self.assertEqual(service.status()["restore_drill_status"], "failure")
            self.assertEqual(list((service.state_dir / "restore-drills").iterdir()), [])

    def test_restore_drill_rejects_symlink_escaping_verified_source_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "source"
            root.mkdir()
            outside = parent / "outside.txt"
            outside.write_bytes(b"must-not-be-used")
            (root / "marker.txt").symlink_to(outside)
            runner = RecoveryRunner()
            service = self.make_service(root, runner, "marker.txt")
            with self.assertRaisesRegex(BackupError, "escapes|outside the T7 root"):
                service.restore_drill()
            self.assertEqual(runner.calls, [])
            self.assertEqual(service.status()["restore_drill_status"], "failure")

    def test_production_runner_refuses_storage_root_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(desktop=root / "Desktop", t7_root=root, t7_volume_uuid=T7_TEST_UUID)
            service = BackupService(
                config,
                fake_prober([]),
                state_dir=root / "state",
                lock_path=root / "machine-state" / "restic.lock",
            )
            with patch("tools.backup.shutil.which", return_value=None):
                with self.assertRaisesRegex(BackupError, "storage_root_override"):
                    service.preflight()

    def test_general_restore_rejects_volumes_and_existing_nonempty_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            runner = RecoveryRunner()
            service = self.make_service(root, runner, None)
            with self.assertRaisesRegex(BackupError, "/Volumes"):
                service.restore("latest", Path("/Volumes/unsafe-restore"))

            target = Path(tmp) / "existing-target"
            target.mkdir()
            (target / "keep.txt").write_text("do not overwrite", encoding="utf-8")
            with self.assertRaisesRegex(BackupError, "empty|nonexistent"):
                service.restore("latest", target)
            self.assertEqual(runner.calls, [])

    def test_restore_drill_rejects_whole_volume_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            runner = RecoveryRunner()
            service = self.make_service(root, runner, ".")
            with self.assertRaisesRegex(BackupError, "whole|entire T7"):
                service.restore_drill()
            self.assertEqual(runner.calls, [])
            self.assertEqual(service.status()["restore_drill_status"], "failure")

    def test_restore_drill_cleanup_failure_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "marker.txt").write_bytes(b"expected")
            service = self.make_service(root, RecoveryRunner(), "marker.txt")
            with patch("tools.backup.shutil.rmtree", side_effect=OSError("cleanup denied")):
                with self.assertRaisesRegex(BackupError, "cleanup"):
                    service.restore_drill()
            self.assertEqual(service.status()["restore_drill_status"], "failure")

    def test_credential_environment_discards_alternate_secret_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.make_service(root, RecoveryRunner(), None)
            hostile = {
                "RESTIC_PASSWORD": "wrong",
                "RESTIC_PASSWORD_FILE": "/tmp/wrong",
                "RESTIC_PASSWORD_COMMAND": "wrong command",
                "RCLONE_CONFIG_PASS": "wrong",
                "RCLONE_PASSWORD_COMMAND": "wrong command",
            }
            with patch.dict(os.environ, hostile, clear=False):
                env = service._env()
            for name in ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "RCLONE_CONFIG_PASS"):
                self.assertNotIn(name, env)
            self.assertEqual(env["RESTIC_PASSWORD_COMMAND"], service.password_command)
            self.assertEqual(env["RCLONE_PASSWORD_COMMAND"], service.rclone_password_command)

    def test_repository_identity_mismatch_refuses_operation(self):
        class WrongRepositoryRunner(RecoveryRunner):
            def __call__(self, args, env):  # type: ignore[no-untyped-def]
                command = list(args)
                if command[-2:] == ["cat", "config"]:
                    self.calls.append(command)
                    return CommandResult(0, '{"id":"not-the-approved-repository"}\n')
                return super().__call__(command, env)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = WrongRepositoryRunner()
            service = self.make_service(root, runner, None)
            with self.assertRaisesRegex(BackupError, "identity mismatch"):
                service.check()
            self.assertFalse(any(command[-1] == "check" for command in runner.calls))
            self.assertEqual(service.status()["last_outcome"], "failure")

    def test_runtime_state_lock_and_logs_are_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.make_service(root, RecoveryRunner(), None)
            previous_umask = os.umask(0o022)
            try:
                self.assertEqual(service.backup().returncode, 0)
            finally:
                os.umask(previous_umask)
            self.assertEqual(service.state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(service.status_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(service.lock_path.stat().st_mode & 0o777, 0o600)
            for log in service.log_dir.iterdir():
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_injected_lock_path_serializes_different_checkout_state_dirs(self):
        import threading
        import time

        class SlowRunner(RecoveryRunner):
            entered = threading.Event()

            def __call__(self, args, env):  # type: ignore[no-untyped-def]
                if args[0] == "restic" and args[3] == "backup":
                    self.entered.set()
                    time.sleep(0.15)
                return super().__call__(args, env)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            config = build_config(desktop=root / "Desktop", t7_root=source, t7_volume_uuid=T7_TEST_UUID)
            runner = SlowRunner()
            shared_lock = root / "machine-state" / "restic.lock"
            first = BackupService(
                config,
                fake_prober([]),
                runner=runner,
                state_dir=root / "checkout-a-state",
                lock_path=shared_lock,
                allow_test_source_override=True,
            )
            second = BackupService(
                config,
                fake_prober([]),
                runner=runner,
                state_dir=root / "checkout-b-state",
                lock_path=shared_lock,
                allow_test_source_override=True,
            )
            worker = threading.Thread(target=first.backup)
            worker.start()
            self.assertTrue(runner.entered.wait(1))
            with self.assertRaisesRegex(BackupError, "already running"):
                second.check()
            worker.join(1)

    def test_launchd_templates_set_owner_only_umask(self):
        for name in ("backup", "check"):
            payload = plistlib.loads(
                Path(f"launchd/com.ai-homelab.t7-{name}.plist.template")
                .read_bytes()
                .replace(b"__REPOSITORY_ROOT__", b"/tmp/repository")
            )
            self.assertEqual(payload["Umask"], 0o077)

    def test_launchd_renderer_uses_current_checkout_and_preserves_schedules(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp)
            source_repository = Path.cwd().resolve()
            repository = fixture_root / "fresh checkout"
            (repository / "scripts").mkdir(parents=True)
            (repository / "launchd").mkdir()
            shutil.copy2(source_repository / "scripts/render-backup-t7-launchd", repository / "scripts")
            for name in ("backup", "check"):
                shutil.copy2(
                    source_repository / f"launchd/com.ai-homelab.t7-{name}.plist.template",
                    repository / "launchd",
                )
            target = fixture_root / "agents"
            subprocess.run(
                [str(repository / "scripts/render-backup-t7-launchd"), str(target)],
                cwd="/",
                check=True,
                capture_output=True,
                text=True,
            )
            backup = plistlib.loads((target / "com.ai-homelab.t7-backup.plist").read_bytes())
            check = plistlib.loads((target / "com.ai-homelab.t7-check.plist").read_bytes())

        self.assertEqual(backup["ProgramArguments"], [str(repository / "scripts/backup-t7-scheduled"), "backup"])
        self.assertEqual(backup["WorkingDirectory"], str(repository))
        self.assertEqual(backup["StartCalendarInterval"], {"Hour": 2, "Minute": 15})
        self.assertEqual(check["ProgramArguments"], [str(repository / "scripts/backup-t7-scheduled"), "check"])
        self.assertEqual(check["WorkingDirectory"], str(repository))
        self.assertEqual(check["StartCalendarInterval"], {"Weekday": 7, "Hour": 3, "Minute": 15})


if __name__ == "__main__":
    unittest.main()
