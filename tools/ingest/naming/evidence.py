"""What we know about a file, as plain data.

`NamingEvidence` is the entire input a proposer receives. It carries no `Path`,
no file object, no callable -- only strings, numbers, and one optional PNG
byte-string. That is deliberate and structural: a proposer, including a future
model-backed one, has nothing to mutate the filesystem *with*. Invariant 3 is
enforced by the shape of the data, not by asking politely.

Gathering the evidence is deterministic, read-only, and happens in trusted code
before any proposer is called. Facts come straight from `media/probe.py` and
keep its key names, so a filename can always be traced back to the measurement
that produced it.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.ingest.media.probe import (
    MEDIA_IMAGE,
    MediaFacts,
    classify_media_kind,
    probe_file,
    thumbnail_png,
)
from tools.ingest.naming.quality import assess

# Screenshot filenames as macOS and the common capture tools write them.
# Matching the name is far more reliable than inferring from pixels, and it is
# what lets a shared folder (macOS defaults screenshots to ~/Desktop) be
# filtered down to actual screenshots.
SCREENSHOT_NAME_PREFIXES = (
    "screenshot", "screen shot", "cleanshot", "bildschirmfoto",
    "capture d'écran", "capture d’écran", "captura de pantalla", "snimok ekrana",
)


@dataclass(frozen=True)
class NamingEvidence:
    """Immutable, filesystem-free description of one file."""

    source_class: str
    original_filename: str
    extension: str
    size: int
    mime: str | None
    media_kind: str
    name_quality: str
    name_quality_reason: str
    modified_date: str | None = None
    # True when *anything* suggests a screen capture, including the probe's
    # weak pixel heuristic. Good enough to route a file to review; not good
    # enough to assert an origin in a filename.
    is_screenshot: bool = False
    # True only when the evidence is strong: the filename identifies it as a
    # capture, or it came from the configured screenshots folder. This is what
    # a proposer is allowed to turn into the word "screen-capture".
    screenshot_by_name: bool = False
    # Deterministic probe output, exactly as measured, under the probe's own
    # key names. An absent key means "not known" -- never zero or empty.
    facts: dict[str, Any] = field(default_factory=dict)
    probe_errors: tuple[str, ...] = ()
    # Vocabulary the owner's own work uses, for recognising real subject matter.
    vocabulary: tuple[str, ...] = ()
    # A downscaled PNG for a future vision proposer. None unless requested.
    thumbnail_png: bytes | None = None

    def fact(self, key: str, default: Any = None) -> Any:
        return self.facts.get(key, default)

    @property
    def capture_date(self) -> str | None:
        """The ISO date the file is *about*, if the file says so."""
        timestamp = self.fact("capture_timestamp")
        if isinstance(timestamp, str) and len(timestamp) >= 10:
            return timestamp[:10]
        return None


def name_looks_like_screenshot(filename: str) -> bool:
    lowered = filename.lower().lstrip("._")
    return any(lowered.startswith(prefix) for prefix in SCREENSHOT_NAME_PREFIXES)


def looks_like_screenshot(filename: str, media_kind: str, facts: dict[str, Any]) -> bool:
    """Screenshot by filename, or by the probe's pixel-level hint.

    The name test is the strong signal. `is_probably_screenshot` is explicitly
    a heuristic in the probe (a PNG with no camera EXIF), so it is only a
    secondary cue: enough to route a file to review, never enough to assert an
    origin in a filename. `screenshot_by_name` is the stronger signal a
    proposer is given for that.
    """
    if name_looks_like_screenshot(filename):
        return True
    if media_kind != MEDIA_IMAGE:
        return False
    return bool(facts.get("is_probably_screenshot"))


def modified_date(path: Path) -> str | None:
    import datetime as dt

    try:
        stat = path.stat()
    except OSError:
        return None
    return dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).date().isoformat()


def gather_evidence(
    path: Path,
    *,
    source_class: str,
    vocabulary: tuple[str, ...] = (),
    include_thumbnail: bool = False,
    media_facts: MediaFacts | None = None,
    enable_video_probe: bool = True,
) -> NamingEvidence:
    """Measure a file and package the result for a proposer.

    `include_thumbnail` is off by default: the pixels are only useful to a
    proposer that can actually look at them, and encoding them for one that
    cannot would be wasted work on every file in Downloads.
    """
    mime, _ = mimetypes.guess_type(path.name)
    facts = (
        media_facts
        if media_facts is not None
        else probe_file(path, mime=mime, enable_video=enable_video_probe)
    )
    quality, reason = assess(path.name, vocabulary=vocabulary)

    thumbnail: bytes | None = None
    if include_thumbnail and facts.media_kind == MEDIA_IMAGE:
        thumbnail = thumbnail_png(path)

    return NamingEvidence(
        source_class=source_class,
        original_filename=path.name,
        extension=path.suffix.lower(),
        size=int(facts.facts.get("size_bytes") or 0),
        mime=facts.mime or mime,
        media_kind=facts.media_kind or classify_media_kind(path, mime),
        name_quality=quality,
        name_quality_reason=reason,
        modified_date=modified_date(path),
        is_screenshot=looks_like_screenshot(path.name, facts.media_kind, facts.facts),
        screenshot_by_name=name_looks_like_screenshot(path.name) or source_class == "screenshots",
        facts=dict(facts.facts),
        probe_errors=facts.errors,
        vocabulary=vocabulary,
        thumbnail_png=thumbnail,
    )
