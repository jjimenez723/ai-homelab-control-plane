"""Versioned, intentionally small vocabulary for operator-entered truth."""

from __future__ import annotations

LABEL_SCHEMA_VERSION = "angel-human-label/v1"
TAXONOMY_VERSION = "angel-taxonomy/v1"

NAMING_ACTIONS = frozenset({"KEEP", "RENAME", "UNSURE"})
REASON_CODES = frozenset({
    "insufficient_evidence", "ambiguous_identity", "misleading_original_filename",
    "semantic_date_unclear", "ocr_required", "visual_context_required",
    "transcript_required", "external_project_context_required",
})
DOCUMENT_MEDIA_TYPES = frozenset({
    "image", "video", "audio", "pdf", "text", "document", "spreadsheet",
    "archive", "code_config", "other",
})


def validate_reason_codes(values: tuple[str, ...]) -> tuple[str, ...]:
    unknown = set(values) - REASON_CODES
    if unknown:
        raise ValueError(f"unknown reason codes for {TAXONOMY_VERSION}: {sorted(unknown)!r}")
    return tuple(dict.fromkeys(values))


def normalize_tags(values: tuple[str, ...]) -> tuple[str, ...]:
    """Keep v1 permissive but stable; a later vocabulary may map aliases explicitly."""
    normalized = tuple(dict.fromkeys(value.strip().casefold() for value in values if value.strip()))
    if any(len(value) > 80 for value in normalized):
        raise ValueError("a normalized tag exceeds 80 characters")
    return normalized
