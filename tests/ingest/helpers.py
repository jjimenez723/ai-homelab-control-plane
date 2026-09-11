"""Shared test fixtures for the ingest test suite.

Everything here builds fully isolated temp-directory sources and a fake T7
via `storage_root_override` / a fake volume prober -- no test ever touches a
real Desktop, Documents, Downloads, or removable disk.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from tools.ingest.config import (
    ExclusionsConfig,
    HeicToJpegConfig,
    IngestConfig,
    MediaConfig,
    SourcesConfig,
    T7Config,
)
from tools.ingest.t7 import ResolvedT7, resolve_t7
from tools.ingest.volumes import MountedVolume

T7_TEST_UUID = "11111111-1111-1111-1111-111111111111"
SD_TEST_UUID = "22222222-2222-2222-2222-222222222222"
UNKNOWN_SD_UUID = "99999999-9999-9999-9999-999999999999"

DEFAULT_EXCLUSIONS = ExclusionsConfig(
    dir_names=frozenset(
        {"node_modules", ".venv", "venv", "env", ".tox", "__pycache__", ".mypy_cache", ".pytest_cache", ".git"}
    ),
    bundle_extensions=frozenset(
        {".app", ".bundle", ".framework", ".plugin", ".kext", ".photoslibrary", ".xcodeproj", ".xcworkspace"}
    ),
    incomplete_download_suffixes=frozenset({".crdownload", ".download", ".part"}),
    keep_local_marker=".keep-local",
    extra_paths=(),
)


def build_config(
    *,
    desktop: Path,
    t7_root: Path | None = None,
    t7_volume_uuid: str | None = None,
    documents: Path | None = None,
    downloads: Path | None = None,
    sd_volume_uuids: frozenset[str] = frozenset(),
    settle_seconds: int = 2,
    heic: HeicToJpegConfig | None = None,
    exclusions: ExclusionsConfig | None = None,
) -> IngestConfig:
    documents = documents or (desktop.parent / "documents-unused")
    downloads = downloads or (desktop.parent / "downloads-unused")
    return IngestConfig(
        t7=T7Config(
            volume_uuid=t7_volume_uuid,
            storage_root_override=t7_root,
            inbox_relative_path="00-inbox/organized",
            system_relative_path="90-system",
        ),
        sources=SourcesConfig(
            desktop=desktop, documents=documents, downloads=downloads, sd_volume_uuids=sd_volume_uuids
        ),
        settle_seconds=settle_seconds,
        downloads_watcher_min_settle_seconds=1200,
        exclusions=exclusions or DEFAULT_EXCLUSIONS,
        media=MediaConfig(heic_to_jpeg=heic or HeicToJpegConfig()),
    )


def make_volume(uuid: str, mount_point: Path, *, writable: bool = True, removable: bool = False, label: str = "") -> MountedVolume:
    return MountedVolume(
        volume_uuid=uuid, mount_point=mount_point, filesystem="apfs", writable=writable, removable=removable, label=label or uuid
    )


def fake_prober(volumes: list[MountedVolume]):
    def _prober() -> list[MountedVolume]:
        return list(volumes)

    return _prober


def write_file(path: Path, content: bytes = b"data", *, age_seconds: int = 3600) -> Path:
    """Write a file whose mtime is already old enough to be considered settled."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    return path


def touch_fresh(path: Path) -> None:
    now = time.time()
    os.utime(path, (now, now))


def make_resolved_t7(config: IngestConfig) -> ResolvedT7:
    return resolve_t7(config, prober=lambda: [], for_apply=True)
