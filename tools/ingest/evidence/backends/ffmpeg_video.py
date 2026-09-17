"""Optional, offline video-frame sampling via the `ffmpeg` CLI.

Permitted, alongside `tesseract.py`, to use `subprocess` -- see
`EVIDENCE_SUBPROCESS_JUSTIFICATIONS` in
`tests/ingest/test_mutation_authority.py`, which fails the build for any
third module that imports `subprocess` under `tools/ingest/evidence/`.

Detection follows the same pattern as `tesseract.py`/
`media/probe.py::ffprobe_available`: `shutil.which` plus a short-timeout
`--version` call, cached for the process, degrading to unavailable rather
than raising anywhere.

Writes no file, ever. `ffmpeg` reads the source video from its real path on
disk (a `Path`, unlike every other perception backend in this package,
which receives only in-memory bytes -- streaming an arbitrary container
format into ffmpeg's stdin byte-for-byte is not practical, so this is the
one narrow exception; it is still read-only) and emits exactly one PNG
frame on its stdout -- `-f image2pipe -vcodec png -`, no `-y`, no output
path, nothing landing in a temp directory. The frame is downscaled by
ffmpeg's own `-vf scale=...` filter before it ever reaches this process, so
no second decompression-bomb guard is needed the way `preprocess.py`'s
`open_image_safely` is needed for a file decoded straight off disk: ffmpeg
itself is the trust boundary here, exactly as pypdfium2's `page.render(scale=)`
already is for `raster.py::rasterize_pdf_pages`.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from tools.ingest.evidence.types import (
    EvidenceWarning,
    RasterImage,
    WARN_BACKEND_UNAVAILABLE,
    WARN_MALFORMED,
    WARN_TIMEOUT,
    WARN_TOO_LARGE,
)

_STAGE = "visual"
_VERSION_PROBE_TIMEOUT_SECONDS = 10

# The long edge ffmpeg's own scale filter is asked to cap a sampled frame at.
# A vision model gets no benefit from a full 4K frame, and capping it here
# (rather than after decoding) means the subprocess itself never emits more
# bytes than this implies -- there is nothing oversized to guard against
# downstream.
MAX_FRAME_EDGE_PIXELS = 1280

# A single downscaled PNG frame should never approach this; if it does, the
# encoded frame is treated as suspect rather than trusted.
MAX_FRAME_BYTES = 20_000_000

# A video's very first frame is disproportionately likely to be a black
# fade-in or a title card; sampling past it gives a more representative
# frame. Used only when duration is unknown or too short to have a midpoint
# past this offset.
_DEFAULT_TIMESTAMP_SECONDS = 1.0


@lru_cache(maxsize=1)
def ffmpeg_version() -> str | None:
    """The installed ffmpeg's version string, or None if unusable.

    Cached for the life of the process, matching
    `tesseract.py::tesseract_version` and `probe.py::ffprobe_available`.
    """
    if shutil.which("ffmpeg") is None:
        return None
    try:
        completed = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    text = completed.stdout.decode("utf-8", "replace").strip()
    if not text:
        return None
    first_line = text.splitlines()[0]
    parts = first_line.split()
    # Expected shape: "ffmpeg version 6.1.1 Copyright ...". Fall back to the
    # raw first line so an unexpected banner still cache-busts distinctly
    # rather than masquerading as "absent".
    if len(parts) >= 3 and parts[0].lower() == "ffmpeg" and parts[1].lower() == "version":
        return parts[2]
    return first_line


def available() -> bool:
    return ffmpeg_version() is not None


def _sample_timestamp(duration_seconds: float | None) -> float:
    if not duration_seconds or duration_seconds <= 0:
        return _DEFAULT_TIMESTAMP_SECONDS
    midpoint = duration_seconds / 2.0
    return midpoint if midpoint > 0 else _DEFAULT_TIMESTAMP_SECONDS


def extract_frame(
    path: Path,
    *,
    source_sha256: str,
    duration_seconds: float | None,
    timeout_seconds: int,
    max_edge: int = MAX_FRAME_EDGE_PIXELS,
) -> tuple[RasterImage | None, tuple[EvidenceWarning, ...]]:
    """One representative frame from `path`, as a `RasterImage`.

    Never raises. A missing/broken ffmpeg, a timeout, or a video ffmpeg
    cannot decode all degrade to `(None, warnings)`, mirroring every other
    backend in this package.
    """
    if not available():
        return None, (
            EvidenceWarning(code=WARN_BACKEND_UNAVAILABLE, stage=_STAGE, detail="ffmpeg binary not found on PATH"),
        )

    timestamp_seconds = _sample_timestamp(duration_seconds)
    scale_filter = (
        f"scale='min({max_edge},iw)':'min({max_edge},ih)':force_original_aspect_ratio=decrease"
    )
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{timestamp_seconds:.3f}", "-i", str(path),
        "-frames:v", "1", "-vf", scale_filter,
        "-f", "image2pipe", "-vcodec", "png", "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return None, (
            EvidenceWarning(code=WARN_TIMEOUT, stage=_STAGE, detail=f"ffmpeg timed out after {timeout_seconds}s"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, (
            EvidenceWarning(
                code=WARN_BACKEND_UNAVAILABLE, stage=_STAGE,
                detail=f"ffmpeg could not be executed: {type(exc).__name__}: {exc}",
            ),
        )

    if completed.returncode != 0 or not completed.stdout:
        stderr_lines = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = stderr_lines[-1] if stderr_lines else f"exit code {completed.returncode}"
        return None, (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"ffmpeg produced no frame: {detail}"),)

    if len(completed.stdout) > MAX_FRAME_BYTES:
        return None, (
            EvidenceWarning(code=WARN_TOO_LARGE, stage=_STAGE, detail="ffmpeg frame output exceeded the size guard"),
        )

    try:
        from PIL import Image  # deferred: keeps this module importable without Pillow installed

        with Image.open(io.BytesIO(completed.stdout)) as decoded:
            width, height = decoded.size
    except Exception as exc:  # noqa: BLE001 - a frame that fails to decode is not usable
        return None, (
            EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"ffmpeg frame could not be decoded: {type(exc).__name__}: {exc}"),
        )

    return (
        RasterImage(png_bytes=completed.stdout, width=width, height=height, page=1, source_sha256=source_sha256),
        (),
    )
