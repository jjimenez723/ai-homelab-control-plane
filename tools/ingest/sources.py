"""Source class definitions and root/volume resolution.

Desktop, Documents, and Downloads are manual-trigger only (D-016): nothing in
this module scans or transfers a file on its own. SD/removable media is
*detected* automatically by UUID, but only a registered UUID is ever
eligible, and originals still require an explicit `--apply`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tools.ingest.config import IngestConfig
from tools.ingest.volumes import MountedVolume, VolumeProber, registered_sd_volumes

SOURCE_DESKTOP = "desktop"
SOURCE_DOCUMENTS = "documents"
SOURCE_DOWNLOADS = "downloads"
SOURCE_SD = "sd"
SOURCE_LEGACY = "legacy"

MANUAL_STATIC_SOURCES = (SOURCE_DESKTOP, SOURCE_DOCUMENTS, SOURCE_DOWNLOADS)
ALL_SOURCE_CLASSES = (SOURCE_DESKTOP, SOURCE_DOCUMENTS, SOURCE_DOWNLOADS, SOURCE_SD)


def static_source_root(config: IngestConfig, source_class: str) -> Path:
    return {
        SOURCE_DESKTOP: config.sources.desktop,
        SOURCE_DOCUMENTS: config.sources.documents,
        SOURCE_DOWNLOADS: config.sources.downloads,
    }[source_class]


def resolve_volume_for_path(path: Path, volumes: list[MountedVolume]) -> MountedVolume | None:
    """The mounted volume whose mount point is the longest prefix of `path`.

    This is the standard "which filesystem is this path on" resolution: of
    all candidate mount points that contain the path, the most specific
    (longest) one wins.
    """
    resolved = path.resolve() if path.exists() else path
    best: MountedVolume | None = None
    best_len = -1
    for volume in volumes:
        mount = volume.mount_point
        try:
            resolved.relative_to(mount)
        except ValueError:
            continue
        length = len(mount.parts)
        if length > best_len:
            best = volume
            best_len = length
    return best


@dataclass(frozen=True)
class SDSource:
    volume: MountedVolume

    @property
    def root(self) -> Path:
        return self.volume.mount_point


def detect_sd_sources(prober: VolumeProber, config: IngestConfig) -> list[SDSource]:
    """Registered, currently-mounted SD/removable sources.

    An unregistered removable disk is never returned here, no matter how
    plausible it looks -- registration is an explicit owner action in
    config/ingest.yaml, never inferred.
    """
    volumes = prober()
    eligible = registered_sd_volumes(volumes, config.sources.sd_volume_uuids)
    return [SDSource(volume=v) for v in eligible]


def all_removable_volumes(prober: VolumeProber) -> list[MountedVolume]:
    return [v for v in prober() if v.removable]
