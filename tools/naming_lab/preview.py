"""Best-effort file previews for the Review screen.

Deliberately modest (spec section 8: "do not spend excessive engineering
effort building perfect previews for every obscure format in V1"). Image,
PDF-first-page, and video-frame previews reuse the evidence package's own
rendering (`thumbnail_png`, `rasterize_pdf_pages`, `backends.ffmpeg_video`)
rather than a new renderer; everything else falls back to metadata/evidence
text plus a "reveal in Finder" action.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tools.ingest.evidence.backends import ffmpeg_video
from tools.ingest.evidence.raster import rasterize_pdf_pages
from tools.ingest.evidence.types import EvidenceSource
from tools.ingest.media.probe import thumbnail_png


def image_preview_png(path: Path) -> bytes | None:
    try:
        return thumbnail_png(path)
    except Exception:  # noqa: BLE001 -- a preview must never crash the review screen
        return None


def pdf_first_page_png(
    path: Path, *, sha256: str, size: int, dpi: int = 150, max_pixels: int = 20_000_000,
) -> bytes | None:
    source = EvidenceSource(path=path, sha256=sha256, mime="application/pdf", media_kind="document", size=size)
    pages, _warnings = rasterize_pdf_pages(source, dpi=dpi, max_pages=1, max_pixels=max_pixels)
    return pages[0].png_bytes if pages else None


def video_frame_preview_png(path: Path, *, sha256: str, duration_seconds: float | None) -> bytes | None:
    """The same representative frame the vision backend would be shown --
    lets a reviewer see exactly what the model saw, not a different frame."""
    raster, _warnings = ffmpeg_video.extract_frame(
        path, source_sha256=sha256, duration_seconds=duration_seconds, timeout_seconds=15,
    )
    return raster.png_bytes if raster else None


def text_excerpt(text: str, *, max_chars: int = 2000) -> str:
    return text[:max_chars]


def can_reveal_in_finder() -> bool:
    return sys.platform == "darwin"


def reveal_in_finder(path: Path) -> bool:
    """Opens `path` in Finder via macOS's `open`. No-op (returns False) elsewhere.

    Invoked with an argv list, never `shell=True` -- `path` is a local
    filesystem path the Lab itself discovered, not untrusted text.
    """
    if not can_reveal_in_finder():
        return False
    try:
        subprocess.run(["open", str(path)], check=False, timeout=10)
        return True
    except OSError:
        return False
