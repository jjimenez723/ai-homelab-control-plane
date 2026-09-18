"""Safe, manually-operated restic backups for the Samsung T7 (``angel``).

The module owns construction of every external command.  It deliberately
limits the repository to the one dedicated Google Drive location and never
puts a password in Python arguments, configuration, logs, or Git.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

import fcntl
import hashlib

from tools.ingest.config import BackupConfig, IngestConfig
from tools.ingest.volumes import MountedVolume, VolumeProber, find_volume_by_uuid

REPOSITORY = "rclone:gdrive-backup:AI-Homelab-Backups/T7-restic"
REPOSITORY_ID = "49548ef63ed7b91439a59c67979607ea3f22cc40ade8630e8b97674b4505f2cd"
RCLONE_REMOTE = "gdrive-backup:"
COMMAND_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KEYCHAIN_SERVICE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def remote_failure_message(stderr: str) -> str:
    """One line a person can act on when rclone cannot reach the repository.

    rclone's CRITICAL line says *why* -- an expired OAuth grant, a DNS
    failure, a missing remote -- and hiding it behind one generic sentence
    turned a credential problem into an apparent network problem during the
    V1 acceptance pass. The last non-empty stderr line is appended, bounded;
    an expired/revoked token additionally names the exact interactive
    command that repairs it, which no unattended job may run on its own.
    """
    base = "Google Drive remote gdrive-backup is unavailable or unauthorized"
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    detail = lines[-1][:300] if lines else ""
    if "invalid_grant" in detail or "token expired" in detail.lower():
        return (
            f"{base}: the Google Drive OAuth token is expired or revoked; run "
            f"`rclone config reconnect {RCLONE_REMOTE}` interactively, then retry ({detail})"
        )
    return f"{base}: {detail}" if detail else base


class BackupError(RuntimeError):
    """A backup precondition or external backup operation failed."""


@dataclass(frozen=True)
class BackupSource:
    root: Path
    volume_uuid: str
    label: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str], dict[str, str]], CommandResult]


def subprocess_runner(args: Sequence[str], env: dict[str, str]) -> CommandResult:
    try:
        completed = subprocess.run(args, text=True, capture_output=True, check=False, env=env)
    except FileNotFoundError as exc:
        raise BackupError(f"required command is unavailable: {args[0]}") from exc
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_backup_config(config: BackupConfig) -> None:
    if config.repository != REPOSITORY:
        raise BackupError(
            "backup.repository must be the dedicated Google Drive repository "
            f"{REPOSITORY!r}"
        )
    if not all(
        KEYCHAIN_SERVICE_RE.fullmatch(service)
        for service in (config.keychain_service, config.rclone_keychain_service)
    ):
        raise BackupError("backup Keychain service contains unsupported characters")


def resolve_backup_source(
    config: IngestConfig,
    prober: VolumeProber,
    *,
    allow_storage_root_override: bool = False,
) -> BackupSource:
    """Resolve the T7 by UUID, without trusting a Finder volume name."""
    uuid = config.t7.volume_uuid
    if not uuid:
        raise BackupError("no t7.volume_uuid configured; refusing to back up an unspecified source")

    if config.t7.storage_root_override is not None:
        if not allow_storage_root_override:
            raise BackupError("t7.storage_root_override is test-only and is refused for backups")
        root = config.t7.storage_root_override
        label = "development override"
    else:
        mounted: MountedVolume | None = find_volume_by_uuid(prober(), uuid)
        if mounted is None:
            raise BackupError(f"T7 angel is unavailable: UUID {uuid} is not mounted (locked or disconnected)")
        if not mounted.writable:
            raise BackupError(f"T7 angel is unavailable: UUID {uuid} is read-only or still locked")
        root = mounted.mount_point
        label = mounted.label or "angel"

    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise BackupError(f"T7 angel source is not an accessible mounted directory: {root}") from exc
    home = Path.home().resolve()
    if resolved in (Path("/"), home) or not resolved.is_dir():
        raise BackupError(f"refusing unsafe backup source path: {resolved}")
    return BackupSource(root=resolved, volume_uuid=uuid, label=label)


def validate_restore_target(
    target: Path,
    config: IngestConfig,
    prober: VolumeProber,
    *,
    allow_storage_root_override: bool = False,
) -> Path:
    """Reject the source T7 itself (and filesystem root/home) as a restore target."""
    resolved = target.expanduser().resolve(strict=False)
    home = Path.home().resolve()
    if resolved in (Path("/"), home):
        raise BackupError("restore target must be a dedicated directory, not / or the home directory")
    if _is_relative_to(resolved, Path("/Volumes")):
        raise BackupError("restore target must be local and may not be under /Volumes")
    if target.is_symlink():
        raise BackupError("restore target must not be a symlink")
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise BackupError("restore target must be a nonexistent or empty directory")
    try:
        source = resolve_backup_source(
            config,
            prober,
            allow_storage_root_override=allow_storage_root_override,
        ).root
    except BackupError:
        # A disconnected T7 cannot be overwritten. Restoration to an isolated
        # local directory is still useful in that normal recovery case.
        source = None
    if source is not None and _is_relative_to(resolved, source):
        raise BackupError("refusing to restore onto the live T7 source volume")
    return resolved


class BackupService:
    def __init__(
        self,
        config: IngestConfig,
        prober: VolumeProber,
        *,
        runner: Runner = subprocess_runner,
        hostname: str | None = None,
        log_dir: Path | None = None,
        state_dir: Path | None = None,
        lock_path: Path | None = None,
        allow_test_source_override: bool = False,
    ) -> None:
        validate_backup_config(config.backup)
        self.config = config
        self.prober = prober
        self.runner = runner
        self.hostname = hostname or platform.node() or "mac"
        self.state_dir = state_dir or (config.repo_root / ".state" / "backup")
        self.log_dir = log_dir or (self.state_dir / "logs")
        self.lock_path = lock_path or (
            self.state_dir / "restic.lock"
            if state_dir is not None
            else Path.home()
            / "Library"
            / "Application Support"
            / "ai-homelab-control-plane"
            / "backup"
            / "restic.lock"
        )
        self.allow_test_source_override = allow_test_source_override

    @property
    def status_path(self) -> Path:
        return self.state_dir / "status.json"

    @contextmanager
    def _job_lock(self) -> Iterator[None]:
        """Serialize all restic jobs without relying on restic's own lock alone."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path.parent.chmod(0o700)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            self.lock_path.chmod(0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupError("another T7 restic job is already running; refusing an overlapping run") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_status(self) -> dict[str, object]:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_status(self, changes: dict[str, object]) -> None:
        """Atomically replace small local status state; it never contains secrets."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.state_dir.chmod(0o700)
            status = self._read_status()
            status.update(changes)
            fd, temporary = tempfile.mkstemp(prefix="status-", suffix=".json", dir=self.state_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(status, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                os.replace(temporary, self.status_path)
                self.status_path.chmod(0o600)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError as exc:
            raise BackupError(f"unable to persist backup status: {exc}") from exc

    def status(self) -> dict[str, object]:
        return self._read_status()

    def _record_operation(
        self,
        operation: str,
        callback: Callable[[], CommandResult],
        *,
        backup: bool = False,
        check: bool = False,
        restore_drill: bool = False,
    ) -> CommandResult:
        """Run a job once, recording nonzero restic codes as failures (including 3)."""
        started = datetime.now(timezone.utc)
        monotonic_started = time.monotonic()
        self._write_status(
            {
                "last_attempt_at": started.isoformat(),
                "last_operation": operation,
                "last_outcome": "running",
                "restic_exit_code": None,
                "failure_message": None,
            }
        )
        try:
            with self._job_lock():
                result = callback()
        except BackupError as exc:
            finished = datetime.now(timezone.utc)
            elapsed = round(time.monotonic() - monotonic_started, 3)
            changes: dict[str, object] = {
                "last_operation": operation,
                "last_attempt_at": started.isoformat(),
                "last_finished_at": finished.isoformat(),
                "last_outcome": "failure",
                "restic_exit_code": None,
                "elapsed_seconds": elapsed,
                "failure_message": str(exc),
            }
            if check:
                changes["check_status"] = "failure"
            if restore_drill:
                changes["restore_drill_status"] = "failure"
            self._write_status(changes)
            self._write_log({"event": operation, "started_at": started.isoformat(), "finished_at": finished.isoformat(), "outcome": "failure", "returncode": None, "failure_message": str(exc)})
            raise
        finished = datetime.now(timezone.utc)
        elapsed = round(time.monotonic() - monotonic_started, 3)
        success = result.returncode == 0
        changes = {
            "last_operation": operation,
            "last_attempt_at": started.isoformat(),
            "last_finished_at": finished.isoformat(),
            "last_outcome": "success" if success else "failure",
            "restic_exit_code": result.returncode,
            "elapsed_seconds": elapsed,
            "failure_message": None if success else f"restic exited with code {result.returncode}",
        }
        if backup and success:
            snapshot_id = self._summary(result.stdout).get("snapshot_id")
            changes["last_successful_backup_at"] = finished.isoformat()
            if snapshot_id:
                changes["last_successful_snapshot_id"] = snapshot_id
        if check:
            changes["check_status"] = "success" if success else "failure"
            if success:
                changes["last_successful_check_at"] = finished.isoformat()
        if restore_drill:
            changes["restore_drill_status"] = "success" if success else "failure"
            if success:
                changes["last_successful_restore_drill_at"] = finished.isoformat()
        self._write_status(changes)
        self._write_log(
            {
                "event": operation,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "outcome": "success" if success else "failure",
                "returncode": result.returncode,
                "elapsed_seconds": elapsed,
                "failure_message": changes["failure_message"],
            }
        )
        return result

    @property
    def password_command(self) -> str:
        # Only this command string goes into the environment. The password is
        # emitted directly to restic's stdin by macOS Keychain, never exposed
        # in an argv element or a repository file.
        return f"security find-generic-password -s {self.config.backup.keychain_service} -w"

    @property
    def rclone_password_command(self) -> str:
        return f"security find-generic-password -s {self.config.backup.rclone_keychain_service} -w"

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # launchd does not read interactive shell startup files. Keep command
        # discovery deterministic for both direct and scheduled invocations.
        env["PATH"] = COMMAND_PATH
        for name in (
            "RESTIC_PASSWORD",
            "RESTIC_PASSWORD_FILE",
            "RESTIC_PASSWORD_COMMAND",
            "RCLONE_CONFIG_PASS",
            "RCLONE_PASSWORD_COMMAND",
        ):
            env.pop(name, None)
        env["RESTIC_PASSWORD_COMMAND"] = self.password_command
        # Restic's rclone backend inherits this when it invokes rclone, so
        # direct validation and scheduled backups remain noninteractive.
        env["RCLONE_PASSWORD_COMMAND"] = self.rclone_password_command
        return env

    def _run(self, args: Sequence[str]) -> CommandResult:
        return self.runner(args, self._env())

    def _require(self, executable: str) -> None:
        if self.runner is subprocess_runner and shutil.which(executable) is None:
            raise BackupError(f"required command is unavailable: {executable}")

    def ensure_remote(self) -> None:
        self._require("rclone")
        self.ensure_rclone_keychain()
        result = self._run(["rclone", "lsd", RCLONE_REMOTE])
        if result.returncode != 0:
            raise BackupError(remote_failure_message(result.stderr))

    def ensure_keychain(self) -> None:
        self._require("security")
        result = self._run(
            ["security", "find-generic-password", "-s", self.config.backup.keychain_service, "-w"]
        )
        if result.returncode != 0 or not result.stdout:
            raise BackupError(
                f"macOS Keychain item {self.config.backup.keychain_service!r} is unavailable; no password was printed"
            )

    def ensure_rclone_keychain(self) -> None:
        self._require("security")
        result = self._run(
            ["security", "find-generic-password", "-s", self.config.backup.rclone_keychain_service, "-w"]
        )
        if result.returncode != 0 or not result.stdout:
            raise BackupError(
                "macOS Keychain item "
                f"{self.config.backup.rclone_keychain_service!r} is unavailable; no password was printed"
            )

    def _restic(self, *args: str) -> CommandResult:
        self._require("restic")
        return self._run(["restic", "-r", self.config.backup.repository, *args])

    def _require_repository_access(self) -> None:
        self.ensure_remote()
        self.ensure_keychain()
        self._verify_repository_identity()

    def _verify_repository_identity(self) -> None:
        result = self._restic("cat", "config")
        if result.returncode != 0:
            raise BackupError("unable to read the dedicated restic repository identity")
        try:
            repository_id = json.loads(result.stdout)["id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BackupError("dedicated restic repository returned an invalid identity") from exc
        if repository_id != REPOSITORY_ID:
            raise BackupError("restic repository identity mismatch; refusing the configured remote")

    def preflight(self) -> BackupSource:
        source = resolve_backup_source(
            self.config,
            self.prober,
            allow_storage_root_override=self.allow_test_source_override,
        )
        self._require_repository_access()
        return source

    @staticmethod
    def source_data_size(source: Path) -> int:
        """Read-only estimate of source bytes, without following symlinks."""
        total = 0
        for directory, _, filenames in os.walk(source, followlinks=False):
            for filename in filenames:
                candidate = Path(directory) / filename
                try:
                    total += candidate.stat(follow_symlinks=False).st_size
                except OSError:
                    # A changing/unreadable file makes the eventual restic
                    # result authoritative; it must not make this report lie
                    # or modify the source.
                    continue
        return total

    def preflight_report(self) -> dict[str, object]:
        source = self.preflight()
        return {
            "source_name": source.label,
            "source_path": str(source.root),
            "source_uuid": source.volume_uuid,
            "source_bytes": self.source_data_size(source.root),
            "repository": self.config.backup.repository,
            "exclusions": list(self.config.backup.exclude_patterns),
            "google_drive_remote": "ok",
            "rclone_config_password_command": "ok",
            "keychain_password_command": "ok",
        }

    def initialize(self) -> str:
        self._require_repository_access()
        return "Pinned repository already exists; no initialization performed."

    def _write_log(self, event: dict[str, object]) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.log_dir.chmod(0o700)
            log_file = self.log_dir / f"backup-{datetime.now(timezone.utc).strftime('%Y-%m')}.jsonl"
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
            log_file.chmod(0o600)
        except OSError as exc:
            raise BackupError(f"unable to persist backup log: {exc}") from exc

    @staticmethod
    def _summary(output: str) -> dict[str, int | str]:
        summary: dict[str, int | str] = {}
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("message_type") == "summary":
                for key in ("total_files_processed", "total_bytes_processed", "data_added", "snapshot_id"):
                    if key in event:
                        summary[key] = event[key]
        return summary

    def backup(self, extra_tag: str | None = None) -> CommandResult:
        def run() -> CommandResult:
            source = self.preflight()
            if extra_tag is not None and not TAG_RE.fullmatch(extra_tag):
                raise BackupError("backup tag must use letters, numbers, dots, underscores, or hyphens")
            tags = ["t7", "angel"] + ([extra_tag] if extra_tag else ["automatic"])
            command = ["backup", "--json", "--host", self.hostname, "--one-file-system"]
            for tag in tags:
                command.extend(["--tag", tag])
            for pattern in self.config.backup.exclude_patterns:
                command.extend(["--exclude", pattern])
            command.append(str(source.root))
            result = self._restic(*command)
            summary = self._summary(result.stdout)
            self._write_log(
                {
                    "event": "backup",
                    "outcome": "success" if result.returncode == 0 else "restic_failure",
                    "returncode": result.returncode,
                    "repository": self.config.backup.repository,
                    "source_uuid": source.volume_uuid,
                    "source_label": source.label,
                    "tags": tags,
                    **summary,
                }
            )
            return result

        return self._record_operation("backup", run, backup=True)

    def snapshots(self) -> CommandResult:
        self._require_repository_access()
        return self._restic("snapshots")

    def check(self, *, deep: bool = False) -> CommandResult:
        def run() -> CommandResult:
            self._require_repository_access()
            return self._restic("check", "--read-data" if deep else "--read-data-subset=5%")

        return self._record_operation("check", run, check=True)

    def restore(self, snapshot: str, target: Path, includes: Sequence[str] = ()) -> CommandResult:
        def run() -> CommandResult:
            if not snapshot or snapshot.startswith("-"):
                raise BackupError("a valid restic snapshot ID (or 'latest') is required")
            destination = validate_restore_target(
                target,
                self.config,
                self.prober,
                allow_storage_root_override=self.allow_test_source_override,
            )
            self._require_repository_access()
            destination.mkdir(parents=True, exist_ok=True)
            command = ["restore", snapshot, "--target", str(destination), "--verify"]
            for include in includes:
                command.extend(["--include", include])
            return self._restic(*command)

        return self._record_operation("restore", run)

    def retention(self, *, apply: bool = False, confirmed_after_restore: bool = False) -> CommandResult:
        def run() -> CommandResult:
            self._require_repository_access()
            if apply and not self.config.backup.retention.enabled:
                raise BackupError("retention is disabled in config; refusing destructive forget/prune")
            if apply and not confirmed_after_restore:
                raise BackupError("refusing destructive prune: also pass --confirmed-after-restore")
            retention = self.config.backup.retention
            command = [
                "forget", "--keep-daily", str(retention.keep_daily), "--keep-weekly", str(retention.keep_weekly),
                "--keep-monthly", str(retention.keep_monthly), "--keep-yearly", str(retention.keep_yearly),
            ]
            command.append("--prune" if apply else "--dry-run")
            return self._restic(*command)

        return self._record_operation("retention", run)

    @staticmethod
    def _hash_tree(path: Path) -> dict[str, str]:
        if path.is_file():
            paths = [path]
            base = path.parent
        elif path.is_dir():
            paths = [candidate for candidate in path.rglob("*") if candidate.is_file()]
            base = path
        else:
            raise BackupError(f"restore drill source disappeared or is not a file/directory: {path}")
        hashes: dict[str, str] = {}
        for candidate in paths:
            digest = hashlib.sha256()
            try:
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise BackupError(f"unable to hash restore drill source: {candidate}") from exc
            hashes[str(candidate.relative_to(base))] = digest.hexdigest()
        return hashes

    def restore_drill(self, snapshot: str = "latest") -> CommandResult:
        """Restore a configured small path to a disposable local directory and compare SHA-256s."""
        def run() -> CommandResult:
            relative = self.config.backup.restore_drill_relative_path
            if not relative:
                raise BackupError("backup.restore_drill_relative_path is not configured; refusing an unspecified drill")
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise BackupError("backup.restore_drill_relative_path must be a safe path relative to the T7 root")
            if relative_path == Path("."):
                raise BackupError("restore drill may not target the entire T7 volume")
            source_root = resolve_backup_source(
                self.config,
                self.prober,
                allow_storage_root_override=self.allow_test_source_override,
            ).root
            try:
                source = (source_root / relative_path).resolve(strict=True)
            except OSError as exc:
                raise BackupError("restore drill source is unavailable") from exc
            if not _is_relative_to(source, source_root):
                raise BackupError("backup.restore_drill_relative_path escapes the T7 root")
            source_hashes = self._hash_tree(source)
            if not source_hashes:
                raise BackupError("restore drill source has no files to verify")
            self._require_repository_access()
            drills_dir = self.state_dir / "restore-drills"
            drills_dir.mkdir(parents=True, exist_ok=True)
            target = Path(tempfile.mkdtemp(prefix="drill-", dir=drills_dir))
            include = str(source)
            try:
                result = self._restic("restore", snapshot, "--target", str(target), "--verify", "--include", include)
                if result.returncode != 0:
                    return result
                restored = target / source.relative_to(source.anchor)
                if self._hash_tree(restored) != source_hashes:
                    raise BackupError("restore drill hash verification failed")
                return result
            finally:
                # target is created directly beneath our private state path.
                try:
                    shutil.rmtree(target)
                except OSError as exc:
                    raise BackupError("restore drill cleanup failed") from exc

        return self._record_operation("restore_drill", run, restore_drill=True)
