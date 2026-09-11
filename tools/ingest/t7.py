"""Resolve and preflight-check the T7 (or any configured) destination volume.

Every `--apply` path must go through `resolve_t7` first. If the configured
volume is absent, mismatched, locked, or read-only, this raises `VolumeError`
and the caller must not touch any source path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tools.ingest.config import IngestConfig
from tools.ingest.volumes import MountedVolume, VolumeError, VolumeProber, find_volume_by_uuid


@dataclass(frozen=True)
class ResolvedT7:
    root: Path
    system_dir: Path
    inbox_dir: Path
    staging_dir: Path
    catalog_path: Path
    sidecars_dir: Path
    volume_uuid: str | None


def _check_writable(root: Path) -> None:
    if not root.exists():
        raise VolumeError(f"T7 root does not exist: {root}")
    if not root.is_dir():
        raise VolumeError(f"T7 root is not a directory: {root}")
    if not os.access(root, os.W_OK):
        raise VolumeError(f"T7 root is not writable (read-only or locked): {root}")


def resolve_t7(config: IngestConfig, prober: VolumeProber, *, for_apply: bool) -> ResolvedT7:
    """Resolve the configured destination volume's root path.

    Preflight order matches the spec: volume present -> UUID matches ->
    mounted -> writable -> structure valid. Any failure raises `VolumeError`
    before anything is written.
    """
    volume_uuid = config.t7.volume_uuid
    root: Path
    resolved_uuid: str | None

    if config.t7.storage_root_override is not None:
        # Explicit dev/test root; bypasses UUID detection entirely by design
        # (see config/ingest.example.yaml). Never used for the real T7.
        root = config.t7.storage_root_override
        resolved_uuid = volume_uuid
    else:
        if not volume_uuid:
            raise VolumeError(
                "no t7.volume_uuid configured and no storage_root_override set; "
                "set t7.volume_uuid in config/ingest.yaml (see config/ingest.example.yaml)"
            )
        volumes: list[MountedVolume] = prober()
        match = find_volume_by_uuid(volumes, volume_uuid)
        if match is None:
            raise VolumeError(f"configured T7 volume (UUID {volume_uuid}) is not mounted")
        if not match.writable:
            raise VolumeError(f"T7 volume {volume_uuid} is mounted read-only or locked")
        root = match.mount_point
        resolved_uuid = match.volume_uuid

    _check_writable(root)

    system_dir = root / config.t7.system_relative_path
    inbox_dir = root / config.t7.inbox_relative_path
    staging_dir = system_dir / "staging"
    catalog_path = system_dir / "catalog.sqlite"
    sidecars_dir = system_dir / "sidecars"

    if for_apply:
        # Scaffolding required directories is normal --apply behavior, not a
        # destructive/organizational action; dry-run never reaches this branch.
        for path in (system_dir, inbox_dir, staging_dir, sidecars_dir):
            path.mkdir(parents=True, exist_ok=True)
    else:
        # Preflight-only: don't require the tree to already exist, but if the
        # root itself resolves to something bogus (e.g. a bare empty path
        # outside any real volume) that's still a legitimate "not ready" case
        # surfaced by the caller's report rather than an exception.
        pass

    return ResolvedT7(
        root=root,
        system_dir=system_dir,
        inbox_dir=inbox_dir,
        staging_dir=staging_dir,
        catalog_path=catalog_path,
        sidecars_dir=sidecars_dir,
        volume_uuid=resolved_uuid,
    )
