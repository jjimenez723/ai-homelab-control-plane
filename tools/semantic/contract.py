"""Phase 3 semantic classification contract.

This module is deliberately model-facing only. It receives bounded strings,
numbers, tuples, and immutable candidate records. It never receives a
``Path``, file handle, database connection, or callable. The parser is a
validator, not a repair layer: malformed or out-of-vocabulary answers become
an explicit review-required result.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

MAX_TEXT_CHARS = 1800
MAX_TAGS = 20
MAX_TAG_CHARS = 48
MAX_RAW_OUTPUT_CHARS = 20_000
MIN_AUTO_CONFIDENCE = 0.80
SEMANTIC_VALIDATOR_VERSION = "phase3-semantic-v1"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class SemanticOutputError(ValueError):
    """The model did not satisfy the closed semantic output contract."""


@dataclass(frozen=True)
class Candidate:
    """Canonical identity offered to the classifier."""

    id: str
    display_name: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _SLUG_RE.fullmatch(self.id):
            raise ValueError(f"invalid canonical candidate id: {self.id!r}")


@dataclass(frozen=True)
class SemanticEvidence:
    """Bounded plain data sent across the model boundary."""

    asset_id: str
    filename: str
    capture_time: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    native_text_excerpt: str = ""
    ocr_excerpt: str = ""
    transcript_excerpt: str = ""
    visual_evidence: tuple[str, ...] = ()
    shoot_context: str = ""

    def __post_init__(self) -> None:
        for name in (
            "filename", "native_text_excerpt", "ocr_excerpt",
            "transcript_excerpt", "shoot_context",
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            object.__setattr__(self, name, value[:MAX_TEXT_CHARS])
        object.__setattr__(self, "visual_evidence", tuple(str(v)[:MAX_TEXT_CHARS] for v in self.visual_evidence[:8]))
        object.__setattr__(self, "metadata", tuple((str(k)[:80], str(v)[:300]) for k, v in self.metadata[:32]))

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "filename": self.filename,
            "capture_time": self.capture_time,
            "metadata": list(self.metadata),
            "native_text_excerpt": self.native_text_excerpt,
            "ocr_excerpt": self.ocr_excerpt,
            "transcript_excerpt": self.transcript_excerpt,
            "visual_evidence": list(self.visual_evidence),
            "shoot_context": self.shoot_context,
        }

    def sha256(self) -> str:
        encoded = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SemanticProposal:
    """Validated model proposal plus complete provenance."""

    client_id: str | None
    client_confidence: float
    project_id: str | None
    project_confidence: float
    subject: str | None
    tags: tuple[str, ...]
    tag_confidences: tuple[tuple[str, float], ...]
    model_id: str
    model_digest: str | None
    prompt_id: str
    prompt_version: str
    prompt_sha256: str
    evidence_sha256: str
    raw_output: str
    classified_at: str
    validator: str = SEMANTIC_VALIDATOR_VERSION
    human_review_state: str = "review_required"

    @property
    def review_required(self) -> bool:
        return self.human_review_state != "auto_accepted"

    def as_record(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "client_confidence": self.client_confidence,
            "project_id": self.project_id,
            "project_confidence": self.project_confidence,
            "subject": self.subject,
            "tags": list(self.tags),
            "tag_confidences": dict(self.tag_confidences),
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "evidence_sha256": self.evidence_sha256,
            "raw_output": self.raw_output,
            "classified_at": self.classified_at,
            "validator": self.validator,
            "human_review_state": self.human_review_state,
        }


def _bounded_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SemanticOutputError(f"{field} must be a string")
    return value.strip()[:MAX_TEXT_CHARS]


def _confidence(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SemanticOutputError(f"{field} must be a number")
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise SemanticOutputError(f"{field} must be between 0 and 1")
    return value


def _candidate_id(value: Any, field: str, candidates: tuple[Candidate, ...], *, allow_unknown: bool = True) -> str | None:
    if value is None and allow_unknown:
        return None
    if not isinstance(value, str):
        raise SemanticOutputError(f"{field} must be a canonical id or null")
    value = value.strip()
    if value in {"", "unknown", "proposed-new", "review_required"} and allow_unknown:
        return None
    known = {candidate.id for candidate in candidates}
    if value not in known:
        raise SemanticOutputError(f"{field} {value!r} is not in the canonical candidate registry")
    return value


def _tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_TAGS:
        raise SemanticOutputError(f"tags must be a list of at most {MAX_TAGS} strings")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise SemanticOutputError("every tag must be a string")
        tag = raw.strip().lower().replace("_", "-").replace(" ", "-")
        if not _TAG_RE.fullmatch(tag):
            raise SemanticOutputError(f"invalid normalized tag: {raw!r}")
        if tag not in result:
            result.append(tag)
    return tuple(sorted(result))


def _parse_object(raw_output: str | Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if isinstance(raw_output, Mapping):
        value = dict(raw_output)
        raw_text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    elif isinstance(raw_output, str):
        raw_text = raw_output[:MAX_RAW_OUTPUT_CHARS]
        try:
            value = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise SemanticOutputError("model output is not valid JSON") from exc
    else:
        raise SemanticOutputError("model output must be a JSON object or JSON text")
    if not isinstance(value, dict):
        raise SemanticOutputError("model output must be a JSON object")
    return value, raw_text


def parse_semantic_output(
    raw_output: str | Mapping[str, Any],
    *,
    candidates: tuple[Candidate, ...],
    projects: tuple[Candidate, ...],
    model_id: str,
    model_digest: str | None,
    prompt_id: str,
    prompt_version: str,
    prompt_sha256: str,
    evidence_sha256: str,
    classified_at: str | None = None,
) -> SemanticProposal:
    """Validate a model response against canonical identities and tag rules."""
    value, raw_text = _parse_object(raw_output)
    allowed = {
        "client_id", "client_confidence", "project_id", "project_confidence",
        "subject", "tags", "tag_confidences",
    }
    unknown = set(value) - allowed
    if unknown:
        raise SemanticOutputError(f"unknown semantic output fields: {sorted(unknown)}")
    client_id = _candidate_id(value.get("client_id"), "client_id", candidates)
    project_id = _candidate_id(value.get("project_id"), "project_id", projects)
    client_confidence = _confidence(value.get("client_confidence", 0.0), "client_confidence")
    project_confidence = _confidence(value.get("project_confidence", 0.0), "project_confidence")
    subject_raw = value.get("subject")
    subject = None if subject_raw in (None, "") else _bounded_text(subject_raw, "subject").lower()
    tags = _tags(value.get("tags", []))
    raw_confidences = value.get("tag_confidences", {})
    if not isinstance(raw_confidences, dict):
        raise SemanticOutputError("tag_confidences must be an object")
    tag_confidences = tuple((tag, _confidence(raw_confidences.get(tag, 0.0), f"tag_confidences.{tag}")) for tag in tags)
    project_client_ids = {candidate.id for candidate in projects}
    if project_id and project_id not in project_client_ids:
        raise SemanticOutputError("project candidate registry is inconsistent")
    auto = bool(
        client_id and project_id
        and client_confidence >= MIN_AUTO_CONFIDENCE
        and project_confidence >= MIN_AUTO_CONFIDENCE
        and all(confidence >= MIN_AUTO_CONFIDENCE for _, confidence in tag_confidences)
    )
    return SemanticProposal(
        client_id=client_id,
        client_confidence=client_confidence,
        project_id=project_id,
        project_confidence=project_confidence,
        subject=subject,
        tags=tags,
        tag_confidences=tag_confidences,
        model_id=model_id,
        model_digest=model_digest,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        evidence_sha256=evidence_sha256,
        raw_output=raw_text,
        classified_at=classified_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
        human_review_state="auto_accepted" if auto else "review_required",
    )


def normalized_slug(value: str) -> str:
    """Validate one path component before any trusted code uses it."""
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"invalid path component: {value!r}")
    return value
