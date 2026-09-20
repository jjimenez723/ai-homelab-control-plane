"""Pure model-facing Phase 3 classifier.

The provider is a text-generation adapter only. It is never given a path or
catalog object; the only input crossing the boundary is rendered text made
from :class:`SemanticEvidence` and canonical candidate data. The returned
object is validated before a caller can persist it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from tools.prompts.renderer import render

from tools.prompts.phase3 import PHASE3_PROMPT_REGISTRY

from .contract import Candidate, SemanticEvidence, SemanticOutputError, SemanticProposal, parse_semantic_output

TASK_CLASS = "classification.semantic_catalog"
PROMPT_ID = "phase3-semantic-classification"
MAX_PROMPT_CHARS = 12_000


class TextProvider(Protocol):
    model: str

    def generate(self, prompt: str, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ClassificationRequest:
    evidence: SemanticEvidence
    clients: tuple[Candidate, ...]
    projects: tuple[Candidate, ...]


def build_task_text(request: ClassificationRequest) -> str:
    """Build bounded, labeled, untrusted evidence for the prompt."""
    def candidate_line(candidate: Candidate) -> str:
        aliases = ", ".join(candidate.aliases) if candidate.aliases else "none"
        return f"- id={candidate.id}; display_name={candidate.display_name}; aliases={aliases}"

    lines = [
        "Canonical client candidates (choose id only):",
        *(candidate_line(item) for item in request.clients),
        "Canonical project candidates (choose id only):",
        *(candidate_line(item) for item in request.projects),
        "Untrusted asset evidence:",
        f"filename: {request.evidence.filename}",
        f"capture_time: {request.evidence.capture_time or 'unknown'}",
        f"metadata: {json.dumps(dict(request.evidence.metadata), sort_keys=True)}",
        f"native_text_excerpt: {request.evidence.native_text_excerpt}",
        f"ocr_excerpt: {request.evidence.ocr_excerpt}",
        f"transcript_excerpt: {request.evidence.transcript_excerpt}",
        f"visual_evidence: {' | '.join(request.evidence.visual_evidence)}",
        f"shoot_context: {request.evidence.shoot_context}",
    ]
    return "\n".join(lines)[:MAX_PROMPT_CHARS]


def _provider_output(result: Any) -> Any:
    if isinstance(result, dict) and "output" in result:
        return result["output"]
    return result


class SemanticClassifier:
    def __init__(self, provider: TextProvider, *, model_digest: str | None = None, timeout_seconds: int = 120):
        self.provider = provider
        self.model_digest = model_digest
        self.timeout_seconds = timeout_seconds

    def classify(self, request: ClassificationRequest) -> SemanticProposal:
        rendered = render(
            PHASE3_PROMPT_REGISTRY.active(PROMPT_ID),
            task_text=build_task_text(request),
        )
        result = self.provider.generate(rendered.text, timeout_seconds=self.timeout_seconds)
        raw = _provider_output(result)
        if not isinstance(raw, (str, dict)):
            raise SemanticOutputError("provider returned neither JSON text nor an object")
        return parse_semantic_output(
            raw,
            candidates=request.clients,
            projects=request.projects,
            model_id=str(getattr(self.provider, "model", "unknown")),
            model_digest=self.model_digest or getattr(self.provider, "model_digest", None),
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.version,
            prompt_sha256=rendered.sha256,
            evidence_sha256=request.evidence.sha256(),
        )
