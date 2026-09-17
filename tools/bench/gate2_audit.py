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
from tools.bench.suite import SuiteTask
from tools.prompts.library import ROUTING_CLASSIFICATION_LABELS
from tools.prompts.registry import PromptVersion, load_default_registry

AUDIT_SCHEMA_VERSION = 1
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

    findings = _audit_routing_prompt(routing_prompt)
    findings.extend(_audit_naming_contract(naming_tasks, naming_prompt))
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
