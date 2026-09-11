"""Synthetic HEIC fixture generation for derivative tests.

Real camera HEIC files aren't available in CI, so fixtures are built with
`pillow_heif` directly (the same library the production converter uses to
*read* HEIC, but here used to *write* one -- a legitimate, independent round
trip: the converter under test never calls these functions).
"""

from __future__ import annotations

from pathlib import Path

import pillow_heif
from PIL import Image

pillow_heif.register_heif_opener()

FAKE_ICC_PROFILE = b"FAKEICCPROFILE_" + b"x" * 200


def make_heic(
    path: Path,
    *,
    size: tuple[int, int] = (64, 48),
    color: tuple[int, int, int] = (200, 60, 10),
    orientation: int | None = None,
    capture_time: str | None = "2020:06:15 08:30:00",
    icc_profile: bytes | None = FAKE_ICC_PROFILE,
    quality: int = 90,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    heif_file = pillow_heif.from_pillow(img)

    if orientation is not None or capture_time is not None:
        exif = Image.Exif()
        if orientation is not None:
            exif[0x0112] = orientation
        if capture_time is not None:
            exif[0x9003] = capture_time
        heif_file.info["exif"] = exif.tobytes()

    if icc_profile is not None:
        heif_file.info["icc_profile"] = icc_profile

    heif_file.save(str(path), quality=quality)
    return path


def make_corrupt_heic(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real heic file" * 20)
    return path
