"""Load and merge the human-editable `config/ingest.yaml`.

Defaults live in code so the tool works with no config file at all (useful for
tests and for `--config` pointed at a minimal fixture); `config/ingest.yaml`
only needs to override what differs on a given machine.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by the CLI environment
    raise SystemExit("PyYAML is required; run `uv pip install -r requirements-social-video.txt`") from exc


DEFAULT_CONFIG_PATH = Path("config/ingest.yaml")

DEFAULTS: dict[str, Any] = {
    "t7": {
        "volume_uuid": None,
        "storage_root_override": None,
        "inbox_relative_path": "00-inbox/organized",
        "system_relative_path": "90-system",
    },
    "sources": {
        "desktop": "~/Desktop",
        "documents": "~/Documents",
        "downloads": "~/Downloads",
        "sd_volume_uuids": [],
    },
    "settle_seconds": 30,
    "downloads_watcher_min_settle_seconds": 1200,
    "exclusions": {
        "dir_names": [
            "node_modules",
            ".venv",
            "venv",
            "env",
            ".tox",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".git",
        ],
        "bundle_extensions": [
            ".app",
            ".bundle",
            ".framework",
            ".plugin",
            ".kext",
            ".photoslibrary",
            ".xcodeproj",
            ".xcworkspace",
        ],
        "incomplete_download_suffixes": [".crdownload", ".download", ".part"],
        "keep_local_marker": ".keep-local",
        "extra_paths": [],
    },
    "media": {
        "heic_to_jpeg": {
            "enabled": True,
            "preserve_original": True,
            "jpeg_quality": 100,
            "preserve_metadata": True,
            "preserve_icc_profile": True,
            "normalize_orientation": True,
        }
    },
    "backup": {
        # This is intentionally constrained in code to the one dedicated
        # Google Drive repository for this machine. It is not a general
        # arbitrary-restic-repository setting.
        "repository": "rclone:gdrive-backup:AI-Homelab-Backups/T7-restic",
        "keychain_service": "ai-homelab-t7-restic",
        "retention": {
            "keep_daily": 30,
            "keep_weekly": 12,
            "keep_monthly": 24,
            "keep_yearly": 5,
        },
        # The only default exclusions are incomplete transfer artifacts. Do
        # not add valuable data classes here without an explicit decision.
        "exclude_patterns": ["90-system/staging/**", "**/*.partial"],
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class HeicToJpegConfig:
    enabled: bool = True
    preserve_original: bool = True
    jpeg_quality: int = 100
    preserve_metadata: bool = True
    preserve_icc_profile: bool = True
    normalize_orientation: bool = True


@dataclass(frozen=True)
class MediaConfig:
    heic_to_jpeg: HeicToJpegConfig = field(default_factory=HeicToJpegConfig)


@dataclass(frozen=True)
class ExclusionsConfig:
    dir_names: frozenset[str]
    bundle_extensions: frozenset[str]
    incomplete_download_suffixes: frozenset[str]
    keep_local_marker: str
    extra_paths: tuple[Path, ...]


@dataclass(frozen=True)
class T7Config:
    volume_uuid: str | None
    storage_root_override: Path | None
    inbox_relative_path: str
    system_relative_path: str


@dataclass(frozen=True)
class SourcesConfig:
    desktop: Path
    documents: Path
    downloads: Path
    sd_volume_uuids: frozenset[str]


@dataclass(frozen=True)
class BackupRetentionConfig:
    keep_daily: int = 30
    keep_weekly: int = 12
    keep_monthly: int = 24
    keep_yearly: int = 5


@dataclass(frozen=True)
class BackupConfig:
    repository: str = "rclone:gdrive-backup:AI-Homelab-Backups/T7-restic"
    keychain_service: str = "ai-homelab-t7-restic"
    retention: BackupRetentionConfig = field(default_factory=BackupRetentionConfig)
    exclude_patterns: tuple[str, ...] = ("90-system/staging/**", "**/*.partial")


@dataclass(frozen=True)
class IngestConfig:
    t7: T7Config
    sources: SourcesConfig
    settle_seconds: int
    downloads_watcher_min_settle_seconds: int
    exclusions: ExclusionsConfig
    media: MediaConfig
    backup: BackupConfig = field(default_factory=BackupConfig)
    config_path: Path | None = None

    @property
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parents[2]


def _expand(raw: str) -> Path:
    return Path(raw).expanduser()


def load_config(path: Path | None = None) -> IngestConfig:
    """Load config, merging over built-in defaults. Missing file -> pure defaults."""
    raw: dict[str, Any] = {}
    resolved_path: Path | None = None
    candidate = path if path is not None else DEFAULT_CONFIG_PATH
    if candidate.exists():
        resolved_path = candidate
        loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{candidate}: expected a YAML mapping at the top level")
        raw = loaded
    elif path is not None:
        raise FileNotFoundError(f"config file not found: {path}")

    merged = _deep_merge(DEFAULTS, raw)

    t7_raw = merged["t7"]
    t7 = T7Config(
        volume_uuid=t7_raw.get("volume_uuid"),
        storage_root_override=(
            _expand(t7_raw["storage_root_override"]) if t7_raw.get("storage_root_override") else None
        ),
        inbox_relative_path=t7_raw["inbox_relative_path"],
        system_relative_path=t7_raw["system_relative_path"],
    )

    sources_raw = merged["sources"]
    sources = SourcesConfig(
        desktop=_expand(sources_raw["desktop"]),
        documents=_expand(sources_raw["documents"]),
        downloads=_expand(sources_raw["downloads"]),
        sd_volume_uuids=frozenset(sources_raw.get("sd_volume_uuids") or []),
    )

    excl_raw = merged["exclusions"]
    exclusions = ExclusionsConfig(
        dir_names=frozenset(excl_raw["dir_names"]),
        bundle_extensions=frozenset(ext.lower() for ext in excl_raw["bundle_extensions"]),
        incomplete_download_suffixes=frozenset(s.lower() for s in excl_raw["incomplete_download_suffixes"]),
        keep_local_marker=excl_raw["keep_local_marker"],
        extra_paths=tuple(_expand(p) for p in excl_raw.get("extra_paths") or []),
    )

    heic_raw = merged["media"]["heic_to_jpeg"]
    if not heic_raw.get("preserve_original", True):
        # Not a real knob: docs/13 and D-016 require the HEIC/HEIF original to
        # remain the untouched archival asset. Refuse to start rather than
        # silently ignore an operator's expectation that this would delete it.
        raise ValueError(
            "media.heic_to_jpeg.preserve_original may not be set to false: "
            "the original HEIC/HEIF is never deleted by this tool"
        )
    media = MediaConfig(
        heic_to_jpeg=HeicToJpegConfig(
            enabled=bool(heic_raw["enabled"]),
            preserve_original=bool(heic_raw["preserve_original"]),
            jpeg_quality=int(heic_raw["jpeg_quality"]),
            preserve_metadata=bool(heic_raw["preserve_metadata"]),
            preserve_icc_profile=bool(heic_raw["preserve_icc_profile"]),
            normalize_orientation=bool(heic_raw["normalize_orientation"]),
        )
    )

    backup_raw = merged["backup"]
    retention_raw = backup_raw["retention"]
    retention = BackupRetentionConfig(
        keep_daily=int(retention_raw["keep_daily"]),
        keep_weekly=int(retention_raw["keep_weekly"]),
        keep_monthly=int(retention_raw["keep_monthly"]),
        keep_yearly=int(retention_raw["keep_yearly"]),
    )
    if any(value < 1 for value in (retention.keep_daily, retention.keep_weekly, retention.keep_monthly, retention.keep_yearly)):
        raise ValueError("backup retention values must each be at least 1")
    backup = BackupConfig(
        repository=str(backup_raw["repository"]),
        keychain_service=str(backup_raw["keychain_service"]),
        retention=retention,
        exclude_patterns=tuple(str(pattern) for pattern in backup_raw["exclude_patterns"]),
    )

    return IngestConfig(
        t7=t7,
        sources=sources,
        settle_seconds=int(merged["settle_seconds"]),
        downloads_watcher_min_settle_seconds=int(merged["downloads_watcher_min_settle_seconds"]),
        exclusions=exclusions,
        media=media,
        backup=backup,
        config_path=resolved_path,
    )
