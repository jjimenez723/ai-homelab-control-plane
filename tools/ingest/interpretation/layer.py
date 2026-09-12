"""Evidence → hypothesis, with no filesystem or policy authority.

This deliberately small baseline interpreter is deterministic and inspectable.
It consumes copied plain values (never an EvidenceSource or Path) and returns
claims that callers may display or evaluate. It cannot rename, move, ingest,
delete, or mutate anything, and text that looks like an instruction remains
textual evidence rather than a control signal.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from tools.ingest.evidence.types import sanitize_extracted_text


@dataclass(frozen=True)
class Interpretation:
    subject: str = "unknown"
    document_kind: str = "unknown"
    category: str = "unknown"
    likely_client: str = "unknown"
    likely_project: str = "unknown"
    candidate_title: str = "unknown"
    keywords: tuple[str, ...] = ()
    confidence: Mapping[str, float] = None  # type: ignore[assignment]
    evidence_references: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    review_needed: bool = True
    interpreter: str = "deterministic-interpretation"
    interpreter_version: str = "1"

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["confidence"] = dict(self.confidence or {})
        return value


def _plain_values(evidence: Mapping[str, Any]) -> tuple[str, str, str, tuple[str, ...]]:
    """Extract bounded strings; mapping is intentionally the only input seam."""
    native = evidence.get("native_text", "")
    if isinstance(native, Mapping):
        native = native.get("text", "")
    visual = evidence.get("visual") or {}
    if not isinstance(visual, Mapping):
        visual = {}
    description = visual.get("description", "")
    context = visual.get("context", "")
    entities = visual.get("entities", ())
    if not isinstance(entities, (list, tuple)):
        entities = ()
    native = sanitize_extracted_text(str(native), max_chars=8_000)[0]
    description = sanitize_extracted_text(str(description), max_chars=2_000)[0]
    context = sanitize_extracted_text(str(context), max_chars=500)[0]
    safe_entities = tuple(sanitize_extracted_text(str(x), max_chars=100)[0] for x in entities[:32])
    return native, description, context, safe_entities


def interpret_evidence(evidence: Mapping[str, Any]) -> Interpretation:
    """Return non-authoritative hypotheses from a plain evidence mapping.

    ``evidence`` may be assembled from ``FileEvidence`` by a caller, but the
    function accepts only mappings and copies bounded scalar values. No claim
    is emitted for an absent or ambiguous signal.
    """
    native, description, context, entities = _plain_values(evidence)
    corpus = " ".join((native, description, context, *entities)).lower()
    refs: list[str] = []
    reasons: list[str] = []
    confidence: dict[str, float] = {}

    if any(word in corpus for word in ("invoice", "receipt", "expense", "purchase order")):
        document_kind, category = "financial_document", "finance"
        refs.append("text:financial-keyword")
        confidence.update(document_kind=0.72, category=0.68)
        reasons.append("financial terms appear in bounded evidence")
    elif any(word in corpus for word in ("contract", "agreement", "statement of work", "nda")):
        document_kind, category = "agreement", "legal"
        refs.append("text:agreement-keyword")
        confidence.update(document_kind=0.68, category=0.62)
        reasons.append("agreement terms appear in bounded evidence")
    elif any(word in corpus for word in ("screenshot", "screen capture", "browser", "github", "terminal")):
        document_kind, category = "screen_capture", "reference"
        refs.append("visual:screen-keyword")
        confidence.update(document_kind=0.66, category=0.52)
        reasons.append("screen-oriented terms appear in visual evidence")
    else:
        document_kind = "image" if evidence.get("visual") else "document"
        category = "unknown"
        confidence["document_kind"] = 0.25
        refs.append("absence:insufficient-signal")
        reasons.append("evidence does not support a narrower classification")

    # Extracting a likely client/project is intentionally conservative: only
    # explicit labels qualify, and the values remain hypotheses.
    client_match = re.search(r"(?:client|customer)\s*[:=-]\s*([\w .&-]{2,60}?)(?=\s+project\b|$)", corpus, re.I)
    project_match = re.search(r"project\s*[:=-]\s*([\w .&-]{2,60}?)(?=\s+(?:client|customer)\b|$)", corpus, re.I)
    client = client_match.group(1).strip() if client_match else "unknown"
    project = project_match.group(1).strip() if project_match else "unknown"
    if client != "unknown":
        confidence["likely_client"] = 0.65
        refs.append("text:explicit-client-label")
    if project != "unknown":
        confidence["likely_project"] = 0.65
        refs.append("text:explicit-project-label")

    keywords = tuple(dict.fromkeys(word for word in (category, document_kind, *entities) if word and word != "unknown"))[:16]
    subject = description.split(".", 1)[0].strip()[:240] or document_kind
    return Interpretation(
        subject=subject, document_kind=document_kind, category=category,
        likely_client=client, likely_project=project,
        candidate_title="unknown", keywords=keywords,
        confidence=confidence, evidence_references=tuple(refs),
        reasons=tuple(reasons), review_needed=True,
    )
