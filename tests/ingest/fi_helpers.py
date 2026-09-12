"""Fixtures for the file-intelligence tests.

Everything here builds isolated temp directories. No test touches a real
Desktop, Documents, Downloads, Screenshots folder, or removable disk (§19).
"""

from __future__ import annotations

import dataclasses
import os
import time
import zipfile
from pathlib import Path

from tools.ingest.config import IngestConfig, ScreenshotsConfig

from tests.ingest.helpers import build_config

LOCAL_VOLUME_ID = "mac-local"


def fi_config(
    root: Path,
    *,
    downloads: Path | None = None,
    screenshots: Path | None = None,
    screenshots_enabled: bool = True,
    t7_root: Path | None = None,
    settle_seconds: int = 1,
    **overrides,
) -> IngestConfig:
    """An IngestConfig wired entirely to temp directories."""
    config = build_config(
        desktop=root / "Desktop",
        downloads=downloads,
        t7_root=t7_root,
        settle_seconds=settle_seconds,
    )
    config = dataclasses.replace(
        config,
        sources=dataclasses.replace(
            config.sources,
            screenshots=ScreenshotsConfig(path=screenshots, enabled=screenshots_enabled and screenshots is not None),
        ),
        automation=dataclasses.replace(config.automation, state_relative_path=".state/test-file-intelligence"),
    )
    for key, value in overrides.items():
        config = dataclasses.replace(config, **{key: value})
    return config


def aged(path: Path, *, seconds: int = 7200) -> Path:
    """Push a file's mtime back so it counts as settled."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


def fresh(path: Path) -> Path:
    """Give a file a right-now mtime, so it is deliberately *not* settled."""
    now = time.time()
    os.utime(path, (now, now))
    return path


def make_png(path: Path, *, size: tuple[int, int] = (1440, 900), color=(20, 80, 160), settled: bool = True) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return aged(path) if settled else fresh(path)


def make_camera_jpeg(
    path: Path,
    *,
    size: tuple[int, int] = (4032, 3024),
    make: str = "Apple",
    model: str = "iPhone 15 Pro",
    captured: str = "2026:05:04 13:22:11",
    settled: bool = True,
) -> Path:
    """A JPEG carrying the EXIF a real camera would write."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, (200, 120, 40))
    exif = Image.Exif()
    exif[0x010F] = make
    exif[0x0110] = model
    exif[0x9003] = captured
    image.save(path, exif=exif.tobytes())
    return aged(path) if settled else fresh(path)


def make_blob(path: Path, content: bytes = b"payload" * 2000, *, settled: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return aged(path) if settled else fresh(path)


def make_zip(path: Path, entries: list[str], *, settled: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for entry in entries:
            archive.writestr(entry, b"0" * 256)
    return aged(path) if settled else fresh(path)
