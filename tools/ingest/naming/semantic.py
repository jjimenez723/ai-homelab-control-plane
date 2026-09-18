"""The opt-in, model-backed name proposer (D-057).

This is the first production caller of `tools/prompts/production.py` --
D-051's "still never exercised by a production caller" stops being true here.
It renders the *same* versioned prompt, through the *same* renderer, that the
Gate-2 `naming.semantic` suite evaluates, because D-041's whole point is that
a deployed model role uses the prompt definition that was measured rather than
a second copy of it.

Everything a model says here is a proposal and nothing more. The path a raw
model string travels before it could ever become a filename:

1. `SemanticProposer.propose` renders a bounded evidence text into the active
   `naming.semantic` prompt and asks a **loopback-only** Ollama model for one
   filename. No frontier/cloud model is reachable from this module: the only
   client it can construct is `OllamaTextProvider`, whose
   `OllamaLocalClient.__init__` refuses any endpoint that is not loopback
   HTTP. A non-loopback endpoint in config therefore degrades to a recorded
   fallback, not a request.
2. `_clean_model_output` strips the ways a chat model wraps an answer --
   markdown fences, quotation marks, a leading "Filename:" label, a trailing
   explanation -- and then **refuses** rather than repairs anything
   path-shaped, invisible, extension-changing, or over-long. A refusal is a
   normal outcome.
3. Whatever survives is handed back as a `NameProposal.proposed_filename` and
   goes through `naming/policy.py::validate_proposal` in `rename.py` exactly
   as a deterministic proposal does -- same extension rule, same sanitizer,
   same quality re-check, same rejection path. Step 2 is defense in depth in
   front of that, never instead of it.
4. Any failure at all -- connection refused, timeout, a `ValueError` out of
   the client, empty output, JSON, prose, a refused string, a derived
   confidence under `naming.semantic.min_confidence` -- falls back to
   `DeterministicProposer` when `fallback_to_deterministic` is set, and to
   `None` when it is not. The reason is recorded on the proposal either way,
   and the raw output is recorded on the `naming_history` row even when the
   string was thrown away. A hostile answer is auditable, not merely blocked.

The model never sees a `Path` and never sees the file. It is handed
`NamingEvidence` -- the same frozen plain data every proposer gets -- and what
it writes back is a string.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Any

from tools.ingest.naming.evidence import NamingEvidence, PerceivedEvidence
from tools.ingest.naming.policy import normalized_extension
from tools.ingest.naming.quality import looks_like_instruction
from tools.ingest.naming.proposer import DeterministicProposer, NameProposal, NameProposer
from tools.prompts.production import render_for_task_class

# Bump when the *proposer's* own behaviour changes in a way that should reopen
# every previously-declined file (the prompt text, the cleanup rules, the
# confidence formula). It is one component of `model_version`, which
# `ledger.evaluated_by` keys the "already answered" check on, so a bump here
# deliberately costs one re-evaluation per file.
SEMANTIC_PROPOSER_VERSION = "1"

TASK_CLASS = "naming.semantic"

# The model's answer is one filename. Anything past this is not a filename,
# and storing more of it in the audit ledger buys nothing.
MAX_RAW_OUTPUT_CHARS = 500

# How much evidence text the prompt carries. Bounded well below the
# `PerceivedEvidence` excerpt ceilings so a document, a screenshot's OCR and
# five video frames' descriptions together still leave a small local model
# room to answer.
MAX_TASK_TEXT_CHARS = 3000

# -- confidence -------------------------------------------------------------
#
# The model outputs only a filename -- no self-reported score, and a score it
# invented would be worth less than nothing, since a confidently wrong name is
# the exact failure mode this system is built to avoid. So confidence is
# derived here, deterministically, from properties of the string the model
# produced *relative to the evidence it was shown*. The rule, in full:
#
#   start at CONFIDENCE_BASE (0.70)
#   - PENALTY_GENERIC_STEM   if every word in the stem is a generic filler
#                            word ("document", "photo", "scan"): the model
#                            produced a category, not a name
#   - PENALTY_NO_OVERLAP     if no stem word appears anywhere in the evidence
#                            text: the name may be right, but nothing the
#                            model was shown supports it
#   + BONUS_EVIDENCE_TERMS   if two or more stem words appear in the evidence
#                            text: the name is grounded in what it read
#   + BONUS_VOCABULARY       if a configured `naming.vocabulary` word appears:
#                            the owner's own project/client language
#   clamped to [MIN_DERIVED_CONFIDENCE, MAX_DERIVED_CONFIDENCE]
#
# The ceiling is 0.90: below `CONFIDENCE_EMBEDDED_TITLE` (0.92), because a
# document that states its own title is better evidence than a model's reading
# of one, and this proposer should never outrank that.
CONFIDENCE_BASE = 0.70
PENALTY_GENERIC_STEM = 0.15
PENALTY_NO_OVERLAP = 0.10
BONUS_EVIDENCE_TERMS = 0.10
BONUS_VOCABULARY = 0.05
MIN_DERIVED_CONFIDENCE = 0.05
MAX_DERIVED_CONFIDENCE = 0.90

# Words that describe a category rather than a subject. Shared vocabulary with
# the Naming Lab's own hallucination scoring (`tools/naming_lab/runner.py`)
# in spirit; restated here because `naming/` may not import the Lab either.
GENERIC_STEM_WORDS = frozenset({
    "document", "documents", "photo", "photos", "image", "images", "video",
    "audio", "screenshot", "screen", "capture", "file", "files", "scan",
    "scanned", "copy", "final", "draft", "new", "untitled", "download",
    "downloads", "page", "pages", "attachment", "export", "output", "misc",
})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9']+")

# Characters a filename may never carry. The same classes `policy.py` refuses,
# checked here so the *model's* attempt shows up as an explicit refusal with a
# reason in the ledger rather than as a silently sanitized name.
_PATHISH = ("/", "\\", "\x00")
_INVISIBLE_CHARS = frozenset(
    "​‌‍‎‏‪‫‬‭‮"
    "⁠⁦⁧⁨⁩﻿"
)
_SLASH_HOMOGLYPHS = frozenset("⁄∕／⧸∖＼⧵")

# The wrappers a chat model puts around a one-line answer. Stripped, in order,
# before the string is judged -- a model that answered correctly and then
# added a code fence has not produced a hostile name, it has produced a
# correctly-formatted answer inside markdown.
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\s*(.*?)\s*```", re.DOTALL)
_LABEL_RE = re.compile(
    r"^(?:the\s+)?(?:proposed\s+|suggested\s+|new\s+|canonical\s+)?"
    r"(?:file\s*name|filename|name|answer|output|result)\s*[:=]\s*",
    re.IGNORECASE,
)


def _bounded_raw(text: str) -> str:
    """The model's answer, bounded but otherwise **verbatim**, for the ledger.

    Deliberately not sanitized, and that is the one place in this repository
    where file-derived text is stored as-is. The column exists so a refused
    answer can be read back exactly as the model produced it: a zero-width
    character, a BOM or a NUL byte is *why* the answer was refused, and a
    column that had quietly removed it would show a name that looks perfectly
    legal next to a rejection reason that makes no sense.
    `tests/ingest/test_semantic_proposer.py` asserts this fidelity per hostile
    case.

    What that costs is that the column can hold a terminal escape sequence --
    the prompt carried attacker-influenced document text, so a payload can
    persuade a model to echo one. That is defanged at the **print** boundary
    instead (`cli.py::_print_excerpt`), which is the only place this value
    reaches a terminal, and which therefore also protects every row written
    before anyone thought about it. Storage keeps the truth; display is where
    the truth is made safe to look at.
    """
    return text[:MAX_RAW_OUTPUT_CHARS]


def build_task_text(evidence: NamingEvidence, *, max_chars: int = MAX_TASK_TEXT_CHARS) -> str:
    """The `{task}` body: bounded, labeled, human-readable evidence.

    Modelled on `tools/naming_lab/runner.py::build_task_text`, which is where
    this format was actually iterated against real files and real local models
    (D-054) -- but written out here rather than imported, because
    `tools/naming_lab/` is a development tool that may change shape freely and
    production naming must not acquire a dependency on it. The two are allowed
    to diverge; what matters is that both feed the same *prompt*, through
    `render_for_task_class`.

    Every line is `key: value`. The prompt's policy section already tells the
    model that everything in this block is untrusted data describing a file
    and never an instruction to it; the labels are what make that claim
    legible to the model rather than merely true.
    """
    perceived = evidence.perceived or PerceivedEvidence()
    lines = [
        f"media_kind: {evidence.media_kind}",
        f"extension: {evidence.extension}",
        f"current_name_quality: {evidence.name_quality} ({evidence.name_quality_reason})",
    ]
    if evidence.capture_date:
        lines.append(f"capture_date: {evidence.capture_date}")
    elif evidence.modified_date:
        lines.append(f"modified_date: {evidence.modified_date}")
    if evidence.screenshot_by_name:
        lines.append("known_screen_capture: yes")
    for key in ("metadata_title", "metadata_author", "image_description", "camera_make", "camera_model", "page_count"):
        value = evidence.fact(key)
        if value:
            lines.append(f"{key}: {value}")
    if evidence.vocabulary:
        lines.append(f"owner_vocabulary: {', '.join(evidence.vocabulary[:20])}")

    if perceived.headings:
        lines.append(f"document_headings: {' | '.join(perceived.headings)}")
    if perceived.visual_image_kind:
        lines.append(f"visual_image_kind: {perceived.visual_image_kind}")
    if len(perceived.visual_descriptions) == 1:
        lines.append(f"visual_description: {perceived.visual_descriptions[0]}")
    elif perceived.visual_descriptions:
        lines.append(f"visual_description ({len(perceived.visual_descriptions)} sampled frames):")
        for index, description in enumerate(perceived.visual_descriptions, start=1):
            lines.append(f"  frame {index}: {description}")
    if perceived.visual_entities:
        lines.append(f"visual_entities: {', '.join(perceived.visual_entities)}")
    if perceived.transcript_excerpt:
        lines.append("speech_transcript:")
        lines.append(perceived.transcript_excerpt)
    if perceived.native_text_excerpt:
        lines.append("extracted_text:")
        lines.append(perceived.native_text_excerpt)
    if perceived.ocr_text_excerpt:
        lines.append("text_visible_in_pixels:")
        lines.append(perceived.ocr_text_excerpt)
    if perceived.warnings:
        lines.append(f"evidence_warnings: {', '.join(perceived.warnings)}")
    if not perceived.sources:
        lines.append("perceived_evidence: none available for this file")

    return "\n".join(lines)[:max_chars]


def _clean_model_output(raw: str, *, original_filename: str, max_filename_length: int) -> tuple[str | None, str]:
    """Turn a model's answer into a candidate filename, or refuse it.

    Returns `(candidate, reason)`. `candidate is None` means refused, and the
    reason says why -- that string is what lands in the ledger, so it has to
    be specific enough to be read a year later.

    Unwrapping (fences, labels, quotes) is generous, because a model wrapping
    a correct answer in markdown is a formatting miss, not an attack.
    Judgement is then strict and refuses rather than repairs: every check
    below rejects outright, because a name that had to be *fixed* is a name
    the model did not actually produce, and quietly renaming a file to the
    repaired version would put this module's guesswork on disk under the
    model's byline.
    """
    if not isinstance(raw, str):
        return None, f"model output is not a string: {type(raw).__name__}"
    text = raw.strip()
    if not text:
        return None, "model returned empty output"

    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    # A fence that was opened and never closed still has to be stripped, or
    # the leading "```" becomes part of the name.
    text = text.strip().lstrip("`").strip()

    # The answer is one filename, so take the first non-empty line and drop
    # whatever explanation followed it.
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    if not line:
        return None, "model returned no non-empty line"
    line = _LABEL_RE.sub("", line).strip()
    line = line.strip("\"'`•*").strip()
    # Trailing sentence punctuation from a model that wrote a sentence.
    line = line.rstrip(".,;:!").strip() if line.count(".") > 1 else line.rstrip(",;:!").strip()
    if not line:
        return None, "model output was only formatting"

    if any(character in line for character in _PATHISH):
        return None, "model output contains a path separator"
    if any(character in _SLASH_HOMOGLYPHS for character in line):
        return None, "model output contains a separator-lookalike character"
    if ".." in line:
        return None, "model output contains a parent-directory reference"
    if line in {".", ".."} or line.startswith("."):
        return None, "model output is a path component or a hidden filename"
    if any(character in _INVISIBLE_CHARS for character in line):
        return None, "model output contains zero-width or bidi-override characters"
    if any(unicodedata.category(character) in ("Cc", "Cf") for character in line):
        return None, "model output contains control characters"
    if len(line) > max_filename_length or len(line.encode("utf-8")) > max_filename_length:
        return None, f"model output is longer than the {max_filename_length}-character policy limit"

    # The extension belongs to the file, not to the model. `policy.validate_
    # proposal` would re-attach the real one regardless, but a model that
    # *changed* it was trying to change what the file is, and that is worth
    # recording as a refusal rather than silently correcting.
    true_extension = normalized_extension(original_filename)
    proposed_suffix = Path(line).suffix.lower()
    if proposed_suffix and true_extension and proposed_suffix != true_extension:
        if not line.lower().endswith(true_extension):
            return None, f"model changed the extension from {true_extension} to {proposed_suffix}"
    if proposed_suffix and not true_extension:
        return None, f"model added an extension ({proposed_suffix}) to a file that has none"

    stem = line[: -len(true_extension)] if (true_extension and line.lower().endswith(true_extension)) else line
    if not _WORD_RE.search(stem):
        return None, "model output contains no word characters"
    # A name that is itself an instruction ("ignore-all-previous-instructions")
    # is the model echoing document text rather than naming a subject; the
    # same naming-quality rule the deterministic title scan applies.
    if looks_like_instruction(stem.replace("-", " ").replace("_", " ")):
        return None, "model output is instruction-shaped text, not a subject"
    return line, "accepted"


def _stem_words(candidate: str, *, extension: str) -> list[str]:
    stem = candidate[: -len(extension)] if (extension and candidate.lower().endswith(extension)) else candidate
    # Drop a leading ISO date: it is grammar, not a claim about content.
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}[_-]", "", stem)
    return [word.lower() for word in _WORD_RE.findall(stem)]


def derive_confidence(
    candidate: str,
    *,
    evidence: NamingEvidence,
    extension: str,
) -> tuple[float, tuple[str, ...]]:
    """Score a model-produced filename without asking the model to score it.

    Returns `(confidence, notes)`; the notes name every adjustment that fired,
    so a low score is explainable from the ledger row. See this module's
    header comment for the rule in full -- it is documented there rather than
    only here because the numbers are the interesting part and they should be
    readable without opening this function.
    """
    words = _stem_words(candidate, extension=extension)
    if not words:
        return MIN_DERIVED_CONFIDENCE, ("no-words",)

    perceived = evidence.perceived or PerceivedEvidence()
    evidence_text = " ".join(
        part.lower()
        for part in (
            perceived.combined_text,
            perceived.transcript_excerpt,
            " ".join(perceived.visual_descriptions),
            " ".join(perceived.visual_entities),
            " ".join(perceived.headings),
            str(evidence.fact("metadata_title") or ""),
            str(evidence.fact("metadata_author") or ""),
            str(evidence.fact("image_description") or ""),
        )
        if part
    )
    vocabulary = {word.lower() for word in evidence.vocabulary}

    confidence = CONFIDENCE_BASE
    notes: list[str] = []
    if all(word in GENERIC_STEM_WORDS for word in words):
        confidence -= PENALTY_GENERIC_STEM
        notes.append("generic-stem")
    grounded = [word for word in words if word not in GENERIC_STEM_WORDS and word in evidence_text]
    if not grounded:
        confidence -= PENALTY_NO_OVERLAP
        notes.append("no-evidence-overlap")
    elif len(grounded) >= 2:
        confidence += BONUS_EVIDENCE_TERMS
        notes.append("grounded-in-evidence")
    if vocabulary and any(word in vocabulary for word in words):
        confidence += BONUS_VOCABULARY
        notes.append("owner-vocabulary")

    confidence = max(MIN_DERIVED_CONFIDENCE, min(MAX_DERIVED_CONFIDENCE, confidence))
    return round(confidence, 3), tuple(notes)


def resolve_model_identity(provider: Any, model: str) -> str:
    """`<proposer version>:<model digest or tag>`, resolved once per run.

    `model_version` is the key `ledger.evaluated_by` uses to decide whether a
    file's name has already been considered, so it has to be a single stable
    value for the whole run: a version that changed partway through would make
    every file after the change look unevaluated. That rules out resolving the
    digest lazily on first use, so it is resolved here, at construction, from
    one cheap loopback call -- and a failure is not an error, it just means
    the identity falls back to the configured tag.
    """
    digest = ""
    try:
        record = provider.client.installed_model(model)
        if record is not None and record.digest:
            digest = str(record.digest)[:12]
    except Exception:  # noqa: BLE001 - an unreachable Ollama is a fallback, not a crash
        digest = ""
    return f"{SEMANTIC_PROPOSER_VERSION}:{digest or model}"


class SemanticProposer:
    """A local model proposes; deterministic policy still disposes.

    Opt-in via `naming.proposer: "semantic"`. Takes only `NamingEvidence`, so
    it holds no filesystem authority for the same structural reason every
    other proposer does not: there is nothing in what it was handed to mutate
    a file with.
    """

    def __init__(
        self,
        *,
        model: str,
        endpoint: str,
        timeout_seconds: int = 60,
        num_predict: int = 64,
        min_confidence: float = 0.6,
        fallback_to_deterministic: bool = True,
        max_filename_length: int = 120,
        provider: Any | None = None,
        fallback: NameProposer | None = None,
        registry: Any | None = None,
    ) -> None:
        self.model_name = model
        self.endpoint = endpoint
        self.timeout_seconds = int(timeout_seconds)
        self.num_predict = int(num_predict)
        self.min_confidence = float(min_confidence)
        self.fallback_to_deterministic = bool(fallback_to_deterministic)
        self.max_filename_length = int(max_filename_length)
        self._registry = registry
        self._fallback: NameProposer | None = (
            fallback if fallback is not None
            else (DeterministicProposer() if fallback_to_deterministic else None)
        )

        self._provider = provider
        self._provider_error = ""
        if self._provider is None:
            try:
                # Imported here rather than at module scope so a machine with
                # no Ollama, or a config with a non-loopback endpoint, costs a
                # recorded fallback reason instead of an import-time or
                # construction-time failure of the whole run.
                from tools.models.ollama_local import OllamaTextProvider

                self._provider = OllamaTextProvider(endpoint, model, num_predict=self.num_predict)
            except Exception as exc:  # noqa: BLE001 - includes the loopback-only refusal
                self._provider = None
                self._provider_error = f"{type(exc).__name__}: {exc}"

        self.model = f"semantic:{model}"
        self.model_version = (
            resolve_model_identity(self._provider, model)
            if self._provider is not None
            else f"{SEMANTIC_PROPOSER_VERSION}:unavailable"
        )

    # -- the protocol -------------------------------------------------------

    def propose(self, evidence: NamingEvidence) -> NameProposal | None:
        """Ask the model for one filename; fall back on anything going wrong."""
        sources = evidence.perceived.sources if evidence.perceived is not None else ()

        if self._provider is None:
            return self._fall_back(
                evidence,
                reason=f"semantic proposer unavailable ({self._provider_error or 'no provider'})",
                sources=sources,
            )

        try:
            rendered = render_for_task_class(
                TASK_CLASS,
                task_text=build_task_text(evidence),
                extra={"original_filename": evidence.original_filename},
                registry=self._registry,
            )
        except Exception as exc:  # noqa: BLE001 - a prompt registry failure is not a rename
            return self._fall_back(
                evidence, reason=f"prompt rendering failed: {type(exc).__name__}: {exc}", sources=sources
            )

        prompt_fields: dict[str, Any] = {
            "prompt_id": rendered.prompt_id,
            "prompt_version": rendered.version,
            "prompt_sha256": rendered.sha256,
        }

        try:
            generation = self._provider.generate(
                rendered.text,
                timeout_seconds=self.timeout_seconds,
                num_predict=self.num_predict,
            )
        except Exception as exc:  # noqa: BLE001 - ConnectionError/TimeoutError/ValueError and anything else
            return self._fall_back(
                evidence,
                reason=f"model call failed: {type(exc).__name__}: {exc}",
                sources=sources,
                **prompt_fields,
            )

        raw = ""
        try:
            raw = str((generation or {}).get("output") or "")
        except Exception:  # noqa: BLE001 - a provider returning something unindexable
            raw = ""
        prompt_fields["raw_output"] = _bounded_raw(raw)

        candidate, reason = _clean_model_output(
            raw,
            original_filename=evidence.original_filename,
            max_filename_length=self.max_filename_length,
        )
        if candidate is None:
            return self._fall_back(evidence, reason=f"model output refused: {reason}", sources=sources, **prompt_fields)

        extension = normalized_extension(evidence.original_filename)
        confidence, notes = derive_confidence(candidate, evidence=evidence, extension=extension)
        if confidence < self.min_confidence:
            return self._fall_back(
                evidence,
                reason=(
                    f"derived confidence {confidence:.2f} below naming.semantic.min_confidence "
                    f"{self.min_confidence:.2f} ({', '.join(notes) or 'no adjustments'})"
                ),
                sources=sources,
                **prompt_fields,
            )

        detail = f" ({', '.join(notes)})" if notes else ""
        return NameProposal(
            proposed_filename=candidate,
            confidence=confidence,
            model=self.model,
            model_version=self.model_version,
            reason=f"local model {self.model_name} named this from perceived evidence{detail}",
            evidence_sources=sources,
            **prompt_fields,
        )

    # -- fallback -----------------------------------------------------------

    def _fall_back(
        self,
        evidence: NamingEvidence,
        *,
        reason: str,
        sources: tuple[str, ...],
        prompt_id: str | None = None,
        prompt_version: str | None = None,
        prompt_sha256: str | None = None,
        raw_output: str | None = None,
    ) -> NameProposal | None:
        """Hand the question to the deterministic proposer, or decline.

        The returned proposal keeps **this** proposer's `model_version`, so
        `ledger.evaluated_by`'s "already answered by this proposer version"
        check still works and a fallback does not cost a model call on every
        subsequent run. `model` says plainly which code produced the string.
        The prompt identity and raw output ride along even though they were
        not used, because "the model said this and it was thrown away" is the
        single most useful row in this audit trail.
        """
        audit: dict[str, Any] = {
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "prompt_sha256": prompt_sha256,
            "raw_output": raw_output,
            "evidence_sources": sources,
        }
        if self._fallback is None:
            return None
        try:
            fallback = self._fallback.propose(evidence)
        except Exception as exc:  # noqa: BLE001 - a broken fallback is still not a rename
            del exc
            return None
        if fallback is None:
            return None
        return replace(
            fallback,
            model=f"{self.model}->{fallback.model}",
            model_version=self.model_version,
            reason=f"{reason}; fell back to {fallback.model}: {fallback.reason}",
            **audit,
        )
