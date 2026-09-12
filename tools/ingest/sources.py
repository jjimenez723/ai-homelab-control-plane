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
from tools.ingest.volumes import MountedVolume, registered_sd_volumes

SOURCE_DESKTOP = "desktop"
SOURCE_DOCUMENTS = "documents"
SOURCE_DOWNLOADS = "downloads"
SOURCE_SD = "sd"
SOURCE_SCREENSHOTS = "screenshots"
SOURCE_LEGACY = "legacy"

MANUAL_STATIC_SOURCES = (SOURCE_DESKTOP, SOURCE_DOCUMENTS, SOURCE_DOWNLOADS)
ALL_SOURCE_CLASSES = (SOURCE_DESKTOP, SOURCE_DOCUMENTS, SOURCE_DOWNLOADS, SOURCE_SD)

# Source classes the local file-intelligence pipeline (naming + retention
# triage) may look at. Desktop and Documents are deliberately absent: this
# phase does not evaluate them automatically at all.
TRIAGE_SOURCE_CLASSES = (SOURCE_DOWNLOADS, SOURCE_SCREENSHOTS)

# Image types a screenshot could plausibly be.
_SCREENSHOT_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".heic", ".heif", ".tiff", ".tif", ".gif", ".webp"})


class ScreenshotsNotConfigured(RuntimeError):
    """The screenshots source was requested but cannot be resolved safely."""


def resolve_screenshots_root(config: IngestConfig) -> Path:
    """The configured screenshots folder, or a clear refusal.

    Never inferred. macOS keeps the real location in
    `com.apple.screencapture location`, and on a default install that is
    `~/Desktop` -- so a silent fallback would put the entire Desktop under
    automatic evaluation, which D-016 forbids. `organize doctor` reports what
    macOS says so the owner can paste it in deliberately.
    """
    screenshots = config.sources.screenshots
    if not screenshots.enabled:
        raise ScreenshotsNotConfigured(
            "sources.screenshots.enabled is false; enable it and set sources.screenshots.path "
            "in config/ingest.yaml (run `organize doctor` to see where macOS is saving screenshots)"
        )
    if screenshots.path is None:
        raise ScreenshotsNotConfigured(
            "sources.screenshots.path is not set; the screenshot folder is never guessed"
        )
    root = screenshots.path
    if not root.exists():
        raise ScreenshotsNotConfigured(f"configured screenshots path does not exist: {root}")
    if not root.is_dir():
        raise ScreenshotsNotConfigured(f"configured screenshots path is not a directory: {root}")
    return root


def screenshots_root_is_shared(config: IngestConfig, root: Path) -> bool:
    """Whether the screenshots folder is also a managed source root.

    The macOS default puts screenshots on the Desktop. When that is the case,
    the screenshots source must not treat every Desktop file as fair game --
    see `is_screenshot_candidate`.
    """
    resolved = root.resolve() if root.exists() else root
    for other in (config.sources.desktop, config.sources.documents, config.sources.downloads):
        other_resolved = other.resolve() if other.exists() else other
        if resolved == other_resolved:
            return True
        try:
            resolved.relative_to(other_resolved)
            return True
        except ValueError:
            pass
        try:
            other_resolved.relative_to(resolved)
            return True
        except ValueError:
            continue
    return False


def is_screenshot_candidate(path: Path, *, require_name_pattern: bool) -> bool:
    """Whether a file in the screenshots root should be evaluated.

    Always restricted to image types: a screenshots folder is not a general
    ingestion source. When the folder is shared with the Desktop or Documents,
    the filename pattern is required as well, so pointing this at the macOS
    default cannot turn into "evaluate everything on the Desktop".
    """
    if path.suffix.lower() not in _SCREENSHOT_IMAGE_SUFFIXES:
        return False
    if not require_name_pattern:
        return True
    from tools.ingest.naming import quality

    return quality.assess(path.name)[0] == quality.QUALITY_POOR and _looks_like_screenshot_name(path.name)


def _looks_like_screenshot_name(filename: str) -> bool:
    lowered = filename.lower()
    return any(
        lowered.startswith(prefix)
        for prefix in ("screenshot", "screen shot", "screen_shot", "cleanshot", "bildschirmfoto", "capture d", "captura de pantalla", "simulator screenshot")
    )


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


def detect_sd_sources(volumes: list[MountedVolume], config: IngestConfig) -> list[SDSource]:
    """Registered, currently-mounted SD/removable sources.

    An unregistered removable disk is never returned here, no matter how
    plausible it looks -- registration is an explicit owner action in
    config/ingest.yaml, never inferred.
    """
    eligible = registered_sd_volumes(volumes, config.sources.sd_volume_uuids)
    return [SDSource(volume=v) for v in eligible]


def all_removable_volumes(volumes: list[MountedVolume]) -> list[MountedVolume]:
    return [v for v in volumes if v.removable]


def screenshot_location_hint() -> str | None:
    """Where macOS is currently saving screenshots, for `organize doctor`.

    Reported only. It is never written into config and never used as a scan
    root: the whole point of requiring an explicit `sources.screenshots.path`
    is that the macOS default is the Desktop, and silently adopting it would
    put every Desktop file under automatic evaluation. The owner copies this
    value in deliberately, or does not.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["defaults", "read", "com.apple.screencapture", "location"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        # Unset means macOS is using its default, which is the Desktop.
        return "~/Desktop (macOS default; com.apple.screencapture location is unset)"
    value = result.stdout.strip()
    return value or None
