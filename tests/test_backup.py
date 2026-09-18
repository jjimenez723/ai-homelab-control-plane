"""Unit tests for the T7 angel Google Drive restic command boundary."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
import threading
import time

from tools.backup import COMMAND_PATH, BackupError, BackupService, CommandResult, REPOSITORY, REPOSITORY_ID, resolve_backup_source
from tests.ingest.helpers import T7_TEST_UUID, build_config, fake_prober, make_volume


class RecordingRunner:
    def __init__(self, *, remote_ok: bool = True, restic_code: int = 0, cat_code: int = 0) -> None:
        self.remote_ok = remote_ok
        self.restic_code = restic_code
        self.cat_code = cat_code
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, args, env):  # type: ignore[no-untyped-def]
        args = list(args)
        self.calls.append((args, dict(env)))
        if args[0] == "rclone":
            return CommandResult(0 if self.remote_ok else 1)
        if args[0] == "security":
            return CommandResult(0, "never-log-this-secret\n")
        if args[-2:] == ["cat", "config"]:
            return CommandResult(self.cat_code, f'{{"id":"{REPOSITORY_ID}"}}\n' if self.cat_code == 0 else "")
        if args[0] == "restic":
            return CommandResult(
                self.restic_code,
                '{"message_type":"summary","snapshot_id":"abc123","total_files_processed":7,"total_bytes_processed":12}\n',
            )
        raise AssertionError(args)


class BackupTests(unittest.TestCase):
    def make_service(self, root: Path, runner: RecordingRunner | None = None) -> tuple[BackupService, RecordingRunner]:
        config = build_config(desktop=root / "Desktop", t7_root=root, t7_volume_uuid=T7_TEST_UUID)
        active_runner = runner or RecordingRunner()
        return (
            BackupService(
                config,
                fake_prober([]),
                runner=active_runner,
                hostname="mac-m4pro",
                log_dir=root / "logs",
                state_dir=root / "state",
                lock_path=root / "machine-state" / "restic.lock",
                allow_test_source_override=True,
            ),
            active_runner,
        )

    def test_missing_and_wrong_uuid_refuse_before_external_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(desktop=root / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            runner = RecordingRunner()
            service = BackupService(
                config,
                fake_prober([]),
                runner=runner,
                state_dir=root / "state",
                log_dir=root / "logs",
                lock_path=root / "machine-state" / "restic.lock",
            )
            with self.assertRaises(BackupError):
                service.backup()
            self.assertEqual(runner.calls, [])

            wrong = make_volume("wrong-uuid", root)
            with self.assertRaises(BackupError):
                resolve_backup_source(config, fake_prober([wrong]))

    def test_locked_or_unmounted_volume_refuses_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(desktop=root / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            locked = make_volume(T7_TEST_UUID, root, writable=False)
            with self.assertRaisesRegex(BackupError, "read-only or still locked"):
                resolve_backup_source(config, fake_prober([locked]))

    def test_source_named_angel_with_a_stale_uuid_is_refused(self):
        """The backup source is the UUID, never the label `angel` (D-019)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(desktop=root / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            impostor = make_volume("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", root, label="Angel")
            with self.assertRaisesRegex(BackupError, "is not mounted"):
                resolve_backup_source(config, fake_prober([impostor]))

    def test_backup_constructs_uuid_pinned_command_with_expected_tags_and_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, runner = self.make_service(Path(tmp))
            result = service.backup("pre-organize")
            self.assertEqual(result.returncode, 0)
            command = next(args for args, _ in runner.calls if args[0] == "restic" and args[3] == "backup")
            self.assertEqual(command[:4], ["restic", "-r", REPOSITORY, "backup"])
            self.assertIn("--one-file-system", command)
            self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--tag"], ["t7", "angel", "pre-organize"])
            self.assertIn("90-system/staging/**", command)
            self.assertIn("40-ai/model-cache/**", command)
            self.assertIn("**/*.partial", command)
            self.assertNotIn("automatic", command)

    def test_default_backup_uses_automatic_tag_and_password_stays_out_of_argv_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, runner = self.make_service(root)
            service.backup()
            flattened = " ".join(" ".join(args) for args, _ in runner.calls)
            self.assertNotIn("never-log-this-secret", flattened)
            self.assertTrue(all("RESTIC_PASSWORD" not in env or env.get("RESTIC_PASSWORD") is None for _, env in runner.calls))
            self.assertTrue(all(env["PATH"] == COMMAND_PATH for _, env in runner.calls))
            log_contents = next((root / "logs").glob("*.jsonl")).read_text(encoding="utf-8")
            self.assertIn('"outcome": "success"', log_contents)
            self.assertNotIn("never-log-this-secret", log_contents)

    def test_preflight_reports_the_uuid_remote_keychain_and_source_size_without_restic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.bin").write_bytes(b"12345")
            service, runner = self.make_service(root)
            report = service.preflight_report()
            self.assertEqual(report["source_uuid"], T7_TEST_UUID)
            self.assertEqual(report["source_bytes"], 5)
            self.assertEqual(report["google_drive_remote"], "ok")
            self.assertEqual(report["rclone_config_password_command"], "ok")
            self.assertEqual(report["keychain_password_command"], "ok")
            restic_calls = [args for args, _ in runner.calls if args[0] == "restic"]
            self.assertEqual([args[3:] for args in restic_calls], [["cat", "config"]])

    def test_remote_and_restic_failures_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, _ = self.make_service(root, RecordingRunner(remote_ok=False))
            with self.assertRaisesRegex(BackupError, "Google Drive remote"):
                service.backup()

            service, _ = self.make_service(root, RecordingRunner(restic_code=1))
            self.assertEqual(service.backup().returncode, 1)
            log_contents = next((root / "logs").glob("*.jsonl")).read_text(encoding="utf-8")
            self.assertIn("restic_failure", log_contents)

    def test_keychain_lookup_failure_refuses_backup_without_calling_restic(self):
        class MissingKeychainRunner(RecordingRunner):
            def __call__(self, args, env):  # type: ignore[no-untyped-def]
                if args[0] == "security":
                    self.calls.append((list(args), dict(env)))
                    return CommandResult(44)
                return super().__call__(args, env)

        with tempfile.TemporaryDirectory() as tmp:
            runner = MissingKeychainRunner()
            service, _ = self.make_service(Path(tmp), runner)
            with self.assertRaisesRegex(BackupError, "Keychain item"):
                service.backup()
            self.assertFalse(any(args[0] == "restic" for args, _ in runner.calls))

    def test_rclone_config_keychain_failure_refuses_remote_access(self):
        class MissingRcloneKeychainRunner(RecordingRunner):
            def __call__(self, args, env):  # type: ignore[no-untyped-def]
                if args[0] == "security" and args[args.index("-s") + 1] == "ai-homelab-rclone-config":
                    self.calls.append((list(args), dict(env)))
                    return CommandResult(44)
                return super().__call__(args, env)

        with tempfile.TemporaryDirectory() as tmp:
            runner = MissingRcloneKeychainRunner()
            service, _ = self.make_service(Path(tmp), runner)
            with self.assertRaisesRegex(BackupError, "ai-homelab-rclone-config"):
                service.backup()
            self.assertFalse(any(args[0] == "rclone" for args, _ in runner.calls))

    def test_init_does_not_reinitialize_an_existing_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, runner = self.make_service(Path(tmp), RecordingRunner(cat_code=0))
            self.assertIn("already exists", service.initialize())
            self.assertFalse(any(args[-1] == "init" for args, _ in runner.calls))

    def test_retention_is_dry_run_and_destructive_mode_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, runner = self.make_service(Path(tmp))
            service.retention()
            command = [args for args, _ in runner.calls if args[0] == "restic"][-1]
            self.assertIn("--dry-run", command)
            self.assertNotIn("--prune", command)
            with self.assertRaisesRegex(BackupError, "disabled in config"):
                service.retention(apply=True)
            service.config = replace(  # type: ignore[misc]
                service.config,
                backup=replace(service.config.backup, retention=replace(service.config.backup.retention, enabled=True)),
            )
            with self.assertRaisesRegex(BackupError, "confirmed-after-restore"):
                service.retention(apply=True)
            service.retention(apply=True, confirmed_after_restore=True)
            command = [args for args, _ in runner.calls if args[0] == "restic"][-1]
            self.assertIn("--prune", command)
            self.assertNotIn("--dry-run", command)

    def test_restore_refuses_t7_and_allows_isolated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, runner = self.make_service(root)
            with self.assertRaisesRegex(BackupError, "live T7"):
                service.restore("abc123", root / "recovery")
            isolated = root.parent / f"{root.name}-restore"
            result = service.restore("abc123", isolated, includes=["/some/file"])
            self.assertEqual(result.returncode, 0)
            command = [args for args, _ in runner.calls if args[0] == "restic"][-1]
            self.assertIn("--verify", command)
            self.assertIn("/some/file", command)
            self.assertTrue(isolated.is_dir())

    def test_repository_is_not_configurable_to_an_unrelated_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(desktop=root / "Desktop", t7_root=root, t7_volume_uuid=T7_TEST_UUID)
            config = replace(config, backup=replace(config.backup, repository="local:/tmp/not-allowed"))
            with self.assertRaisesRegex(BackupError, "dedicated Google Drive"):
                BackupService(
                    config,
                    fake_prober([]),
                    runner=RecordingRunner(),
                    state_dir=root / "state",
                    log_dir=root / "logs",
                    lock_path=root / "machine-state" / "restic.lock",
                )

    def test_scheduler_template_has_no_plaintext_credential_fields(self):
        template = Path("launchd/com.ai-homelab.t7-backup.plist.template").read_text(encoding="utf-8").lower()
        for prohibited in ("password", "client_secret", "oauth", "token"):
            self.assertNotIn(prohibited, template)

    def test_exit_code_three_is_a_failure_not_a_successful_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, _ = self.make_service(root, RecordingRunner(restic_code=3))
            self.assertEqual(service.backup().returncode, 3)
            status = service.status()
            self.assertEqual(status["last_outcome"], "failure")
            self.assertEqual(status["restic_exit_code"], 3)
            self.assertNotIn("last_successful_backup_at", status)

    def test_status_records_preflight_failure_and_check_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(desktop=root / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            failed = BackupService(
                config,
                fake_prober([]),
                runner=RecordingRunner(),
                state_dir=root / "state",
                lock_path=root / "machine-state" / "restic.lock",
            )
            with self.assertRaises(BackupError):
                failed.backup()
            self.assertEqual(failed.status()["last_outcome"], "failure")
            self.assertIn("UUID", str(failed.status()["failure_message"]))

            service, _ = self.make_service(root)
            service.check()
            self.assertEqual(service.status()["check_status"], "success")
            self.assertIn("elapsed_seconds", service.status())

    def test_concurrent_job_is_refused_before_a_second_restic_command(self):
        class SlowRunner(RecordingRunner):
            entered = threading.Event()

            def __call__(self, args, env):  # type: ignore[no-untyped-def]
                if args[0] == "restic" and args[3] == "backup":
                    self.entered.set()
                    time.sleep(0.15)
                return super().__call__(args, env)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = SlowRunner()
            first, _ = self.make_service(root, runner)
            second = BackupService(
                first.config,
                fake_prober([]),
                runner=runner,
                state_dir=first.state_dir,
                lock_path=first.lock_path,
                allow_test_source_override=True,
            )
            worker = threading.Thread(target=first.backup)
            worker.start()
            self.assertTrue(runner.entered.wait(1))
            with self.assertRaisesRegex(BackupError, "already running"):
                second.check()
            worker.join(1)

    def test_restore_drill_uses_private_temp_target_and_removes_it(self):
        class DrillRunner(RecordingRunner):
            def __call__(self, args, env):  # type: ignore[no-untyped-def]
                result = super().__call__(args, env)
                if args[0] == "restic" and args[3] == "restore":
                    target = Path(args[args.index("--target") + 1])
                    included = Path(args[args.index("--include") + 1])
                    restored = target / included.relative_to(included.anchor)
                    restored.parent.mkdir(parents=True, exist_ok=True)
                    restored.write_bytes(b"drill-data")
                return result

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "known-marker.txt").write_bytes(b"drill-data")
            service, _ = self.make_service(root, DrillRunner())
            service.config = replace(service.config, backup=replace(service.config.backup, restore_drill_relative_path="known-marker.txt"))  # type: ignore[misc]
            self.assertEqual(service.restore_drill().returncode, 0)
            self.assertEqual(service.status()["restore_drill_status"], "success")
            drill_dir = service.state_dir / "restore-drills"
            self.assertEqual(list(drill_dir.iterdir()), [])

    def test_rendered_launchd_pair_has_explicit_scheduled_commands(self):
        backup = Path("launchd/com.ai-homelab.t7-backup.plist.template").read_text(encoding="utf-8")
        check = Path("launchd/com.ai-homelab.t7-check.plist.template").read_text(encoding="utf-8")
        self.assertIn("backup-t7-scheduled</string><string>backup", backup)
        self.assertIn("backup-t7-scheduled</string><string>check", check)
        self.assertIn("Weekday</key><integer>7", check)


if __name__ == "__main__":
    unittest.main()


class RemoteFailureMessageTests(unittest.TestCase):
    """The V1 acceptance pass hit an expired Google Drive OAuth grant that the
    old one-sentence error made look like a network outage."""

    def test_an_expired_grant_names_the_repair_command(self):
        from tools.backup import remote_failure_message

        stderr = (
            '2026/09/18 12:29:34 CRITICAL: Failed to create file system for "gdrive-backup:": '
            "couldn't fetch token: invalid_grant: maybe token expired? - try refreshing"
        )
        message = remote_failure_message(stderr)
        self.assertIn("rclone config reconnect gdrive-backup:", message)
        self.assertIn("invalid_grant", message)
        self.assertTrue(message.startswith("Google Drive remote gdrive-backup is unavailable or unauthorized"))

    def test_other_failures_carry_the_last_stderr_line_bounded(self):
        from tools.backup import remote_failure_message

        message = remote_failure_message("noise\n" + "x" * 1000)
        self.assertTrue(message.startswith("Google Drive remote gdrive-backup is unavailable or unauthorized: "))
        self.assertLessEqual(len(message), 400)

    def test_no_stderr_keeps_the_plain_sentence(self):
        from tools.backup import remote_failure_message

        self.assertEqual(remote_failure_message(""), "Google Drive remote gdrive-backup is unavailable or unauthorized")
