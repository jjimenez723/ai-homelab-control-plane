"""Read-only failure analysis for Gate-2 campaign JSONL records.

The original gate2-validation-001 schema did not persist complete raw model
text, so this module is explicit about provenance: schema-v2 rows expose the
actual raw output, while older schema-v1 rows can only recover limited output
from verifier details (for example routing's ``got`` label or a naming
verifier's normalized filename). Nothing here re-runs inference or changes a
verdict.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from tools.campaign.runner import DEFAULT_STATE_ROOT, result_store_path
from tools.campaign.store import ResultRecord, ResultStore

ANALYSIS_SCHEMA_VERSION = 2

# Where a failure category most likely *originates*. A hint for triage, never a
# verdict: the same reason can have more than one cause, and only two of these
# loci can ever be established mechanically (`infrastructure` and `verifier`,
# both of which are recorded statuses rather than inferences).
#
# The distinction this exists to support is the one D-059's audit turns on:
# a benchmark result is only evidence about a model once the contract, the
# budget and the oracle have been ruled out as the cause.
ATTRIBUTION_HINTS: dict[str, str] = {
    # The model understood the job and got the answer wrong.
    "wrong_label": "model",
    "missing_required_concept": "model",
    "forbidden_token_present": "model",
    "unsupported_or_hallucinated_date": "model",
    "insufficient_evidence_grounding": "model",
    "missing_required_date": "model_or_prompt_contract",
    "leakage": "model",
    # The answer was right or unknowable, but the published output contract was
    # not obeyed -- a prompt/format question, not a capability one.
    "format_noncompliant_label": "prompt_contract",
    "label_not_in_closed_set": "prompt_contract",
    "wrong_extension": "prompt_contract",
    "policy_rejected": "prompt_contract_or_model",
    "non_string_output": "prompt_contract",
    # The model never finished answering: a sampling-budget fact.
    "empty_output": "sampling_budget",
    "truncated": "sampling_budget",
    # Recorded, not inferred.
    "verifier_error": "verifier",
    "timeout": "infrastructure",
    "provider_unavailable": "infrastructure",
    "provider_error": "infrastructure",
    "sandbox_error": "infrastructure",
}


def attribution_hint(failure_reason: str) -> str:
    if failure_reason == "pass":
        return "pass"
    return ATTRIBUTION_HINTS.get(failure_reason, "unclassified")


def _failure_reason(record: ResultRecord) -> str:
    if record.status == "verified_pass":
        return "pass"
    failure = record.detail.get("failure") if isinstance(record.detail, dict) else None
    if isinstance(failure, str) and failure:
        return failure
    if record.status == "verified_fail":
        expected = record.detail.get("expected") if isinstance(record.detail, dict) else None
        got = record.detail.get("got") if isinstance(record.detail, dict) else None
        if expected is not None and got is not None and expected != got:
            return "wrong_label"
        return "verifier_reject"
    return record.status


def _recover_output(record: ResultRecord) -> tuple[str | None, str]:
    if record.raw_output is not None:
        return record.raw_output, "raw_output"
    detail = record.detail if isinstance(record.detail, dict) else {}
    got = detail.get("got")
    if isinstance(got, str):
        return got, "verifier_detail.got"
    filename = detail.get("filename")
    if isinstance(filename, str):
        return filename, "verifier_detail.filename_normalized_not_raw"
    return None, "unavailable"


def _record_view(record: ResultRecord) -> dict[str, Any]:
    output, provenance = _recover_output(record)
    return {
        "candidate": record.candidate_name,
        "suite_id": record.suite_id,
        "split": record.split,
        "item_id": record.item_id,
        "scenario": record.scenario,
        "sample_index": record.sample_index,
        "status": record.status,
        "passed": record.passed,
        "failure_reason": _failure_reason(record),
        "attribution_hint": attribution_hint(_failure_reason(record)),
        "output": output,
        "output_provenance": provenance,
        "raw_output_available": record.raw_output is not None,
        "finish_reason": record.finish_reason,
        "detail": record.detail,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "prompt_id": record.prompt_id,
        "prompt_version": record.prompt_version,
        "prompt_sha256": record.prompt_sha256,
    }


def generate_failure_analysis(
    campaign_id: str,
    *,
    root: Path = DEFAULT_STATE_ROOT,
    candidates: Iterable[str] | None = None,
    suites: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Summarize existing campaign records without contacting a model."""
    records = ResultStore(result_store_path(campaign_id, root=root)).read_all()
    candidate_filter = set(candidates or ())
    suite_filter = set(suites or ())
    filtered = [
        record for record in records
        if (not candidate_filter or record.candidate_name in candidate_filter)
        and (not suite_filter or record.suite_id in suite_filter)
    ]

    by_cell: dict[tuple[str, str], list[ResultRecord]] = defaultdict(list)
    for record in filtered:
        by_cell[(record.candidate_name, record.suite_id)].append(record)

    cells: list[dict[str, Any]] = []
    for (candidate, suite_id), group in sorted(by_cell.items()):
        statuses = Counter(record.status for record in group)
        reasons = Counter(_failure_reason(record) for record in group if record.status != "verified_pass")
        attributions = Counter(
            attribution_hint(_failure_reason(record)) for record in group
            if record.status != "verified_pass")
        scenarios = Counter(
            record.scenario for record in group if record.status != "verified_pass")
        thinking = [r.thinking_chars for r in group if r.thinking_chars]
        raw_available = sum(record.raw_output is not None for record in group)
        recoverable = sum(_recover_output(record)[0] is not None for record in group)
        cells.append({
            "candidate": candidate,
            "suite_id": suite_id,
            "attempts": len(group),
            "status_counts": dict(sorted(statuses.items())),
            "failure_reason_counts": dict(sorted(reasons.items())),
            "attribution_hint_counts": dict(sorted(attributions.items())),
            "failures_by_scenario": dict(sorted(scenarios.items())),
            "attempts_with_reasoning_output": len(thinking),
            "max_thinking_chars": max(thinking) if thinking else 0,
            "raw_output_available": raw_available,
            "raw_output_missing": len(group) - raw_available,
            "some_output_recoverable": recoverable,
            "output_unavailable": len(group) - recoverable,
        })

    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "filters": {
            "candidates": sorted(candidate_filter),
            "suites": sorted(suite_filter),
        },
        "total_records_in_store": len(records),
        "records_analyzed": len(filtered),
        "limitations": [
            "Schema-v1 campaign rows did not persist raw model output.",
            "For old routing rows, verifier detail may recover the normalized predicted label.",
            "For some old naming rows, verifier detail may contain a normalized/validated filename; this is not the original raw model text.",
            "This report never re-runs inference and never changes stored verdicts.",
            "attribution_hint is a triage hint, not a verdict: only `verifier` and "
            "`infrastructure` are recorded facts; `model` versus `prompt_contract` is an "
            "inference from the failure category and should be checked against the raw output.",
        ],
        "cells": cells,
        "records": [_record_view(record) for record in filtered],
    }


def to_markdown(analysis: dict[str, Any]) -> str:
    lines = [
        f"# Gate-2 failure analysis: {analysis['campaign_id']}",
        "",
        f"Records analyzed: {analysis['records_analyzed']} of {analysis['total_records_in_store']}",
        "",
        "## Important limitations",
        "",
    ]
    lines.extend(f"- {item}" for item in analysis["limitations"])
    lines += [
        "",
        "## Summary by candidate / suite",
        "",
        "| Candidate | Suite | Attempts | Raw output available | Some output recoverable | Failure reasons |",
        "|---|---|---:|---:|---:|---|",
    ]
    for cell in analysis["cells"]:
        reasons = ", ".join(f"{key}={value}" for key, value in cell["failure_reason_counts"].items()) or "none"
        lines.append(
            f"| {cell['candidate']} | {cell['suite_id']} | {cell['attempts']} | "
            f"{cell['raw_output_available']} | {cell['some_output_recoverable']} | {reasons} |"
        )

    lines += [
        "",
        "## Failure attribution hints",
        "",
        "| Candidate | Suite | Attribution hints | Failing scenarios |",
        "|---|---|---|---|",
    ]
    for cell in analysis["cells"]:
        hints = ", ".join(f"{k}={v}" for k, v in cell.get("attribution_hint_counts", {}).items()) or "none"
        scenarios = ", ".join(f"{k}={v}" for k, v in cell.get("failures_by_scenario", {}).items()) or "none"
        lines.append(f"| {cell['candidate']} | {cell['suite_id']} | {hints} | {scenarios} |")

    lines += ["", "## Attempt-level records", ""]
    for record in analysis["records"]:
        lines += [
            f"### {record['candidate']} — {record['item_id']} — sample {record['sample_index']}",
            "",
            f"- Suite: `{record['suite_id']}`",
            f"- Scenario: `{record['scenario']}`",
            f"- Status: `{record['status']}`",
            f"- Failure category: `{record['failure_reason']}`",
            f"- Output provenance: `{record['output_provenance']}`",
            f"- Finish reason: `{record['finish_reason']}`",
            "",
        ]
        if record["output"] is None:
            lines.append("Output: _unavailable in the stored record_")
        else:
            lines += ["Output:", "", "```text", str(record["output"]), "```"]
        lines += ["", "Verifier detail:", "", "```json", json.dumps(record["detail"], indent=2, sort_keys=True), "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def write_failure_analysis(
    campaign_id: str,
    *,
    root: Path = DEFAULT_STATE_ROOT,
    out_dir: Path,
    candidates: Iterable[str] | None = None,
    suites: Iterable[str] | None = None,
) -> tuple[Path, Path]:
    analysis = generate_failure_analysis(
        campaign_id, root=root, candidates=candidates, suites=suites,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "failure-analysis.json"
    markdown_path = out_dir / "failure-analysis.md"
    json_path.write_text(json.dumps(analysis, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(to_markdown(analysis), encoding="utf-8")
    return json_path, markdown_path
