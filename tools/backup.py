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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

from tools.ingest.config import BackupConfig, IngestConfig
from tools.ingest.volumes import MountedVolume, VolumeError, VolumeProber, find_volume_by_uuid

REPOSITORY = "rclone:gdrive-backup:AI-Homelab-Backups/T7-restic"
RCLONE_REMOTE = "gdrive-backup:"
KEYCHAIN_SERVICE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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


def resolve_backup_source(config: IngestConfig, prober: VolumeProber) -> BackupSource:
    """Resolve the T7 by UUID, without trusting a Finder volume name."""
    uuid = config.t7.volume_uuid
    if not uuid:
        raise BackupError("no t7.volume_uuid configured; refusing to back up an unspecified source")

    if config.t7.storage_root_override is not None:
        # Retained solely for isolated project tests / development fixtures.
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


def validate_restore_target(target: Path, config: IngestConfig, prober: VolumeProber) -> Path:
    """Reject the source T7 itself (and filesystem root/home) as a restore target."""
    resolved = target.expanduser().resolve(strict=False)
    home = Path.home().resolve()
    if resolved in (Path("/"), home):
        raise BackupError("restore target must be a dedicated directory, not / or the home directory")
    try:
        source = resolve_backup_source(config, prober).root
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
    ) -> None:
        validate_backup_config(config.backup)
        self.config = config
        self.prober = prober
        self.runner = runner
        self.hostname = hostname or platform.node() or "mac"
        self.log_dir = log_dir or (config.repo_root / ".state" / "backup-logs")

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
        env.pop("RESTIC_PASSWORD", None)
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
            raise BackupError("Google Drive remote gdrive-backup is unavailable or unauthorized")

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

    def preflight(self) -> BackupSource:
        source = resolve_backup_source(self.config, self.prober)
        self.ensure_remote()
        self.ensure_keychain()
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
        existing = self._restic("cat", "config")
        if existing.returncode == 0:
            return "Repository already exists; no initialization performed."
        if existing.returncode != 10:
            raise BackupError("unable to determine whether the dedicated restic repository already exists")
        created = self._restic("init")
        if created.returncode != 0:
            raise BackupError("restic repository initialization failed")
        return "Repository initialized."

    def _write_log(self, event: dict[str, object]) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_file = self.log_dir / f"backup-{datetime.now(UTC).strftime('%Y-%m')}.jsonl"
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
        except OSError:
            # Logging is observability only. A log-directory problem must not
            # turn a completed backup into a data-source failure.
            pass

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
        source = self.preflight()
        if extra_tag is not None and not TAG_RE.fullmatch(extra_tag):
            raise BackupError("backup tag must use letters, numbers, dots, underscores, or hyphens")
        tags = ["t7", "angel"] + ([extra_tag] if extra_tag else ["automatic"])
        command = [
            "backup",
            "--json",
            "--host",
            self.hostname,
            "--one-file-system",
        ]
        for tag in tags:
            command.extend(["--tag", tag])
        for pattern in self.config.backup.exclude_patterns:
            command.extend(["--exclude", pattern])
        command.append(str(source.root))
        started = datetime.now(UTC)
        result = self._restic(*command)
        summary = self._summary(result.stdout)
        self._write_log(
            {
                "event": "backup",
                "started_at": started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
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

    def snapshots(self) -> CommandResult:
        self._require_repository_access()
        return self._restic("snapshots")

    def check(self, *, deep: bool = False) -> CommandResult:
        self._require_repository_access()
        return self._restic("check", "--read-data" if deep else "--read-data-subset=5%")

    def restore(self, snapshot: str, target: Path, includes: Sequence[str] = ()) -> CommandResult:
        if not snapshot or snapshot.startswith("-"):
            raise BackupError("a valid restic snapshot ID (or 'latest') is required")
        destination = validate_restore_target(target, self.config, self.prober)
        self._require_repository_access()
        destination.mkdir(parents=True, exist_ok=True)
        command = ["restore", snapshot, "--target", str(destination), "--verify"]
        for include in includes:
            command.extend(["--include", include])
        return self._restic(*command)

    def retention(self, *, apply: bool = False, confirmed_after_restore: bool = False) -> CommandResult:
        self._require_repository_access()
        if apply and not confirmed_after_restore:
            raise BackupError("refusing destructive prune: also pass --confirmed-after-restore")
        retention = self.config.backup.retention
        command = [
            "forget",
            "--keep-daily",
            str(retention.keep_daily),
            "--keep-weekly",
            str(retention.keep_weekly),
            "--keep-monthly",
            str(retention.keep_monthly),
            "--keep-yearly",
            str(retention.keep_yearly),
        ]
        if apply:
            command.append("--prune")
        else:
            command.append("--dry-run")
        return self._restic(*command)
