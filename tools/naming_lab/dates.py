"""Semantic-date formatting and precedence for the Naming Lab.

`SemanticDateCandidate` carries a date's *meaning* (what kind of date it is,
where it came from, how sure we are) alongside its ISO value, so the UI can
show why a date was chosen instead of only what was chosen (spec section 2).

The full ISO date is always what gets stored and reasoned about internally.
`format_date_prefix` is the one place the `YY_MMDD` filename-rendering
shorthand is computed, deterministically, from that full value -- never
asked of a model (spec section 1).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

# -- kinds --------------------------------------------------------------------
#
# Ordered worst-to-best is not meaningful here; precedence lives in
# `document_dates.py` (which kind of *document* date wins) and in
# `derive_semantic_date` below (document date vs. capture metadata vs.
# machine-filename date vs. nothing).

KIND_INVOICE_DATE = "invoice_date"
KIND_RECEIPT_DATE = "receipt_date"
KIND_EFFECTIVE_DATE = "effective_date"
KIND_MEETING_DATE = "meeting_date"
KIND_DOCUMENT_DATE = "document_date"
KIND_DUE_DATE = "due_date"
KIND_CAPTURE_DATE = "capture_date"
KIND_MACHINE_FILENAME_DATE = "machine_filename_date"

SOURCE_DOCUMENT_TEXT_OR_OCR = "document_text_or_ocr"
SOURCE_CAPTURE_METADATA = "capture_metadata"
SOURCE_MACHINE_FILENAME = "machine_filename"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


@dataclass(frozen=True)
class SemanticDateCandidate:
    """One date, with why we think it is the date the file is *about*."""

    value: str  # ISO 8601 "YYYY-MM-DD"
    kind: str
    source: str
    confidence: str

    def __post_init__(self) -> None:
        if _parse_iso_date(self.value) is None:
            raise ValueError(f"SemanticDateCandidate.value is not a valid ISO date: {self.value!r}")


_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_iso_date(text: str | None) -> dt.date | None:
    """A strict, ISO-only, calendar-validated parse. None for anything else.

    Deliberately does not accept `YYYY-M-D` or other loosely-formatted
    variants -- the rest of the Lab always produces and stores strict ISO,
    so accepting more here would only make malformed input harder to catch.
    """
    if not text:
        return None
    match = _ISO_DATE_RE.match(text.strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def format_date_prefix(iso_date: str | None) -> str | None:
    """`2025-06-15` -> `25_0615`. `None` for malformed, absent, or empty input.

    Deterministic and total: never raises. A caller that wants "no date
    prefix" on bad input gets exactly that -- omission, not a guess -- which
    is also spec section 3's rule for the filename grammar itself.
    """
    parsed = _parse_iso_date(iso_date)
    if parsed is None:
        return None
    return f"{parsed.year % 100:02d}_{parsed.month:02d}{parsed.day:02d}"


# -- machine-generated capture filename dates ----------------------------------
#
# Recognises an 8-digit YYYYMMDD run embedded in a filename that also carries
# one of the machine-capture prefixes `tools/ingest/naming/quality.py` already
# knows about (IMG_, PXL_, VID_, DSCN, Screenshot...). Requiring both the
# prefix and a calendar-valid date keeps this from firing on an arbitrary
# 8-digit number that happens to appear in a name.

_MACHINE_CAPTURE_PREFIX_RE = re.compile(
    r"^(?:img|pxl|vid|dscn|dsc|mvimg|screenshot|screen shot|cleanshot)[ _-]",
    re.IGNORECASE,
)
_EMBEDDED_YYYYMMDD_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")


def machine_filename_date(original_filename: str) -> SemanticDateCandidate | None:
    """A `YYYYMMDD` run in a recognised machine-capture filename, if any.

    Only ever a candidate, never authoritative on its own -- `derive_semantic_date`
    ranks it below both an explicit document date and capture metadata.
    """
    if not _MACHINE_CAPTURE_PREFIX_RE.match(original_filename.strip()):
        return None
    for match in _EMBEDDED_YYYYMMDD_RE.finditer(original_filename):
        year, month, day = match.groups()
        try:
            parsed = dt.date(int(year), int(month), int(day))
        except ValueError:
            continue
        return SemanticDateCandidate(
            value=parsed.isoformat(),
            kind=KIND_MACHINE_FILENAME_DATE,
            source=SOURCE_MACHINE_FILENAME,
            confidence=CONFIDENCE_MEDIUM,
        )
    return None


def capture_date_candidate(capture_date: str | None) -> SemanticDateCandidate | None:
    """Wrap `NamingEvidence.capture_date` (EXIF/AV capture metadata) as a candidate."""
    if _parse_iso_date(capture_date) is None:
        return None
    return SemanticDateCandidate(
        value=capture_date,  # type: ignore[arg-type]
        kind=KIND_CAPTURE_DATE,
        source=SOURCE_CAPTURE_METADATA,
        confidence=CONFIDENCE_HIGH,
    )


def derive_semantic_date(
    *,
    document_candidates: tuple[SemanticDateCandidate, ...] = (),
    capture_date: str | None = None,
    original_filename: str = "",
) -> SemanticDateCandidate | None:
    """The precedence in spec section 2: document date > capture > machine filename > none.

    `document_candidates` must already be ordered best-first (that ordering
    is `document_dates.extract_document_dates`'s job -- it is the one place
    that knows invoice_date should beat due_date). This function only
    decides between *kinds of evidence*, not between dates of the same kind.

    Filesystem `modified_date` is never considered here -- it is a fact about
    this filesystem, not about the file, and is never treated as a semantic
    date (spec section 2 / `tools/ingest/naming/proposer.py`'s own comment
    to the same effect).
    """
    if document_candidates:
        return document_candidates[0]
    captured = capture_date_candidate(capture_date)
    if captured is not None:
        return captured
    return machine_filename_date(original_filename)
