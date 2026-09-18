"""Fusing one file's perceptions into a bounded, attributed summary.

A `FileEvidence` for a video can hold container facts from ffprobe, a
transcript from a speech engine, OCR text from every sampled frame, a visual
description per frame, and a dozen warnings. Every consumer that wants to
*read* that -- the `organize evidence` CLI, the Naming Lab's review screen, a
future naming bridge -- otherwise writes its own loop over the same five
places, and each one invents its own answer to the same four questions:
how much text is too much, which frame's OCR wins when they disagree, what
to do about a frame that produced nothing, and where a given sentence
actually came from.

`perception_summary` answers those once. What comes back is deliberately:

* **Frozen plain data.** No `Path`, no callable, no `FileEvidence` reference.
  Same posture as every type in `types.py`.
* **Bounded.** Every text field has a hard character cap, every collection a
  hard count cap, applied here rather than trusted from the evidence.
* **Attributed.** Each field carries the producer that supplied it
  (`container_source`, `transcript_source`, `ocr_source`, and a `source` on
  each frame description). A reader must never have to guess whether a
  sentence was *said* in a file or *shown* in it -- that distinction is the
  whole reason speech is a separate seam from OCR (D-058).
* **Dependency-free.** This module imports nothing but `dataclasses` and this
  package's own `types`. In particular it imports nothing from `naming/`,
  `triage/`, or `interpretation/`, so adopting it cannot become the path by
  which perception reaches a decision.

The one genuinely opinionated rule
----------------------------------

**No single frame may dominate the OCR excerpt.** A video's frames are
sampled from one clip, so their OCR text is usually near-duplicate (the same
lower-third caption, the same watermark) and occasionally pathological (one
frame of dense credits worth more characters than the rest of the clip
combined). So the excerpt is built by:

1. taking each frame's blocks in reading order,
2. capping that frame's contribution at `MAX_OCR_CHARS_PER_FRAME`,
3. dropping a frame whose text is a case-insensitive repeat of text already
   taken from an earlier frame,
4. and only then capping the joined result at `MAX_OCR_EXCERPT_CHARS`.

Step 3 is per-frame, not per-line: a recurring caption collapses to one
mention, while two frames that genuinely say different things both survive.
`ocr_frames_seen` and `ocr_frames_used` report what that cost, so a reader
can tell "this clip showed one caption" from "this summary threw text away".

Nothing here is a decision, an interpretation, or a filename. It is the
left-hand column of `docs/17`'s table, made convenient to read.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.ingest.evidence.types import FileEvidence

# Every bound in one place, so "why is this text cut off" has one answer.
MAX_TRANSCRIPT_EXCERPT_CHARS = 2_000
MAX_OCR_EXCERPT_CHARS = 2_000
MAX_OCR_CHARS_PER_FRAME = 400
MAX_NATIVE_TEXT_EXCERPT_CHARS = 2_000
MAX_FRAME_DESCRIPTIONS = 12
MAX_DESCRIPTION_CHARS = 500
MAX_ENTITIES_PER_FRAME = 12
MAX_CONTAINER_FACTS = 24
MAX_WARNINGS = 32

# The container facts worth putting in front of a reader, in the order a
# person would want them. Anything else the probe measured stays in
# `FileEvidence.facts` -- this is a summary, not a second copy of the probe.
CONTAINER_FACT_KEYS = (
    "duration_seconds",
    "container_format",
    "video_codec",
    "audio_codec",
    "audio_present",
    "width",
    "height",
    "aspect_ratio",
    "frame_rate",
    "bit_rate",
    "capture_timestamp",
    "page_count",
)

SOURCE_PROBE = "probe:ffprobe-facts"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class SummaryProvenance:
    """One producer that contributed to this summary, and how it went."""

    kind: str  # "native_text" | "ocr" | "visual" | "transcript" | "structure"
    producer: str
    producer_version: str
    model: str
    status: str  # "ok" | "partial" | "unavailable" | "failed"
    cache_hit: bool


@dataclass(frozen=True)
class FrameDescription:
    """What the vision backend said about one sampled frame."""

    frame: int  # 1-based sampling index; may be non-contiguous after dedupe
    image_kind: str
    description: str
    entities: tuple[str, ...]
    confidence: float
    source: str  # "vision:<backend>[/<model>]"


@dataclass(frozen=True)
class SummaryWarning:
    code: str
    stage: str
    detail: str


@dataclass(frozen=True)
class PerceptionSummary:
    """Everything perceived about one file, bounded and attributed.

    Frozen plain data with no `Path` and no callable, like every other type
    in this package. Each `*_source` field names the producer that supplied
    the field beside it, or `SOURCE_NONE` when nothing did -- an empty
    excerpt with a named source means "the producer ran and found nothing",
    and an empty excerpt with no source means "nothing ran".
    """

    content_sha256: str
    media_kind: str
    mime: str | None
    rules: tuple[str, ...]  # the route rules that fired, verbatim
    route_reason: str

    # Measured container facts, straight from the probe. Never inference.
    container: tuple[tuple[str, str], ...]
    container_source: str

    # What the file SAYS.
    transcript_excerpt: str
    transcript_language: str
    transcript_segment_count: int
    transcript_duration_covered_seconds: float
    transcript_truncated: bool
    transcript_confidence: float
    transcript_source: str

    # What the file SHOWS as text.
    ocr_excerpt: str
    ocr_block_count: int
    ocr_frames_seen: int
    ocr_frames_used: int
    ocr_source: str

    # What the file DEPICTS.
    visual_frames: tuple[FrameDescription, ...]
    visual_source: str

    # A document's own text layer, for completeness: a summary of a file is
    # useless if it silently omits the one stage that already answered.
    native_text_excerpt: str
    native_text_char_count: int
    native_text_source: str

    warnings: tuple[SummaryWarning, ...]
    provenance: tuple[SummaryProvenance, ...]

    @property
    def has_transcript(self) -> bool:
        return self.transcript_source != SOURCE_NONE

    def warning_codes(self) -> tuple[str, ...]:
        return tuple(warning.code for warning in self.warnings)


def perception_summary(evidence: FileEvidence) -> PerceptionSummary:
    """Fuse one `FileEvidence` into a `PerceptionSummary`. Pure; never raises."""
    transcript = getattr(evidence, "transcript", None)
    native = evidence.native_text

    ocr_excerpt, block_count, frames_seen, frames_used = _ocr_excerpt(evidence)
    ocr_producer = _first_producer(evidence, "ocr")
    visual_producer = _first_producer(evidence, "visual")
    container = _container_facts(evidence)

    return PerceptionSummary(
        content_sha256=evidence.content_sha256,
        media_kind=evidence.media_kind,
        mime=evidence.mime,
        rules=tuple(evidence.route.rules),
        route_reason=evidence.route.reason,
        container=container,
        container_source=SOURCE_PROBE if container else SOURCE_NONE,
        transcript_excerpt=_cap(getattr(transcript, "text", "") or "", MAX_TRANSCRIPT_EXCERPT_CHARS),
        transcript_language=str(getattr(transcript, "language", "") or ""),
        transcript_segment_count=len(getattr(transcript, "segments", ()) or ()),
        transcript_duration_covered_seconds=float(
            getattr(transcript, "duration_covered_seconds", 0.0) or 0.0
        ),
        transcript_truncated=bool(getattr(transcript, "truncated", False)),
        transcript_confidence=float(getattr(transcript, "confidence", 0.0) or 0.0),
        transcript_source=(
            f"transcript:{getattr(transcript, 'engine', '') or 'unknown'}"
            f"{_model_suffix(getattr(transcript, 'model', ''))}"
            if transcript is not None
            else SOURCE_NONE
        ),
        ocr_excerpt=ocr_excerpt,
        ocr_block_count=block_count,
        ocr_frames_seen=frames_seen,
        ocr_frames_used=frames_used,
        ocr_source=f"ocr:{ocr_producer}" if block_count else SOURCE_NONE,
        visual_frames=_frame_descriptions(evidence),
        visual_source=f"vision:{visual_producer}" if evidence.visual_frames or evidence.visual else SOURCE_NONE,
        native_text_excerpt=_cap(getattr(native, "text", "") or "", MAX_NATIVE_TEXT_EXCERPT_CHARS),
        native_text_char_count=int(getattr(native, "char_count", 0) or 0),
        native_text_source=(
            f"native_text:{native.extractor}" if native is not None else SOURCE_NONE
        ),
        warnings=_warnings(evidence),
        provenance=tuple(
            SummaryProvenance(
                kind=record.kind,
                producer=record.producer,
                producer_version=record.producer_version,
                model=record.model or "",
                status=record.status,
                cache_hit=record.cache_hit,
            )
            for record in evidence.provenance
        ),
    )


# -- pieces -------------------------------------------------------------------


def _warnings(evidence: FileEvidence) -> tuple[SummaryWarning, ...]:
    """The file's warnings, bounded and with exact repeats collapsed.

    `FileEvidence.warnings` keeps every warning verbatim, including the same
    one twice: for a video, OCR and vision each ask `_rasterize` for the
    frames, so a frame-selection warning is honestly recorded once per stage
    that saw it. That is right for the stored evidence and wrong for a reader,
    who does not learn anything from being told twice that four frames were
    deduplicated. Collapsing happens here, in the display/fusion layer, and
    only for warnings identical in all three fields.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[SummaryWarning] = []
    for warning in evidence.warnings:
        key = (warning.code, warning.stage, warning.detail)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            SummaryWarning(code=warning.code, stage=warning.stage, detail=_cap(warning.detail, 300))
        )
        if len(out) >= MAX_WARNINGS:
            break
    return tuple(out)


def _container_facts(evidence: FileEvidence) -> tuple[tuple[str, str], ...]:
    """The named container facts this file actually has, in a fixed order.

    Stringified deliberately: a summary is for reading and printing, and a
    consumer that needs a number reads `FileEvidence.facts` instead of
    re-parsing a display string.
    """
    facts = evidence.facts or {}
    rows: list[tuple[str, str]] = []
    for key in CONTAINER_FACT_KEYS:
        if key not in facts:
            continue
        value = facts[key]
        if value is None:
            continue
        rows.append((key, _cap(str(value), 120)))
        if len(rows) >= MAX_CONTAINER_FACTS:
            break
    return tuple(rows)


def _ocr_excerpt(evidence: FileEvidence) -> tuple[str, int, int, int]:
    """`(excerpt, block_count, frames_seen, frames_used)`.

    The per-frame cap and per-frame de-duplication described in this module's
    docstring live here. Blocks are grouped by the page (frame) they came
    from and ordered by the reading order this package assigned -- never by
    the order a backend happened to return them in.
    """
    blocks = tuple(evidence.ocr_blocks or ())
    if not blocks:
        return "", 0, 0, 0

    by_frame: dict[int, list] = {}
    for block in blocks:
        by_frame.setdefault(int(block.page), []).append(block)

    pieces: list[str] = []
    seen_normalized: set[str] = set()
    frames_used = 0
    for frame in sorted(by_frame):
        ordered = sorted(by_frame[frame], key=lambda block: block.reading_order)
        frame_text = "\n".join(block.text for block in ordered).strip()
        if not frame_text:
            continue
        frame_text = _cap(frame_text, MAX_OCR_CHARS_PER_FRAME)
        normalized = " ".join(frame_text.lower().split())
        if normalized in seen_normalized:
            # The same caption again. Recording it twice would let one
            # recurring overlay outweigh everything else in the clip.
            continue
        seen_normalized.add(normalized)
        pieces.append(frame_text)
        frames_used += 1

    return _cap("\n".join(pieces), MAX_OCR_EXCERPT_CHARS), len(blocks), len(by_frame), frames_used


def _frame_descriptions(evidence: FileEvidence) -> tuple[FrameDescription, ...]:
    """One `FrameDescription` per frame the vision backend actually described.

    `visual_frames` is the source of truth when it is populated; `visual`
    alone (an older cached row, or a single image) is treated as one frame.
    The frame index is the 1-based position in sampling order -- which for a
    de-duplicated video is the index *within the kept frames*, since
    `VisualEvidence` itself carries no page number.
    """
    frames = tuple(evidence.visual_frames or ())
    if not frames and evidence.visual is not None:
        frames = (evidence.visual,)
    out: list[FrameDescription] = []
    for index, frame in enumerate(frames[:MAX_FRAME_DESCRIPTIONS], start=1):
        out.append(
            FrameDescription(
                frame=index,
                image_kind=frame.image_kind,
                description=_cap(frame.description, MAX_DESCRIPTION_CHARS),
                entities=tuple(frame.entities[:MAX_ENTITIES_PER_FRAME]),
                confidence=float(frame.confidence),
                source=f"vision:{frame.backend or 'unknown'}{_model_suffix(frame.model)}",
            )
        )
    return tuple(out)


def _first_producer(evidence: FileEvidence, kind: str) -> str:
    """The producer recorded for one evidence kind, or "unknown".

    Provenance is the authority on who produced what, so the summary reads it
    from there rather than from the values themselves -- which is what makes
    "this OCR text came from tesseract 5.5.3" survive a cache hit.
    """
    for record in evidence.provenance:
        if record.kind == kind:
            suffix = _model_suffix(record.model or "")
            return f"{record.producer}{suffix}"
    return "unknown"


def _model_suffix(model: str | None) -> str:
    model = (model or "").strip()
    return f"/{model}" if model else ""


def _cap(text: str, limit: int) -> str:
    """Bound one string, marking the cut so a reader knows there was more."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."
