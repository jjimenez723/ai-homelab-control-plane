"""The conservative decision of *which* perception stages a file is worth running.

`route_for` answers "given what the deterministic probe already measured
about this file, is it worth spending an OCR pass, a vision call, or a native
text extraction on it?" It never answers "what does the file mean" or "should
this be renamed/kept/ingested" -- those stay strictly downstream, and this
module is not allowed to reach them even by accident: it is a pure function
of its arguments, nothing else.

Purity is a safety property, not a style preference. This router runs ahead
of every backend call in the pipeline, so if it could do I/O, read config from
disk, or log, then a single hostile or malformed file could turn "decide
whether to look at this" into an attack surface of its own. It cannot: it
imports nothing but plain data types, dataclasses, and the handful of
`MEDIA_*` string constants that name a probe's classification. There is a
dedicated test that walks this file's AST and fails the build if that ever
stops being true.

Every rule that fires appends a stable, named id to `EvidenceRoute.rules`
(`r0-evidence-disabled`, `r1-too-large`, ...). The ids are part of the public
contract -- other code and tests key off them -- so once assigned an id's
meaning must not change; a new rule gets a new id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tools.ingest.config import EvidenceConfig
from tools.ingest.evidence.types import TEXT_BEARING_IMAGE_KINDS, VisualEvidence
from tools.ingest.media.probe import (
    MEDIA_ARCHIVE,
    MEDIA_AUDIO,
    MEDIA_DOCUMENT,
    MEDIA_IMAGE,
    MEDIA_INSTALLER,
    MEDIA_OTHER,
    MEDIA_TEXT,
    MEDIA_VIDEO,
)

# Media kinds that never carry anything worth perceiving: an archive's
# interesting facts are its listing (already in `media_metadata`), an
# installer is a binary blob, and audio/video get no evidence backend in this
# phase. Grouped under one rule id because the reason -- "opaque to this
# layer" -- is the same for all of them.
_OPAQUE_MEDIA_KINDS = frozenset({MEDIA_ARCHIVE, MEDIA_INSTALLER, MEDIA_AUDIO, MEDIA_VIDEO, MEDIA_OTHER})


@dataclass(frozen=True)
class EvidenceRoute:
    """Which perception stages are worth attempting, and why."""

    native_text: bool
    ocr: bool
    vision: bool
    reason: str  # one sentence, human-readable
    rules: tuple[str, ...]  # every rule id that fired, in order


def route_for(
    *,
    media_kind: str,
    mime: str | None,
    extension: str,
    size: int,
    facts: dict[str, Any],
    screenshot_by_name: bool,
    config: EvidenceConfig,
) -> EvidenceRoute:
    """Decide which of native-text extraction, OCR, and vision to attempt.

    A pure function of its arguments: the same inputs always produce the same
    route, and nothing here touches a file, a network socket, or a log. `mime`
    is accepted for symmetry with the rest of this codebase's probe-facing
    signatures and to leave room for a future rule; no current rule reads it.
    """
    rules: list[str] = []

    if not config.enabled:
        rules.append("r0-evidence-disabled")
        return EvidenceRoute(
            native_text=False,
            ocr=False,
            vision=False,
            reason="evidence collection is disabled (evidence.enabled is false)",
            rules=tuple(rules),
        )

    if size > config.max_bytes:
        rules.append("r1-too-large")
        return EvidenceRoute(
            native_text=False,
            ocr=False,
            vision=False,
            reason=f"file size {size} exceeds evidence.max_bytes ({config.max_bytes})",
            rules=tuple(rules),
        )

    native_text = False
    ocr = False
    vision = False
    reason = ""

    if media_kind == MEDIA_TEXT:
        rules.append("r2-text")
        native_text = True
        reason = "plain/structured text file: native text only"
    elif media_kind == MEDIA_DOCUMENT and extension == ".pdf":
        if facts.get("text_available") is True:
            rules.append("r3-pdf-embedded-text")
            native_text = True
            reason = "pdf reports embedded text: native extraction only, no OCR"
        else:
            rules.append("r4-pdf-no-embedded-text")
            native_text = True
            ocr = True
            reason = "pdf has no confirmed embedded text: attempt native extraction and OCR"
    elif media_kind == MEDIA_DOCUMENT:
        # Any non-PDF document extension classified as MEDIA_DOCUMENT by the
        # probe (.docx, .pptx, .xlsx, .pages, .rtf, .epub, and so on) is an
        # office/e-book container with its own native text layer -- OCR has
        # nothing to add over a text extractor that understands the format.
        rules.append("r5-office-document")
        native_text = True
        reason = "office/e-book document: native text only"
    elif media_kind == MEDIA_IMAGE:
        if screenshot_by_name or bool(facts.get("is_probably_screenshot")):
            rules.append("r6-screenshot")
            ocr = True
            vision = True
            reason = "screenshot: OCR and vision"
        elif facts.get("camera_make") or facts.get("camera_model"):
            rules.append("r7-camera-photo")
            vision = True
            reason = "camera photo (has camera EXIF): vision only"
        else:
            rules.append("r8-image-other")
            vision = True
            reason = "image of unknown origin: vision only"
    else:
        # MEDIA_ARCHIVE, MEDIA_INSTALLER, MEDIA_AUDIO, MEDIA_VIDEO, MEDIA_OTHER,
        # and anything this router does not otherwise recognise: opaque to
        # perception in this phase.
        rules.append("r9-opaque")
        if media_kind in _OPAQUE_MEDIA_KINDS:
            reason = f"{media_kind}: opaque to the evidence layer, nothing to perceive"
        else:
            reason = f"unrecognised media kind {media_kind!r}: no evidence collection"

    if ocr and not config.ocr.enabled:
        rules.append("r10-ocr-disabled")
        ocr = False
    if vision and not config.vision.enabled:
        rules.append("r11-vision-disabled")
        vision = False

    return EvidenceRoute(native_text=native_text, ocr=ocr, vision=vision, reason=reason, rules=tuple(rules))


def refine_route(route: EvidenceRoute, visual: VisualEvidence | None, *, config: EvidenceConfig) -> EvidenceRoute:
    """Let a vision result ask for OCR on a second pass. Never turns anything off.

    A screenshot detected by pixel heuristics alone might be missed by
    `route_for`'s filename/EXIF-only signals, but once a vision backend has
    actually looked at the image and called it text-bearing (a scanned
    document, a chart, a diagram, ...), that is strong enough evidence to add
    OCR -- provided config still allows OCR at all. This function can only
    ever add `ocr=True`; it never clears a stage `route_for` already decided
    on, and it never adds OCR when `config.ocr.enabled` is False.
    """
    if visual is None or visual.image_kind not in TEXT_BEARING_IMAGE_KINDS:
        return route
    if route.ocr or not config.ocr.enabled:
        return route
    return EvidenceRoute(
        native_text=route.native_text,
        ocr=True,
        vision=route.vision,
        reason=route.reason,
        rules=route.rules + ("r12-vision-says-text-bearing",),
    )


# Below this many characters, a document's "embedded text layer" is a header,
# a watermark, or a stray form field rather than the document's contents.
# Deliberately generous: escalating to OCR costs a rasterization pass, while
# failing to escalate means a scanned contract is perceived as a blank page.
MIN_ADEQUATE_NATIVE_CHARS = 60


def refine_route_for_native_text(
    route: EvidenceRoute, *, char_count: int, extracted: bool, config: EvidenceConfig
) -> EvidenceRoute:
    """Escalate to OCR when a document's own text layer turned out to be thin.

    `route_for` can only ask the probe whether a PDF has *any* extractable
    text, and `text_available` is a boolean derived from the first page alone.
    A scanned document with a one-line OCR watermark, or a page of vector art
    carrying a single form label, answers True and would otherwise never be
    looked at. Once the extractor has actually run, the character count is a
    real measurement and is allowed to reopen the question.

    This is the "no OCR unless the evidence is insufficient" rule made
    concrete, and like `refine_route` it can only ever add OCR: it never
    clears a stage, and never overrides `config.ocr.enabled`.
    """
    if not route.native_text or route.ocr or not config.ocr.enabled:
        return route
    if extracted and char_count >= MIN_ADEQUATE_NATIVE_CHARS:
        return route
    return EvidenceRoute(
        native_text=route.native_text,
        ocr=True,
        vision=route.vision,
        reason=route.reason,
        rules=route.rules + ("r13-native-text-inadequate",),
    )
