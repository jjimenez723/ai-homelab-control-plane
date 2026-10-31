"""Deterministic photo/video classification for removable-media ingest.

Classifies a local file from its extension, an optional provider MIME type,
and its leading file signature -- never by decoding pixels. Anything unknown,
temporary, or conflicting fails closed to ``review``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PHOTO_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp", ".tif", ".tiff"}
)
RAW_EXTENSIONS = frozenset({".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".dng", ".raw"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".m2ts", ".mxf", ".3gp"})
TEMPORARY_SUFFIXES = frozenset({".crdownload", ".download", ".part", ".partial", ".tmp"})

RAW_MIME_TYPES = frozenset(
    {
        "image/x-canon-cr2",
        "image/x-canon-cr3",
        "image/x-nikon-nef",
        "image/x-sony-arw",
        "image/x-adobe-dng",
        "image/x-panasonic-rw2",
        "image/x-fuji-raf",
    }
)
GENERIC_MIME_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})


@dataclass(frozen=True)
class MediaClassification:
    kind: str
    source: str
    reason: str
    provider_mime: str | None = None

    @property
    def route_type(self) -> str | None:
        if self.kind in {"photo", "photo/raw"}:
            return "Pics"
        if self.kind == "video":
            return "Vids"
        return None


def _mime_kind(provider_mime: str | None) -> str | None:
    if not provider_mime:
        return None
    mime = provider_mime.strip().casefold().split(";", 1)[0]
    if mime in RAW_MIME_TYPES:
        return "photo/raw"
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime in GENERIC_MIME_TYPES:
        return None
    return "review"


def _extension_kind(path: Path) -> str | None:
    suffix = path.suffix.casefold()
    if suffix in RAW_EXTENSIONS:
        return "photo/raw"
    if suffix in PHOTO_EXTENSIONS:
        return "photo"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def _signature_kind(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return None
    if header.startswith(b"\xff\xd8\xff"):
        return "photo"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "photo"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "photo"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "photo"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12].lower()
        if brand in {b"crx ", b"cr3 ", b"heic", b"heix", b"hevc", b"hevx", b"avif"}:
            return "photo/raw" if brand in {b"crx ", b"cr3 "} else "photo"
        return "video"
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return "video"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video"
    if header.startswith(b"\x47"):
        return "video"
    return None


def _broad_kind(kind: str) -> str:
    return "photo" if kind in {"photo", "photo/raw"} else kind


def classify_media(path: Path, *, provider_mime: str | None = None) -> MediaClassification:
    """Classify without visual decoding; unknown/conflicting inputs review."""

    if path.suffix.casefold() in TEMPORARY_SUFFIXES:
        return MediaClassification("review", "guard", "temporary or incomplete file", provider_mime)
    try:
        if not path.is_file():
            return MediaClassification("review", "guard", "not a regular file", provider_mime)
        if path.stat().st_size == 0:
            return MediaClassification("review", "guard", "empty file", provider_mime)
    except OSError as exc:
        return MediaClassification("review", "guard", f"stat failed: {exc}", provider_mime)

    provider_kind = _mime_kind(provider_mime)
    if provider_kind == "review":
        return MediaClassification("review", "provider-mime", "unsupported provider MIME type", provider_mime)

    extension_kind = _extension_kind(path)
    signature_kind = _signature_kind(path)

    if provider_kind is not None:
        if signature_kind is None:
            return MediaClassification("review", "signature", "provider MIME has no approved file signature", provider_mime)
        if _broad_kind(provider_kind) != _broad_kind(signature_kind):
            return MediaClassification("review", "conflict", "provider MIME conflicts with file signature", provider_mime)
        if extension_kind is not None and _broad_kind(provider_kind) != _broad_kind(extension_kind):
            return MediaClassification("review", "conflict", "provider MIME conflicts with extension", provider_mime)
        return MediaClassification(provider_kind, "provider-mime", "provider MIME accepted", provider_mime)

    if extension_kind is not None:
        if signature_kind is None:
            return MediaClassification("review", "signature", "known extension has no approved file signature", provider_mime)
        if _broad_kind(extension_kind) != _broad_kind(signature_kind):
            return MediaClassification("review", "conflict", "extension conflicts with file signature", provider_mime)
        return MediaClassification(extension_kind, "extension", "approved extension and signature", provider_mime)

    if signature_kind is not None:
        return MediaClassification(signature_kind, "signature", "approved file signature", provider_mime)
    return MediaClassification("review", "fallback", "unknown or unsupported media", provider_mime)
