"""HEIC/HEIF -> JPEG compatibility derivative (docs/13, D-016 media addendum).

Tooling choice: `pillow-heif` (libheif bindings for Pillow) rather than
macOS `sips`/PhotoKit. Reasons, recorded here because the spec asked the
choice to be justified and proven with tests rather than assumed:

- It is cross-platform (manylinux + macOS wheels), so the conversion path is
  exercised by CI on Linux, not only by hand on a Mac.
- Verified directly (see tests/test_heic_derivative.py): opening a HEIC via
  Pillow's registered HEIF opener already applies the container's
  orientation to the pixel data and normalizes the EXIF orientation tag to
  1, and passes EXIF (including DateTimeOriginal) and the ICC profile
  through byte-for-byte in `Image.info`. That covers every metadata
  requirement (EXIF, capture timestamp, orientation, ICC) with one small,
  maintained dependency instead of hand-rolled HEIF box parsing.
- A corrupted/non-HEIF input raises `PIL.UnidentifiedImageError`, which is
  easy to catch and turn into a recorded conversion failure without ever
  touching the original file.

The original HEIC/HEIF is opened read-only and never written to. Output is
written to a temp file in the destination directory and atomically renamed
into place, so a crash mid-encode cannot leave a half-written JPEG mistaken
for a finished one.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tools.ingest.hashing import sha256_file
from tools.ingest.media.interface import DerivativeResult

try:
    import pillow_heif
    from PIL import Image, UnidentifiedImageError

    pillow_heif.register_heif_opener()
    _IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised only if the dependency is missing
    _IMPORT_ERROR = exc

CONVERTER_NAME = "pillow-heif"
HEIC_SUFFIXES = frozenset({".heic", ".heif"})

# EXIF tags that are safe/meaningful to carry into the JPEG derivative.
# Orientation (0x0112) is deliberately re-derived (see below), not copied verbatim.
_ORIENTATION_TAG = 0x0112


@dataclass(frozen=True)
class HeicToJpegOptions:
    jpeg_quality: int = 100
    preserve_metadata: bool = True
    preserve_icc_profile: bool = True
    normalize_orientation: bool = True


def converter_version() -> str:
    if _IMPORT_ERROR is not None:
        return "unavailable"
    return getattr(pillow_heif, "__version__", "unknown")


def is_heic(path: Path) -> bool:
    return path.suffix.lower() in HEIC_SUFFIXES


def convert_heic_to_jpeg(
    source_path: Path,
    output_path: Path,
    options: HeicToJpegOptions,
    *,
    allow_replace: bool = False,
) -> DerivativeResult:
    """Write a JPEG derivative of `source_path` at `output_path`.

    `allow_replace` is the caller asserting that `output_path` is this
    derivative's own previous output and may be regenerated. It defaults to
    False so that an unrelated file occupying the path fails the conversion
    instead of being destroyed.
    """
    if _IMPORT_ERROR is not None:
        return DerivativeResult(success=False, converter=CONVERTER_NAME, error_message=f"pillow-heif not installed: {_IMPORT_ERROR}")

    try:
        with Image.open(source_path) as im:
            im.load()
            exif = im.getexif() if options.preserve_metadata else None
            if exif is not None and options.normalize_orientation:
                # pillow-heif already applies the HEIC container's orientation
                # to the decoded pixels and resets this tag to 1; setting it
                # explicitly keeps behavior correct even if that ever changes.
                exif[_ORIENTATION_TAG] = 1
            icc_profile = im.info.get("icc_profile") if options.preserve_icc_profile else None

            save_kwargs: dict[str, object] = {
                "format": "JPEG",
                "quality": options.jpeg_quality,
                "subsampling": 0 if options.jpeg_quality >= 95 else 2,
            }
            if exif is not None:
                save_kwargs["exif"] = exif.tobytes()
            if icc_profile:
                save_kwargs["icc_profile"] = icc_profile

            rgb = im.convert("RGB") if im.mode not in {"RGB", "L"} else im

            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists() and not allow_replace:
                # The caller is responsible for choosing a free path (or for
                # stating that overwriting this one is its own prior output).
                # Refusing here means a converter can never silently destroy a
                # file it did not create, whatever a caller gets wrong.
                return DerivativeResult(
                    success=False,
                    converter=CONVERTER_NAME,
                    converter_version=converter_version(),
                    error_message=f"refusing to overwrite an existing file: {output_path}",
                )
            fd, tmp_name = tempfile.mkstemp(prefix=".heic-jpeg-", suffix=".partial", dir=str(output_path.parent))
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                rgb.save(tmp_path, **save_kwargs)
                with open(tmp_path, "rb") as fh:
                    os.fsync(fh.fileno())
                os.replace(tmp_path, output_path)
            finally:
                tmp_path.unlink(missing_ok=True)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return DerivativeResult(success=False, converter=CONVERTER_NAME, converter_version=converter_version(), error_message=str(exc))

    return DerivativeResult(
        success=True,
        output_path=output_path,
        sha256=sha256_file(output_path),
        size=output_path.stat().st_size,
        converter=CONVERTER_NAME,
        converter_version=converter_version(),
    )
