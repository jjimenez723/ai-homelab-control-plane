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

`PerceivedEvidence` extends the same rule to *perception* -- a document's own
text, OCR off a screenshot, a vision backend's description, a speech
transcript (D-057). It is plain data too: bounded strings and tuples, nothing
else. Two things about it are structural rather than stylistic:

* **This module does not import `tools.ingest.evidence`.** An AST guard in
  `tests/ingest/test_evidence_security.py` forbids it, because a naming module
  reaching into the perception package is exactly how perceived content would
  acquire decision authority rather than merely being recorded. The
  `FileEvidence` -> `PerceivedEvidence` conversion therefore lives *outside*
  both packages, in `tools/ingest/perception_bridge.py`, which is allowed to
  import both and does nothing else.
* **Every excerpt is bounded and sanitized here, again.** The perception layer
  already sanitizes what it stores; `_sanitize_excerpt` below repeats that work
  at the naming boundary, so a `PerceivedEvidence` built from a mapping (a
  cache row, a hand-written fixture, a future caller) is safe by construction
  rather than by the provenance of its input.
"""

from __future__ import annotations

import mimetypes
import re
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


# Per-excerpt ceiling on the perceived text a proposer is handed. This is a
# *naming* bound, deliberately far below `evidence.max_text_chars` (20 000): a
# filename is derived from the first useful line or two, and a proposer handed
# a whole contract is being handed an opportunity to hallucinate, not
# information. It also keeps a rendered prompt small enough that a small local
# model's context limit is not what decides which evidence the model sees.
MAX_PERCEIVED_EXCERPT_CHARS = 1500

# How many descriptions/entities/headings survive, and how long each may be.
# Bounded for the same reason the text is: a video sampled into thirty frames
# must not be able to turn one naming call into thirty descriptions of prompt.
MAX_PERCEIVED_ITEMS = 12
MAX_PERCEIVED_ITEM_CHARS = 300

# Evidence kinds, as they appear in `PerceivedEvidence.sources`. Named
# constants rather than bare strings because the bridge that fills them and
# the proposers that read them have to agree, and a typo would degrade
# silently into "that kind was not present".
SOURCE_NATIVE_TEXT = "native_text"
SOURCE_OCR = "ocr"
SOURCE_VISUAL = "visual"
SOURCE_TRANSCRIPT = "transcript"
SOURCE_STRUCTURE = "structure"

KNOWN_EVIDENCE_SOURCES = frozenset(
    {SOURCE_NATIVE_TEXT, SOURCE_OCR, SOURCE_VISUAL, SOURCE_TRANSCRIPT, SOURCE_STRUCTURE}
)

# Mirrors `tools.ingest.evidence.types.sanitize_extracted_text`'s character
# classes. Restated rather than imported: see this module's docstring for why
# `naming/` may not import the perception package. That the two definitions
# still agree is itself a test (`tests/ingest/test_perception_bridge.py`),
# so the duplication cannot drift silently.
_C1_AND_HIDDEN_RE = re.compile("[\x80-\x9f​-‏‪-‮⁦-⁩﻿]")
_C0_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_EXCESS_BLANK_LINES_RE = re.compile("\n{4,}")


def sanitize_perceived_text(value: Any, *, max_chars: int = MAX_PERCEIVED_EXCERPT_CHARS) -> str:
    """Coerce anything to a bounded, control-character-free string.

    Takes `Any` on purpose. A `PerceivedEvidence` may be built from a mapping
    that came from a cache row, a JSON payload, or a fixture, so "the caller
    handed us an int, a list, or an object with a hostile `__str__`" is a case
    this has to answer with a string rather than let a `TypeError` escape into
    the naming gate.
    """
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # noqa: BLE001 - a hostile __str__ is not a naming failure
        return ""
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _C1_AND_HIDDEN_RE.sub("", normalized)
    normalized = _C0_CONTROL_RE.sub("", normalized)
    normalized = _EXCESS_BLANK_LINES_RE.sub("\n\n\n", normalized)
    if len(normalized) > max_chars:
        normalized = normalized[:max_chars]
    return normalized


def _clamp_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _sanitize_items(values: Any, *, limit: int = MAX_PERCEIVED_ITEMS) -> tuple[str, ...]:
    """A bounded tuple of short, sanitized strings. Empty entries are dropped."""
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        values = [values]
    try:
        iterator = list(values)
    except TypeError:
        return ()
    items: list[str] = []
    for value in iterator[:limit]:
        cleaned = sanitize_perceived_text(value, max_chars=MAX_PERCEIVED_ITEM_CHARS).strip()
        if cleaned:
            items.append(cleaned)
    return tuple(items)


@dataclass(frozen=True)
class PerceivedEvidence:
    """What was *perceived* about one file's bytes, as plain data (D-057).

    The perception layer's own value types (`tools/ingest/evidence/types.py`)
    are far richer than this: polygons, per-block confidences, provenance
    records, page regions and tables. None of that is here, and the omission is
    the design. A proposer needs the text and the description; handing it the
    geometry would hand it a second, unvalidated way to reason about a file,
    and every extra field is one more thing a future consumer could mistake
    for authority.

    Frozen, and strings/tuples only -- no `Path`, no callable, no nested
    dataclass. `tests/ingest/test_mutation_authority.py` asserts that
    field by field, exactly as it does for `NamingEvidence` itself.
    """

    # The file's own text layer (PDF/DOCX/plain text), bounded.
    native_text_excerpt: str = ""
    # Text read off the pixels, in reading order, bounded.
    ocr_text_excerpt: str = ""
    # Speech, once a transcript stage exists (another workstream). Empty until
    # then, and never used deterministically -- see `DeterministicProposer`.
    transcript_excerpt: str = ""
    # One description per image the vision backend was actually shown: length 1
    # for a photo or screenshot, one per sampled frame for a video (D-056).
    visual_descriptions: tuple[str, ...] = ()
    visual_entities: tuple[str, ...] = ()
    # `tools.ingest.evidence.types.IMAGE_KINDS` vocabulary, as a bare string.
    visual_image_kind: str = ""
    # The vision backend's own confidence in the descriptions above (the
    # highest across frames, 0.0-1.0). `HeuristicVisionBackend` is honestly
    # low-confidence; a proposer must not treat its guess as a fact.
    visual_confidence: float = 0.0
    # Document headings, in document order.
    headings: tuple[str, ...] = ()
    # Which kinds were actually present, e.g. ("native_text", "structure").
    # A proposer reads this rather than testing an excerpt for emptiness,
    # because "OCR ran and found nothing" and "OCR never ran" are different
    # facts, and only one of them means the pixels carry no text.
    sources: tuple[str, ...] = ()
    # `EvidenceWarning` codes, flattened: why the perception is incomplete.
    warnings: tuple[str, ...] = ()
    # The content hash of the bytes this perception describes. Recorded on
    # every naming decision so "which evidence produced this filename?" is
    # answerable from the ledger (`organize naming-history`).
    evidence_sha256: str = ""

    @property
    def has_text(self) -> bool:
        return bool(self.native_text_excerpt or self.ocr_text_excerpt)

    @property
    def combined_text(self) -> str:
        """Native text then OCR text, both already bounded."""
        return "\n".join(part for part in (self.native_text_excerpt, self.ocr_text_excerpt) if part)

    @classmethod
    def from_mapping(cls, data: Any) -> "PerceivedEvidence":
        """Build from a plain mapping, sanitizing and bounding every field.

        The mapping is untrusted: it may come from a cache row, a JSON
        payload, or a fixture, and it may be missing keys, carry the wrong
        types, or carry unbounded text. Nothing here raises -- a field that
        cannot be made sense of becomes empty, which is the honest answer.
        """
        if not isinstance(data, dict):
            return cls()
        raw_sources = _sanitize_items(data.get("sources"), limit=len(KNOWN_EVIDENCE_SOURCES) + 4)
        seen: list[str] = []
        for source in raw_sources:
            if source in KNOWN_EVIDENCE_SOURCES and source not in seen:
                seen.append(source)
        return cls(
            native_text_excerpt=sanitize_perceived_text(data.get("native_text_excerpt")),
            ocr_text_excerpt=sanitize_perceived_text(data.get("ocr_text_excerpt")),
            transcript_excerpt=sanitize_perceived_text(data.get("transcript_excerpt")),
            visual_descriptions=_sanitize_items(data.get("visual_descriptions")),
            visual_entities=_sanitize_items(data.get("visual_entities")),
            visual_image_kind=sanitize_perceived_text(data.get("visual_image_kind"), max_chars=64).strip(),
            visual_confidence=_clamp_confidence(data.get("visual_confidence")),
            headings=_sanitize_items(data.get("headings")),
            sources=tuple(seen),
            warnings=_sanitize_items(data.get("warnings"), limit=32),
            evidence_sha256=sanitize_perceived_text(data.get("evidence_sha256"), max_chars=64).strip(),
        )

    def to_mapping(self) -> dict[str, Any]:
        """The inverse of `from_mapping`, for the audit ledger and the CLI."""
        return {
            "native_text_excerpt": self.native_text_excerpt,
            "ocr_text_excerpt": self.ocr_text_excerpt,
            "transcript_excerpt": self.transcript_excerpt,
            "visual_descriptions": list(self.visual_descriptions),
            "visual_entities": list(self.visual_entities),
            "visual_image_kind": self.visual_image_kind,
            "visual_confidence": self.visual_confidence,
            "headings": list(self.headings),
            "sources": list(self.sources),
            "warnings": list(self.warnings),
            "evidence_sha256": self.evidence_sha256,
        }


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
    # A date written into the original filename (see `date_from_filename`).
    filename_date: str | None = None
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
    # What was perceived about these bytes, if the perception layer ran and
    # `perception_bridge` converted its output (D-057). `None` means
    # "perception did not run, or failed" -- never "the file contains
    # nothing", and a proposer must treat it as the absence of information
    # rather than as a negative fact. Attached by `pipeline.py` before naming.
    perceived: PerceivedEvidence | None = None

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


# Dates a person or a tool wrote into the filename itself: `Screenshot 2026-09-11
# at 9.21.48 PM.png`, `IMG_20260911_212148.jpg`, `report_2026_09_11.pdf`. This is
# a fact the file carries about itself (weaker than EXIF capture time, stronger
# than this filesystem's mtime, which is merely when the bytes last landed
# here -- a Drive sync or a copy resets it). Bounded to plausible years and
# validated as a real calendar date; anything else is not a date.
_FILENAME_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_. ]?(0[1-9]|1[0-2])[-_. ]?(0[1-9]|[12]\d|3[01])(?!\d)")


def date_from_filename(filename: str) -> str | None:
    """The first plausible ISO date written into a filename, or None."""
    import datetime as dt

    for match in _FILENAME_DATE_RE.finditer(filename):
        year, month, day = (int(part) for part in match.groups())
        try:
            found = dt.date(year, month, day)
        except ValueError:
            continue
        if 1990 <= found.year <= dt.date.today().year + 1:
            return found.isoformat()
    return None


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
        filename_date=date_from_filename(path.name),
        is_screenshot=looks_like_screenshot(path.name, facts.media_kind, facts.facts),
        screenshot_by_name=name_looks_like_screenshot(path.name) or source_class == "screenshots",
        facts=dict(facts.facts),
        probe_errors=facts.errors,
        vocabulary=vocabulary,
        thumbnail_png=thumbnail,
    )
