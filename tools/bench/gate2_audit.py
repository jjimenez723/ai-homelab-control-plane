"""Human-reviewable audit of the Gate-2 benchmark contract.

This module does not change suite answers, prompts, thresholds, or campaign
records.  It renders the model-facing prompt contract beside the human-authored
oracle expectations and performs deterministic self-consistency checks so an
operator can inspect what Angel currently calls "correct" before tuning a
model or prompt.

Frozen-holdout task content is deliberately excluded by default.  Development
and validation inputs are reviewable because the immediate purpose is to audit
the already-run validation campaign.  If validation material is then used to
change a prompt, skill, examples, or thresholds, it has been consumed for
model-development purposes and must not continue to serve as a clean
validation set.  The frozen holdout remains untouched for final certification.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from tools.bench.acb_v1 import routing_classification as routing
from tools.bench.acb_v1 import semantic_naming as naming
from tools.bench.suite import ContaminationError, SuiteTask, assert_no_holdout_leak
from tools.prompts.library import ROUTING_CLASSIFICATION_LABELS
from tools.prompts.registry import PromptVersion, load_default_registry

# v2 (D-059): adds answer/few-shot/holdout leakage checks, live oracle
# false-negative and false-positive probes, extension-separation, expectation
# groundedness, uncertainty guidance, and scenario/label balance -- the audit
# dimensions `docs/30-gate2-naming-audit.md` enumerates.
AUDIT_SCHEMA_VERSION = 2
DEFAULT_SPLITS: tuple[str, ...] = ("dev", "validation")
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


@dataclass(frozen=True)
class AuditFinding:
    severity: str
    check: str
    suite_id: str
    detail: str
    task_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "check": self.check,
            "suite_id": self.suite_id,
            "task_id": self.task_id,
            "detail": self.detail,
        }


def _visible_iso_dates(task: SuiteTask) -> tuple[set[str], set[str], set[str]]:
    evidence = set(_ISO_DATE_RE.findall(str(task.input.get("evidence_text", ""))))
    original = set(_ISO_DATE_RE.findall(str(task.input.get("original_filename", ""))))
    return evidence, original, evidence | original


def _prompt_snapshot(prompt: PromptVersion) -> dict[str, Any]:
    return {
        "prompt_id": prompt.prompt_id,
        "version": prompt.version,
        "status": prompt.status,
        "capability": prompt.capability,
        "policy_template": prompt.policy_template,
        "role_template": prompt.role_template,
        "task_template": prompt.task_template,
        "output_schema": prompt.output_schema,
        "tool_schema_version": prompt.tool_schema_version,
        "renderer_version": prompt.renderer_version,
        "applicable_constraints": list(prompt.applicable_constraints),
        "notes": prompt.notes,
        "content_fingerprint": prompt.content_fingerprint,
    }


def _routing_item(task: SuiteTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "split": task.split,
        "scenario": task.scenario,
        "description": task.input["description"],
        "expected_label": task.expected["label"],
        "risk": task.risk,
        "privacy": task.privacy,
        "notes": task.notes,
    }


def _naming_item(task: SuiteTask) -> dict[str, Any]:
    expected = task.expected
    return {
        "id": task.id,
        "split": task.split,
        "scenario": task.scenario,
        "original_filename": task.input["original_filename"],
        "evidence_text": task.input["evidence_text"],
        "vocabulary": list(task.input.get("vocabulary", ())),
        "expected": {
            "required_concept_sets": [list(group) for group in expected.get("required_concept_sets", ())],
            "forbidden_tokens": list(expected.get("forbidden_tokens", ())),
            "allowed_dates": list(expected.get("allowed_dates", ())),
            "require_date": bool(expected.get("require_date", False)),
            "forbidden_substrings": list(expected.get("forbidden_substrings", ())),
            "max_filename_length": int(expected.get("max_filename_length", 80)),
        },
        "risk": task.risk,
        "privacy": task.privacy,
        "notes": task.notes,
    }


def _selected(tasks: Iterable[SuiteTask], splits: tuple[str, ...]) -> list[SuiteTask]:
    allowed = set(splits)
    return [task for task in tasks if task.split in allowed]


def _audit_routing_prompt(prompt: PromptVersion) -> list[AuditFinding]:
    """Flag when the model sees label names but no label-by-label definitions."""
    material = "\n".join((prompt.role_template, prompt.task_template, prompt.output_schema)).lower()
    defined = [label for label in ROUTING_CLASSIFICATION_LABELS if f"{label}:" in material]
    if len(defined) == len(ROUTING_CLASSIFICATION_LABELS):
        return []
    return [AuditFinding(
        severity="warning",
        check="routing_taxonomy_undefined_in_prompt",
        suite_id=routing.SUITE_ID,
        detail=(
            "The model-facing routing prompt exposes the closed label names but does not provide "
            "label-by-label definitions or precedence rules. Ground-truth labels therefore rely "
            "on distinctions the model must infer from label names."
        ),
    )]


def _audit_naming_contract(tasks: list[SuiteTask], prompt: PromptVersion) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    if any(bool(task.expected.get("require_date", False)) for task in tasks):
        schema = prompt.output_schema.lower()
        if "optional leading iso-8601 date" in schema:
            findings.append(AuditFinding(
                severity="error",
                check="prompt_date_requirement_mismatch",
                suite_id=naming.SUITE_ID,
                detail=(
                    "The naming prompt tells the model that a leading ISO date is optional, but "
                    "one or more reviewed tasks set require_date=True. A model can obey the "
                    "published output contract and still be deterministically rejected."
                ),
            ))

    for task in tasks:
        expected = task.expected
        allowed_dates = set(expected.get("allowed_dates", ()))
        evidence_dates, original_dates, visible_dates = _visible_iso_dates(task)

        if expected.get("require_date", False) and not allowed_dates:
            findings.append(AuditFinding(
                severity="error",
                check="required_date_without_allowed_date",
                suite_id=naming.SUITE_ID,
                task_id=task.id,
                detail="Task requires a date but defines no allowed_dates value.",
            ))

        unsupported_evidence_dates = sorted(evidence_dates - allowed_dates)
        if unsupported_evidence_dates:
            findings.append(AuditFinding(
                severity="error",
                check="evidence_date_not_allowed",
                suite_id=naming.SUITE_ID,
                task_id=task.id,
                detail=(
                    "Evidence shown to the model contains ISO date(s) "
                    f"{unsupported_evidence_dates}, but they are absent from allowed_dates. "
                    "If the model uses one as the filename prefix, the verifier classifies the "
                    "evidence-supported date as unsupported_or_hallucinated_date."
                ),
            ))

        invisible_allowed_dates = sorted(allowed_dates - visible_dates)
        if invisible_allowed_dates:
            findings.append(AuditFinding(
                severity="error",
                check="allowed_date_not_visible_to_model",
                suite_id=naming.SUITE_ID,
                task_id=task.id,
                detail=(
                    f"allowed_dates contains {invisible_allowed_dates}, but those date strings are "
                    "not present in either the evidence_text or original_filename supplied to the "
                    "model. The current input adapter has no other naming context channel."
                ),
            ))

        if expected.get("require_date", False) and not visible_dates:
            findings.append(AuditFinding(
                severity="error",
                check="required_date_not_visible_to_model",
                suite_id=naming.SUITE_ID,
                task_id=task.id,
                detail=(
                    "Task requires a date but no ISO date is visible in the model-facing evidence "
                    "or original filename."
                ),
            ))

        # Keep original-filename-only dates visible in the audit because they can be useful metadata,
        # but do not call their omission from allowed_dates an error: contradictory-metadata tasks
        # intentionally contain stale dates in a filename that the model must reject.
        _ = original_dates

    return findings


# ---------------------------------------------------------------------------
# D-059 additions: the audit dimensions `docs/30-gate2-naming-audit.md` names.
# Every check below is deterministic and runs against the live suite/prompt
# objects, so the audit is a regression guard, not a snapshot of one review.
# ---------------------------------------------------------------------------

_UNCERTAINTY_WORDS = ("unclear", "uncertain", "unknown", "not legible", "insufficient",
                      "cannot be identified", "no clear", "ambiguous")

# Words a prompt must be able to use to state its own contract. Their presence
# in a template is scaffolding, not an expected answer leaked into it.
_PROMPT_SCAFFOLD_WORDS = frozenset({
    "contract", "proposal", "policy", "instruction", "instructions", "output",
    "filename", "evidence", "context", "extension", "organization", "person",
    "classification", "label", "naming", "routing", "general", "document",
})


def _prompt_material(prompt: PromptVersion) -> list[str]:
    return [prompt.policy_template, prompt.role_template, prompt.task_template,
            prompt.output_schema, prompt.notes or ""]


def _normalize(text: str) -> str:
    return " ".join(str(text).split()).strip().lower()


def _audit_prompt_leakage(prompt: PromptVersion, tasks: list[SuiteTask], suite_id: str,
                          *, text_fields: tuple[str, ...]) -> list[AuditFinding]:
    """Answer leakage and few-shot contamination, checked in both directions.

    Two separate risks, both of which would silently inflate a score:

    * a *task's own text* appearing in the prompt (an item the model is graded
      on has been shown to it as an example);
    * an *answer* appearing in the prompt (an allowed date or a distinctive
      required-concept word baked into the template).

    Checked over every reviewed split, not only frozen_holdout -- the run-time
    guard `tools.bench.suite.assert_no_holdout_leak` already covers holdout,
    and dev contamination is still a measurement defect even though it is not
    a holdout breach.
    """
    findings: list[AuditFinding] = []
    blob = _normalize("\n".join(_prompt_material(prompt)))
    if not blob:
        return findings

    for task in tasks:
        for field in text_fields:
            value = task.input.get(field)
            if not value:
                continue
            needle = _normalize(str(value))
            if len(needle) > 30 and needle in blob:
                findings.append(AuditFinding(
                    severity="error", check="prompt_contains_suite_item_text",
                    suite_id=suite_id, task_id=task.id,
                    detail=(f"The {field!r} of a graded {task.split} item appears verbatim in the "
                            "prompt template, so the model is shown an item it is scored on."),
                ))

        for date in task.expected.get("allowed_dates", ()):
            if str(date) in blob:
                findings.append(AuditFinding(
                    severity="error", check="prompt_contains_expected_answer",
                    suite_id=suite_id, task_id=task.id,
                    detail=f"The prompt template contains this item's expected date {date!r}.",
                ))
        for concept in task.expected.get("required_concept_sets", ()):
            for word in concept:
                lowered = str(word).lower()
                if len(lowered) < 5 or lowered in _PROMPT_SCAFFOLD_WORDS:
                    # A prompt necessarily uses words like "output contract" and
                    # "proposal" to state its own contract; those collisions are
                    # scaffolding, not an answer leaked into the template.
                    continue
                if re.search(rf"\b{re.escape(lowered)}\b", blob):
                    findings.append(AuditFinding(
                        severity="warning", check="prompt_contains_expected_answer",
                        suite_id=suite_id, task_id=task.id,
                        detail=(f"The prompt template contains the required-concept word "
                                f"{word!r} for a graded item."),
                    ))
        label = task.expected.get("label")
        if isinstance(label, str) and label not in ROUTING_CLASSIFICATION_LABELS:
            findings.append(AuditFinding(
                severity="error", check="expected_label_outside_closed_set",
                suite_id=suite_id, task_id=task.id,
                detail=f"Expected label {label!r} is not in the published closed label set.",
            ))
    return findings


def _audit_few_shot(prompt: PromptVersion, suites: dict[str, Any]) -> list[AuditFinding]:
    """Whether a prompt carrying examples declares and isolates them."""
    findings: list[AuditFinding] = []
    material = "\n".join(_prompt_material(prompt))
    looks_like_examples = "example" in material.lower() and "\n" in material
    if prompt.example_set_version and not looks_like_examples:
        findings.append(AuditFinding(
            severity="warning", check="example_set_declared_but_absent",
            suite_id="-", detail=(f"{prompt.prompt_id}@{prompt.version} declares example_set_version "
                                  f"{prompt.example_set_version!r} but its templates contain no examples."),
        ))
    if looks_like_examples and not prompt.example_set_version:
        findings.append(AuditFinding(
            severity="warning", check="examples_without_example_set_version",
            suite_id="-", detail=(f"{prompt.prompt_id}@{prompt.version} appears to contain few-shot "
                                  "examples but declares no example_set_version, so the example set "
                                  "is not separately versioned or pinnable."),
        ))
    if not prompt.example_set_version:
        return findings
    # A declared example set must not quote any suite item, in any split.
    for suite_id, definition in suites.items():
        for task in definition.tasks:
            for field in ("description", "evidence_text"):
                value = task.input.get(field)
                if not value:
                    continue
                needle = _normalize(str(value))
                if len(needle) > 30 and needle in _normalize(material):
                    findings.append(AuditFinding(
                        severity="error", check="few_shot_contamination",
                        suite_id=suite_id, task_id=task.id,
                        detail=(f"A few-shot example in {prompt.prompt_id}@{prompt.version} quotes "
                                f"a {task.split} suite item."),
                    ))
    return findings


def _reference_answer(task: SuiteTask) -> str:
    """A deliberately obvious correct answer, built only from the item's own expectations.

    If this does not pass, the item has no reachable passing answer and the
    defect is in the benchmark, not in any model. Built from the expectation
    fields alone -- never from an observed model output, so this check cannot
    become a way of tuning the oracle to whatever a candidate happened to say.
    """
    parts: list[str] = []
    for concept in task.expected.get("required_concept_sets", ()):
        if concept:
            parts.append(str(concept[0]))
    minimum = int(task.expected.get("min_evidence_words", 0) or 0)
    if minimum:
        parts.extend(naming.evidence_content_words(str(task.input.get("evidence_text", "")))[:minimum])
    if not parts:
        parts.append("document")
    stem = "-".join(parts)
    allowed = tuple(task.expected.get("allowed_dates", ()))
    if allowed and task.expected.get("require_date", False):
        stem = f"{allowed[0]}_{stem}"
    extension = Path(str(task.input["original_filename"])).suffix.lower()
    return stem + extension


def _audit_naming_oracle_behaviour(tasks: list[SuiteTask]) -> list[AuditFinding]:
    """Live probes of the oracle: false negatives, false positives, extension split."""
    findings: list[AuditFinding] = []
    for task in tasks:
        reference = _reference_answer(task)
        passed, detail = naming.verify(task, reference)
        if not passed:
            findings.append(AuditFinding(
                severity="error", check="semantic_false_negative",
                suite_id=naming.SUITE_ID, task_id=task.id,
                detail=(f"An answer assembled only from this item's own expectations "
                        f"({reference!r}) is rejected by the oracle: "
                        f"{detail.get('failure')!r}. The item has no reachable passing answer."),
            ))

        # False positive: the original name, unchanged, must never pass.
        original = str(task.input["original_filename"])
        passed_original, _ = naming.verify(task, original)
        if passed_original:
            findings.append(AuditFinding(
                severity="error", check="semantic_false_positive",
                suite_id=naming.SUITE_ID, task_id=task.id,
                detail=f"The unchanged original filename {original!r} passes the oracle.",
            ))
        # False positive: a contentless generic name must never pass.
        generic = "document" + Path(original).suffix.lower()
        passed_generic, _ = naming.verify(task, generic)
        if passed_generic:
            findings.append(AuditFinding(
                severity="error", check="semantic_false_positive",
                suite_id=naming.SUITE_ID, task_id=task.id,
                detail=f"A contentless name {generic!r} passes the oracle.",
            ))

        # Extension handling must be reported separately from semantics.
        wrong_extension = str(Path(reference).with_suffix(".zzz"))
        ext_passed, ext_detail = naming.verify(task, wrong_extension)
        if ext_passed:
            findings.append(AuditFinding(
                severity="error", check="extension_not_enforced",
                suite_id=naming.SUITE_ID, task_id=task.id,
                detail=f"A proposal with the wrong extension ({wrong_extension!r}) passes.",
            ))
        elif not ext_detail.get("semantic_pass", False):
            findings.append(AuditFinding(
                severity="error", check="extension_conflated_with_semantics",
                suite_id=naming.SUITE_ID, task_id=task.id,
                detail=("A proposal that is semantically correct but has the wrong extension is "
                        "not reported as semantically correct, so a formatting mistake and a "
                        "comprehension failure are indistinguishable in the result records."),
            ))
    return findings


def _audit_naming_expectation_sanity(tasks: list[SuiteTask]) -> list[AuditFinding]:
    """Synonyms, entity contradictions, unrealistic evidence, leakage-check sanity."""
    findings: list[AuditFinding] = []
    for task in tasks:
        evidence = str(task.input.get("evidence_text", "")).lower()
        original = str(task.input.get("original_filename", "")).lower()
        visible = f"{evidence}\n{original}"

        for concept in task.expected.get("required_concept_sets", ()):
            if not any(str(word).lower() in visible for word in concept):
                findings.append(AuditFinding(
                    severity="error", check="concept_set_not_evidence_grounded",
                    suite_id=naming.SUITE_ID, task_id=task.id,
                    detail=(f"No synonym in required concept {list(concept)} appears in the "
                            "evidence or original filename the model is shown, so the "
                            "expectation cannot be met from the evidence supplied."),
                ))
            if len(concept) == 1 and len(str(concept[0])) >= 5:
                findings.append(AuditFinding(
                    severity="info", check="single_surface_concept",
                    suite_id=naming.SUITE_ID, task_id=task.id,
                    detail=(f"Required concept {list(concept)} accepts one surface form. Stem and "
                            "phrase matching widen it, but a genuine synonym "
                            "(a second word for the same thing) would widen it further."),
                ))

        forbidden = {str(word).lower() for word in task.expected.get("forbidden_tokens", ())}
        for concept in task.expected.get("required_concept_sets", ()):
            overlap = forbidden & {str(word).lower() for word in concept}
            if overlap:
                findings.append(AuditFinding(
                    severity="error", check="forbidden_token_contradicts_required_concept",
                    suite_id=naming.SUITE_ID, task_id=task.id,
                    detail=f"Token(s) {sorted(overlap)} are both required and forbidden.",
                ))
        for word in sorted(forbidden):
            for concept in task.expected.get("required_concept_sets", ()):
                for required in concept:
                    lowered = str(required).lower()
                    if lowered != word and (word in lowered or lowered in word):
                        findings.append(AuditFinding(
                            severity="warning", check="forbidden_token_overlaps_required_concept",
                            suite_id=naming.SUITE_ID, task_id=task.id,
                            detail=(f"Forbidden token {word!r} shares a surface with required "
                                    f"concept word {required!r}; a correct answer could trip the "
                                    "prohibition if matching is ever widened."),
                        ))

        for fragment in task.expected.get("forbidden_substrings", ()):
            if str(fragment).lower() not in visible:
                findings.append(AuditFinding(
                    severity="warning", check="leakage_check_not_grounded",
                    suite_id=naming.SUITE_ID, task_id=task.id,
                    detail=(f"Forbidden substring {fragment!r} does not appear in the evidence or "
                            "original filename, so the leakage check cannot fire for this item."),
                ))
    return findings


def _audit_uncertainty_guidance(prompt: PromptVersion, definition: Any,
                                suite_id: str) -> list[AuditFinding]:
    """A suite with ambiguous items whose prompt says nothing about uncertainty."""
    ambiguous = [t for t in definition.tasks
                 if "ambiguous" in t.scenario or t.scenario == "ambiguous_asset"]
    if not ambiguous:
        return []
    material = "\n".join(_prompt_material(prompt)).lower()
    if any(word in material for word in _UNCERTAINTY_WORDS):
        return []
    return [AuditFinding(
        severity="warning", check="no_uncertainty_guidance_in_prompt",
        suite_id=suite_id,
        detail=(f"{len(ambiguous)} item(s) present deliberately ambiguous or illegible evidence, "
                "but the prompt gives the model no instruction for what to do when the evidence "
                "does not identify the subject. The oracle still grades those items."),
    )]


def _audit_balance(definition: Any, suite_id: str) -> list[AuditFinding]:
    """Scenario and label imbalance across splits, reported rather than assumed away."""
    findings: list[AuditFinding] = []
    for split in ("dev", "validation", "frozen_holdout"):
        counts: dict[str, int] = {}
        for task in definition.by_split(split):
            counts[task.scenario] = counts.get(task.scenario, 0) + 1
        if not counts:
            continue
        low, high = min(counts.values()), max(counts.values())
        if high > low:
            heavy = sorted(name for name, n in counts.items() if n == high)
            findings.append(AuditFinding(
                severity="info", check="scenario_imbalance",
                suite_id=suite_id,
                detail=(f"split {split!r} carries {low}-{high} items per scenario; "
                        f"scenario(s) {heavy} carry the most, so a single pass rate over the split "
                        "weights them more heavily than the rest."),
            ))
    labels: dict[str, int] = {}
    for task in definition.tasks:
        label = task.expected.get("label")
        if isinstance(label, str):
            labels[label] = labels.get(label, 0) + 1
    if labels:
        total = sum(labels.values())
        for label, count in sorted(labels.items()):
            if count / total >= 0.20:
                findings.append(AuditFinding(
                    severity="info", check="label_frequency_skew",
                    suite_id=suite_id,
                    detail=(f"label {label!r} is the answer for {count} of {total} items "
                            f"({count / total:.0%}); a model that always answers it scores that "
                            "much without classifying anything."),
                ))
        unused = [name for name in ROUTING_CLASSIFICATION_LABELS if name not in labels]
        if unused:
            findings.append(AuditFinding(
                severity="info", check="label_never_correct",
                suite_id=suite_id,
                detail=(f"label(s) {unused} are published in the closed set but are never the "
                        "correct answer for any item, so the suite does not measure them."),
            ))
    return findings


def _audit_contamination_guard(suites: dict[str, Any],
                               prompts: dict[str, PromptVersion]) -> list[AuditFinding]:
    """Run the real run-time guard and record its verdict as a finding."""
    findings: list[AuditFinding] = []
    fields = {naming.SUITE_ID: ("evidence_text",), routing.SUITE_ID: ("description",)}
    for suite_id, definition in suites.items():
        prompt = prompts[suite_id]
        try:
            assert_no_holdout_leak(definition, prompt_material=_prompt_material(prompt),
                                   fields=fields[suite_id])
        except ContaminationError as exc:
            findings.append(AuditFinding(
                severity="error", check="frozen_holdout_leak", suite_id=suite_id,
                detail=str(exc),
            ))
        else:
            findings.append(AuditFinding(
                severity="info", check="frozen_holdout_guard_clean", suite_id=suite_id,
                detail=(f"tools.bench.suite.assert_no_holdout_leak passes for "
                        f"{prompt.prompt_id}@{prompt.version}: no frozen_holdout item's text "
                        "appears in the prompt material."),
            ))
    return findings


def generate_gate2_audit(*, splits: tuple[str, ...] = DEFAULT_SPLITS) -> dict[str, Any]:
    """Return a JSON-serializable, read-only audit of the Gate-2 contract."""
    unknown = set(splits) - {"dev", "validation", "frozen_holdout"}
    if unknown:
        raise ValueError(f"unknown split(s): {sorted(unknown)}")

    registry = load_default_registry()
    routing_prompt = registry.active("acb-v1-routing-classification")
    naming_prompt = registry.active("acb-v1-semantic-naming")
    routing_tasks = _selected(routing.SUITE.tasks, splits)
    naming_tasks = _selected(naming.SUITE.tasks, splits)

    suites = {routing.SUITE_ID: routing.SUITE, naming.SUITE_ID: naming.SUITE}
    prompts = {routing.SUITE_ID: routing_prompt, naming.SUITE_ID: naming_prompt}

    findings = _audit_routing_prompt(routing_prompt)
    findings.extend(_audit_naming_contract(naming_tasks, naming_prompt))
    findings.extend(_audit_prompt_leakage(routing_prompt, routing_tasks, routing.SUITE_ID,
                                          text_fields=("description",)))
    findings.extend(_audit_prompt_leakage(naming_prompt, naming_tasks, naming.SUITE_ID,
                                          text_fields=("evidence_text",)))
    findings.extend(_audit_few_shot(routing_prompt, suites))
    findings.extend(_audit_few_shot(naming_prompt, suites))
    findings.extend(_audit_contamination_guard(suites, prompts))
    findings.extend(_audit_naming_oracle_behaviour(naming_tasks))
    findings.extend(_audit_naming_expectation_sanity(naming_tasks))
    findings.extend(_audit_uncertainty_guidance(naming_prompt, naming.SUITE, naming.SUITE_ID))
    findings.extend(_audit_balance(routing.SUITE, routing.SUITE_ID))
    findings.extend(_audit_balance(naming.SUITE, naming.SUITE_ID))
    findings.sort(key=lambda finding: (
        {"error": 0, "warning": 1, "info": 2}.get(finding.severity, 9),
        finding.suite_id,
        finding.task_id or "",
        finding.check,
    ))

    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "splits": list(splits),
        "frozen_holdout_content_included": "frozen_holdout" in splits,
        "contamination_notice": (
            "Development and validation inputs are shown for human contract audit. If validation "
            "items are used to change prompts, skills, examples, thresholds, or expected answers, "
            "those items have been consumed for development and must not remain the clean validation "
            "set. Frozen holdout content is excluded unless explicitly requested."
        ),
        "summary": {
            "routing_items": len(routing_tasks),
            "naming_items": len(naming_tasks),
            "errors": sum(1 for finding in findings if finding.severity == "error"),
            "warnings": sum(1 for finding in findings if finding.severity == "warning"),
            "infos": sum(1 for finding in findings if finding.severity == "info"),
            "suite_versions": {
                routing.SUITE_ID: routing.SUITE_VERSION,
                naming.SUITE_ID: naming.SUITE_VERSION,
            },
            "checks_run": sorted({finding.check for finding in findings}),
        },
        "prompts": {
            routing.SUITE_ID: _prompt_snapshot(routing_prompt),
            naming.SUITE_ID: _prompt_snapshot(naming_prompt),
        },
        "findings": [finding.as_dict() for finding in findings],
        "routing_items": [_routing_item(task) for task in routing_tasks],
        "naming_items": [_naming_item(task) for task in naming_tasks],
    }


def to_markdown(audit: dict[str, Any]) -> str:
    """Render the audit without hiding the actual model/task language."""
    summary = audit["summary"]
    lines = [
        "# Gate-2 evaluator contract audit",
        "",
        f"Splits reviewed: {', '.join(audit['splits'])}",
        "",
        f"> {audit['contamination_notice']}",
        "",
        "## Summary",
        "",
        f"- Routing items rendered: {summary['routing_items']}",
        f"- Semantic-naming items rendered: {summary['naming_items']}",
        f"- Contract errors detected: {summary['errors']}",
        f"- Contract warnings detected: {summary['warnings']}",
        "",
        "## Deterministic findings",
        "",
    ]
    if audit["findings"]:
        lines += ["| Severity | Check | Suite | Task | Detail |", "|---|---|---|---|---|"]
        for finding in audit["findings"]:
            detail = str(finding["detail"]).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {finding['severity']} | {finding['check']} | {finding['suite_id']} | "
                f"{finding['task_id'] or '-'} | {detail} |"
            )
    else:
        lines.append("_No deterministic contract findings for the selected splits._")

    lines += ["", "## Exact model-facing prompt definitions", ""]
    for suite_id, prompt in audit["prompts"].items():
        lines += [
            f"### {suite_id}",
            "",
            f"Prompt: `{prompt['prompt_id']}` version `{prompt['version']}`",
            "",
            "#### Policy",
            "",
            "```text",
            prompt["policy_template"],
            "```",
            "",
            "#### Role",
            "",
            "```text",
            prompt["role_template"],
            "```",
            "",
            "#### Task template",
            "",
            "```text",
            prompt["task_template"],
            "```",
            "",
            "#### Output schema",
            "",
            "```text",
            prompt["output_schema"],
            "```",
            "",
        ]

    lines += ["## Routing/classification ground truth", ""]
    for item in audit["routing_items"]:
        lines += [
            f"### {item['id']} — {item['scenario']} — {item['split']}",
            "",
            item["description"],
            "",
            f"Expected label: `{item['expected_label']}`",
            "",
        ]

    lines += ["## Semantic-naming ground truth", ""]
    for item in audit["naming_items"]:
        expected = item["expected"]
        lines += [
            f"### {item['id']} — {item['scenario']} — {item['split']}",
            "",
            f"Original filename: `{item['original_filename']}`",
            "",
            "Evidence:",
            "",
            item["evidence_text"],
            "",
            "Expected deterministic properties:",
            "",
            "```json",
            json.dumps(expected, indent=2, sort_keys=True),
            "```",
            "",
        ]

    return "\n".join(lines).rstrip() + "\n"


def write_gate2_audit(*, out_dir: Path, splits: tuple[str, ...] = DEFAULT_SPLITS) -> tuple[Path, Path]:
    audit = generate_gate2_audit(splits=splits)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "gate2-contract-audit.json"
    markdown_path = out_dir / "gate2-contract-audit.md"
    json_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(to_markdown(audit), encoding="utf-8")
    return json_path, markdown_path
