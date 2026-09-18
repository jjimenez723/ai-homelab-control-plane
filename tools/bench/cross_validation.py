"""D-043: an independent second evaluator, checked for agreement.

D-043 makes Inspect AI the *preferred* cross-validation substrate, pending a
compatibility check, and requires that no adoption happen before running
the same task through both Angel's own evaluator and a second
implementation and investigating any disagreement.

**Compatibility finding, recorded here rather than assumed:** `inspect_ai`
is not importable in this repository's `.venv`
(`ModuleNotFoundError: No module named 'inspect_ai'`), and the venv has no
`pip` module at all (`python3 -m pip` -> `No module named pip`) -- there is
no working package installer to add it with in this environment, only a
plain interpreter. Bootstrapping one, then installing a framework with its
own dependency tree (a chat/agent harness pulling in provider SDKs, a
sandboxing layer, its own logging format) to satisfy a documentation
requirement is exactly the "add technology merely because it sounds
impressive" this project's own engineering standard forbids -- especially
before a single ACB-v1 result exists for it to cross-check. Per D-043's own
fallback ("if Inspect AI is no longer the best fit, document the evidence
and use a better independent mechanism"), this module is that mechanism:
a from-scratch, independently-written second implementation of each Gate-2
suite's deterministic oracle, sharing no code with
`tools/bench/acb_v1/routing_classification.py` or
`tools/bench/acb_v1/semantic_naming.py` beyond the immutable task data
itself (`SuiteTask.input`/`expected`) -- not even
`tools.ingest.naming.policy`. `tests/test_cross_validation.py` runs a
representative set of real and adversarial candidate outputs through both
implementations and asserts the verdicts agree; a disagreement there is a
bug in one of the two, exactly as D-043 requires, never assumed away.

This does not migrate anything to Inspect AI and does not change which
implementation is authoritative -- `tools/bench/acb_v1/*` remains the suite
oracle a campaign run and a certification are measured against
(`tools/campaign/suites.py`). This module exists only to say, with
evidence, that Angel's own oracle is not grading itself incorrectly.
If Inspect AI becomes installable in a future environment, it is a
candidate to *replace* this module's role, not `tools/bench/acb_v1/*`'s.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import PurePosixPath

from tools.bench.suite import SuiteTask

INSPECT_AI_COMPATIBILITY_FINDING = (
    "inspect_ai is not importable in this repository's .venv, and the venv's "
    "python has no pip module to install it with (checked 2026-09-14/15). No "
    "migration performed; this module is the documented fallback per D-043."
)


def _tokens(stem: str) -> set[str]:
    """A deliberately different tokenizer from quality.tokenize -- same job,
    independent code: split on any run of non-alphanumeric characters."""
    return {piece.lower() for piece in re.split(r"[^A-Za-z0-9]+", stem) if piece}


def cross_verify_routing_classification(task: SuiteTask, output: object) -> bool:
    """Independent re-check: exact match against the expected label.

    Written directly against `task.expected["label"]` with no import of
    `tools.bench.acb_v1.routing_classification`. Suite 1.1.0 separated format
    compliance from label correctness in the *reported detail* but did not
    loosen the verdict: one bare label, exactly, is still what passes -- so
    this side stays a plain equality check, which is the point of an
    independent implementation (if the primary oracle had quietly started
    accepting decorated answers, these two would disagree).
    """
    if not isinstance(output, str):
        return False
    return output.strip().lower() == str(task.expected["label"]).strip().lower()


# Suite 1.1.0 scores a date-shaped token anywhere in the stem, not only a
# leading prefix. Written independently here: a plain scan for the three
# shapes, with a calendar check, rather than the primary oracle's pattern
# tuple.
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-_]?(\d{2})[-_]?(\d{2})(?!\d)")

_SUFFIXES = ("ings", "ing", "ies", "es", "ed", "s")

# Words that say nothing about a file's subject, for the evidence-grounding
# rule suite 1.1.0 added for ambiguous assets. Written here independently of
# the oracle's own list.
_FUNCTION_WORDS = frozenset({
    "about", "also", "and", "any", "are", "been", "beyond", "both", "but",
    "clear", "contains", "does", "each", "either", "from", "have", "into",
    "only", "other", "over", "page", "part", "reads", "resembles", "short",
    "some", "such", "than", "that", "their", "them", "then", "there", "these",
    "this", "three", "through", "times", "very", "what", "when", "which",
    "while", "with", "without", "would",
})


def _word_family(word: str) -> str:
    """Crude independent stemmer; different code, same intent as the oracle's."""
    lowered = word.lower()
    for suffix in _SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 4:
            return lowered[: -len(suffix)]
    return lowered


def _same_family(left: str, right: str) -> bool:
    if left == right or _word_family(left) == _word_family(right):
        return True
    shared = 0
    for a, b in zip(left, right):
        if a != b:
            break
        shared += 1
    return shared >= 7


def _dates_in(text: str) -> set[str]:
    found: set[str] = set()
    for match in _DATE_RE.finditer(text):
        year, month, day = (int(match.group(i)) for i in (1, 2, 3))
        if not 1900 <= year <= 2099:
            continue
        try:
            found.add(dt.date(year, month, day).isoformat())
        except ValueError:
            continue
    return found


def cross_verify_semantic_naming(task: SuiteTask, proposed: object) -> bool:
    """Independent re-check of the same deterministic properties.

    Reimplemented from scratch against `task.input`/`task.expected` --
    no import of `tools.ingest.naming.policy` or
    `tools.bench.acb_v1.semantic_naming`. Deliberately simpler than the
    production policy (no reserved-stem rewriting, no collision handling):
    it only needs to reach the same pass/fail verdict on the suite's own
    fixed items, not reimplement the whole naming pipeline.
    """
    if not isinstance(proposed, str):
        return False
    name = proposed.strip()
    if not name or "/" in name or "\\" in name or "\x00" in name:
        return False
    if name in {".", ".."}:
        return False

    original = task.input["original_filename"]
    expected = task.expected
    required_ext = PurePosixPath(original.lower()).suffix
    got_ext = PurePosixPath(name.lower()).suffix
    if required_ext and got_ext != required_ext:
        return False

    if len(name.encode("utf-8")) > expected.get("max_filename_length", 80):
        return False

    stem = name.rsplit(".", 1)[0] if "." in name else name
    tokens = _tokens(stem)
    squashed = re.sub(r"[^a-z0-9]+", "", stem.lower())

    def satisfied(word: str) -> bool:
        lowered_word = word.lower()
        if any(_same_family(lowered_word, token) for token in tokens):
            return True
        flat = re.sub(r"[^a-z0-9]+", "", lowered_word)
        return len(flat) >= 6 and flat in squashed

    for concept in expected.get("required_concept_sets", ()):
        if not any(satisfied(word) for word in concept):
            return False

    minimum = int(expected.get("min_evidence_words", 0) or 0)
    if minimum:
        evidence = str(task.input.get("evidence_text", "")).lower()
        words = {
            piece for piece in re.split(r"[^a-z0-9]+", evidence)
            if len(piece) >= 4 and not piece.isdigit() and piece not in _FUNCTION_WORDS
        }
        if sum(1 for word in words if satisfied(word)) < minimum:
            return False

    for word in expected.get("forbidden_tokens", ()):
        # Exact, unstemmed -- widening a prohibition would fail correct names.
        if word.lower() in tokens:
            return False

    allowed_dates = set(expected.get("allowed_dates", ()))
    found = _dates_in(stem)
    if found - allowed_dates:
        return False
    if not found and expected.get("require_date", False):
        return False

    lowered = name.lower()
    for fragment in expected.get("forbidden_substrings", ()):
        if fragment.lower() in lowered:
            return False

    return True
