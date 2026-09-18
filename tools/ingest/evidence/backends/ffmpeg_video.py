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

Frame selection, exactly
------------------------

`extract_frames` is the whole algorithm, and every step of it is
deterministic -- the same file and the same configuration always produce the
same frames, because cached evidence keyed by content hash would otherwise
be an answer to a question that changed between runs.

1. **How many.** `frame_count_for(duration, requested, duration_aware=...)`.
   The requested count is clamped to `1..MAX_VIDEO_FRAME_COUNT` (12; see
   `config.MAX_VIDEO_FRAME_COUNT` for why a ceiling exists at all). With
   `duration_aware` on (the default) a clip too short to *contain*
   `requested` distinct moments is sampled less: under
   `SHORT_VIDEO_SECONDS` (4.0 s) at most `SHORT_VIDEO_FRAMES` (2), and under
   `VERY_SHORT_VIDEO_SECONDS` (1.0 s) exactly one. An unknown duration is
   treated as long, because refusing to sample is worse than sampling a
   three-second clip five times.
2. **Where.** `_sample_timestamps` spaces them evenly *strictly inside* the
   duration -- `duration * (i+1) / (count+1)` -- never the exact start (a
   fade-in or title card) and never the exact end (possibly past the
   container's last decodable frame). Unknown duration falls back to fixed
   one-second-apart offsets.
3. **Which survive.** `deduplicate_frames` collapses near-identical frames
   with an 8x8 average hash (`average_hash`): each frame is converted to
   greyscale, resized to 8x8 with `Image.BILINEAR`, and every pixel becomes
   one bit -- set when it is at or above the mean of those 64 pixels. Two
   frames are "the same picture" when their hashes differ in at most
   `DEDUPE_HAMMING_THRESHOLD` (4) of 64 bits. A frame is compared against
   every *kept* frame, not merely its predecessor, so a clip that alternates
   between two shots keeps two frames rather than all of them. The earliest
   frame of a group is the representative, and it keeps its original 1-based
   sampling index in `RasterImage.page` -- so the surviving pages may be
   non-contiguous (`1, 2, 4`), which is deliberate: the index says *when* in
   the clip the frame came from, and renumbering would destroy that.

Average hash is used rather than a DCT-based pHash because the question here
is only "is this the same picture again", the frames are already downscaled
by ffmpeg, and it needs no NumPy -- a banned import in this package
(`EVIDENCE_BANNED_IMPORTS`). It is implemented in plain Pillow, in
`average_hash` below, with no adapted third-party code.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from tools.ingest.config import MAX_VIDEO_FRAME_COUNT
from tools.ingest.evidence.types import (
    EvidenceWarning,
    RasterImage,
    WARN_BACKEND_UNAVAILABLE,
    WARN_DEDUPLICATED,
    WARN_MALFORMED,
    WARN_SKIPPED_BY_POLICY,
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

# Duration-aware frame counts. A two-second clip has nowhere to put five
# distinct moments, so asking for five buys four redundant backend calls.
VERY_SHORT_VIDEO_SECONDS = 1.0
SHORT_VIDEO_SECONDS = 4.0
SHORT_VIDEO_FRAMES = 2

# The average-hash grid. 8x8 is the classical size: small enough that a
# re-encode, a compression artifact, or a slow pan does not change the hash,
# large enough that two genuinely different shots do.
HASH_EDGE = 8
HASH_BITS = HASH_EDGE * HASH_EDGE

# Out of `HASH_BITS`, how many may differ before two frames are still "the
# same picture". Four of sixty-four is a little over six percent: it absorbs
# encoder noise and a ticking on-screen clock, and does not absorb a cut.
DEDUPE_HAMMING_THRESHOLD = 4


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


def _sample_timestamps(duration_seconds: float | None, count: int) -> list[float]:
    """`count` timestamps, evenly spaced strictly inside the video's duration.

    Deliberately avoids the exact start (disproportionately likely to be a
    black fade-in or title card) and the exact end (may already be past a
    container's last decodable frame). With an unknown duration, falls back
    to fixed one-second-apart offsets rather than refusing to sample at all.
    """
    count = max(1, count)
    if not duration_seconds or duration_seconds <= 0:
        return [_DEFAULT_TIMESTAMP_SECONDS * (index + 1) for index in range(count)]
    return [duration_seconds * (index + 1) / (count + 1) for index in range(count)]


def _extract_one_frame(
    path: Path,
    *,
    timestamp_seconds: float,
    source_sha256: str,
    page: int,
    timeout_seconds: int,
    max_edge: int,
) -> tuple[RasterImage | None, tuple[EvidenceWarning, ...]]:
    """The actual ffmpeg invocation for one frame. Never raises."""
    scale_filter = (
        f"scale='min({max_edge},iw)':'min({max_edge},ih)':force_original_aspect_ratio=decrease"
    )
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, timestamp_seconds):.3f}", "-i", str(path),
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
        return None, (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"ffmpeg produced no frame at {timestamp_seconds:.1f}s: {detail}"),)

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
        RasterImage(png_bytes=completed.stdout, width=width, height=height, page=page, source_sha256=source_sha256),
        (),
    )


def extract_frame(
    path: Path,
    *,
    source_sha256: str,
    duration_seconds: float | None,
    timeout_seconds: int,
    max_edge: int = MAX_FRAME_EDGE_PIXELS,
) -> tuple[RasterImage | None, tuple[EvidenceWarning, ...]]:
    """One representative (midpoint) frame from `path`, as a `RasterImage`.

    Used for a quick preview thumbnail, independent of how many frames
    `extract_frames` samples for vision/OCR. Never raises.
    """
    if not available():
        return None, (
            EvidenceWarning(code=WARN_BACKEND_UNAVAILABLE, stage=_STAGE, detail="ffmpeg binary not found on PATH"),
        )
    return _extract_one_frame(
        path, timestamp_seconds=_sample_timestamp(duration_seconds), source_sha256=source_sha256,
        page=1, timeout_seconds=timeout_seconds, max_edge=max_edge,
    )


def frame_count_for(
    duration_seconds: float | None, requested: int, *, duration_aware: bool = True
) -> int:
    """How many frames to sample, clamped and (optionally) duration-aware.

    Step 1 of the algorithm documented in this module's docstring. Pure, so
    the decision can be tested without ffmpeg installed at all.
    """
    count = max(1, min(int(requested), MAX_VIDEO_FRAME_COUNT))
    if not duration_aware:
        return count
    if not duration_seconds or duration_seconds <= 0:
        # Unknown duration: treat it as long. Sampling a short clip several
        # times costs a few redundant calls; refusing to sample costs the
        # only evidence this file was ever going to produce.
        return count
    if duration_seconds < VERY_SHORT_VIDEO_SECONDS:
        return 1
    if duration_seconds < SHORT_VIDEO_SECONDS:
        return min(count, SHORT_VIDEO_FRAMES)
    return count


def average_hash(png_bytes: bytes) -> int | None:
    """A 64-bit average hash of a PNG, or None if it cannot be decoded.

    Greyscale, resized to 8x8, one bit per pixel: set when the pixel is at or
    above the mean of the 64. Plain Pillow, no NumPy (a banned import here),
    no adapted third-party code. Deterministic for identical bytes, and
    stable under the small changes a video encoder makes between adjacent
    frames -- which is the entire point.
    """
    try:
        from PIL import Image  # deferred: keeps this module importable without Pillow

        with Image.open(io.BytesIO(png_bytes)) as decoded:
            small = decoded.convert("L").resize((HASH_EDGE, HASH_EDGE), Image.BILINEAR)
            # `tobytes()` rather than `getdata()`: one byte per pixel for
            # mode "L", in row order, and not deprecated in Pillow 14.
            pixels = list(small.tobytes())
    except Exception:  # noqa: BLE001 - an undecodable frame simply has no hash
        return None
    if len(pixels) != HASH_BITS:
        return None
    mean = sum(pixels) / float(HASH_BITS)
    value = 0
    for index, pixel in enumerate(pixels):
        if pixel >= mean:
            value |= 1 << index
    return value


def _hamming(left: int, right: int) -> int:
    return bin(left ^ right).count("1")


def deduplicate_frames(
    frames: list[RasterImage], *, threshold: int = DEDUPE_HAMMING_THRESHOLD
) -> tuple[list[RasterImage], int]:
    """Collapse near-identical frames to one representative each.

    Step 3 of the algorithm in this module's docstring. Returns the kept
    frames in sampling order plus how many were dropped, so the caller can
    record the count as evidence rather than silently perceiving less than it
    was asked to.

    A frame whose pixels cannot be hashed at all is **kept**, not dropped:
    "we could not compare this" must never become "this was a duplicate".
    """
    kept: list[RasterImage] = []
    kept_hashes: list[int] = []
    dropped = 0
    for frame in frames:
        digest = average_hash(frame.png_bytes)
        if digest is None:
            kept.append(frame)
            continue
        if any(_hamming(digest, existing) <= threshold for existing in kept_hashes):
            dropped += 1
            continue
        kept.append(frame)
        kept_hashes.append(digest)
    return kept, dropped


def extract_frames(
    path: Path,
    *,
    source_sha256: str,
    count: int,
    duration_seconds: float | None,
    timeout_seconds: int,
    max_edge: int = MAX_FRAME_EDGE_PIXELS,
    dedupe: bool = True,
    duration_aware: bool = True,
) -> tuple[list[RasterImage], tuple[EvidenceWarning, ...]]:
    """The frames a video is perceived from, evenly spaced and de-duplicated.

    See this module's docstring for the exact three-step algorithm (how many,
    where, which survive). `page` on each returned frame is its 1-based index
    in the *original* sampling order, which is what lets `extract.py`'s
    existing per-page OCR loop handle a video with zero changes -- and what
    keeps "this frame came from a third of the way in" recoverable after
    deduplication has removed its neighbours.

    One frame that fails to extract is skipped, with its own warning, rather
    than aborting every other frame -- the same degrade-per-page posture
    `raster.py::rasterize_pdf_pages` already uses. Never raises.
    """
    if not available():
        return [], (
            EvidenceWarning(code=WARN_BACKEND_UNAVAILABLE, stage=_STAGE, detail="ffmpeg binary not found on PATH"),
        )

    resolved_count = frame_count_for(duration_seconds, count, duration_aware=duration_aware)
    frames: list[RasterImage] = []
    warnings: list[EvidenceWarning] = []
    if resolved_count < max(1, min(int(count), MAX_VIDEO_FRAME_COUNT)):
        warnings.append(
            EvidenceWarning(
                code=WARN_SKIPPED_BY_POLICY,
                stage=_STAGE,
                detail=f"clip is {duration_seconds:.1f}s long: sampled {resolved_count} frame(s) "
                       f"instead of the requested {count}",
            )
        )
    for index, timestamp in enumerate(_sample_timestamps(duration_seconds, resolved_count)):
        frame, frame_warnings = _extract_one_frame(
            path, timestamp_seconds=timestamp, source_sha256=source_sha256,
            page=index + 1, timeout_seconds=timeout_seconds, max_edge=max_edge,
        )
        warnings.extend(frame_warnings)
        if frame is not None:
            frames.append(frame)

    if dedupe and len(frames) > 1:
        frames, dropped = deduplicate_frames(frames)
        if dropped:
            warnings.append(
                EvidenceWarning(
                    code=WARN_DEDUPLICATED,
                    stage=_STAGE,
                    detail=f"{dropped} near-identical sampled frame(s) collapsed into an earlier one; "
                           f"{len(frames)} distinct frame(s) perceived",
                )
            )

    return frames, tuple(warnings)
