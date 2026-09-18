"""The one module that carries perception across into naming (D-057).

`tests/ingest/test_evidence_security.py` forbids any module under
`tools/ingest/naming/` or `tools/ingest/triage/` from importing
`tools.ingest.evidence`. That guard is not being relaxed: it is the structural
reason perceived content cannot acquire decision authority by accident, and it
stays exactly as strict as it was. What changes with D-057 is that naming is
now *allowed* to read perception -- through a deliberate, reviewed, one-way
conversion that lives here, outside both guarded packages, the same way
`tools/naming_lab/evidence_bridge.py` lives outside them (D-054).

This module is the whole of that permission, and it is deliberately tiny:

* it imports `tools.ingest.evidence.types` (value types only -- no backend, no
  extractor, no cache) and `tools.ingest.naming.evidence`;
* it converts one frozen plain-data value into another frozen plain-data value
  and returns it;
* it holds no config, opens no file, calls no backend, and never raises.

The direction is one-way and total. `FileEvidence` -> `PerceivedEvidence`
*loses* information on purpose: polygons, per-block confidences, provenance
records, page regions and tables all stop here. A proposer downstream can see
the text and the description and nothing else, so there is no second,
unvalidated channel by which a naming decision could come to depend on
perception's internal structure.

What this module does NOT do, and must not grow into:

* it does not decide anything. The value it returns is an input to
  `NameProposer.propose`, whose output still passes through
  `naming/policy.py::validate_proposal` unchanged;
* it does not reach retention. `triage/policy.py` never sees a
  `PerceivedEvidence` -- retention triage stays evidence-blind (D-029 holds
  for retention, and `tests/ingest/test_evidence_security.py` still proves
  it behaviourally);
* it does not gather evidence. `pipeline.py` calls `collect_evidence` and
  hands the result here.
"""

from __future__ import annotations

from typing import Any

from tools.ingest.evidence.types import FileEvidence
from tools.ingest.naming.evidence import (
    MAX_PERCEIVED_EXCERPT_CHARS,
    SOURCE_NATIVE_TEXT,
    SOURCE_OCR,
    SOURCE_STRUCTURE,
    SOURCE_TRANSCRIPT,
    SOURCE_VISUAL,
    PerceivedEvidence,
)


def _transcript_text(file_evidence: FileEvidence) -> str:
    """The speech transcript, if a transcript stage exists yet.

    Read with `getattr` rather than as an attribute because
    `FileEvidence.transcript` is landing in a different workstream. Today the
    attribute is absent and this returns `""`; the moment the field exists
    (with `.text`, `.language`, `.engine`) this starts returning its text with
    no change here. The alternative -- waiting for that field before wiring
    naming to perception at all -- would serialize two independent pieces of
    work for no safety gain, since an absent transcript is exactly the same
    input as an empty one.
    """
    transcript = getattr(file_evidence, "transcript", None)
    if transcript is None:
        return ""
    return str(getattr(transcript, "text", "") or "")


def _visual_confidence(file_evidence: FileEvidence) -> float:
    """The highest backend confidence across every frame that was seen."""
    frames = file_evidence.visual_frames or (
        (file_evidence.visual,) if file_evidence.visual is not None else ()
    )
    best = 0.0
    for visual in frames:
        try:
            best = max(best, float(getattr(visual, "confidence", 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
    return best


def _visual_descriptions(file_evidence: FileEvidence) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """(descriptions, entities, image_kind) across every frame that was seen.

    `visual_frames` is the general case (one entry per sampled video frame,
    D-056) and `visual` is its first element whenever either is set, so
    preferring `visual_frames` and falling back to `visual` covers both an
    image (always exactly one) and a video (one per frame) without a
    media-kind test here.

    `image_kind` is taken from the *first* frame only. A single string cannot
    honestly describe five frames, and picking a majority would be inventing
    a summary the perception layer never produced.
    """
    frames = file_evidence.visual_frames or (
        (file_evidence.visual,) if file_evidence.visual is not None else ()
    )
    descriptions: list[str] = []
    entities: list[str] = []
    for visual in frames:
        if visual is None:
            continue
        description = str(getattr(visual, "description", "") or "")
        context = str(getattr(visual, "context", "") or "")
        # Context ("GitHub pull request page") is a different claim from the
        # description ("a code review with three files changed"), and both are
        # useful to a namer, so they are joined rather than one being dropped.
        combined = " -- ".join(part for part in (description, context) if part)
        if combined:
            descriptions.append(combined)
        for entity in getattr(visual, "entities", ()) or ():
            text = str(entity or "").strip()
            if text and text not in entities:
                entities.append(text)
    first_kind = ""
    for visual in frames:
        if visual is not None:
            first_kind = str(getattr(visual, "image_kind", "") or "")
            break
    return tuple(descriptions), tuple(entities), first_kind


def perceived_from_file_evidence(
    file_evidence: FileEvidence | None,
    *,
    max_excerpt_chars: int = MAX_PERCEIVED_EXCERPT_CHARS,
) -> PerceivedEvidence | None:
    """Convert one `FileEvidence` into the plain data a proposer may read.

    Returns `None` for `None`, so a caller can pass through "perception did
    not run" without a branch of its own. Never raises: this sits between a
    perception stage that is allowed to fail and a naming stage that must not,
    so a malformed evidence object costs the perception, not the name.

    Bounding and sanitizing are done by `PerceivedEvidence.from_mapping`
    rather than here, so the ceiling applies identically whether the value was
    built from a live `FileEvidence` or from a stored mapping.
    """
    if file_evidence is None:
        return None
    try:
        native = file_evidence.native_text
        native_text = str(native.text) if native is not None else ""
        ocr_text = file_evidence.ocr_text
        transcript = _transcript_text(file_evidence)
        descriptions, entities, image_kind = _visual_descriptions(file_evidence)
        visual_confidence = _visual_confidence(file_evidence)
        structure = file_evidence.structure
        headings = tuple(
            str(heading.text) for heading in (structure.headings if structure is not None else ())
        )

        # "Present" means content was actually perceived, not that a stage was
        # attempted. A backend that ran and found nothing leaves its kind out,
        # which is what lets a proposer tell "the pixels carry no text" apart
        # from "OCR never ran" -- the warnings say which.
        sources: list[str] = []
        if native_text.strip():
            sources.append(SOURCE_NATIVE_TEXT)
        if ocr_text.strip():
            sources.append(SOURCE_OCR)
        if descriptions:
            sources.append(SOURCE_VISUAL)
        if transcript.strip():
            sources.append(SOURCE_TRANSCRIPT)
        if headings:
            sources.append(SOURCE_STRUCTURE)

        payload: dict[str, Any] = {
            "native_text_excerpt": native_text,
            "ocr_text_excerpt": ocr_text,
            "transcript_excerpt": transcript,
            "visual_descriptions": descriptions,
            "visual_entities": entities,
            "visual_image_kind": image_kind,
            "visual_confidence": visual_confidence,
            "headings": headings,
            "sources": sources,
            "warnings": file_evidence.warning_codes(),
            "evidence_sha256": file_evidence.content_sha256,
        }
    except Exception:  # noqa: BLE001 - see the docstring: perception may fail, naming may not
        return None

    perceived = PerceivedEvidence.from_mapping(payload)
    if max_excerpt_chars != MAX_PERCEIVED_EXCERPT_CHARS:
        # A caller asking for a tighter bound gets one; the default path does
        # not pay for a second pass.
        perceived = PerceivedEvidence.from_mapping(
            {
                **payload,
                "native_text_excerpt": perceived.native_text_excerpt[:max_excerpt_chars],
                "ocr_text_excerpt": perceived.ocr_text_excerpt[:max_excerpt_chars],
                "transcript_excerpt": perceived.transcript_excerpt[:max_excerpt_chars],
            }
        )
    return perceived
