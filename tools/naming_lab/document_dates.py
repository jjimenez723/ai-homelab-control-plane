"""Deterministic candidate-date extraction from document text.

Trusted code, not a model, identifies *which* date in a document's text is
its semantic date and *why* (spec section 2: "Do not just select the first
date OCR finds"). Input is native/OCR text already produced by
`tools.ingest.evidence` -- this module does no perception of its own, only
regex-based labeling over text it is handed.

Deliberately conservative: a bare year, quarter, or month-year ("2024",
"Q1 2025", "March 2025") never becomes a candidate, because turning one into
a full date would be inventing the missing day (spec section 3).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Iterable

from tools.naming_lab.dates import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    KIND_DOCUMENT_DATE,
    KIND_DUE_DATE,
    KIND_EFFECTIVE_DATE,
    KIND_INVOICE_DATE,
    KIND_MEETING_DATE,
    KIND_RECEIPT_DATE,
    SOURCE_DOCUMENT_TEXT_OR_OCR,
    SemanticDateCandidate,
)

# Best-first. `due_date` sits deliberately below every other labeled kind
# (spec section 2's invoice-date-beats-due-date example); `document_date` is
# the generic bare "Date:" label, lower priority than any named kind because
# a named kind is stronger evidence of what the date *means*.
_KIND_PRIORITY: tuple[str, ...] = (
    KIND_INVOICE_DATE,
    KIND_RECEIPT_DATE,
    KIND_EFFECTIVE_DATE,
    KIND_MEETING_DATE,
    KIND_DOCUMENT_DATE,
    KIND_DUE_DATE,
)

_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)

# One alternation of "a date, spelled some common way", used inside every
# labeled pattern below. Capturing groups differ per alternative, so each
# match is re-parsed by `_parse_date_literal` rather than by group index.
_DATE_LITERAL = (
    r"(?:(\d{4})-(\d{1,2})-(\d{1,2}))"  # ISO: 2025-06-15
    rf"|(?:({_MONTH_NAMES})\.?\s+(\d{{1,2}}),?\s+(\d{{4}}))"  # June 15, 2025
    r"|(?:(\d{1,2})/(\d{1,2})/(\d{4}))"  # 6/15/2025
)

_MONTH_LOOKUP = {
    name: index
    for index, names in enumerate(
        (
            ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
            ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
            ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}


def _parse_date_literal(match: re.Match) -> str | None:
    groups = match.groups()
    if groups[0]:  # ISO
        year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
    elif groups[3]:  # Month D, YYYY
        month = _MONTH_LOOKUP.get(groups[3].lower())
        if month is None:
            return None
        day, year = int(groups[4]), int(groups[5])
    elif groups[6]:  # M/D/YYYY
        month, day, year = int(groups[6]), int(groups[7]), int(groups[8])
    else:
        return None
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None  # malformed (month 13, day 40, ...): not a candidate


def _label_pattern(label: str) -> re.Pattern[str]:
    return re.compile(
        rf"\b{label}\s*[:\-]?\s*(?:{_DATE_LITERAL})",
        re.IGNORECASE,
    )


_LABELED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (KIND_INVOICE_DATE, _label_pattern(r"invoice\s*date")),
    (KIND_RECEIPT_DATE, _label_pattern(r"(?:receipt|transaction)\s*date")),
    (KIND_EFFECTIVE_DATE, _label_pattern(r"effective\s*date")),
    (KIND_MEETING_DATE, _label_pattern(r"meeting\s*date")),
    (KIND_DUE_DATE, _label_pattern(r"due\s*date")),
)

# The generic fallback: a bare "Date:" not already claimed by a more specific
# label above. Matched only against lines that no specific pattern matched,
# so "Invoice Date: 2025-06-15" is never double-counted as a generic date too.
_GENERIC_DATE_PATTERN = _label_pattern(r"date")


def extract_document_dates(text: str) -> tuple[SemanticDateCandidate, ...]:
    """Best-first candidates found in `text`, one per kind (first match wins).

    A confident source: this is exactly the evidence a human reviewing the
    document would cite, which is also what fixes the Gate-2 "supported
    dates rejected" defect (spec section 14) for the Lab's own scoring --
    the allowed date comes from this extraction, not a hand-duplicated list.
    """
    if not text:
        return ()

    found: dict[str, str] = {}
    for line in text.splitlines():
        matched_specific = False
        for kind, pattern in _LABELED_PATTERNS:
            if kind in found:
                matched_specific = matched_specific or bool(pattern.search(line))
                continue
            match = pattern.search(line)
            if not match:
                continue
            matched_specific = True
            value = _parse_date_literal(match)
            if value is not None:
                found[kind] = value
        if matched_specific or KIND_DOCUMENT_DATE in found:
            continue
        generic_match = _GENERIC_DATE_PATTERN.search(line)
        if generic_match:
            value = _parse_date_literal(generic_match)
            if value is not None:
                found[KIND_DOCUMENT_DATE] = value

    candidates = [
        SemanticDateCandidate(
            value=found[kind],
            kind=kind,
            source=SOURCE_DOCUMENT_TEXT_OR_OCR,
            confidence=CONFIDENCE_HIGH if kind != KIND_DOCUMENT_DATE else CONFIDENCE_MEDIUM,
        )
        for kind in _KIND_PRIORITY
        if kind in found
    ]
    return tuple(candidates)


def preferred_document_date(candidates: Iterable[SemanticDateCandidate]) -> SemanticDateCandidate | None:
    """First (highest-priority) candidate, or None. `candidates` must already be ordered."""
    return next(iter(candidates), None)
