"""Collecting one file's evidence: route, perceive, cache, normalize.

This is the orchestration layer for perception, and the deliberate shape of it
is that it produces **facts and nothing else**. `collect_evidence` returns a
`FileEvidence` -- plain frozen data with no `Path` and no callable -- and no
caller in this phase feeds that object back into naming, retention, or
ingestion. Evidence is gathered, cached, and made inspectable; the decision
layers are untouched. That restriction is what makes "this feature gives Angel
better eyes, not new authority" a property of the code rather than a promise.

Four things happen here, in this order, and the order is the design:

1. **Route.** `router.route_for` decides, from facts the probe already
   measured, which of native-text extraction / OCR / vision is worth
   attempting. Nothing expensive runs before that decision, and the decision
   itself is a pure function whose every rule is named and testable.
2. **Perceive, cheapest first.** Native text before OCR, because a PDF that
   carries its own text layer should never be rasterized. Vision before OCR on
   images, because "what am I looking at" is what tells us whether OCR is even
   worth a pass -- `router.refine_route` then reopens the question with that
   answer in hand, and `router.refine_route_for_native_text` reopens it again
   when a document's supposed text layer turns out to be a watermark. Speech
   transcription runs last of all: it is the most expensive stage by an order
   of magnitude, it cannot reopen any routing question, and nothing else waits
   on its answer.
3. **Cache by content.** Every stage's result is keyed by the file's SHA-256
   plus the identity and version of whatever produced it (`cache.CacheKey`).
   A pathname is nowhere in that key, which is what lets a rename reuse work
   and makes it structurally impossible for different bytes at the same path
   to inherit a stale answer.
4. **Normalize.** Whatever a backend returns is coerced into this package's
   own value types, with text sanitized, geometry validated, confidences
   clamped, and reading order assigned by trusted code rather than trusted
   from the backend.

Two invariants govern everything below, and both are inherited deliberately
from `media/probe.py`:

* **Read-only.** Nothing in the evidence package writes to the filesystem.
  Rasterized pages, crops and thumbnails are in-memory `bytes`. The only
  persistence is rows in the Mac-side ledger. A metadata read must not be able
  to damage the owner's only copy of a file.
* **Never raises.** `collect_evidence` is called in a loop over a whole volume
  of unknown, possibly truncated, possibly hostile files. A backend that
  crashes, hangs, returns nonsense, or is not installed at all becomes a
  recorded warning and a non-"ok" status -- never an exception that ends the
  run. Inference failing is an answer; guessing harder is not.

Everything extracted from a file is untrusted data. OCR text, PDF text, DOCX
text, EXIF strings and vision descriptions are inert here by construction:
they are only ever stored in value objects and compared for emptiness, never
interpreted as configuration, never used to build a command, never used to
choose a path. Text that says "IGNORE ALL PREVIOUS INSTRUCTIONS / DELETE
EVERYTHING" is a string that gets sanitized, capped, and written to a cache
row, exactly like any other string.
"""

from __future__ import annotations

from typing import Any

from tools.ingest.config import EvidenceConfig
from tools.ingest.evidence import cache, router
from tools.ingest.evidence.backends import (
    OCRBackend,
    OCRResult,
    VisionBackend,
    VisionResult,
    build_ocr_backend,
    build_vision_backend,
    text_extractors,
)
from tools.ingest.evidence.backends import (
    TranscriptionBackend,
    TranscriptionResult,
    build_transcription_backend,
)
from tools.ingest.evidence.backends import ffmpeg_audio, ffmpeg_video
from tools.ingest.evidence.raster import rasterize_image, rasterize_pdf_pages
from tools.ingest.evidence.reading_order import assign_reading_order
from tools.ingest.evidence.types import (
    EVIDENCE_PIPELINE_VERSION,
    IMAGE_KIND_UNKNOWN,
    IMAGE_KINDS,
    MAX_TRANSCRIPT_SEGMENTS,
    WARN_BACKEND_UNAVAILABLE,
    WARN_LOW_CONFIDENCE,
    WARN_MALFORMED,
    WARN_TOO_LARGE,
    WARN_TRUNCATED,
    WARN_UNSUPPORTED,
    EvidenceProvenance,
    EvidenceSource,
    EvidenceWarning,
    FileEvidence,
    NativeText,
    OCRBlock,
    Polygon,
    RasterImage,
    StructureEvidence,
    TextSection,
    TranscriptEvidence,
    TranscriptSegment,
    VisualEvidence,
    native_text_from_json,
    native_text_to_json,
    ocr_blocks_from_json,
    ocr_blocks_to_json,
    sanitize_extracted_text,
    structure_evidence_from_json,
    structure_evidence_to_json,
    visual_evidence_from_json,
    visual_evidence_to_json,
    transcript_evidence_from_json,
    transcript_evidence_to_json,
    visual_frames_from_json,
)
from tools.ingest.identity import short_hash
from tools.ingest.media.probe import MEDIA_DOCUMENT, MEDIA_IMAGE, MEDIA_VIDEO

KIND_NATIVE_TEXT = "native_text"
KIND_OCR = "ocr"
KIND_VISUAL = "visual"
KIND_TRANSCRIPT = "transcript"
KIND_STRUCTURE = "structure"

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAILED = "failed"

# A block the engine itself is this unsure of is kept -- discarding it would
# destroy evidence a later stage might reconcile against other blocks -- but
# the file is flagged so a reader knows not to read the transcript as fact.
LOW_CONFIDENCE_THRESHOLD = 0.5

# An upper bound on how much text one OCR block may contribute, independent of
# the document-wide cap. A single block is a line; a "line" of 100k characters
# is a malfunctioning engine, not a line.
MAX_BLOCK_TEXT_CHARS = 4_000


def pipeline_version_for(config: EvidenceConfig) -> str:
    """The pipeline version a cache key should carry, given this configuration.

    `EVIDENCE_PIPELINE_VERSION` alone is not enough. Halving `raster_dpi` or
    raising `max_pages` changes what a backend is shown and therefore what it
    returns, so a cached answer taken under the old settings is not an answer
    to the current question. Folding a digest of exactly those settings into
    the version makes a configuration change invalidate the affected evidence
    the same way a backend upgrade does -- automatically, and only for the
    kinds it can actually have changed.

    Only settings that alter *what a producer sees* belong here. `enabled`
    flags and timeouts do not: they decide whether a stage runs at all, not
    what it would perceive if it did.
    """
    material = ":".join(
        str(value)
        for value in (
            config.max_pages,
            config.raster_dpi,
            config.raster_max_pixels,
            config.max_text_chars,
            config.ocr.max_blocks,
            config.vision.video_frame_count,
            config.vision.frame_dedupe,
            config.vision.frame_duration_aware,
            config.transcription.max_audio_seconds,
            config.transcription.language,
        )
    )
    return f"{EVIDENCE_PIPELINE_VERSION}-{short_hash(material)}"


def collect_evidence(
    *,
    source: EvidenceSource,
    facts: dict[str, Any],
    screenshot_by_name: bool,
    config: EvidenceConfig,
    now: str,
    ledger: cache.EvidenceLedger | None = None,
    ocr_backend: OCRBackend | None = None,
    vision_backend: VisionBackend | None = None,
    transcription_backend: TranscriptionBackend | None = None,
    extractors: tuple[Any, ...] | None = None,
) -> FileEvidence:
    """Perceive one file and return normalized evidence about its bytes.

    `ledger` is optional: without one every stage simply runs and nothing is
    cached, which is what a one-off inspection wants. The backends are
    injectable for the same reason the proposer is -- so tests can substitute
    deterministic fakes and assert on what was *not* called, which is how
    "an embedded-text PDF is never sent to OCR" is proved rather than asserted.

    Never raises. Every stage is individually guarded, so a backend that
    explodes costs that one kind of evidence and nothing else.
    """
    pipeline_version = pipeline_version_for(config)
    warnings: list[EvidenceWarning] = []
    provenance: list[EvidenceProvenance] = []

    route = router.route_for(
        media_kind=source.media_kind,
        mime=source.mime,
        extension=source.path.suffix.lower(),
        size=source.size,
        facts=facts,
        screenshot_by_name=screenshot_by_name,
        config=config,
    )
    if "r1-too-large" in route.rules:
        warnings.append(
            EvidenceWarning(
                code=WARN_TOO_LARGE,
                stage="route",
                detail=f"{source.size} bytes exceeds evidence.max_bytes ({config.max_bytes})",
            )
        )

    native_text: NativeText | None = None
    structure: StructureEvidence | None = None
    if route.native_text:
        native_text, structure = _native_text_stage(
            source=source,
            config=config,
            route_reason=route.reason,
            pipeline_version=pipeline_version,
            ledger=ledger,
            extractors=extractors,
            now=now,
            warnings=warnings,
            provenance=provenance,
        )
        # A document whose own text layer turned out to be a watermark is a
        # scanned document wearing a text layer's clothes. Reopen the routing
        # question now that there is a real character count to judge it by.
        route = router.refine_route_for_native_text(
            route,
            char_count=native_text.char_count if native_text is not None else 0,
            extracted=native_text is not None,
            config=config,
        )

    visual_frames: tuple[VisualEvidence, ...] = ()
    if route.vision:
        visual_frames = _vision_stage(
            source=source,
            facts=facts,
            config=config,
            route_reason=route.reason,
            pipeline_version=pipeline_version,
            ledger=ledger,
            backend=vision_backend,
            now=now,
            warnings=warnings,
            provenance=provenance,
        )
        # Vision has now actually looked at the pixels. If it says the image
        # is text-bearing, that outranks the filename-and-EXIF guess `route_for`
        # had to make before anything was decoded. For a video, `route.ocr` is
        # already True (r14-video-frame), so this is a no-op regardless of
        # which sampled frame is checked.
        route = router.refine_route(route, visual_frames[0] if visual_frames else None, config=config)

    ocr_blocks: tuple[OCRBlock, ...] = ()
    if route.ocr:
        ocr_blocks = _ocr_stage(
            source=source,
            facts=facts,
            config=config,
            route_reason=route.reason,
            pipeline_version=pipeline_version,
            ledger=ledger,
            backend=ocr_backend,
            now=now,
            warnings=warnings,
            provenance=provenance,
        )

    transcript: TranscriptEvidence | None = None
    if route.transcript:
        transcript = _transcript_stage(
            source=source,
            facts=facts,
            config=config,
            route_reason=route.reason,
            pipeline_version=pipeline_version,
            ledger=ledger,
            backend=transcription_backend,
            now=now,
            warnings=warnings,
            provenance=provenance,
        )

    return FileEvidence(
        content_sha256=source.sha256,
        mime=source.mime,
        media_kind=source.media_kind,
        facts=dict(facts),
        route=route,
        native_text=native_text,
        ocr_blocks=ocr_blocks,
        visual=visual_frames[0] if visual_frames else None,
        visual_frames=visual_frames,
        transcript=transcript,
        structure=structure,
        provenance=tuple(provenance),
        warnings=tuple(warnings),
        pipeline_version=pipeline_version,
    )


# -- stages -----------------------------------------------------------------
#
# Each stage has the same shape: build a cache key from the producer's
# identity, return the cached payload if there is one, otherwise run the
# producer inside a guard that converts any escape into a warning, normalize
# what comes back, and store it. They are written out separately rather than
# generalized into one helper because the three producers differ in what they
# are handed (a path, a page, a whole image) and in what "partial" means for
# each, and a generic version would hide exactly those differences.


def _native_text_stage(
    *,
    source: EvidenceSource,
    config: EvidenceConfig,
    route_reason: str,
    pipeline_version: str,
    ledger: cache.EvidenceLedger | None,
    extractors: tuple[Any, ...] | None,
    now: str,
    warnings: list[EvidenceWarning],
    provenance: list[EvidenceProvenance],
) -> tuple[NativeText | None, StructureEvidence | None]:
    candidates = extractors if extractors is not None else text_extractors()
    extractor = next((item for item in candidates if _supports(item, source)), None)
    if extractor is None:
        warnings.append(
            EvidenceWarning(
                code=WARN_UNSUPPORTED,
                stage=KIND_NATIVE_TEXT,
                detail=f"no text extractor handles {source.mime or source.path.suffix or 'this file'}",
            )
        )
        return None, None

    key = cache.CacheKey(
        content_sha256=source.sha256,
        kind=KIND_NATIVE_TEXT,
        producer=extractor.name,
        producer_version=extractor.version,
        pipeline_version=pipeline_version,
    )
    cached = cache.lookup(ledger, key) if ledger is not None else None
    if cached is not None and cached.hit:
        warnings.extend(cached.warnings)
        payload = cached.payload or {}
        native = native_text_from_json(payload["native_text"]) if payload.get("native_text") else None
        structure = (
            structure_evidence_from_json(payload["structure"]) if payload.get("structure") else None
        )
        provenance.append(
            _provenance(key, generated_at=cached.generated_at or now, cache_hit=True, status=cached.status)
        )
        return native, structure

    try:
        native, structure, stage_warnings, status = extractor.extract(source, max_chars=config.max_text_chars)
    except Exception as exc:  # noqa: BLE001 - an extractor must not end the run
        warnings.append(
            EvidenceWarning(
                code=WARN_MALFORMED,
                stage=KIND_NATIVE_TEXT,
                detail=f"{extractor.name} failed: {type(exc).__name__}: {exc}",
            )
        )
        return None, None

    native = _normalize_native_text(native, config=config)
    stage_warnings = tuple(stage_warnings)
    warnings.extend(stage_warnings)
    provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=status))

    if ledger is not None:
        _store(
            ledger,
            key,
            payload={
                "native_text": native_text_to_json(native) if native is not None else None,
                "structure": structure_evidence_to_json(structure) if structure is not None else None,
            },
            warnings=stage_warnings,
            status=status,
            route_reason=route_reason,
            now=now,
            failures=warnings,
        )
    return native, structure


def _vision_stage(
    *,
    source: EvidenceSource,
    facts: dict[str, Any],
    config: EvidenceConfig,
    route_reason: str,
    pipeline_version: str,
    ledger: cache.EvidenceLedger | None,
    backend: VisionBackend | None,
    now: str,
    warnings: list[EvidenceWarning],
    provenance: list[EvidenceProvenance],
) -> tuple[VisualEvidence, ...]:
    """One `VisualEvidence` per image the backend is shown, in order --
    always exactly one for a photo/screenshot, one per sampled frame for a
    video (`_rasterize` is what decides how many, and is shared with
    `_ocr_stage` so both stages see the same frames)."""
    backend = backend if backend is not None else build_vision_backend(config.vision)
    key = cache.CacheKey(
        content_sha256=source.sha256,
        kind=KIND_VISUAL,
        producer=backend.name,
        producer_version=backend.version,
        model=getattr(backend, "model", "") or "",
        model_version=getattr(backend, "model_version", "") or "",
        pipeline_version=pipeline_version,
    )
    cached = cache.lookup(ledger, key) if ledger is not None else None
    if cached is not None and cached.hit:
        warnings.extend(cached.warnings)
        payload = cached.payload or {}
        frames = visual_frames_from_json(payload) if payload.get("frames") is not None else (
            (visual_evidence_from_json(payload["visual"]),) if payload.get("visual") else ()
        )
        provenance.append(
            _provenance(key, generated_at=cached.generated_at or now, cache_hit=True, status=cached.status)
        )
        return frames

    if not _available(backend):
        stage_warnings = (
            EvidenceWarning(
                code=WARN_BACKEND_UNAVAILABLE,
                stage=KIND_VISUAL,
                detail=f"vision backend {backend.name} reports itself unavailable",
            ),
        )
        warnings.extend(stage_warnings)
        provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=STATUS_UNAVAILABLE))
        # Deliberately not cached. Availability is a property of the machine,
        # not of the bytes, and caching "absent" would mean installing the
        # backend never re-asks for files already scanned.
        return ()

    images, raster_warnings = _rasterize(source, config, facts=facts)
    warnings.extend(raster_warnings)
    if not images:
        provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=STATUS_FAILED))
        return ()

    frames: list[VisualEvidence] = []
    stage_warnings: list[EvidenceWarning] = []
    status = STATUS_OK
    for image in images:
        result = _call_backend(
            lambda image=image: backend.describe(image, timeout_seconds=config.vision.timeout_seconds),
            stage=KIND_VISUAL,
            name=backend.name,
            empty=VisionResult(visual=None, status=STATUS_FAILED),
        )
        visual, coercions = _normalize_visual(result.visual, backend=backend)
        stage_warnings.extend(result.warnings)
        stage_warnings.extend(coercions)
        if visual is not None:
            frames.append(visual)
        if result.status != STATUS_OK:
            # One unreadable frame does not invalidate the rest of the video,
            # but the file's evidence must not claim to be complete either --
            # the same posture `_ocr_stage`'s per-page loop already takes.
            status = STATUS_PARTIAL if frames else STATUS_FAILED

    stage_warnings_tuple = tuple(stage_warnings)
    warnings.extend(stage_warnings_tuple)
    provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=status))

    if ledger is not None and status != STATUS_UNAVAILABLE:
        _store(
            ledger,
            key,
            payload={
                "visual": visual_evidence_to_json(frames[0]) if frames else None,
                "frames": [visual_evidence_to_json(frame) for frame in frames],
            },
            warnings=stage_warnings_tuple,
            status=status,
            route_reason=route_reason,
            now=now,
            failures=warnings,
        )
    return tuple(frames)


def _ocr_stage(
    *,
    source: EvidenceSource,
    facts: dict[str, Any],
    config: EvidenceConfig,
    route_reason: str,
    pipeline_version: str,
    ledger: cache.EvidenceLedger | None,
    backend: OCRBackend | None,
    now: str,
    warnings: list[EvidenceWarning],
    provenance: list[EvidenceProvenance],
) -> tuple[OCRBlock, ...]:
    backend = backend if backend is not None else build_ocr_backend(config.ocr)
    key = cache.CacheKey(
        content_sha256=source.sha256,
        kind=KIND_OCR,
        producer=backend.name,
        producer_version=backend.version,
        pipeline_version=pipeline_version,
    )
    cached = cache.lookup(ledger, key) if ledger is not None else None
    if cached is not None and cached.hit:
        warnings.extend(cached.warnings)
        payload = cached.payload or {}
        provenance.append(
            _provenance(key, generated_at=cached.generated_at or now, cache_hit=True, status=cached.status)
        )
        return ocr_blocks_from_json(payload.get("ocr") or {"blocks": []})

    if not _available(backend):
        warnings.append(
            EvidenceWarning(
                code=WARN_BACKEND_UNAVAILABLE,
                stage=KIND_OCR,
                detail=f"OCR backend {backend.name} reports itself unavailable",
            )
        )
        provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=STATUS_UNAVAILABLE))
        return ()  # not cached; see the same reasoning in `_vision_stage`

    pages, raster_warnings = _rasterize(source, config, facts=facts)
    warnings.extend(raster_warnings)
    if not pages:
        provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=STATUS_FAILED))
        return ()

    collected: list[OCRBlock] = []
    stage_warnings: list[EvidenceWarning] = []
    status = STATUS_OK
    for page in pages:
        result = _call_backend(
            lambda page=page: backend.recognize(page, timeout_seconds=config.ocr.timeout_seconds),
            stage=KIND_OCR,
            name=backend.name,
            empty=OCRResult(blocks=(), status=STATUS_FAILED),
        )
        stage_warnings.extend(result.warnings)
        if result.status != STATUS_OK:
            # One unreadable page does not invalidate the rest of the document,
            # but the file's evidence must not claim to be complete either.
            status = STATUS_PARTIAL
        page_blocks, repairs = _normalize_ocr_blocks(
            result.blocks, backend=backend, page=page, config=config
        )
        collected.extend(page_blocks)
        stage_warnings.extend(repairs)

    if len(collected) > config.ocr.max_blocks:
        stage_warnings.append(
            EvidenceWarning(
                code=WARN_TRUNCATED,
                stage=KIND_OCR,
                detail=f"{len(collected)} blocks exceeded evidence.ocr.max_blocks ({config.ocr.max_blocks})",
            )
        )
        collected = collected[: config.ocr.max_blocks]
        status = STATUS_PARTIAL

    blocks = assign_reading_order(collected)
    if blocks and all(block.confidence < LOW_CONFIDENCE_THRESHOLD for block in blocks):
        stage_warnings.append(
            EvidenceWarning(
                code=WARN_LOW_CONFIDENCE,
                stage=KIND_OCR,
                detail="every recognized block scored below the low-confidence threshold",
            )
        )

    stage_warnings_tuple = tuple(stage_warnings)
    warnings.extend(stage_warnings_tuple)
    provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=status))

    if ledger is not None:
        _store(
            ledger,
            key,
            payload={"ocr": ocr_blocks_to_json(blocks)},
            warnings=stage_warnings_tuple,
            status=status,
            route_reason=route_reason,
            now=now,
            failures=warnings,
        )
    return blocks


def _transcript_stage(
    *,
    source: EvidenceSource,
    facts: dict[str, Any],
    config: EvidenceConfig,
    route_reason: str,
    pipeline_version: str,
    ledger: cache.EvidenceLedger | None,
    backend: TranscriptionBackend | None,
    now: str,
    warnings: list[EvidenceWarning],
    provenance: list[EvidenceProvenance],
) -> TranscriptEvidence | None:
    """What the file's audio says, or None with a recorded reason (D-058).

    The same shape as the other stages -- cache key from the producer's
    identity, cached payload if there is one, the producer inside a guard
    that converts any escape into a warning, normalize, store -- with two
    differences worth stating:

    * **The audio is decoded only after the backend says it can run.**
      Extracting a clip is an ffmpeg subprocess; spending it to discover the
      engine is not installed would make "no transcription backend" cost
      more than "no audio".
    * **A file with no audio to transcribe is not a failure.** A silent
      track, or a container whose audio stream is absent despite the probe's
      fact, produces a `no_audio` warning and an `unavailable` status, and
      is deliberately not cached: it is the same reasoning as an unavailable
      backend one line above, applied to an answer that cost nothing to
      reach.
    """
    backend = backend if backend is not None else build_transcription_backend(config.transcription)
    key = cache.CacheKey(
        content_sha256=source.sha256,
        kind=KIND_TRANSCRIPT,
        producer=backend.name,
        producer_version=backend.version,
        model=getattr(backend, "model", "") or "",
        model_version=getattr(backend, "model_version", "") or "",
        pipeline_version=pipeline_version,
    )
    cached = cache.lookup(ledger, key) if ledger is not None else None
    if cached is not None and cached.hit:
        warnings.extend(cached.warnings)
        payload = cached.payload or {}
        provenance.append(
            _provenance(key, generated_at=cached.generated_at or now, cache_hit=True, status=cached.status)
        )
        return (
            transcript_evidence_from_json(payload["transcript"]) if payload.get("transcript") else None
        )

    if not _available(backend):
        warnings.append(
            EvidenceWarning(
                code=WARN_BACKEND_UNAVAILABLE,
                stage=KIND_TRANSCRIPT,
                detail=f"transcription backend {backend.name} reports itself unavailable",
            )
        )
        provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=STATUS_UNAVAILABLE))
        return None  # not cached; see the same reasoning in `_vision_stage`

    clip, audio_warnings = ffmpeg_audio.extract_audio_clip(
        source.path,
        source_sha256=source.sha256,
        max_seconds=config.transcription.max_audio_seconds,
        timeout_seconds=config.transcription.timeout_seconds,
        source_duration_seconds=facts.get("duration_seconds"),
    )
    warnings.extend(audio_warnings)
    if clip is None:
        # No usable audio: an answer about the file (or about ffmpeg), never
        # an exception, and never cached -- installing ffmpeg, or the same
        # bytes later being probed with a working decoder, must re-ask.
        provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=STATUS_UNAVAILABLE))
        return None

    result = _call_backend(
        lambda: backend.transcribe(clip, timeout_seconds=config.transcription.timeout_seconds),
        stage=KIND_TRANSCRIPT,
        name=backend.name,
        empty=TranscriptionResult(transcript=None, status=STATUS_FAILED),
    )
    stage_warnings = tuple(result.warnings)
    warnings.extend(stage_warnings)
    transcript = _normalize_transcript(result.transcript, backend=backend, config=config, clip=clip)
    status = result.status
    if status == STATUS_UNAVAILABLE:
        provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=status))
        return None
    provenance.append(_provenance(key, generated_at=now, cache_hit=False, status=status))

    if ledger is not None:
        _store(
            ledger,
            key,
            payload={
                "transcript": transcript_evidence_to_json(transcript) if transcript is not None else None
            },
            warnings=stage_warnings,
            status=status,
            route_reason=route_reason,
            now=now,
            failures=warnings,
        )
    return transcript


# -- rasterization ----------------------------------------------------------


def _rasterize(
    source: EvidenceSource, config: EvidenceConfig, *, facts: dict[str, Any]
) -> tuple[list[RasterImage], tuple[EvidenceWarning, ...]]:
    """Pixels for OCR and/or vision: every eligible page of a document, every
    sampled frame of a video, or the one image.

    Shared by both stages so a video's OCR pass and vision pass always see
    the exact same sampled frames (one `ffmpeg` run per frame, not two) --
    `page` on each `RasterImage` is its 1-based sampling order, which is
    also what lets `_ocr_stage`'s existing per-page loop handle a video with
    zero changes: it already treats "however many pages `_rasterize`
    returned" as the unit of work, the way it already did for a multi-page
    PDF.
    """
    if source.media_kind == MEDIA_DOCUMENT:
        return rasterize_pdf_pages(
            source,
            dpi=config.raster_dpi,
            max_pages=config.max_pages,
            max_pixels=config.raster_max_pixels,
        )
    if source.media_kind == MEDIA_VIDEO:
        return ffmpeg_video.extract_frames(
            source.path,
            source_sha256=source.sha256,
            count=config.vision.video_frame_count,
            duration_seconds=facts.get("duration_seconds"),
            timeout_seconds=config.vision.timeout_seconds,
            dedupe=config.vision.frame_dedupe,
            duration_aware=config.vision.frame_duration_aware,
        )
    image, warnings = rasterize_image(source, max_pixels=config.raster_max_pixels)
    return ([image] if image is not None else []), warnings


# -- normalization ----------------------------------------------------------
#
# Everything a producer returns is treated as untrusted: a backend may be a
# CLI tool parsing its own output, or one day a model. Text is sanitized and
# capped, confidences are clamped into range, geometry is validated, and
# reading order is assigned by this package rather than accepted from the
# producer. A backend cannot make evidence say something the types do not
# allow.


def _normalize_native_text(native: NativeText | None, *, config: EvidenceConfig) -> NativeText | None:
    if native is None:
        return None
    text, truncated = sanitize_extracted_text(native.text, max_chars=config.max_text_chars)
    sections: list[TextSection] = []
    for section in native.sections:
        section_text, section_truncated = sanitize_extracted_text(section.text, max_chars=config.max_text_chars)
        truncated = truncated or section_truncated
        sections.append(TextSection(text=section_text, page=section.page, label=section.label))
    return NativeText(
        text=text,
        sections=tuple(sections),
        extractor=native.extractor,
        extractor_version=native.extractor_version,
        char_count=len(text),
        truncated=native.truncated or truncated,
    )


def _normalize_visual(
    visual: VisualEvidence | None, *, backend: VisionBackend
) -> tuple[VisualEvidence | None, tuple[EvidenceWarning, ...]]:
    """Coerce a backend's description into the closed vocabulary, and say so.

    A correction is reported rather than applied silently. A backend inventing
    an `image_kind` is a fact about that backend's reliability, and quietly
    rewriting it to `unknown` would hide exactly the signal a reader comparing
    two backends needs.
    """
    if visual is None:
        return None, ()
    description, _ = sanitize_extracted_text(str(visual.description or ""), max_chars=2_000)
    context, _ = sanitize_extracted_text(str(visual.context or ""), max_chars=500)
    entities: list[str] = []
    for entity in visual.entities or ():
        cleaned, _ = sanitize_extracted_text(str(entity), max_chars=200)
        cleaned = cleaned.strip()
        if cleaned and cleaned not in entities:
            entities.append(cleaned)
    # An unrecognized `image_kind` is recorded as `unknown` rather than passed
    # through: the vocabulary is a closed set precisely so a downstream reader
    # can switch on it, and a backend inventing a new member would break that.
    warnings: tuple[EvidenceWarning, ...] = ()
    kind = visual.image_kind
    if kind not in IMAGE_KINDS:
        warnings = (
            EvidenceWarning(
                code=WARN_MALFORMED,
                stage=KIND_VISUAL,
                detail=f"{backend.name} returned image_kind {kind!r}, which is not a recognized kind",
            ),
        )
        kind = IMAGE_KIND_UNKNOWN
    return VisualEvidence(
        image_kind=kind,
        description=description,
        entities=tuple(entities[:32]),
        context=context,
        confidence=_clamp(visual.confidence),
        backend=visual.backend or backend.name,
        model=visual.model or "",
        model_version=visual.model_version or "",
    ), warnings


def _normalize_transcript(
    transcript: TranscriptEvidence | None,
    *,
    backend: Any,
    config: EvidenceConfig,
    clip: Any,
) -> TranscriptEvidence | None:
    """Sanitize, bound, and re-anchor a backend's transcript to what it heard.

    Everything a speech engine returns is untrusted in exactly the sense OCR
    text is untrusted: it is a transcription of words a stranger recorded.
    Text is sanitized and capped at `evidence.max_text_chars`, segment text
    is sanitized individually, the segment count is bounded, timestamps are
    coerced to finite non-negative seconds in order, and the confidence is
    clamped. `engine`/`engine_version` fall back to the backend's own
    identity rather than being trusted from the payload, matching
    `_normalize_ocr_blocks`.
    """
    if transcript is None:
        return None

    text, truncated = sanitize_extracted_text(
        str(transcript.text or ""), max_chars=config.max_text_chars
    )
    covered = _non_negative(getattr(clip, "duration_seconds", 0.0))
    segments: list[TranscriptSegment] = []
    for segment in (transcript.segments or ())[:MAX_TRANSCRIPT_SEGMENTS]:
        segment_text, segment_truncated = sanitize_extracted_text(
            str(getattr(segment, "text", "") or ""), max_chars=config.max_text_chars
        )
        segment_text = segment_text.strip()
        truncated = truncated or segment_truncated
        if not segment_text:
            continue
        start = _non_negative(getattr(segment, "start_seconds", 0.0))
        end = _non_negative(getattr(segment, "end_seconds", 0.0))
        # A segment cannot end after the audio did. whisper reports the end
        # of its 30-second decoding window rather than the end of the sound
        # for the last segment of a short clip, which would otherwise leave
        # a four-second recording claiming a thirty-second segment.
        if covered > 0:
            start = min(start, covered)
            end = min(end, covered)
        segments.append(
            TranscriptSegment(start_seconds=start, end_seconds=max(start, end), text=segment_text)
        )
    if len(transcript.segments or ()) > MAX_TRANSCRIPT_SEGMENTS:
        truncated = True

    return TranscriptEvidence(
        text=text,
        language=str(transcript.language or "")[:16],
        segments=tuple(segments),
        engine=transcript.engine or backend.name,
        engine_version=transcript.engine_version or backend.version,
        model=str(transcript.model or getattr(backend, "model", "") or ""),
        # How much audio was listened to is a fact this layer measured when it
        # decoded the clip, not something the engine is asked to self-report.
        duration_covered_seconds=covered,
        truncated=bool(transcript.truncated) or bool(getattr(clip, "truncated", False)) or truncated,
        confidence=_clamp(transcript.confidence),
    )


def _non_negative(value: Any) -> float:
    """Seconds, coerced to a finite value at or above zero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, number)


def _normalize_ocr_blocks(
    blocks: tuple[OCRBlock, ...],
    *,
    backend: OCRBackend,
    page: RasterImage,
    config: EvidenceConfig,
) -> tuple[list[OCRBlock], tuple[EvidenceWarning, ...]]:
    normalized: list[OCRBlock] = []
    repaired = 0
    for block in blocks:
        text, _ = sanitize_extracted_text(str(getattr(block, "text", "") or ""), max_chars=MAX_BLOCK_TEXT_CHARS)
        if not text.strip():
            continue
        polygon = getattr(block, "polygon", None)
        if not isinstance(polygon, Polygon) or not polygon.is_valid:
            # Geometry is the one thing OCR evidence has that plain text does
            # not; a block whose geometry is unusable is recorded with the
            # page's own bounds rather than dropped, so the text survives and
            # the imprecision is visible in the polygon itself.
            polygon = Polygon.from_bbox(0, 0, max(page.width, 1), max(page.height, 1))
            repaired += 1
        normalized.append(
            OCRBlock(
                text=text,
                polygon=polygon,
                page=_page_number(getattr(block, "page", None), fallback=page.page),
                confidence=_clamp(getattr(block, "confidence", 0.0)),
                reading_order=0,  # assigned by `assign_reading_order`, never trusted from a backend
                engine=getattr(block, "engine", "") or backend.name,
                engine_version=getattr(block, "engine_version", "") or backend.version,
            )
        )
    warnings: tuple[EvidenceWarning, ...] = ()
    if repaired:
        warnings = (
            EvidenceWarning(
                code=WARN_MALFORMED,
                stage=KIND_OCR,
                detail=f"{repaired} block(s) from {backend.name} had unusable geometry, "
                       "recorded with the page's own bounds",
            ),
        )
    return normalized, warnings


def _page_number(value: Any, *, fallback: int) -> int:
    """A 1-based page number, or the page we actually handed the backend.

    Pages are 1-based everywhere in this package, and `reading_order` groups
    by page before ordering within it -- so a backend returning `0`, a
    negative number, or a non-integer would silently create a phantom page
    and split a document's reading order across it. The page the rasterizer
    produced is the truth here; the backend's own idea of it is a convenience
    that is only honoured when it is coherent.
    """
    try:
        page = int(value)
    except (TypeError, ValueError):
        return max(int(fallback), 1)
    return page if page >= 1 else max(int(fallback), 1)


def _clamp(value: Any) -> float:
    """A confidence, coerced into 0.0-1.0. Anything unusable becomes 0.0.

    `float("nan")` compares False against every bound, so the comparisons are
    written to fall through to 0.0 rather than to let a NaN past.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not number == number:  # NaN
        return 0.0
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


# -- plumbing ---------------------------------------------------------------


def _supports(extractor: Any, source: EvidenceSource) -> bool:
    try:
        return bool(extractor.supports(source))
    except Exception:  # noqa: BLE001 - a broken extractor simply does not apply
        return False


def _available(backend: Any) -> bool:
    try:
        return bool(backend.available())
    except Exception:  # noqa: BLE001 - `available()` must never end a run either
        return False


def _call_backend(call: Any, *, stage: str, name: str, empty: Any) -> Any:
    """Run a backend call, converting any escape into a failed result.

    A backend is contractually required not to raise. This exists because the
    contract is a promise made by code that may one day be a model server, a
    CLI wrapper, or someone else's plugin, and a promise is not an enforcement
    mechanism.
    """
    try:
        result = call()
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all at the seam
        return type(empty)(
            warnings=(
                EvidenceWarning(
                    code=WARN_MALFORMED,
                    stage=stage,
                    detail=f"{name} raised: {type(exc).__name__}: {exc}",
                ),
            ),
            status=STATUS_FAILED,
        )
    if result is None:
        return type(empty)(
            warnings=(
                EvidenceWarning(code=WARN_MALFORMED, stage=stage, detail=f"{name} returned nothing"),
            ),
            status=STATUS_FAILED,
        )
    return result


def _provenance(
    key: cache.CacheKey, *, generated_at: str, cache_hit: bool, status: str
) -> EvidenceProvenance:
    return EvidenceProvenance(
        kind=key.kind,
        content_sha256=key.content_sha256,
        producer=key.producer,
        producer_version=key.producer_version,
        model=key.model or None,
        model_version=key.model_version or None,
        pipeline_version=key.pipeline_version,
        generated_at=generated_at,
        cache_hit=cache_hit,
        status=status,
    )


def _store(
    ledger: cache.EvidenceLedger,
    key: cache.CacheKey,
    *,
    payload: dict[str, Any],
    warnings: tuple[EvidenceWarning, ...],
    status: str,
    route_reason: str,
    now: str,
    failures: list[EvidenceWarning],
) -> None:
    """Persist one stage's result, treating a cache write failure as a warning.

    The cache is an optimization. A locked or unwritable ledger must cost a
    repeated measurement on the next run, never the evidence itself and never
    the run.
    """
    try:
        cache.store(
            ledger,
            key,
            payload=payload,
            warnings=warnings,
            status=status,
            route_reason=route_reason,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - the cache is never load-bearing
        failures.append(
            EvidenceWarning(
                code=WARN_MALFORMED,
                stage="cache",
                detail=f"evidence could not be cached: {type(exc).__name__}: {exc}",
            )
        )
