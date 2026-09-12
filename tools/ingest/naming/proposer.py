"""Turning evidence into a proposed filename.

This is the seam a model plugs into. A `NameProposer` receives `NamingEvidence`
-- plain data, no filesystem handle -- and returns a `NameProposal` or None. It
has no other powers. Deterministic code downstream validates the suggestion and
performs the rename.

The proposer shipped here is `DeterministicProposer`, and it is deliberately
conservative: it names from facts it can actually read -- a PDF's own title, an
image's EXIF description, a zip's contents, a capture date, pixel dimensions --
and where it cannot establish a subject it says something factual and generic
rather than inventing one. `2026-09-12_web-browser-product-page.png` is a
better outcome than a confidently wrong client name, and the spec asks for
exactly that trade.

Semantic naming -- reading what a screenshot actually shows, recognising a
hawk on a fence -- needs OCR and image understanding. Those are the worker
layer, which this phase deliberately does not build. When they exist they
implement this protocol; nothing downstream changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from tools.ingest.media.probe import (
    MEDIA_ARCHIVE,
    MEDIA_AUDIO,
    MEDIA_DOCUMENT,
    MEDIA_IMAGE,
    MEDIA_VIDEO,
)
from tools.ingest.naming.evidence import NamingEvidence
from tools.ingest.naming.policy import slugify

# Confidence bands. These are honest self-assessments of a *deterministic*
# proposer: high where the file told us its own title, low where all we can say
# is what kind of thing it is.
CONFIDENCE_EMBEDDED_TITLE = 0.92
CONFIDENCE_EMBEDDED_DESCRIPTION = 0.85
CONFIDENCE_ARCHIVE_CONTENTS = 0.78
CONFIDENCE_DOCUMENT_TEXT = 0.72
CONFIDENCE_DEVICE_FACTS = 0.62
CONFIDENCE_GENERIC = 0.58

_STOP_WORDS = frozenset({"the", "a", "an", "of", "and", "for", "to", "in", "on", "with", "this", "that", "is", "are", "by", "from", "at", "as", "it"})
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'&-]+")


@dataclass(frozen=True)
class NameProposal:
    """A suggestion. Not an instruction, and not a filesystem operation."""

    proposed_filename: str
    confidence: float
    model: str
    model_version: str
    reason: str


class NameProposer(Protocol):
    """The contract every proposer -- deterministic or model-backed -- meets."""

    model: str
    model_version: str

    def propose(self, evidence: NamingEvidence) -> NameProposal | None:
        """Return a suggested filename, or None to decline.

        Declining is a first-class answer. A proposer that cannot say anything
        useful must return None rather than produce filler.
        """


class DisabledProposer:
    """Declines everything. Used when naming is switched off in config."""

    model = "disabled"
    model_version = "1"

    def propose(self, evidence: NamingEvidence) -> NameProposal | None:
        return None


def _date_prefix(evidence: NamingEvidence) -> str:
    """The date the file is about: capture date if known, else last modified.

    A capture timestamp is a fact the file carries; mtime is a fact about this
    filesystem. Preferring the former means a photo copied off a card is named
    for when it was taken, not for when it was copied.
    """
    return evidence.capture_date or evidence.modified_date or ""


def _compose(prefix: str, slug: str, extension: str) -> str:
    slug = slug.strip("-")
    if not slug:
        return ""
    stem = f"{prefix}_{slug}" if prefix else slug
    return f"{stem}{extension}"


def _keywords(text: str, *, limit: int = 6, vocabulary: tuple[str, ...] = ()) -> str:
    """The most useful words from a blob of text, in their original order.

    Order is preserved rather than ranked by frequency: a document's title
    reads correctly as written, and reordering it would make the filename
    harder to recognise, not easier.
    """
    known = {word.lower() for word in vocabulary}
    words: list[str] = []
    for match in _WORD_RE.finditer(text):
        word = match.group(0).lower()
        if word in known:
            if word not in words:
                words.append(word)
            continue
        if word in _STOP_WORDS or len(word) < 3:
            continue
        if word not in words:
            words.append(word)
        if len(words) >= limit:
            break
    return slugify(" ".join(words))


def _dimension_tag(evidence: NamingEvidence) -> str:
    width, height = evidence.fact("width"), evidence.fact("height")
    if width and height:
        return f"{int(width)}x{int(height)}"
    return ""


class DeterministicProposer:
    """Names files from measured facts only. No network, no model, no guessing.

    Confidence reflects how much the file actually told us about itself. A
    caller's `min_confidence` therefore controls how much interpretation it is
    willing to accept, which is the knob that matters.
    """

    model = "deterministic-proposer"
    model_version = "1"

    def propose(self, evidence: NamingEvidence) -> NameProposal | None:
        extension = evidence.extension or ""
        prefix = _date_prefix(evidence)

        builders = (
            self._from_embedded_title,
            self._from_embedded_description,
            self._from_archive_contents,
            self._from_document_author,
            self._from_device_facts,
            self._generic,
        )
        for builder in builders:
            built = builder(evidence)
            if built is None:
                continue
            slug, confidence, reason = built
            filename = _compose(prefix, slug, extension)
            if not filename:
                continue
            return NameProposal(
                proposed_filename=filename,
                confidence=confidence,
                model=self.model,
                model_version=self.model_version,
                reason=reason,
            )
        return None

    # -- evidence-specific builders ----------------------------------------

    def _from_embedded_title(self, evidence: NamingEvidence):
        """The file's own title metadata: the strongest evidence available."""
        title = evidence.fact("metadata_title") or evidence.fact("container_title")
        if not title:
            return None
        slug = _keywords(str(title), limit=8, vocabulary=evidence.vocabulary)
        if not slug:
            return None
        return slug, CONFIDENCE_EMBEDDED_TITLE, "document/container title metadata"

    def _from_embedded_description(self, evidence: NamingEvidence):
        description = (
            evidence.fact("image_description")
            or evidence.fact("exif_user_comment")
        )
        if not description:
            return None
        slug = _keywords(str(description), limit=8, vocabulary=evidence.vocabulary)
        if not slug:
            return None
        return slug, CONFIDENCE_EMBEDDED_DESCRIPTION, "embedded EXIF/document description"

    def _from_archive_contents(self, evidence: NamingEvidence):
        """`download (4).zip` is opaque; its central directory usually is not."""
        if evidence.media_kind != MEDIA_ARCHIVE:
            return None
        entries = evidence.fact("top_level_entries") or []
        dominant = evidence.fact("dominant_extension")
        extensions = [dominant] if dominant else []
        parts = [part for part in entries[:2] if part]
        if not parts:
            return None
        slug = _keywords(" ".join(parts), limit=6, vocabulary=evidence.vocabulary)
        if not slug:
            return None
        kind = (extensions[0].lstrip(".") if extensions else "") or "archive"
        return f"{slug}-{kind}-archive", CONFIDENCE_ARCHIVE_CONTENTS, "top-level entries in the zip's central directory"

    def _from_document_author(self, evidence: NamingEvidence):
        """A PDF's author plus its page count is thin, but it is measured."""
        author = evidence.fact("metadata_author")
        pages = evidence.fact("page_count")
        if not author or not isinstance(pages, int):
            return None
        slug = _keywords(str(author), limit=4, vocabulary=evidence.vocabulary)
        if not slug:
            return None
        return f"{slug}-document-{pages}p", CONFIDENCE_DOCUMENT_TEXT, "PDF author metadata and page count"

    def _from_device_facts(self, evidence: NamingEvidence):
        """Camera/phone metadata. Factual, and genuinely useful for sorting."""
        if evidence.media_kind not in (MEDIA_IMAGE, MEDIA_VIDEO):
            return None
        make = str(evidence.fact("camera_make") or "").strip()
        model = str(evidence.fact("camera_model") or "").strip()
        if not (make or model):
            return None
        # `Apple` + `iPhone 15 Pro` should not become `apple-apple-iphone...`.
        device = model if model.lower().startswith(make.lower()[:4] or "\x00") else f"{make} {model}"
        slug = slugify(device, max_length=40)
        if not slug:
            return None
        kind = "photo" if evidence.media_kind == MEDIA_IMAGE else "video"
        tag = _dimension_tag(evidence)
        return (
            "-".join(part for part in (kind, slug, tag) if part),
            CONFIDENCE_DEVICE_FACTS,
            "camera make/model from EXIF",
        )

    def _generic(self, evidence: NamingEvidence):
        """The honest fallback: say what the file is, claim nothing more.

        This is what the spec asks for when interpretation is weak -- a useful
        generic factual name beats a hallucinated specific one. It reads as a
        real name, so it will not be re-proposed on the next run.
        """
        tag = _dimension_tag(evidence)
        if evidence.is_screenshot:
            base = "screen-capture"
        elif evidence.media_kind == MEDIA_IMAGE:
            base = "photo"
        elif evidence.media_kind == MEDIA_VIDEO:
            duration = evidence.fact("duration_seconds")
            base = "video-clip" if not duration else f"video-clip-{int(float(duration))}s"
        elif evidence.media_kind == MEDIA_AUDIO:
            base = "audio-recording"
        elif evidence.media_kind == MEDIA_DOCUMENT:
            pages = evidence.fact("page_count")
            base = "document" if not isinstance(pages, int) else f"document-{pages}p"
            # A bare "document" is a name the quality check rightly rejects.
            if not isinstance(pages, int):
                return None
        elif evidence.media_kind == MEDIA_ARCHIVE:
            return None  # nothing measured; better to decline than to pad
        else:
            return None
        slug = "-".join(part for part in (base, tag) if part)
        return slug, CONFIDENCE_GENERIC, "file type and measured dimensions only"


def build_proposer(name: str = "deterministic") -> NameProposer:
    if name == "disabled":
        return DisabledProposer()
    return DeterministicProposer()
