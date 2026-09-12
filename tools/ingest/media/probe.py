"""Deterministic media facts (docs/13, `media_metadata`).

This module answers "what does the file *measurably* say about itself?" and
nothing else. No model inference, no heuristic naming, no content summary --
those belong to the proposal layer, which is stored separately precisely so a
human can tell a measured fact from a machine's opinion. The one heuristic
here (`is_probably_screenshot`) is named as such and its limits are stated at
the point it is computed.

Two invariants govern everything below:

1. **Read-only.** A probe opens the file and never writes to it, never writes
   beside it, and never asks a library to expand it onto disk. Ingestion runs
   against a user's only copy of a file; a metadata read must not be able to
   damage it.
2. **Never raises.** `probe_file` is called in a loop over a whole volume of
   unknown, possibly truncated, possibly hostile files. A single bad file must
   degrade to an `errors` entry, not abort the run. Optional tooling (ffprobe,
   pypdf) is detected rather than required, so a machine without it records
   `probe_unavailable` instead of failing.

Facts are omitted entirely when they cannot be measured. An absent key means
"not known"; it never means zero, empty, or a default -- a downstream namer
that cannot distinguish those two would invent history.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Any

try:
    import pillow_heif
    from PIL import Image, UnidentifiedImageError

    # Registering here (as well as in media/heic.py) keeps the probe usable
    # standalone: importing the converter just to read a HEIC would be a
    # surprising dependency between two otherwise independent modules.
    pillow_heif.register_heif_opener()
    _PIL_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - only when pillow is missing
    _PIL_IMPORT_ERROR = exc

PROBE_NAME = "ingest-probe"
# Bump when the *shape* of `facts` changes, so previously stored rows can be
# recognised as stale and re-probed rather than silently mixed with new ones.
PROBE_VERSION = "1"

MEDIA_IMAGE = "image"
MEDIA_VIDEO = "video"
MEDIA_AUDIO = "audio"
MEDIA_DOCUMENT = "document"
MEDIA_ARCHIVE = "archive"
MEDIA_INSTALLER = "installer"
MEDIA_TEXT = "text"
MEDIA_OTHER = "other"

FFPROBE_TIMEOUT_SECONDS = 30

# A ZIP central directory can claim millions of entries in a few kilobytes.
# Listing is cheap, but the per-entry bookkeeping below is not, so it is
# capped and the truncation is reported rather than hidden.
MAX_ZIP_ENTRIES = 5000
MAX_TOP_LEVEL_ENTRIES = 20

# Above this pixel count a decode is treated as a decompression bomb and
# skipped. 80MP is comfortably larger than any real camera or scan this
# pipeline expects, and far below what would exhaust memory on the host.
MAX_DECODE_PIXELS = 80_000_000

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_TAG_IMAGE_DESCRIPTION = 0x010E
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_ORIENTATION = 0x0112
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME_DIGITIZED = 0x9004
_TAG_USER_COMMENT = 0x9286
_TAG_LENS_MODEL = 0xA434

_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"

IMAGE_SUFFIXES = frozenset(
    {
        ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".tif", ".tiff",
        ".webp", ".bmp", ".avif", ".jfif", ".jpe", ".ico", ".psd", ".svg",
        # Raw sensor formats: still images, even though no generic decoder
        # here can read their pixels. Classification must not depend on
        # whether a decoder happens to be installed.
        ".nef", ".cr2", ".cr3", ".arw", ".dng", ".raf", ".orf", ".rw2",
        ".srw", ".pef", ".x3f", ".3fr", ".erf", ".kdc", ".mrw",
    }
)

VIDEO_SUFFIXES = frozenset(
    {
        ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg",
        ".mts", ".m2ts", ".wmv", ".flv", ".3gp", ".3g2", ".mxf", ".vob",
        ".ogv", ".m2v", ".divx", ".braw", ".r3d",
    }
)

AUDIO_SUFFIXES = frozenset(
    {
        ".mp3", ".wav", ".aiff", ".aif", ".aifc", ".m4a", ".flac", ".aac",
        ".ogg", ".opus", ".wma", ".caf", ".amr", ".mid", ".midi", ".m4b",
    }
)

DOCUMENT_SUFFIXES = frozenset(
    {
        ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pages",
        ".numbers", ".key", ".rtf", ".odt", ".ods", ".odp", ".epub", ".mobi",
        ".djvu", ".rtfd",
    }
)

ARCHIVE_SUFFIXES = frozenset(
    {
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".tbz2", ".xz", ".txz",
        ".7z", ".rar", ".zst", ".lz4", ".cab", ".sit", ".sitx",
    }
)

INSTALLER_SUFFIXES = frozenset(
    {
        ".dmg", ".pkg", ".mpkg", ".iso", ".exe", ".msi", ".deb", ".rpm",
        ".appimage", ".app", ".apk", ".snap", ".flatpak", ".img", ".vhd",
    }
)

TEXT_SUFFIXES = frozenset(
    {
        ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
        ".xml", ".html", ".htm", ".log", ".rst", ".tex", ".ini", ".cfg",
        ".conf", ".toml", ".env", ".plist", ".srt", ".vtt", ".ics",
        # Source code. Grouped with text on purpose: for this pipeline the
        # meaningful distinction is "human-readable bytes" vs "opaque blob",
        # not which language the bytes are written in.
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".c",
        ".h", ".cc", ".cpp", ".hpp", ".cs", ".java", ".kt", ".kts", ".go",
        ".rs", ".rb", ".php", ".swift", ".m", ".mm", ".scala", ".clj", ".ex",
        ".exs", ".erl", ".hs", ".lua", ".pl", ".pm", ".r", ".jl", ".dart",
        ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".sql",
        ".css", ".scss", ".sass", ".less", ".vue", ".svelte", ".graphql",
        ".proto", ".tf", ".tfvars", ".gradle", ".groovy", ".makefile", ".mk",
        ".dockerfile", ".patch", ".diff", ".ipynb",
    }
)

_MIME_PREFIX_KINDS = (
    ("image/", MEDIA_IMAGE),
    ("video/", MEDIA_VIDEO),
    ("audio/", MEDIA_AUDIO),
    ("text/", MEDIA_TEXT),
)

_MIME_EXACT_KINDS = {
    "application/pdf": MEDIA_DOCUMENT,
    "application/epub+zip": MEDIA_DOCUMENT,
    "application/msword": MEDIA_DOCUMENT,
    "application/rtf": MEDIA_DOCUMENT,
    "application/vnd.ms-excel": MEDIA_DOCUMENT,
    "application/vnd.ms-powerpoint": MEDIA_DOCUMENT,
    "application/zip": MEDIA_ARCHIVE,
    "application/gzip": MEDIA_ARCHIVE,
    "application/x-tar": MEDIA_ARCHIVE,
    "application/x-7z-compressed": MEDIA_ARCHIVE,
    "application/vnd.rar": MEDIA_ARCHIVE,
    "application/x-apple-diskimage": MEDIA_INSTALLER,
    "application/vnd.debian.binary-package": MEDIA_INSTALLER,
    "application/x-msdownload": MEDIA_INSTALLER,
    "application/json": MEDIA_TEXT,
    "application/xml": MEDIA_TEXT,
    "application/x-yaml": MEDIA_TEXT,
    "application/javascript": MEDIA_TEXT,
}

_SUFFIX_KINDS: tuple[tuple[frozenset[str], str], ...] = (
    (IMAGE_SUFFIXES, MEDIA_IMAGE),
    (VIDEO_SUFFIXES, MEDIA_VIDEO),
    (AUDIO_SUFFIXES, MEDIA_AUDIO),
    (DOCUMENT_SUFFIXES, MEDIA_DOCUMENT),
    (ARCHIVE_SUFFIXES, MEDIA_ARCHIVE),
    (INSTALLER_SUFFIXES, MEDIA_INSTALLER),
    (TEXT_SUFFIXES, MEDIA_TEXT),
)


@dataclass(frozen=True)
class MediaFacts:
    media_kind: str
    mime: str | None
    facts: dict[str, Any]
    probe: str
    probe_version: str
    errors: tuple[str, ...] = ()


# -- optional tooling detection --------------------------------------------
#
# Both are cached: a scan probes thousands of files and neither answer can
# change within a run. Callers (and tests) patch these names rather than the
# environment, which is why the probe bodies look them up through the module
# namespace instead of binding them locally.


@lru_cache(maxsize=1)
def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


@lru_cache(maxsize=1)
def pypdf_available() -> bool:
    try:
        return find_spec("pypdf") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def classify_media_kind(path: Path, mime: str | None = None) -> str:
    """Bucket a file by extension, falling back to a caller-supplied MIME type.

    Extension wins over MIME deliberately: the extension is a property of the
    file itself and is identical on every machine, whereas MIME strings arrive
    from whatever sniffed them. A stable classification matters more here than
    a marginally more accurate one, because it is a primary key for how the
    file gets handled for the rest of its life.
    """
    suffix = path.suffix.lower()
    for suffixes, kind in _SUFFIX_KINDS:
        if suffix in suffixes:
            return kind

    if mime:
        normalized = mime.split(";", 1)[0].strip().lower()
        exact = _MIME_EXACT_KINDS.get(normalized)
        if exact is not None:
            return exact
        for prefix, kind in _MIME_PREFIX_KINDS:
            if normalized.startswith(prefix):
                return kind

    return MEDIA_OTHER


def probe_file(path: Path, *, mime: str | None = None, enable_video: bool = True) -> MediaFacts:
    """Measure `path` without modifying it, returning whatever could be read.

    Every failure mode -- unreadable file, unknown type, corrupt container,
    missing optional tool -- resolves to an `errors` entry plus whatever facts
    survived. Nothing in this function propagates an exception to the caller.
    """
    errors: list[str] = []
    facts: dict[str, Any] = {"extension": path.suffix.lower()}

    try:
        facts["size_bytes"] = path.stat().st_size
    except OSError as exc:
        # Size is omitted rather than zeroed: "unreadable" and "empty" lead to
        # very different decisions downstream.
        errors.append(f"stat failed: {exc}")

    media_kind = classify_media_kind(path, mime)

    try:
        if media_kind == MEDIA_IMAGE:
            _probe_image(path, facts, errors)
        elif media_kind in (MEDIA_VIDEO, MEDIA_AUDIO):
            if enable_video:
                _probe_av(path, facts, errors)
            else:
                # Recorded explicitly so a consumer can tell "the caller opted
                # out" apart from "probed and found nothing".
                facts["probe_skipped"] = "ffprobe"
        elif media_kind == MEDIA_DOCUMENT and facts["extension"] == ".pdf":
            _probe_pdf(path, facts, errors)
        elif media_kind == MEDIA_ARCHIVE and facts["extension"] == ".zip":
            _probe_zip(path, facts, errors)
    except Exception as exc:  # noqa: BLE001 - see module docstring invariant 2
        errors.append(f"{media_kind} probe failed: {type(exc).__name__}: {exc}")

    return MediaFacts(
        media_kind=media_kind,
        mime=mime,
        facts=dict(sorted(facts.items())),
        probe=PROBE_NAME,
        probe_version=PROBE_VERSION,
        errors=tuple(errors),
    )


# -- images -----------------------------------------------------------------


def _probe_image(path: Path, facts: dict[str, Any], errors: list[str]) -> None:
    if _PIL_IMPORT_ERROR is not None:  # pragma: no cover - pillow is required
        facts["probe_unavailable"] = "pillow"
        errors.append(f"pillow not installed: {_PIL_IMPORT_ERROR}")
        return

    try:
        with Image.open(path) as im:
            facts["format"] = im.format
            facts["mode"] = im.mode
            width, height = im.size
            facts["width"] = width
            facts["height"] = height
            if height:
                facts["aspect_ratio"] = round(width / height, 4)
            facts["has_alpha"] = _has_alpha(im)

            icc_profile = im.info.get("icc_profile")
            facts["icc_profile_present"] = bool(icc_profile)
            if icc_profile:
                facts["icc_profile_bytes"] = len(icc_profile)

            _read_exif(im, facts, errors)

            # Header parsing alone will happily describe a file whose pixel
            # data was never fully written (an interrupted copy, a truncated
            # download). Forcing a decode is the only way to tell, and the
            # answer changes whether a derivative can be generated later.
            if width * height <= MAX_DECODE_PIXELS:
                try:
                    im.load()
                except Exception as exc:  # noqa: BLE001 - any decoder failure
                    errors.append(f"image pixel data unreadable: {type(exc).__name__}: {exc}")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        errors.append(f"image probe failed: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - e.g. DecompressionBombError
        errors.append(f"image probe failed: {type(exc).__name__}: {exc}")

    _apply_screenshot_heuristic(facts)


def _has_alpha(im: Any) -> bool:
    if im.mode in {"RGBA", "LA", "PA", "RGBa", "La"}:
        return True
    # Palette images carry alpha out of band, via a transparency entry.
    return "transparency" in im.info


def _read_exif(im: Any, facts: dict[str, Any], errors: list[str]) -> None:
    try:
        exif = im.getexif()
    except Exception as exc:  # noqa: BLE001 - malformed EXIF blocks vary wildly
        facts["exif_present"] = False
        facts["gps_present"] = False
        errors.append(f"exif read failed: {type(exc).__name__}: {exc}")
        return

    tags: dict[int, Any] = {}
    try:
        tags.update(dict(exif))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"exif ifd0 unreadable: {type(exc).__name__}: {exc}")

    # Writers disagree about where the timestamp and lens tags live: the
    # standard says the Exif sub-IFD, but plenty of tools (including Pillow
    # itself, when handed a flat Exif mapping) emit them in IFD0. Merging with
    # the sub-IFD last means the standard location wins when both are present.
    try:
        tags.update(dict(exif.get_ifd(_EXIF_IFD)))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"exif sub-ifd unreadable: {type(exc).__name__}: {exc}")

    facts["exif_present"] = bool(tags)

    try:
        gps = dict(exif.get_ifd(_GPS_IFD))
    except Exception:  # noqa: BLE001
        gps = {}
    # Presence only. Coordinates are location data about a person; they are
    # not recorded here, and nothing in this pipeline needs them.
    facts["gps_present"] = bool(gps) or _GPS_IFD in tags

    orientation = _as_int(tags.get(_TAG_ORIENTATION))
    if orientation is not None:
        facts["orientation"] = orientation

    for key, tag in (
        ("camera_make", _TAG_MAKE),
        ("camera_model", _TAG_MODEL),
        ("lens_model", _TAG_LENS_MODEL),
        ("image_description", _TAG_IMAGE_DESCRIPTION),
    ):
        value = _clean_str(tags.get(tag))
        if value is not None:
            facts[key] = value

    comment = _decode_user_comment(tags.get(_TAG_USER_COMMENT))
    if comment is not None:
        facts["exif_user_comment"] = comment

    raw_timestamp = _clean_str(tags.get(_TAG_DATETIME_ORIGINAL)) or _clean_str(
        tags.get(_TAG_DATETIME_DIGITIZED)
    )
    if raw_timestamp:
        parsed = _parse_exif_datetime(raw_timestamp)
        if parsed is not None:
            facts["capture_timestamp"] = parsed
        else:
            # An unparseable timestamp is reported, never repaired. Guessing a
            # date here would put a fabricated capture time into a filename.
            errors.append(f"unparseable exif timestamp: {raw_timestamp!r}")


def _apply_screenshot_heuristic(facts: dict[str, Any]) -> None:
    """Flag images that look like screen captures.

    HEURISTIC, not a measurement: a PNG with no camera identification and no
    capture timestamp is usually a screenshot, because cameras write EXIF and
    screen-capture tools generally do not. It has real false positives --
    exported graphics, diagrams, PNGs stripped of metadata by a messaging app
    -- and real false negatives (macOS screenshots saved as JPEG, screenshots
    that passed through an editor that added EXIF). It is stored as a hint for
    a later proposal step, and must never be treated as proof of origin.
    """
    if "format" not in facts:
        return
    facts["is_probably_screenshot"] = (
        facts.get("format") == "PNG"
        and "camera_make" not in facts
        and "camera_model" not in facts
        and "capture_timestamp" not in facts
    )


# -- video / audio ----------------------------------------------------------


def _probe_av(path: Path, facts: dict[str, Any], errors: list[str]) -> None:
    """Read container and stream facts via ffprobe.

    ffprobe is treated as optional tooling, not a dependency: a machine
    without ffmpeg installed should still catalogue its videos (size, kind,
    extension) rather than fail the file outright.
    """
    if not ffprobe_available():
        facts["probe_unavailable"] = "ffprobe"
        errors.append("ffprobe not found on PATH")
        return

    command = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        facts["probe_unavailable"] = "ffprobe"
        errors.append(f"ffprobe timed out after {FFPROBE_TIMEOUT_SECONDS}s")
        return
    except OSError as exc:
        facts["probe_unavailable"] = "ffprobe"
        errors.append(f"ffprobe could not be executed: {exc}")
        return

    if completed.returncode != 0:
        facts["probe_unavailable"] = "ffprobe"
        errors.append(f"ffprobe exited {completed.returncode}: {_first_line(completed.stderr)}")
        return

    try:
        payload = json.loads(completed.stdout.decode("utf-8", "replace") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        facts["probe_unavailable"] = "ffprobe"
        errors.append(f"ffprobe output was not valid json: {exc}")
        return

    container = payload.get("format") or {}
    streams = payload.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    duration = _as_float(container.get("duration"))
    if duration is not None:
        facts["duration_seconds"] = duration

    bit_rate = _as_int(container.get("bit_rate"))
    if bit_rate is not None:
        facts["bit_rate"] = bit_rate

    container_format = _clean_str(container.get("format_name"))
    if container_format:
        facts["container_format"] = container_format

    creation_time = _clean_str((container.get("tags") or {}).get("creation_time"))
    if creation_time:
        facts["capture_timestamp"] = creation_time

    facts["audio_present"] = bool(audio_streams)
    if audio_streams:
        audio_codec = _clean_str(audio_streams[0].get("codec_name"))
        if audio_codec:
            facts["audio_codec"] = audio_codec

    if video_streams:
        stream = video_streams[0]
        video_codec = _clean_str(stream.get("codec_name"))
        if video_codec:
            facts["video_codec"] = video_codec

        width = _as_int(stream.get("width"))
        height = _as_int(stream.get("height"))
        if width is not None:
            facts["width"] = width
        if height is not None:
            facts["height"] = height
        if width and height:
            facts["aspect_ratio"] = round(width / height, 4)

        frame_rate = _parse_rational(stream.get("avg_frame_rate"))
        if frame_rate is not None:
            facts["frame_rate"] = frame_rate

        if "duration_seconds" not in facts:
            stream_duration = _as_float(stream.get("duration"))
            if stream_duration is not None:
                facts["duration_seconds"] = stream_duration


# -- documents --------------------------------------------------------------


def _probe_pdf(path: Path, facts: dict[str, Any], errors: list[str]) -> None:
    """Read PDF structure facts via pypdf, if it is installed.

    `text_available` is a boolean and stays a boolean. Extracted document text
    is deliberately never stored: the catalog would become a searchable copy
    of every contract, medical letter and tax return on the disk, which is a
    far larger disclosure risk than anything this feature is worth.
    """
    if not pypdf_available():
        facts["probe_unavailable"] = "pypdf"
        errors.append("pypdf not installed; pdf facts skipped")
        return

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - find_spec said otherwise
        facts["probe_unavailable"] = "pypdf"
        errors.append(f"pypdf import failed: {exc}")
        return

    try:
        with open(path, "rb") as handle:
            reader = PdfReader(handle)
            pages = reader.pages
            facts["page_count"] = len(pages)

            metadata = reader.metadata or {}
            title = _clean_str(metadata.get("/Title"))
            if title:
                facts["metadata_title"] = title
            author = _clean_str(metadata.get("/Author"))
            if author:
                facts["metadata_author"] = author

            if len(pages):
                first = pages[0]
                try:
                    box = first.mediabox
                    facts["width_points"] = round(float(box.width), 2)
                    facts["height_points"] = round(float(box.height), 2)
                except Exception as exc:  # noqa: BLE001 - malformed page tree
                    errors.append(f"pdf page geometry unreadable: {type(exc).__name__}: {exc}")

                try:
                    # The extracted text is tested for emptiness and then
                    # dropped on the floor -- see the docstring.
                    facts["text_available"] = bool((first.extract_text() or "").strip())
                except Exception as exc:  # noqa: BLE001 - extraction is fragile
                    errors.append(f"pdf text extraction failed: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - encrypted/corrupt pdfs raise widely
        errors.append(f"pdf probe failed: {type(exc).__name__}: {exc}")


# -- archives ---------------------------------------------------------------


def _probe_zip(path: Path, facts: dict[str, Any], errors: list[str]) -> None:
    """Describe a ZIP from its central directory only.

    `infolist()` reads the directory that the archive declares about itself;
    it never decompresses anything. That restriction is the whole point: a
    zip bomb is a handful of kilobytes that expands to petabytes, so this
    module never calls `.read()`, `.extract()`, `.extractall()` or `.open()`.
    The sizes below are what the archive *claims*, which is exactly the fact
    worth recording -- and is also why they must not be trusted as a disk
    budget by anything downstream.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        errors.append(f"zip probe failed: bad zip file: {exc}")
        return
    except OSError as exc:
        errors.append(f"zip probe failed: {type(exc).__name__}: {exc}")
        return

    facts["entry_count"] = len(infos)
    if len(infos) > MAX_ZIP_ENTRIES:
        errors.append(
            f"zip listing capped at {MAX_ZIP_ENTRIES} of {len(infos)} entries; "
            "derived facts cover the capped subset only"
        )
        infos = infos[:MAX_ZIP_ENTRIES]

    uncompressed_size = 0
    top_level: set[str] = set()
    extensions: Counter[str] = Counter()

    for info in infos:
        uncompressed_size += info.file_size
        # Archive member names are attacker-controlled; they are only ever
        # split and compared as strings here, never joined to a filesystem path.
        name = info.filename.replace("\\", "/").lstrip("/")
        if not name:
            continue
        head = name.split("/", 1)[0]
        if head and head not in {".", ".."}:
            top_level.add(head)
        if info.is_dir():
            continue
        leaf = name.rsplit("/", 1)[-1]
        if "." in leaf[1:]:
            extensions["." + leaf.rsplit(".", 1)[-1].lower()] += 1

    facts["uncompressed_size"] = uncompressed_size
    if top_level:
        facts["top_level_entries"] = sorted(top_level)[:MAX_TOP_LEVEL_ENTRIES]
    if extensions:
        # Ties break on the extension name so the same archive always yields
        # the same answer, whatever order the directory happened to be in.
        facts["dominant_extension"] = min(extensions.items(), key=lambda item: (-item[1], item[0]))[0]


# -- thumbnails -------------------------------------------------------------


def thumbnail_png(path: Path, *, max_edge: int = 512) -> bytes | None:
    """Return a downscaled PNG of an image as bytes, or None if unreadable.

    Returning bytes rather than a path is the point: a later vision-model step
    should receive the pixels it is meant to look at, with no filesystem
    handle to the user's original and no temp file to leak or collide. Nothing
    is written to disk here.
    """
    if _PIL_IMPORT_ERROR is not None:  # pragma: no cover - pillow is required
        return None

    try:
        with Image.open(path) as im:
            width, height = im.size
            # Decompression-bomb guard: a small file can declare an enormous
            # canvas, and decoding it would be the attack. Refuse rather than
            # let one hostile image take down a whole-volume scan.
            if width * height > MAX_DECODE_PIXELS:
                return None

            im.load()
            # PNG cannot store CMYK/YCbCr; converting keeps the output valid
            # for every input format this pipeline sees.
            source = im if im.mode in {"RGB", "RGBA", "L", "LA"} else im.convert("RGB")
            thumb = source.copy()
            # `thumbnail` preserves aspect ratio and only ever shrinks, so a
            # small input comes back untouched instead of being upscaled.
            thumb.thumbnail((max_edge, max_edge), Image.LANCZOS)

            buffer = io.BytesIO()
            thumb.save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - callers get None, never an exception
        return None


# -- value coercion ---------------------------------------------------------
#
# EXIF and ffprobe both return values with inconsistent types (bytes for a
# string tag, a string for a number, IFDRational for an int). Each helper
# returns None rather than a fallback value, so a bad reading is dropped
# instead of becoming a plausible-looking fact.


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\x00", "").strip()
    return cleaned or None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # ffprobe reports "N/A" as NaN for some containers; NaN is not JSON.
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _parse_rational(value: Any) -> float | None:
    """Turn ffprobe's "30000/1001" frame-rate notation into a float."""
    text = _clean_str(value)
    if not text or "/" not in text:
        return _as_float(text)
    numerator, _, denominator = text.partition("/")
    num = _as_float(numerator)
    den = _as_float(denominator)
    if num is None or not den:
        return None
    return round(num / den, 6)


def _parse_exif_datetime(value: str) -> str | None:
    """Convert EXIF "YYYY:MM:DD HH:MM:SS" to ISO 8601, or None.

    No timezone is attached. EXIF DateTimeOriginal is local wall-clock time
    with no offset, and inventing UTC for it would shift capture times by
    hours for anyone who travels.
    """
    text = value.strip()
    # Some writers append subsecond or timezone data; the leading 19
    # characters are the part the format actually defines.
    candidate = text[:19]
    try:
        return datetime.strptime(candidate, _EXIF_DATETIME_FORMAT).isoformat()
    except ValueError:
        return None


def _decode_user_comment(value: Any) -> str | None:
    """Decode the EXIF UserComment tag's 8-byte character-code prefix."""
    if value is None:
        return None
    if isinstance(value, str):
        return _clean_str(value)
    if not isinstance(value, bytes):
        return None
    if len(value) >= 8:
        prefix, body = value[:8], value[8:]
        if prefix.startswith(b"ASCII"):
            return _clean_str(body.decode("ascii", "replace"))
        if prefix.startswith(b"UNICODE"):
            return _clean_str(body.decode("utf-16-be", "replace"))
        if prefix.startswith(b"JIS"):
            return _clean_str(body.decode("shift_jis", "replace"))
        if prefix == b"\x00" * 8:
            return _clean_str(body.decode("utf-8", "replace"))
    return _clean_str(value.decode("utf-8", "replace"))


def _first_line(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    return text.strip().splitlines()[0] if text.strip() else ""


def screenshot_location_hint() -> str | None:
    """Where macOS is currently saving screenshots. Advisory only.

    Reported by `organize doctor` so the owner can paste it into the config
    deliberately. Nothing in the pipeline consumes it: a source path the tool
    inferred is a source path the tool should not be walking (D-016), and on a
    default install this value is `~/Desktop`.
    """
    defaults = shutil.which("defaults")
    if defaults is None:
        return None
    try:
        proc = subprocess.run(
            [defaults, "read", "com.apple.screencapture", "location"],
            capture_output=True, check=False, timeout=10, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        # Unset means the macOS default, which is the Desktop. Said explicitly
        # so the owner sees what enabling this source would actually cover.
        return f"{Path.home() / 'Desktop'}  (macOS default; not set explicitly)"
    location = proc.stdout.strip()
    if not location:
        return None
    return str(Path(location).expanduser())
