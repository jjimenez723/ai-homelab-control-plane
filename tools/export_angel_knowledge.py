"""Export tracked knowledge documents to Angel (the T7).

`knowledge/` is gitignored by design (see `.gitignore` and the repository
map in `README.md`): the canonical copy of anything durable lives in tracked
`docs/` and `templates/`, and an Angel export is a read-only *copy* for
research/reference use on the T7, never the source of truth.

This module follows the exact same volume-identity discipline as
`tools/backup.py` / `tools/ingest/t7.py`: resolve the destination by
`t7.volume_uuid` read from `config/ingest.yaml` (never by Finder volume
name), refuse to run if the configured UUID is not mounted or not writable,
and never touch anything outside the resolved root.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from tools.ingest.config import IngestConfig, load_config
from tools.ingest.volumes import MountedVolume, VolumeProber, find_volume_by_uuid


class ExportError(RuntimeError):
    """A volume/export precondition was not met. Nothing is copied when raised."""


REPO_ROOT = Path(__file__).resolve().parent.parent

# What this exporter knows how to publish. Each entry is (repo-relative
# source, name under knowledge/<export_subdir>/). Extend this tuple, do not
# duplicate the export logic, when a future workstream adds another
# knowledge document.
DEFAULT_EXPORTS: tuple[tuple[Path, str], ...] = (
    (Path("docs") / "27-backend-baselines.md", "27-backend-baselines.md"),
    (Path("templates") / "backend-profiles", "backend-profiles"),
)

# Sub-path under the resolved T7 root's 50-research/knowledge/ directory.
EXPORT_SUBDIR = "backend-baselines"
KNOWLEDGE_RELATIVE_PATH = Path("50-research") / "knowledge"


@dataclass(frozen=True)
class ExportResult:
    destination_root: Path
    copied: tuple[Path, ...]


def resolve_angel_root(config: IngestConfig, prober: VolumeProber) -> Path:
    """Resolve the configured T7 root by volume UUID -- mirrors resolve_t7()/resolve_backup_source()."""
    uuid = config.t7.volume_uuid
    if not uuid:
        raise ExportError(
            "no t7.volume_uuid configured in config/ingest.yaml; refusing to export to an unspecified volume"
        )
    if config.t7.storage_root_override is not None:
        raise ExportError(
            "t7.storage_root_override is a development-only root and is refused for knowledge export; "
            "configure a real t7.volume_uuid instead"
        )
    volumes: list[MountedVolume] = prober()
    match = find_volume_by_uuid(volumes, uuid)
    if match is None:
        raise ExportError(f"configured T7 volume (UUID {uuid}) is not mounted")
    if not match.writable:
        raise ExportError(f"T7 volume {uuid} is mounted read-only or locked")
    try:
        return match.mount_point.resolve(strict=True)
    except OSError as exc:
        raise ExportError(f"T7 volume {uuid} is not an accessible mounted directory") from exc


def _copy_one(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def export_knowledge(
    config: IngestConfig,
    prober: VolumeProber,
    *,
    exports: tuple[tuple[Path, str], ...] = DEFAULT_EXPORTS,
    export_subdir: str = EXPORT_SUBDIR,
) -> ExportResult:
    """Copy the given (source, name) pairs into <angel>/50-research/knowledge/<export_subdir>/.

    Creates only the leaf directories this export needs (the export
    subdirectory, and `50-research/knowledge/` itself if another workstream
    has not created it yet); never restructures or deletes anything already
    present under `50-research/`.
    """
    angel_root = resolve_angel_root(config, prober)
    knowledge_dir = angel_root / KNOWLEDGE_RELATIVE_PATH
    destination_root = knowledge_dir / export_subdir
    destination_root.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    for relative_source, name in exports:
        source = REPO_ROOT / relative_source
        if not source.exists():
            raise ExportError(f"export source does not exist: {source}")
        destination = destination_root / name
        _copy_one(source, destination)
        copied.append(destination)

    return ExportResult(destination_root=destination_root, copied=tuple(copied))


def main(argv: list[str] | None = None) -> int:
    import argparse

    from tools.ingest.volumes import diskutil_volume_prober

    parser = argparse.ArgumentParser(description="Export tracked knowledge docs to the Angel T7.")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        result = export_knowledge(config, diskutil_volume_prober)
    except ExportError as exc:
        print(f"export-angel-knowledge: {exc}")
        return 1

    print(f"Exported to {result.destination_root}")
    for path in result.copied:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
