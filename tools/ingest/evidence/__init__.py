"""Angel's visual & document evidence layer: FACTS/PERCEPTION only.

Everything in this package answers "what does this file's bytes contain,
measurably" and nothing else. It performs zero filesystem writes, opens owner
files read-only, never raises out of a public entry point, and hands
model-facing backends bytes rather than paths. See the binding evidence-layer
spec for the full set of invariants this package must not break, chief among
them: FACTS != INTERPRETATIONS != DECISIONS. Nothing here interprets, and
nothing here decides.

What reads the output of this package is deliberately narrow (D-057): a
*filename proposal* may, via the one-way conversion in
`tools/ingest/perception_bridge.py` into bounded plain data. Retention and
ingestion read nothing produced here, and no module under
`tools/ingest/naming/` or `tools/ingest/triage/` may import this package at
all -- `tests/ingest/test_evidence_security.py` enforces both with AST guards
that have no exception list.

The public surface, in the order a file travels through it:

* `types.py` -- the value types all evidence is expressed in, plus the text
  sanitizer every extracted string passes through.
* `router.py` -- the pure rules deciding which perception stages are worth
  attempting for a given file.
* `text.py`, `raster.py`, `preprocess.py` -- trusted, deterministic readers of
  a file's own contents, and the in-memory rasterization that turns pages and
  images into PNG bytes.
* `backends/` -- the three seams a real engine plugs into (`TextExtractor`,
  `OCRBackend`, `VisionBackend`), plus the offline implementations shipped
  today.
* `reading_order.py`, `perspective.py` -- geometry adapted from PaddleOCR; see
  `THIRD_PARTY_NOTICES.md`.
* `cache.py` -- the content-keyed evidence cache, backed by the local ledger.
* `extract.py` -- `collect_evidence`, which assembles a `FileEvidence` from all
  of the above. This is the entry point almost every caller wants.
"""

from __future__ import annotations

from tools.ingest.evidence.cache import CacheKey, CacheLookup, EvidenceLedger, lookup, store
from tools.ingest.evidence.summary import (
    FrameDescription,
    PerceptionSummary,
    SummaryProvenance,
    SummaryWarning,
    perception_summary,
)
from tools.ingest.evidence.router import (
    EvidenceRoute,
    refine_route,
    refine_route_for_native_text,
    route_for,
)
from tools.ingest.evidence.types import (
    EVIDENCE_PIPELINE_VERSION,
    IMAGE_KIND_CHART,
    IMAGE_KIND_DIAGRAM,
    IMAGE_KIND_MEME,
    IMAGE_KIND_ORDINARY_PHOTO,
    IMAGE_KIND_RENDERING,
    IMAGE_KIND_SCANNED_DOCUMENT,
    IMAGE_KIND_SCREENSHOT,
    IMAGE_KIND_UI_CAPTURE,
    IMAGE_KIND_UNKNOWN,
    IMAGE_KINDS,
    TEXT_BEARING_IMAGE_KINDS,
    WARN_BACKEND_UNAVAILABLE,
    WARN_LOW_CONFIDENCE,
    WARN_MALFORMED,
    WARN_DEDUPLICATED,
    WARN_NO_AUDIO,
    WARN_PARTIAL,
    WARN_SKIPPED_BY_POLICY,
    WARN_TIMEOUT,
    WARN_TOO_LARGE,
    WARN_TRUNCATED,
    WARN_UNSUPPORTED,
    AudioClip,
    EvidenceProvenance,
    EvidenceSource,
    EvidenceWarning,
    FileEvidence,
    Heading,
    NativeText,
    OCRBlock,
    Point,
    Polygon,
    RasterImage,
    RasterPage,
    Region,
    StructureEvidence,
    TableRegion,
    TextSection,
    TranscriptEvidence,
    TranscriptSegment,
    VisualEvidence,
    evidence_section_to_json,
    evidence_warning_from_json,
    evidence_warning_to_json,
    native_text_from_json,
    native_text_to_json,
    ocr_blocks_from_json,
    ocr_blocks_to_json,
    sanitize_extracted_text,
    structure_evidence_from_json,
    structure_evidence_to_json,
    transcript_evidence_from_json,
    transcript_evidence_to_json,
    visual_evidence_from_json,
    visual_evidence_to_json,
)

__all__ = [
    # types.py
    "EVIDENCE_PIPELINE_VERSION",
    "WARN_BACKEND_UNAVAILABLE",
    "WARN_UNSUPPORTED",
    "WARN_MALFORMED",
    "WARN_TRUNCATED",
    "WARN_LOW_CONFIDENCE",
    "WARN_PARTIAL",
    "WARN_TIMEOUT",
    "WARN_TOO_LARGE",
    "WARN_SKIPPED_BY_POLICY",
    "WARN_NO_AUDIO",
    "WARN_DEDUPLICATED",
    "EvidenceWarning",
    "IMAGE_KIND_SCREENSHOT",
    "IMAGE_KIND_ORDINARY_PHOTO",
    "IMAGE_KIND_RENDERING",
    "IMAGE_KIND_SCANNED_DOCUMENT",
    "IMAGE_KIND_DIAGRAM",
    "IMAGE_KIND_CHART",
    "IMAGE_KIND_MEME",
    "IMAGE_KIND_UI_CAPTURE",
    "IMAGE_KIND_UNKNOWN",
    "IMAGE_KINDS",
    "TEXT_BEARING_IMAGE_KINDS",
    "Point",
    "Polygon",
    "OCRBlock",
    "TextSection",
    "NativeText",
    "VisualEvidence",
    "TranscriptSegment",
    "TranscriptEvidence",
    "Heading",
    "Region",
    "TableRegion",
    "StructureEvidence",
    "EvidenceProvenance",
    "FileEvidence",
    "EvidenceSource",
    "RasterImage",
    "RasterPage",
    "AudioClip",
    "sanitize_extracted_text",
    "evidence_section_to_json",
    "evidence_warning_to_json",
    "evidence_warning_from_json",
    "native_text_to_json",
    "native_text_from_json",
    "ocr_blocks_to_json",
    "ocr_blocks_from_json",
    "visual_evidence_to_json",
    "visual_evidence_from_json",
    "structure_evidence_to_json",
    "structure_evidence_from_json",
    "transcript_evidence_to_json",
    "transcript_evidence_from_json",
    # router.py
    "EvidenceRoute",
    "route_for",
    "refine_route",
    "refine_route_for_native_text",
    # cache.py
    "CacheKey",
    "CacheLookup",
    "EvidenceLedger",
    "lookup",
    "store",
    # extract.py
    "collect_evidence",
    "pipeline_version_for",
    "KIND_NATIVE_TEXT",
    "KIND_OCR",
    "KIND_VISUAL",
    "KIND_TRANSCRIPT",
    "KIND_STRUCTURE",
    # summary.py
    "PerceptionSummary",
    "FrameDescription",
    "SummaryProvenance",
    "SummaryWarning",
    "perception_summary",
]


def collect_evidence(*args, **kwargs):  # noqa: D103 - see `extract.collect_evidence`
    """Assemble a `FileEvidence` for one file. See `extract.collect_evidence`.

    Imported lazily so that `from tools.ingest.evidence import types` stays a
    cheap, dependency-free import: the orchestrator pulls in Pillow, the
    backends, and the extractors, and the pure value types are used in places
    (the router's tests, the mutation-authority AST checks) that should not
    have to.
    """
    from tools.ingest.evidence.extract import collect_evidence as _collect

    return _collect(*args, **kwargs)


def pipeline_version_for(*args, **kwargs):
    """The cache's pipeline version for a config. See `extract.pipeline_version_for`."""
    from tools.ingest.evidence.extract import pipeline_version_for as _version

    return _version(*args, **kwargs)


KIND_NATIVE_TEXT = "native_text"
KIND_OCR = "ocr"
KIND_VISUAL = "visual"
KIND_TRANSCRIPT = "transcript"
KIND_STRUCTURE = "structure"
