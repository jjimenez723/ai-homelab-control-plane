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

Since D-057 a proposer may also read `evidence.perceived` -- the file's own
text layer, OCR off its pixels, a vision description, a speech transcript --
converted into plain data by `tools/ingest/perception_bridge.py`. The
deterministic proposer uses only the parts of that it can treat as a *quoted
fact about the file*:

* a document's first heading, or the first meaningful line of its own text
  (`CONFIDENCE_PERCEIVED_HEADING`, 0.70) -- a document's own words about
  itself, one step weaker than its `metadata_title`, which is still preferred
  whenever it exists;
* the title-shaped first line of OCR on a screenshot
  (`CONFIDENCE_PERCEIVED_OCR_TITLE`, 0.62) -- what the window was actually
  called, which is why a screenshot can finally be named something better
  than `screen-capture-1440x900`.

It deliberately uses **neither the transcript nor a visual description**. Both
are summaries somebody or something else wrote about the file rather than text
the file contains, and `HeuristicVisionBackend` in particular is explicitly a
guess from colour distribution and flatness (`docs/17`). Turning either into a
filename deterministically would be asserting certainty the evidence does not
carry -- which is the same reasoning that keeps `is_probably_screenshot` out
of the generic fallback. `SemanticProposer` (`naming/semantic.py`) may read
them, because everything it produces is a *proposal* a local model made and
policy still has to accept, and because a model weighing several weak signals
against each other is the thing it is actually good at.

`2026-09-12_web-browser-product-page.png` remains a better outcome than a
confidently wrong client name: where perception says nothing usable, the
generic factual fallback still wins over invention.
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
# A document's own first heading, or the first real line of its text layer
# (D-057). Below `metadata_title` (0.92) because a title field is a claim the
# authoring tool made deliberately, while a first line is an inference from
# position -- and below `CONFIDENCE_DOCUMENT_TEXT`'s author+page-count only as
# a *measurement*: the heading is far more useful, which is why it is tried
# first. See `DeterministicProposer.propose` on why order and confidence are
# two separate judgements.
CONFIDENCE_PERCEIVED_HEADING = 0.70
# A title-shaped first line of OCR text. Same band as camera EXIF: genuinely
# informative, and genuinely capable of being the wrong line.
CONFIDENCE_PERCEIVED_OCR_TITLE = 0.62
CONFIDENCE_DEVICE_FACTS = 0.62
CONFIDENCE_GENERIC = 0.58

_STOP_WORDS = frozenset({"the", "a", "an", "of", "and", "for", "to", "in", "on", "with", "this", "that", "is", "are", "by", "from", "at", "as", "it"})
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'&-]+")

# A line has to look like a heading rather than like prose or a page artifact
# before it is allowed to become a filename. Bounds, not cleverness: long
# enough to mean something, short enough that it is a title and not a
# paragraph, and carrying at least two real words so a page number, a bare
# date, or a stray glyph does not qualify.
MIN_TITLE_LINE_CHARS = 6
MAX_TITLE_LINE_CHARS = 120
MIN_TITLE_LINE_WORDS = 2
# How far into the text to look for one. A document whose first fifteen lines
# are all page furniture is a document this proposer declines to name from its
# text, rather than one it keeps digging through.
MAX_TITLE_LINE_SCAN = 15


@dataclass(frozen=True)
class NameProposal:
    """A suggestion. Not an instruction, and not a filesystem operation.

    The first five fields are the whole of what `rename.py` needs to decide.
    The rest is audit: what a model was asked, what it said, and which kinds
    of evidence were in front of it -- recorded on the `naming_history` row so
    "which evidence produced this filename?" is answerable afterwards (D-057).
    They default to `None`/`()` because a deterministic proposer has no prompt
    and no raw output, and saying so with an absent field is more honest than
    filling one in.
    """

    proposed_filename: str
    confidence: float
    model: str
    model_version: str
    reason: str
    # Audit trail. Set by `SemanticProposer`; absent for deterministic ones.
    prompt_id: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    # The model's output exactly as it came back, bounded, *before* any
    # cleanup or validation. This is the field that makes a hostile or broken
    # answer inspectable after the fact rather than merely rejected.
    raw_output: str | None = None
    # Which `PerceivedEvidence.sources` kinds the proposer actually read.
    evidence_sources: tuple[str, ...] = ()


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


def _title_line(text: str) -> str:
    """The first line of `text` that could plausibly be a title.

    Deliberately simple and bounded (see `MIN_TITLE_LINE_CHARS` and friends).
    Anything cleverer -- font size, position on the page, a model asked which
    line is the title -- is either information this value type does not carry
    or a judgement that belongs to `SemanticProposer`, not to a deterministic
    one. Returns `""` when no line qualifies, which is a decline rather than a
    fallback to the least-bad line.
    """
    if not text:
        return ""
    for line in text.splitlines()[:MAX_TITLE_LINE_SCAN]:
        candidate = line.strip().strip("|-=_*#•·").strip()
        if not MIN_TITLE_LINE_CHARS <= len(candidate) <= MAX_TITLE_LINE_CHARS:
            continue
        if len(_WORD_RE.findall(candidate)) < MIN_TITLE_LINE_WORDS:
            continue
        return candidate
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

        # Builder order is "most informative first", which is *not* the same
        # ordering as descending confidence, and the difference is deliberate.
        # `_from_perceived_heading` (0.70) is tried before
        # `_from_document_author` (0.72) because a document's own first
        # heading tells a reader what the file is *about*, while an author
        # name plus a page count only tells them who made it and how long it
        # is. Confidence answers "how sure am I this is right"; order answers
        # "which answer is worth having". Conflating them would mean a PDF
        # that carries both gets named after its author.
        builders = (
            self._from_embedded_title,
            self._from_embedded_description,
            self._from_archive_contents,
            self._from_perceived_heading,
            self._from_document_author,
            self._from_perceived_ocr_title,
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
                evidence_sources=perceived.sources if (perceived := evidence.perceived) else (),
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

    def _from_perceived_heading(self, evidence: NamingEvidence):
        """A document's own first heading, or the first real line of its text.

        The strongest thing perception offers a deterministic namer: not a
        description of the file, but words the file itself contains, in the
        position a title occupies. A structural heading (from the document's
        own outline) is preferred over a line picked by position, because the
        authoring tool marked it as a heading rather than it merely being
        first.

        `metadata_title` still wins, via `_from_embedded_title` above: a title
        field is a deliberate claim, where a first line is an inference.
        """
        perceived = evidence.perceived
        if perceived is None:
            return None
        heading = next((text.strip() for text in perceived.headings if text.strip()), "")
        source = "the document's own first heading"
        if not heading:
            heading = _title_line(perceived.native_text_excerpt)
            source = "the first meaningful line of the document's own text layer"
        if not heading:
            return None
        slug = _keywords(heading, limit=8, vocabulary=evidence.vocabulary)
        if not slug:
            return None
        return slug, CONFIDENCE_PERCEIVED_HEADING, source

    def _from_perceived_ocr_title(self, evidence: NamingEvidence):
        """The title-shaped first line of OCR text on a screen capture.

        Restricted to files the *filename or source folder* already proves are
        screen captures (`screenshot_by_name`, not the probe's pixel
        heuristic). A photograph that happens to contain a shop sign should
        not be named after the sign, and OCR over an ordinary photo is exactly
        where a stray line is most likely to be the wrong one. A screenshot's
        top line, by contrast, is usually the window or page title -- the
        thing a person would have called the file themselves.

        Native text wins when both exist: `_from_perceived_heading` runs
        first, and a file with a real text layer has better evidence than a
        transcription of its pixels.
        """
        perceived = evidence.perceived
        if perceived is None or not evidence.screenshot_by_name:
            return None
        title = _title_line(perceived.ocr_text_excerpt)
        if not title:
            return None
        slug = _keywords(title, limit=8, vocabulary=evidence.vocabulary)
        if not slug:
            return None
        return (
            f"screen-capture-{slug}",
            CONFIDENCE_PERCEIVED_OCR_TITLE,
            "the first title-shaped line of OCR text on a screen capture",
        )

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
        # Only name something a screen capture when the evidence actually says
        # so. The probe's `is_probably_screenshot` is a PNG-without-EXIF guess,
        # and a 4032x3024 phone photo saved without metadata trips it -- naming
        # that `screen-capture` would be inventing certainty the file does not
        # support, which is exactly what the generic fallback exists to avoid.
        if evidence.screenshot_by_name:
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
