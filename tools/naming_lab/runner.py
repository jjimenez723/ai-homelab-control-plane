"""Runs one (seed, prompt version, local model) combination and scores it.

Every run keeps raw model compliance and end-to-end Angel naming success as
genuinely separate fields (spec section 4/9) -- nothing here collapses them
into one score. The only network call is to a loopback Ollama endpoint via
`tools.models.ollama_local.OllamaTextProvider`, the one Ollama client in the
repo; no frontier/cloud model is ever reachable from this module.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import Any

from tools.ingest.naming import policy
from tools.models.ollama_local import OllamaTextProvider
from tools.naming_lab.document_dates import extract_document_dates
from tools.naming_lab.evidence_bridge import LabEvidence
from tools.naming_lab.grammar import (
    EndToEndResult,
    RawComplianceResult,
    check_raw_compliance,
    compose_filename,
    validate_end_to_end,
)
from tools.naming_lab.seeds import SeedRecord
from tools.prompts.registry import PromptVersion
from tools.prompts.renderer import render

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"

_GENERIC_STEM_WORDS = frozenset({
    "document", "photo", "video", "audio", "screenshot", "receipt", "invoice",
    "contract", "notes", "recording", "clip", "image", "file", "scan",
    "letter", "report", "presentation", "sheet", "meeting", "proposal",
})
_WORD_RE = re.compile(r"[a-zA-Z]{4,}")


def build_task_text(lab_evidence: LabEvidence, *, max_chars: int = 4000) -> str:
    """Bounded, human-readable evidence text for the naming prompt's `{task}`."""
    naming_evidence = lab_evidence.naming_evidence
    lines = [
        f"media_kind: {naming_evidence.media_kind}",
        f"name_quality: {naming_evidence.name_quality} ({naming_evidence.name_quality_reason})",
    ]
    if naming_evidence.capture_date:
        lines.append(f"capture_date: {naming_evidence.capture_date}")
    if lab_evidence.semantic_date:
        lines.append(
            f"semantic_date: {lab_evidence.semantic_date.value} "
            f"(kind={lab_evidence.semantic_date.kind}, source={lab_evidence.semantic_date.source}, "
            f"confidence={lab_evidence.semantic_date.confidence})"
        )
    for key in (
        "metadata_title", "metadata_author", "image_description",
        "camera_make", "camera_model", "page_count",
    ):
        value = naming_evidence.fact(key)
        if value:
            lines.append(f"{key}: {value}")
    text = lab_evidence.combined_text
    if text:
        lines.append("extracted_text:")
        lines.append(text[:max_chars])
    return "\n".join(lines)


@dataclasses.dataclass(frozen=True)
class SemanticResult:
    """Does the *final*, trusted-normalized filename hold up semantically."""

    exact_accepted_match: bool
    approved_alternate_match: bool
    satisfies_required_semantics: bool
    violates_forbidden_semantics: bool
    passed: bool


@dataclasses.dataclass(frozen=True)
class HallucinationFlags:
    """Heuristic, best-effort signals for human review -- never a gate.

    `unsupported_date` looks at date-shaped literals in the model's *raw*
    text (before trusted code discards whatever date opinion it had) against
    the dates this file's evidence actually supports -- directly avoiding
    the Gate-2 "supported dates rejected" defect (spec section 14): a date
    is compared against `document_date_candidates` derived from evidence,
    never against a hand-duplicated allowlist.
    """

    unsupported_date: bool
    unsupported_entities: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class NamingLabResult:
    """One (seed, prompt, model) run: raw model output through human-reviewable scoring."""

    seed_sha256: str
    prompt_id: str
    prompt_version: str
    model_name: str
    raw_output: str | None
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    raw_compliance: RawComplianceResult | None
    deterministic_filename: str | None
    end_to_end: EndToEndResult | None
    semantic: SemanticResult | None
    hallucination: HallucinationFlags | None
    error: str | None
    created_at: str


def _score_semantic(seed: SeedRecord, end_to_end: EndToEndResult) -> SemanticResult:
    final = end_to_end.filename
    if final is None:
        satisfies_required = not seed.required_semantics
        return SemanticResult(
            exact_accepted_match=False, approved_alternate_match=False,
            satisfies_required_semantics=satisfies_required,
            violates_forbidden_semantics=False, passed=False,
        )
    lowered = final.lower()
    exact_match = final in seed.accepted_filenames
    satisfies_required = all(
        any(concept.lower() in lowered for concept in concepts)
        for concepts in seed.required_semantics.values()
    )
    violates_forbidden = any(term.lower() in lowered for term in seed.forbidden_semantics)
    passed = satisfies_required and not violates_forbidden
    return SemanticResult(
        exact_accepted_match=exact_match,
        approved_alternate_match=exact_match,  # a human-accepted alternate IS an accepted_filenames entry
        satisfies_required_semantics=satisfies_required,
        violates_forbidden_semantics=violates_forbidden,
        passed=passed,
    )


def _score_hallucination(
    raw_output: str | None, seed: SeedRecord, lab_evidence: LabEvidence,
) -> HallucinationFlags:
    if not raw_output:
        return HallucinationFlags(unsupported_date=False, unsupported_entities=())

    known_dates = {candidate.value for candidate in lab_evidence.document_date_candidates}
    if lab_evidence.semantic_date is not None:
        known_dates.add(lab_evidence.semantic_date.value)
    raw_dates = extract_document_dates(raw_output) or ()
    # `raw_output` rarely carries a labeled "Invoice Date:"-style match, so
    # also check any bare ISO/slash/month-name literal the model wrote.
    unsupported_date = any(candidate.value not in known_dates for candidate in raw_dates)

    known_terms = {term.lower() for values in seed.required_semantics.values() for term in values}
    known_terms |= {word.lower() for word in lab_evidence.naming_evidence.vocabulary}
    evidence_text_lower = lab_evidence.combined_text.lower()
    unsupported_entities = tuple(
        token for token in _WORD_RE.findall(raw_output)
        if token.lower() not in _GENERIC_STEM_WORDS
        and token.lower() not in known_terms
        and token.lower() not in evidence_text_lower
    )
    return HallucinationFlags(unsupported_date=unsupported_date, unsupported_entities=unsupported_entities)


def run_candidate(
    seed: SeedRecord,
    lab_evidence: LabEvidence,
    *,
    prompt: PromptVersion,
    model_name: str,
    endpoint: str = DEFAULT_ENDPOINT,
    num_predict: int = 64,
    timeout_seconds: int = 120,
    max_filename_length: int = 120,
) -> NamingLabResult:
    """Render `prompt` against `lab_evidence`, call `model_name`, and score the result."""
    created_at = dt.datetime.now(dt.timezone.utc).isoformat()
    task_text = build_task_text(lab_evidence)
    rendered = render(prompt, task_text=task_text, extra={"original_filename": seed.original_filename})

    provider = OllamaTextProvider(endpoint, model_name, num_predict=num_predict)
    try:
        generation: dict[str, Any] = provider.generate(rendered.text, timeout_seconds=timeout_seconds)
    except (ConnectionError, TimeoutError, ValueError) as exc:
        return NamingLabResult(
            seed_sha256=seed.sha256, prompt_id=prompt.prompt_id, prompt_version=prompt.version,
            model_name=model_name, raw_output=None, finish_reason=None,
            input_tokens=None, output_tokens=None, raw_compliance=None,
            deterministic_filename=None, end_to_end=None, semantic=None, hallucination=None,
            error=str(exc), created_at=created_at,
        )

    raw_output = generation.get("output") or ""
    true_extension = policy.normalized_extension(seed.original_filename)
    raw_compliance = check_raw_compliance(raw_output, expected_extension=true_extension)

    deterministic_filename = (
        compose_filename(lab_evidence.semantic_date, raw_output, true_extension) if raw_output else ""
    )
    if deterministic_filename:
        end_to_end = validate_end_to_end(
            deterministic_filename, original_filename=seed.original_filename,
            max_filename_length=max_filename_length, vocabulary=lab_evidence.naming_evidence.vocabulary,
            semantic_date=lab_evidence.semantic_date,
        )
    else:
        end_to_end = EndToEndResult(
            passed=False, filename=None, changed=False, policy_reason=None,
            error="raw output produced no usable stem",
        )

    semantic = _score_semantic(seed, end_to_end)
    hallucination = _score_hallucination(raw_output, seed, lab_evidence)

    return NamingLabResult(
        seed_sha256=seed.sha256, prompt_id=prompt.prompt_id, prompt_version=prompt.version,
        model_name=model_name, raw_output=raw_output, finish_reason=generation.get("finish_reason"),
        input_tokens=generation.get("input_tokens"), output_tokens=generation.get("output_tokens"),
        raw_compliance=raw_compliance, deterministic_filename=deterministic_filename or None,
        end_to_end=end_to_end, semantic=semantic, hallucination=hallucination,
        error=None, created_at=created_at,
    )


def run_all(
    seed: SeedRecord,
    lab_evidence: LabEvidence,
    *,
    prompts: tuple[PromptVersion, ...],
    models: tuple[str, ...],
    endpoint: str = DEFAULT_ENDPOINT,
) -> list[NamingLabResult]:
    """Every (prompt, model) combination for one seed. Always explicitly called."""
    return [
        run_candidate(seed, lab_evidence, prompt=prompt, model_name=model, endpoint=endpoint)
        for prompt in prompts
        for model in models
    ]


def result_to_json(result: NamingLabResult) -> dict[str, Any]:
    def _opt(value: Any) -> Any:
        return dataclasses.asdict(value) if value is not None else None

    return {
        "seed_sha256": result.seed_sha256,
        "prompt_id": result.prompt_id,
        "prompt_version": result.prompt_version,
        "model_name": result.model_name,
        "raw_output": result.raw_output,
        "finish_reason": result.finish_reason,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "raw_compliance": _opt(result.raw_compliance),
        "deterministic_filename": result.deterministic_filename,
        "end_to_end": _opt(result.end_to_end),
        "semantic": _opt(result.semantic),
        "hallucination": _opt(result.hallucination),
        "error": result.error,
        "created_at": result.created_at,
    }


def result_from_json(data: dict[str, Any]) -> NamingLabResult:
    def _load(cls: Any, key: str) -> Any:
        raw = data.get(key)
        return cls(**raw) if raw is not None else None

    return NamingLabResult(
        seed_sha256=str(data["seed_sha256"]),
        prompt_id=str(data["prompt_id"]),
        prompt_version=str(data["prompt_version"]),
        model_name=str(data["model_name"]),
        raw_output=data.get("raw_output"),
        finish_reason=data.get("finish_reason"),
        input_tokens=data.get("input_tokens"),
        output_tokens=data.get("output_tokens"),
        raw_compliance=_load(RawComplianceResult, "raw_compliance"),
        deterministic_filename=data.get("deterministic_filename"),
        end_to_end=_load(EndToEndResult, "end_to_end"),
        semantic=_load(SemanticResult, "semantic"),
        hallucination=_load(HallucinationFlags, "hallucination"),
        error=data.get("error"),
        created_at=str(data["created_at"]),
    )
