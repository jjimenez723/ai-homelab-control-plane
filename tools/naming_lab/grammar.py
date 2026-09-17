"""vNext filename grammar: `[YY_MMDD_]<descriptive-stem><actual-extension>`.

Two things stay strictly separate here, on purpose (spec section 4):

* `check_raw_compliance` -- did the *model* behave: one line, no prose, the
  expected extension, the vNext grammar shape, no path/prompt leakage.
* `validate_end_to_end` -- after trusted deterministic normalization
  (`compose_filename`, then production's own `policy.validate_proposal`),
  is the *result* filesystem-safe and an improvement.

A model that guesses the wrong extension fails the first without
automatically failing the second, because `compose_filename` always uses
the real source extension, never the model's -- exactly like production's
`tools.ingest.naming.policy.normalized_extension`, which this module calls
directly rather than reimplementing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tools.ingest.naming import policy
from tools.naming_lab.dates import SemanticDateCandidate, format_date_prefix

_LEGACY_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_")
_VNEXT_DATE_PREFIX_RE = re.compile(r"^\d{2}_\d{4}_")

# What a fully-formed vNext filename looks like, for raw-compliance checking.
# Deliberately looser than `policy.py`'s own guarantees (this only judges the
# model's raw text, not the trusted-normalized result).
_VNEXT_SHAPE_RE = re.compile(
    r"^(?:\d{2}_\d{4}_)?[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9]+(?:\.[a-z0-9]+)?$"
)

# Phrases that indicate the model echoed prompt structure back instead of
# proposing a filename -- a leakage signal, not a grammar one.
_PROMPT_LEAKAGE_MARKERS = (
    "policy:", "role:", "task:", "output contract", "context (untrusted",
    "[policy]", "[role]", "[task]",
)


def _strip_model_date_prefix(stem: str) -> str:
    """Drop a date-shaped prefix the model emitted, old or new grammar.

    The trusted prefix is always computed from evidence in `compose_filename`,
    never parsed from the model's text (spec section 1) -- this only exists
    so a model that guessed *a* date prefix does not end up with two.
    """
    for pattern in (_VNEXT_DATE_PREFIX_RE, _LEGACY_DATE_PREFIX_RE):
        match = pattern.match(stem)
        if match:
            return stem[match.end():]
    return stem


def compose_filename(
    semantic_date: SemanticDateCandidate | None,
    stem_text: str,
    extension: str,
) -> str:
    """Build `[YY_MMDD_]<slug><extension>` from a trusted date and free text.

    `extension` must already be the real source extension (callers get it
    from `policy.normalized_extension(original_filename)`), and is used
    verbatim -- this function does not re-derive or trust an extension out
    of `stem_text`.
    """
    candidate = Path(stem_text.strip()).name  # drop any path leakage defensively
    if extension and candidate.lower().endswith(extension.lower()):
        candidate = candidate[: -len(extension)]
    else:
        head, _, tail = candidate.rpartition(".")
        if head and 1 <= len(tail) <= 5 and tail.isalnum():
            candidate = head
    candidate = _strip_model_date_prefix(candidate)

    slug = policy.sanitize_stem(candidate, max_length=80)
    if not slug:
        return ""
    prefix = format_date_prefix(semantic_date.value) if semantic_date is not None else None
    stem = f"{prefix}_{slug}" if prefix else slug
    return f"{stem}{extension}"


@dataclass(frozen=True)
class RawComplianceResult:
    """Did the model's raw text behave -- independent of whether it would
    make a good filename."""

    passed: bool
    single_line: bool
    no_prose: bool
    no_leakage: bool
    extension_matches: bool
    grammar_matches: bool
    reasons: tuple[str, ...]


def check_raw_compliance(raw_output: str, *, expected_extension: str) -> RawComplianceResult:
    stripped = (raw_output or "").strip()
    reasons: list[str] = []

    single_line = bool(stripped) and "\n" not in stripped and "\r" not in stripped
    if not single_line:
        reasons.append("output is empty or spans multiple lines")

    no_prose = bool(stripped) and " " not in stripped and not stripped.startswith(("`", '"', "'"))
    if not no_prose:
        reasons.append("output contains prose/whitespace/quoting rather than a bare filename")

    lowered = stripped.lower()
    no_leakage = "/" not in stripped and "\\" not in stripped and not any(
        marker in lowered for marker in _PROMPT_LEAKAGE_MARKERS
    )
    if not no_leakage:
        reasons.append("output contains a path separator or echoes prompt structure")

    extension_matches = bool(expected_extension) and lowered.endswith(expected_extension.lower())
    if not extension_matches:
        reasons.append(f"output does not end with the expected extension {expected_extension!r}")

    grammar_matches = bool(_VNEXT_SHAPE_RE.match(lowered)) if single_line else False
    if not grammar_matches:
        reasons.append("output does not match the [YY_MMDD_]<stem><ext> grammar")

    passed = single_line and no_prose and no_leakage and extension_matches and grammar_matches
    return RawComplianceResult(
        passed=passed,
        single_line=single_line,
        no_prose=no_prose,
        no_leakage=no_leakage,
        extension_matches=extension_matches,
        grammar_matches=grammar_matches,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class EndToEndResult:
    """After trusted deterministic normalization: is the result usable at all."""

    passed: bool
    filename: str | None
    changed: bool
    policy_reason: str | None
    error: str | None


def _legacy_bridge_prefix(semantic_date: SemanticDateCandidate) -> str:
    return f"{semantic_date.value}_"  # the exact shape policy.sanitize_stem already protects


def validate_end_to_end(
    deterministic_filename: str,
    *,
    original_filename: str,
    max_filename_length: int,
    vocabulary: tuple[str, ...] = (),
    semantic_date: SemanticDateCandidate | None = None,
) -> EndToEndResult:
    """Reuses `policy.validate_proposal` -- the exact gate production applies.

    Never reimplements filesystem-safety/extension-authority/quality-gate
    logic; a policy rejection here is the same kind of rejection production
    would produce, not a Lab-specific approximation of one.

    One bridge is necessary: `policy.sanitize_stem` only special-cases a
    leading `YYYY-MM-DD_` date prefix (the one grammar `proposer.py` has
    always produced) to survive separator normalization untouched. Handed a
    `YY_MMDD_` prefix instead, it does not recognise it as a prefix at all --
    the single underscore after the date just falls into the general
    `[-_\\s]+` separator run and gets folded into a hyphen, silently turning
    `25_0615_northwind-invoice.pdf` into `25-0615-northwind-invoice.pdf`.
    Rather than reimplement `validate_proposal`'s safety/quality checks (or
    edit production's regex, out of scope for the Lab -- see DECISIONS.md
    D-054), this swaps the vNext prefix for the exact full-ISO shape
    `sanitize_stem` already protects before calling it, and swaps back
    afterward. The swap is exactly reversible: `semantic_date.value` is the
    one true source the vNext prefix was computed from, so nothing is
    invented or approximated in either direction.
    """
    if not deterministic_filename:
        return EndToEndResult(passed=False, filename=None, changed=False, policy_reason=None, error="empty proposal")

    bridged = deterministic_filename
    vnext_prefix = None
    if semantic_date is not None:
        vnext_prefix = format_date_prefix(semantic_date.value)
        marker = f"{vnext_prefix}_"
        if vnext_prefix and deterministic_filename.startswith(marker):
            bridged = _legacy_bridge_prefix(semantic_date) + deterministic_filename[len(marker):]

    try:
        validated = policy.validate_proposal(
            bridged,
            original_filename=original_filename,
            max_filename_length=max_filename_length,
            vocabulary=vocabulary,
        )
    except policy.NamePolicyError as exc:
        return EndToEndResult(passed=False, filename=None, changed=False, policy_reason=None, error=str(exc))

    filename = validated.filename
    if semantic_date is not None and vnext_prefix:
        legacy_marker = _legacy_bridge_prefix(semantic_date)
        if filename.startswith(legacy_marker):
            filename = f"{vnext_prefix}_{filename[len(legacy_marker):]}"

    return EndToEndResult(
        passed=True, filename=filename, changed=(filename != original_filename),
        policy_reason=validated.reason, error=None,
    )
