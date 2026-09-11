"""Volume identity and detection.

Detection is injected as a `VolumeProber` callable so production code (which
shells out to `diskutil` on macOS) and tests (which supply a fixed list of
fake volumes) share the exact same resolution logic in `resolve_t7` /
`registered_sd_volumes`. Nothing here ever formats, repartitions, or writes
to a volume -- it only reads identity/mount metadata.
"""

from __future__ import annotations

import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


class VolumeError(RuntimeError):
    """A T7/volume precondition was not met. Callers must not proceed to --apply."""


@dataclass(frozen=True)
class MountedVolume:
    volume_uuid: str
    mount_point: Path
    filesystem: str
    writable: bool
    removable: bool
    label: str = ""


VolumeProber = Callable[[], list[MountedVolume]]


def diskutil_volume_prober() -> list[MountedVolume]:
    """Production prober: read-only `diskutil info -plist` per mounted volume.

    macOS-only. Never called by tests (they inject a fake prober instead).
    """
    volumes: list[MountedVolume] = []
    volumes_dir = Path("/Volumes")
    if not volumes_dir.is_dir():
        return volumes
    for entry in sorted(volumes_dir.iterdir()):
        try:
            proc = subprocess.run(
                ["diskutil", "info", "-plist", str(entry)],
                capture_output=True,
                check=False,
                timeout=15,
            )
            if proc.returncode != 0:
                continue
            info = plistlib.loads(proc.stdout)
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
            continue
        volume_uuid = info.get("VolumeUUID")
        if not volume_uuid:
            continue
        volumes.append(
            MountedVolume(
                volume_uuid=volume_uuid,
                mount_point=Path(info.get("MountPoint", str(entry))),
                filesystem=info.get("FilesystemType", ""),
                writable=bool(info.get("WritableVolume", False)),
                removable=bool(info.get("RemovableMedia", False) or info.get("Ejectable", False)),
                label=info.get("VolumeName", entry.name),
            )
        )
    return volumes


def find_volume_by_uuid(volumes: Iterable[MountedVolume], volume_uuid: str) -> MountedVolume | None:
    for volume in volumes:
        if volume.volume_uuid == volume_uuid:
            return volume
    return None


def registered_sd_volumes(volumes: Iterable[MountedVolume], registered_uuids: Iterable[str]) -> list[MountedVolume]:
    """Removable volumes that are both mounted and explicitly registered.

    Never returns a volume that is not in `registered_uuids`: an unknown
    removable disk being mounted must never become eligible on its own.
    """
    registered = set(registered_uuids)
    return [v for v in volumes if v.removable and v.volume_uuid in registered]
