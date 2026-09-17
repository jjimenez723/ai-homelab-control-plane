"""The vocabulary of perceived evidence. Plain data, no authority.

This module answers "what shape does one piece of *perception* about a file
take?" -- OCR text, a PDF's own text, a vision model's description of a
screenshot, the layout structure a document was read in. It deliberately does
not answer "what should we do about it": every type here is inert data, the
same way `NamingEvidence` (`tools/ingest/naming/evidence.py`) is inert data
for the naming proposer. `FileEvidence` carries no `Path` and no callable, so
nothing holding one -- including a future model-backed consumer -- has
anything to mutate the filesystem with.

The invariant this phase must not break is FACTS != INTERPRETATIONS !=
DECISIONS. Nothing in this module, or anything built on it, is allowed to
change a naming, retention, or ingestion decision. It is collected,
normalized, cached, and exposed for inspection, and nothing else.

Everything extracted from a file is untrusted data with zero authority (OCR
text, PDF text, EXIF, filenames, vision descriptions). It cannot reach config,
policy, a subprocess argument, or a filesystem call from anywhere in this
package. `sanitize_extracted_text` below is part of *how* that stays true for
strings: see its docstring for exactly what it does and does not defend
against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Bump when the *shape* evidence is stored in changes, so a previously cached
# row can be recognised as stale (a stale pipeline_version misses the cache
# for every kind, the way a probe_version bump does for `media_metadata`).
EVIDENCE_PIPELINE_VERSION = "1"

# -- warnings -----------------------------------------------------------------
#
# One string vocabulary shared by every stage, so a caller can reason about
# "why is this evidence incomplete" without special-casing which stage it came
# from.
WARN_BACKEND_UNAVAILABLE = "backend_unavailable"
WARN_UNSUPPORTED = "unsupported"
WARN_MALFORMED = "malformed"
WARN_TRUNCATED = "truncated"
WARN_LOW_CONFIDENCE = "low_confidence"
WARN_PARTIAL = "partial_extraction"
WARN_TIMEOUT = "timeout"
WARN_TOO_LARGE = "too_large"
WARN_SKIPPED_BY_POLICY = "skipped_by_policy"


@dataclass(frozen=True)
class EvidenceWarning:
    """One reason a piece of evidence is missing, partial, or suspect."""

    code: str  # one of WARN_*
    stage: str  # "route" | "native_text" | "raster" | "ocr" | "vision" | "structure" | "cache"
    detail: str = ""


# -- image kinds (vision vocabulary) ------------------------------------------

IMAGE_KIND_SCREENSHOT = "screenshot"
IMAGE_KIND_ORDINARY_PHOTO = "ordinary_photo"
IMAGE_KIND_RENDERING = "rendering"
IMAGE_KIND_SCANNED_DOCUMENT = "scanned_document"
IMAGE_KIND_DIAGRAM = "diagram"
IMAGE_KIND_CHART = "chart"
IMAGE_KIND_MEME = "meme"
IMAGE_KIND_UI_CAPTURE = "ui_capture"
IMAGE_KIND_UNKNOWN = "unknown"

IMAGE_KINDS = frozenset(
    {
        IMAGE_KIND_SCREENSHOT,
        IMAGE_KIND_ORDINARY_PHOTO,
        IMAGE_KIND_RENDERING,
        IMAGE_KIND_SCANNED_DOCUMENT,
        IMAGE_KIND_DIAGRAM,
        IMAGE_KIND_CHART,
        IMAGE_KIND_MEME,
        IMAGE_KIND_UI_CAPTURE,
        IMAGE_KIND_UNKNOWN,
    }
)

# Kinds whose presence justifies asking for OCR on a second pass, once a
# vision backend has actually looked at the pixels -- see
# `router.refine_route`. A photograph or a rendering is not on this list: a
# vision description is the useful evidence for those, and running OCR over
# an ordinary photo is wasted work that will not find text.
TEXT_BEARING_IMAGE_KINDS = frozenset(
    {
        IMAGE_KIND_SCREENSHOT,
        IMAGE_KIND_UI_CAPTURE,
        IMAGE_KIND_SCANNED_DOCUMENT,
        IMAGE_KIND_DIAGRAM,
        IMAGE_KIND_CHART,
        IMAGE_KIND_MEME,
    }
)

# -- geometry ------------------------------------------------------------------

Point = tuple[int, int]


@dataclass(frozen=True)
class Polygon:
    """A closed polygon in *source image pixel* coordinates, origin top-left.

    Stored as points rather than a bbox because a detected text region is
    frequently not axis-aligned, and flattening it to a rectangle at the point
    of capture destroys geometry a later stage cannot recover.
    """

    points: tuple[Point, ...]  # >= 3 points for a valid polygon

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        """The axis-aligned bounding box, as (x0, y0, x1, y1)."""
        if not self.points:
            return (0, 0, 0, 0)
        xs = [point[0] for point in self.points]
        ys = [point[1] for point in self.points]
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def is_valid(self) -> bool:
        """False for anything that cannot be a real detected region.

        Fewer than three points cannot enclose an area. A negative coordinate
        cannot come from a real pixel position -- it means an upstream engine
        (or a hostile OCR/vision payload) fabricated the geometry. A
        zero-width or zero-height bounding box is a degenerate line or point,
        not a region. None of these are trusted enough to draw, store as a
        crop, or feed forward.
        """
        if len(self.points) < 3:
            return False
        if any(x < 0 or y < 0 for x, y in self.points):
            return False
        x0, y0, x1, y1 = self.bbox
        if x1 <= x0 or y1 <= y0:
            return False
        return True

    def as_list(self) -> list[list[int]]:
        """JSON form: a list of [x, y] pairs, in point order."""
        return [[int(x), int(y)] for x, y in self.points]

    @classmethod
    def from_bbox(cls, x0: int, y0: int, x1: int, y1: int) -> "Polygon":
        """Build the rectangle for an axis-aligned box, clockwise from top-left."""
        return cls(
            points=(
                (int(x0), int(y0)),
                (int(x1), int(y0)),
                (int(x1), int(y1)),
                (int(x0), int(y1)),
            )
        )

    @classmethod
    def from_points(cls, points: Iterable[Any]) -> "Polygon":
        """Build from an iterable of (x, y) pairs, tolerant of the caller's shape.

        Backends and JSON payloads hand back points as tuples, two-element
        lists, or (after a round trip through `json.loads`) plain lists of
        numbers that may be ints or floats. Coordinates are rounded rather
        than truncated so a backend's sub-pixel estimate lands on the nearest
        real pixel instead of always being pulled toward the origin.
        """
        coerced: list[Point] = []
        for point in points:
            x, y = point
            coerced.append((int(round(float(x))), int(round(float(y)))))
        return cls(points=tuple(coerced))


# -- OCR ------------------------------------------------------------------


@dataclass(frozen=True)
class OCRBlock:
    text: str
    polygon: Polygon
    page: int  # 1-based; 1 for a single image
    confidence: float  # 0.0-1.0
    reading_order: int  # index within its page, assigned by reading_order.py
    engine: str
    engine_version: str


# -- native text ----------------------------------------------------------


@dataclass(frozen=True)
class TextSection:
    text: str
    page: int | None = None  # 1-based page for paginated formats
    label: str = ""  # e.g. "page", "paragraph", "sheet"


@dataclass(frozen=True)
class NativeText:
    text: str  # sanitized, capped, the concatenation of `sections`
    sections: tuple[TextSection, ...]
    extractor: str
    extractor_version: str
    char_count: int  # chars actually retained
    truncated: bool


# -- vision ---------------------------------------------------------------


@dataclass(frozen=True)
class VisualEvidence:
    image_kind: str  # one of IMAGE_KINDS
    description: str  # concise factual description, sanitized
    entities: tuple[str, ...]  # visible objects/apps/logos; may be empty
    context: str  # e.g. "GitHub pull request page"
    confidence: float
    backend: str
    model: str
    model_version: str


# -- structure ------------------------------------------------------------


@dataclass(frozen=True)
class Heading:
    text: str
    level: int
    page: int | None = None


@dataclass(frozen=True)
class Region:
    kind: str  # "text" | "table" | "figure" | "header" | "footer"
    polygon: Polygon
    page: int
    reading_order: int


@dataclass(frozen=True)
class TableRegion:
    polygon: Polygon
    page: int
    row_count: int | None = None
    column_count: int | None = None


@dataclass(frozen=True)
class StructureEvidence:
    headings: tuple[Heading, ...] = ()
    regions: tuple[Region, ...] = ()
    tables: tuple[TableRegion, ...] = ()
    page_count: int | None = None
    reading_order_engine: str = ""
    reading_order_engine_version: str = ""


# -- provenance -------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceProvenance:
    """Who produced one slice of evidence, from which bytes, under what version."""

    kind: str  # "native_text" | "ocr" | "visual" | "structure"
    content_sha256: str
    producer: str  # extractor or backend name
    producer_version: str
    model: str | None
    model_version: str | None
    pipeline_version: str
    generated_at: str  # ISO8601 UTC
    cache_hit: bool = False
    status: str = "ok"  # "ok" | "partial" | "unavailable" | "failed"


# -- the whole thing --------------------------------------------------------


@dataclass(frozen=True)
class FileEvidence:
    """Everything perceived about one file's bytes. Plain data, no authority.

    Carries no `Path` and no callable, exactly like `NamingEvidence`, so
    nothing holding one can reach the filesystem with it.
    """

    content_sha256: str
    mime: str | None
    media_kind: str
    facts: dict[str, Any]  # verbatim from MediaFacts.facts
    route: "EvidenceRoute"
    native_text: NativeText | None = None
    ocr_blocks: tuple[OCRBlock, ...] = ()
    visual: VisualEvidence | None = None
    # One `VisualEvidence` per image the vision backend was actually shown,
    # in sampling order -- `(visual,)` for a photo/screenshot (always
    # length 1), one entry per sampled frame for a video. `visual` above
    # always equals `visual_frames[0]` when either is present; it stays a
    # separate field rather than being derived, so existing code reading
    # only `.visual` keeps working unchanged.
    visual_frames: tuple[VisualEvidence, ...] = ()
    structure: StructureEvidence | None = None
    provenance: tuple[EvidenceProvenance, ...] = ()
    warnings: tuple[EvidenceWarning, ...] = ()
    pipeline_version: str = EVIDENCE_PIPELINE_VERSION

    @property
    def ocr_text(self) -> str:
        """OCR blocks joined in reading order, one block's text per line."""
        ordered = sorted(self.ocr_blocks, key=lambda block: (block.page, block.reading_order))
        return "\n".join(block.text for block in ordered)

    def warning_codes(self) -> tuple[str, ...]:
        return tuple(warning.code for warning in self.warnings)


@dataclass(frozen=True)
class EvidenceSource:
    """What a TRUSTED in-repo extractor is given. Never handed to a backend.

    In-repo `TextExtractor` implementations are deterministic repo code, not
    a model, and they must be able to stream a large file rather than hold it
    all in memory -- that is why they receive a path. `OCRBackend` and
    `VisionBackend` never do; see `RasterImage`.
    """

    path: Path
    sha256: str
    mime: str | None
    media_kind: str
    size: int


@dataclass(frozen=True)
class RasterImage:
    """Pixels handed to a model-facing backend. Bytes, deliberately not a path.

    Mirrors why `NamingEvidence.thumbnail_png` is `bytes`
    (`tools/ingest/naming/evidence.py`): a backend that only ever receives an
    in-memory PNG has nothing to mutate the filesystem with, whatever else it
    does with the pixels.
    """

    png_bytes: bytes
    width: int
    height: int
    page: int = 1
    source_sha256: str = ""  # identifies the ORIGINAL file's bytes


RasterPage = RasterImage  # alias; a page is a raster image with page > 0


# -- text sanitization --------------------------------------------------------
#
# Every string that comes out of a file -- OCR text, native text, EXIF-derived
# text, a vision description, table cell text -- passes through this before it
# is stored in an evidence object.
#
# This is a display-safety and storage-hygiene measure. It is deliberately
# NOT a prompt-injection defense, and must never be described as one: the
# actual defense against injected instructions is structural. Every type in
# this module is inert data with nowhere to go -- it cannot reach config,
# policy, a subprocess argument, or a filesystem call from anywhere evidence
# is threaded through (see the module docstring). Sanitizing the text changes
# what it looks like on a terminal and what it costs to store; it does not,
# and is not claimed to, "filter malicious instructions" out of the content.

# C1 control characters, plus the bidi-override and zero-width characters this
# repo already refuses in filenames (see naming/policy.py's equivalent guard).
# A C1 byte is what makes an 8-bit ANSI/OSC escape sequence work in a terminal
# that still honours it, which matters because `organize` prints OCR text to
# a terminal and that text is attacker-influenced.
_C1_AND_HIDDEN_RE = re.compile(
    "[-​-‏‪-‮⁦-⁩﻿]"
)

# C0 control characters except \t (0x09) and \n (0x0A). This is what defangs
# a 7-bit ESC-based escape sequence: stripping the ESC byte (0x1B) leaves the
# rest of the sequence as inert printable characters.
_C0_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Three or more consecutive newlines represent two or more blank lines
# (an empty line is the text between two adjacent "\n"s); collapse any such
# run down to exactly two blank lines' worth of newlines.
_EXCESS_BLANK_LINES_RE = re.compile("\n{4,}")


def sanitize_extracted_text(text: str, *, max_chars: int) -> tuple[str, bool]:
    """Make text extracted from a file safe to store and print. Returns (text, truncated).

    Strips C0 control characters (except newline and tab), C1 control
    characters, and the Unicode bidi-override / zero-width characters this
    repo already refuses in filenames; normalizes CRLF/CR to LF; collapses
    long runs of blank lines; and truncates to `max_chars`, reporting whether
    truncation happened so the caller can record a `WARN_TRUNCATED` warning
    rather than silently dropping content.

    This is display safety and storage hygiene, not a prompt-injection
    defense -- see the section comment above.
    """
    if not text:
        return "", False

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _C1_AND_HIDDEN_RE.sub("", normalized)
    normalized = _C0_CONTROL_RE.sub("", normalized)
    normalized = _EXCESS_BLANK_LINES_RE.sub("\n\n\n", normalized)

    truncated = False
    if max_chars is not None and len(normalized) > max_chars:
        normalized = normalized[:max_chars]
        truncated = True
    return normalized, truncated


# -- JSON round-trip helpers --------------------------------------------------
#
# Deterministic key order: callers serialize with `json.dumps(..., sort_keys=True)`
# at the sqlite boundary (see `tools/ingest/evidence/cache.py`), so the exact
# key order produced here does not matter, only that every value round-trips.


def evidence_warning_to_json(warning: EvidenceWarning) -> dict[str, str]:
    return {"code": warning.code, "stage": warning.stage, "detail": warning.detail}


def evidence_warning_from_json(data: dict[str, Any]) -> EvidenceWarning:
    return EvidenceWarning(
        code=str(data["code"]), stage=str(data["stage"]), detail=str(data.get("detail", ""))
    )


def native_text_to_json(native: NativeText) -> dict[str, Any]:
    return {
        "text": native.text,
        "sections": [
            {"text": section.text, "page": section.page, "label": section.label}
            for section in native.sections
        ],
        "extractor": native.extractor,
        "extractor_version": native.extractor_version,
        "char_count": native.char_count,
        "truncated": native.truncated,
    }


def native_text_from_json(data: dict[str, Any]) -> NativeText:
    sections = tuple(
        TextSection(
            text=str(section["text"]),
            page=section.get("page"),
            label=str(section.get("label", "")),
        )
        for section in data.get("sections", [])
    )
    return NativeText(
        text=str(data["text"]),
        sections=sections,
        extractor=str(data["extractor"]),
        extractor_version=str(data["extractor_version"]),
        char_count=int(data["char_count"]),
        truncated=bool(data["truncated"]),
    )


def _ocr_block_to_json(block: OCRBlock) -> dict[str, Any]:
    return {
        "text": block.text,
        "polygon": block.polygon.as_list(),
        "page": block.page,
        "confidence": block.confidence,
        "reading_order": block.reading_order,
        "engine": block.engine,
        "engine_version": block.engine_version,
    }


def _ocr_block_from_json(data: dict[str, Any]) -> OCRBlock:
    return OCRBlock(
        text=str(data["text"]),
        polygon=Polygon.from_points(data["polygon"]),
        page=int(data["page"]),
        confidence=float(data["confidence"]),
        reading_order=int(data["reading_order"]),
        engine=str(data["engine"]),
        engine_version=str(data["engine_version"]),
    )


def ocr_blocks_to_json(blocks: tuple[OCRBlock, ...]) -> dict[str, Any]:
    return {"blocks": [_ocr_block_to_json(block) for block in blocks]}


def ocr_blocks_from_json(data: dict[str, Any]) -> tuple[OCRBlock, ...]:
    return tuple(_ocr_block_from_json(block) for block in data.get("blocks", []))


def visual_evidence_to_json(visual: VisualEvidence) -> dict[str, Any]:
    return {
        "image_kind": visual.image_kind,
        "description": visual.description,
        "entities": list(visual.entities),
        "context": visual.context,
        "confidence": visual.confidence,
        "backend": visual.backend,
        "model": visual.model,
        "model_version": visual.model_version,
    }


def visual_evidence_from_json(data: dict[str, Any]) -> VisualEvidence:
    return VisualEvidence(
        image_kind=str(data["image_kind"]),
        description=str(data["description"]),
        entities=tuple(str(entity) for entity in data["entities"]),
        context=str(data["context"]),
        confidence=float(data["confidence"]),
        backend=str(data["backend"]),
        model=str(data["model"]),
        model_version=str(data["model_version"]),
    )


def visual_frames_to_json(frames: tuple[VisualEvidence, ...]) -> dict[str, Any]:
    return {"frames": [visual_evidence_to_json(frame) for frame in frames]}


def visual_frames_from_json(data: dict[str, Any]) -> tuple[VisualEvidence, ...]:
    return tuple(visual_evidence_from_json(frame) for frame in data.get("frames", []))


def structure_evidence_to_json(structure: StructureEvidence) -> dict[str, Any]:
    return {
        "headings": [
            {"text": heading.text, "level": heading.level, "page": heading.page}
            for heading in structure.headings
        ],
        "regions": [
            {
                "kind": region.kind,
                "polygon": region.polygon.as_list(),
                "page": region.page,
                "reading_order": region.reading_order,
            }
            for region in structure.regions
        ],
        "tables": [
            {
                "polygon": table.polygon.as_list(),
                "page": table.page,
                "row_count": table.row_count,
                "column_count": table.column_count,
            }
            for table in structure.tables
        ],
        "page_count": structure.page_count,
        "reading_order_engine": structure.reading_order_engine,
        "reading_order_engine_version": structure.reading_order_engine_version,
    }


def structure_evidence_from_json(data: dict[str, Any]) -> StructureEvidence:
    headings = tuple(
        Heading(text=str(heading["text"]), level=int(heading["level"]), page=heading.get("page"))
        for heading in data.get("headings", [])
    )
    regions = tuple(
        Region(
            kind=str(region["kind"]),
            polygon=Polygon.from_points(region["polygon"]),
            page=int(region["page"]),
            reading_order=int(region["reading_order"]),
        )
        for region in data.get("regions", [])
    )
    tables = tuple(
        TableRegion(
            polygon=Polygon.from_points(table["polygon"]),
            page=int(table["page"]),
            row_count=table.get("row_count"),
            column_count=table.get("column_count"),
        )
        for table in data.get("tables", [])
    )
    return StructureEvidence(
        headings=headings,
        regions=regions,
        tables=tables,
        page_count=data.get("page_count"),
        reading_order_engine=str(data.get("reading_order_engine", "")),
        reading_order_engine_version=str(data.get("reading_order_engine_version", "")),
    )


def evidence_section_to_json(section: Any) -> dict[str, Any]:
    """Serialize one evidence section to a plain dict, dispatching on type.

    One place that knows every section's JSON shape, so the cache and (later)
    the CLI do not each need their own copy of this dispatch. A tuple of
    `OCRBlock` is accepted directly -- `FileEvidence.ocr_blocks` is a bare
    tuple, not its own dataclass -- rather than requiring callers to wrap it
    first.
    """
    if section is None:
        return {}
    if isinstance(section, NativeText):
        return native_text_to_json(section)
    if isinstance(section, VisualEvidence):
        return visual_evidence_to_json(section)
    if isinstance(section, StructureEvidence):
        return structure_evidence_to_json(section)
    if isinstance(section, EvidenceProvenance):
        return {
            "kind": section.kind,
            "content_sha256": section.content_sha256,
            "producer": section.producer,
            "producer_version": section.producer_version,
            "model": section.model,
            "model_version": section.model_version,
            "pipeline_version": section.pipeline_version,
            "generated_at": section.generated_at,
            "cache_hit": section.cache_hit,
            "status": section.status,
        }
    if isinstance(section, tuple):
        return ocr_blocks_to_json(section)
    raise TypeError(f"no JSON mapping for evidence section of type {type(section)!r}")
