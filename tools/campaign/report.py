"""Regenerate a campaign report from raw records. No agent session required.

`generate_report()` reads only the append-only result store and the
manifest snapshot fingerprint already on disk; it never re-runs inference
and never needs anything this conversation computed in memory. Running
`./scripts/angel-eval report <campaign-id>` next month, on a machine that
never saw this session, produces the same report from the same rows.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.campaign.manifest import CampaignManifest
from tools.campaign.runner import DEFAULT_STATE_ROOT, result_store_path
from tools.campaign.statistics import wilson_interval
from tools.campaign.store import ResultRecord, ResultStore, TERMINAL_STATUSES

REPORT_SCHEMA_VERSION = 2


def _git_info() -> dict[str, Any]:
    def _run(*args: str) -> str | None:
        try:
            return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL, text=True).strip()
        except Exception:  # noqa: BLE001 -- reproducibility metadata is best-effort, never fatal
            return None

    sha = _run("rev-parse", "HEAD")
    dirty = _run("status", "--porcelain")
    return {"sha": sha, "dirty": bool(dirty) if dirty is not None else None}


def _reproducibility_metadata(manifest: CampaignManifest | None) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": _git_info(),
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "python_version": sys.version.split()[0],
        "manifest_fingerprint": manifest.manifest_fingerprint() if manifest else None,
    }


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[index]


@dataclass
class CellQuality:
    suite_id: str
    split: str
    candidate_name: str
    attempts: int
    verified_pass: int
    verified_fail: int
    invalid_output: int
    truncated: int
    unverified: int
    timeout: int
    provider_unavailable: int
    provider_error: int
    sandbox_error: int
    verifier_error: int
    cancelled: int

    @property
    def measured(self) -> int:
        return self.verified_pass + self.verified_fail

    def as_dict(self) -> dict[str, Any]:
        interval = wilson_interval(self.verified_pass, self.measured) if self.measured else None
        infra_failures = (self.timeout + self.provider_unavailable + self.provider_error
                          + self.sandbox_error + self.verifier_error + self.cancelled)
        terminal_attempts = self.measured + self.invalid_output + self.truncated + self.unverified
        return {
            "suite_id": self.suite_id, "split": self.split, "candidate": self.candidate_name,
            "attempts": self.attempts, "measured_samples": self.measured,
            "verified_pass": self.verified_pass, "verified_fail": self.verified_fail,
            "invalid_output": self.invalid_output, "truncated": self.truncated,
            "unverified": self.unverified,
            "infrastructure_failures": infra_failures,
            "infrastructure_failure_breakdown": {
                "timeout": self.timeout, "provider_unavailable": self.provider_unavailable,
                "provider_error": self.provider_error, "sandbox_error": self.sandbox_error,
                "verifier_error": self.verifier_error, "cancelled": self.cancelled,
            },
            "verified_pass_rate": interval.point_estimate if interval else None,
            "verified_pass_rate_wilson_95ci": [interval.lower, interval.upper] if interval else None,
            "end_to_end_success_rate": self.verified_pass / self.attempts if self.attempts else None,
            "completion_rate": terminal_attempts / self.attempts if self.attempts else None,
            "timeout_rate": self.timeout / self.attempts if self.attempts else None,
            "truncation_rate": self.truncated / self.attempts if self.attempts else None,
            "infrastructure_failure_rate": infra_failures / self.attempts if self.attempts else None,
        }


def _item_consistency(records: list[ResultRecord]) -> dict[str, Any]:
    """Measure repeatability inside one candidate/suite cell only.

    The old report grouped all candidates in a suite together, so one model
    passing an item and another model failing it looked like within-model
    instability. This function must only receive records for one candidate.
    It reports measured pass/fail flips separately from broader terminal-status
    changes such as pass -> truncated.
    """
    measured_by_item: dict[str, list[bool]] = defaultdict(list)
    terminal_by_item: dict[str, list[str]] = defaultdict(list)
    for record in records:
        if record.status == "verified_pass":
            measured_by_item[record.item_id].append(True)
        elif record.status == "verified_fail":
            measured_by_item[record.item_id].append(False)
        if record.status in TERMINAL_STATUSES:
            terminal_by_item[record.item_id].append(record.status)

    pass_fail_inconsistent = {
        item_id: outcomes for item_id, outcomes in measured_by_item.items()
        if len(outcomes) > 1 and len(set(outcomes)) > 1
    }
    terminal_inconsistent = {
        item_id: outcomes for item_id, outcomes in terminal_by_item.items()
        if len(outcomes) > 1 and len(set(outcomes)) > 1
    }
    return {
        "items_with_multiple_measured_samples": sum(1 for outcomes in measured_by_item.values() if len(outcomes) > 1),
        "items_with_inconsistent_pass_fail": len(pass_fail_inconsistent),
        "inconsistent_pass_fail_item_ids": sorted(pass_fail_inconsistent),
        "items_with_multiple_terminal_samples": sum(1 for outcomes in terminal_by_item.values() if len(outcomes) > 1),
        "items_with_inconsistent_terminal_outcomes": len(terminal_inconsistent),
        "inconsistent_terminal_item_ids": sorted(terminal_inconsistent),
    }


def _cross_model_disagreement(records: list[ResultRecord]) -> dict[str, Any]:
    """Report cross-model disagreement as a distinct concept from instability.

    For each item/candidate pair, use the majority of measured pass/fail
    samples. Ties and candidates with no measured samples are indeterminate and
    excluded. An item is cross-model-disputed only when at least one candidate
    has a pass majority and another has a fail majority.
    """
    by_item_candidate: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        if record.status == "verified_pass":
            by_item_candidate[record.item_id][record.candidate_name].append(True)
        elif record.status == "verified_fail":
            by_item_candidate[record.item_id][record.candidate_name].append(False)

    disputed: dict[str, dict[str, str]] = {}
    comparable = 0
    for item_id, candidate_outcomes in by_item_candidate.items():
        verdicts: dict[str, str] = {}
        for candidate, outcomes in candidate_outcomes.items():
            passes = sum(outcomes)
            failures = len(outcomes) - passes
            if passes > failures:
                verdicts[candidate] = "pass-majority"
            elif failures > passes:
                verdicts[candidate] = "fail-majority"
        if len(verdicts) < 2:
            continue
        comparable += 1
        if len(set(verdicts.values())) > 1:
            disputed[item_id] = verdicts

    return {
        "items_comparable_across_models": comparable,
        "items_with_cross_model_disagreement": len(disputed),
        "cross_model_disagreement_item_ids": sorted(disputed),
        "candidate_majority_verdicts_for_disputed_items": disputed,
    }


def _performance(records: list[ResultRecord]) -> dict[str, Any]:
    walls = [r.wall_seconds for r in records if r.wall_seconds is not None]
    input_tokens = [r.input_tokens for r in records if r.input_tokens is not None]
    output_tokens = [r.output_tokens for r in records if r.output_tokens is not None]
    return {
        "median_latency_seconds": _percentile(walls, 0.5),
        "p95_latency_seconds": _percentile(walls, 0.95),
        "mean_input_tokens": (sum(input_tokens) / len(input_tokens)) if input_tokens else None,
        "mean_output_tokens": (sum(output_tokens) / len(output_tokens)) if output_tokens else None,
        "n_latency_samples": len(walls),
    }


def _cost_and_resources(records: list[ResultRecord]) -> dict[str, Any]:
    return {
        "api_cost_usd": 0.0,
        "cost_basis": "local inference only; no metered provider in this run",
        "resource_use_note": "non-zero: local compute/GPU/CPU time was spent even though "
                             "no invoice was generated -- see performance.latency for the "
                             "only resource proxy this runner measures directly",
        "energy_estimate": None,
    }


def _reliability(cells: list[CellQuality]) -> dict[str, Any]:
    total_attempts = sum(c.attempts for c in cells)
    total_infra = sum(c.as_dict()["infrastructure_failures"] for c in cells)
    total_verifier_errors = sum(c.verifier_error for c in cells)
    return {
        "provider_failure_rate": (
            sum(c.timeout + c.provider_unavailable + c.provider_error for c in cells) / total_attempts
        ) if total_attempts else None,
        "verifier_failure_rate": (total_verifier_errors / total_attempts) if total_attempts else None,
        "overall_infrastructure_failure_rate": (total_infra / total_attempts) if total_attempts else None,
    }


def _threshold_check(manifest: CampaignManifest, cells: list[CellQuality]) -> list[dict[str, Any]]:
    results = []
    thresholds = {s.suite_id: s.threshold for s in manifest.suites}
    for cell in cells:
        threshold = thresholds.get(cell.suite_id)
        if threshold is None or cell.measured == 0:
            continue
        interval = wilson_interval(cell.verified_pass, cell.measured)
        cell_dict = cell.as_dict()
        infra_rate = cell_dict["infrastructure_failure_rate"] or 0.0
        meets_rate = interval.point_estimate >= threshold.min_pass_rate
        meets_confidence = interval.lower >= threshold.min_confidence if threshold.min_confidence else True
        meets_infra = infra_rate <= threshold.max_provider_error_rate
        meets_timeout = (cell.timeout / cell.attempts if cell.attempts else 0.0) <= threshold.max_timeout_rate
        results.append({
            "suite_id": cell.suite_id, "candidate": cell.candidate_name,
            "measured_pass_rate": interval.point_estimate,
            "end_to_end_success_rate": cell_dict["end_to_end_success_rate"],
            "truncation_rate": cell_dict["truncation_rate"],
            "wilson_95ci_lower": interval.lower,
            "threshold": {
                "min_pass_rate": threshold.min_pass_rate, "min_confidence": threshold.min_confidence,
                "max_timeout_rate": threshold.max_timeout_rate,
                "max_provider_error_rate": threshold.max_provider_error_rate,
            },
            "satisfies_threshold": bool(meets_rate and meets_confidence and meets_infra and meets_timeout),
        })
    return results


@dataclass
class CampaignReport:
    campaign_id: str
    reproducibility: dict[str, Any]
    cells: list[dict[str, Any]]
    item_consistency: dict[str, dict[str, dict[str, Any]]]
    cross_model_disagreement: dict[str, dict[str, Any]]
    performance: dict[str, dict[str, Any]]
    cost_and_resources: dict[str, Any]
    reliability: dict[str, Any]
    qualification: list[dict[str, Any]]
    total_records: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION, "campaign_id": self.campaign_id,
            "reproducibility": self.reproducibility, "total_records": self.total_records,
            "quality_by_suite_candidate": self.cells,
            "item_consistency_by_suite_candidate": self.item_consistency,
            "cross_model_disagreement_by_suite": self.cross_model_disagreement,
            "performance_by_suite": self.performance,
            "cost_and_resources": self.cost_and_resources,
            "reliability": self.reliability,
            "qualification": self.qualification,
        }


def generate_report(campaign_id: str, *, root: Path = DEFAULT_STATE_ROOT,
                    manifest: CampaignManifest | None = None) -> CampaignReport:
    store = ResultStore(result_store_path(campaign_id, root=root))
    records = store.read_all()

    by_cell: dict[tuple[str, str, str], list[ResultRecord]] = defaultdict(list)
    for record in records:
        by_cell[(record.suite_id, record.split, record.candidate_name)].append(record)

    cells: list[CellQuality] = []
    for (suite_id, split, candidate_name), group in sorted(by_cell.items()):
        counts = {status: 0 for status in (
            "verified_pass", "verified_fail", "invalid_output", "truncated", "unverified",
            "timeout", "provider_unavailable", "provider_error", "sandbox_error",
            "verifier_error", "cancelled",
        )}
        for record in group:
            counts[record.status] += 1
        cells.append(CellQuality(suite_id=suite_id, split=split, candidate_name=candidate_name,
                                 attempts=len(group), **counts))

    by_suite_candidate: dict[str, dict[str, list[ResultRecord]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        by_suite_candidate[record.suite_id][record.candidate_name].append(record)
    item_consistency = {
        suite_id: {
            candidate: _item_consistency(candidate_records)
            for candidate, candidate_records in sorted(candidates.items())
        }
        for suite_id, candidates in sorted(by_suite_candidate.items())
    }

    cross_model_disagreement = {
        suite_id: _cross_model_disagreement([r for r in records if r.suite_id == suite_id])
        for suite_id in sorted({r.suite_id for r in records})
    }
    performance = {
        suite_id: _performance([r for r in records if r.suite_id == suite_id])
        for suite_id in sorted({r.suite_id for r in records})
    }

    qualification = _threshold_check(manifest, cells) if manifest else []

    return CampaignReport(
        campaign_id=campaign_id,
        reproducibility=_reproducibility_metadata(manifest),
        cells=[c.as_dict() for c in cells],
        item_consistency=item_consistency,
        cross_model_disagreement=cross_model_disagreement,
        performance=performance,
        cost_and_resources=_cost_and_resources(records),
        reliability=_reliability(cells),
        qualification=qualification,
        total_records=len(records),
    )


def to_markdown(report: CampaignReport) -> str:
    lines = [f"# Campaign report: {report.campaign_id}", ""]
    repro = report.reproducibility
    lines += [
        "## Reproducibility", "",
        f"- Generated: {repro['generated_at']}",
        f"- Git SHA: {repro['git'].get('sha') or 'unknown'} "
        f"({'dirty' if repro['git'].get('dirty') else 'clean' if repro['git'].get('dirty') is not None else 'unknown'})",
        f"- Host: {repro['host']} ({repro['os']})",
        f"- Python: {repro['python_version']}",
        f"- Manifest fingerprint: {repro.get('manifest_fingerprint') or 'unknown'}",
        f"- Total raw records: {report.total_records}",
        "",
        "## Quality by suite / candidate", "",
        "| Suite | Split | Candidate | Attempts | N measured | Measured pass rate | End-to-end success | 95% CI | Infra failure | Truncation |",
        "|---|---|---|---:|---:|---:|---:|---|---:|---:|",
    ]
    for cell in report.cells:
        ci = cell["verified_pass_rate_wilson_95ci"]
        ci_text = f"[{ci[0]:.3f}, {ci[1]:.3f}]" if ci else "n/a"
        rate = cell["verified_pass_rate"]
        rate_text = f"{rate:.3f}" if rate is not None else "n/a"
        e2e = cell["end_to_end_success_rate"]
        e2e_text = f"{e2e:.3f}" if e2e is not None else "n/a"
        infra_text = f"{cell['infrastructure_failure_rate']:.3f}" if cell["infrastructure_failure_rate"] is not None else "n/a"
        trunc_text = f"{cell['truncation_rate']:.3f}" if cell["truncation_rate"] is not None else "n/a"
        lines.append(f"| {cell['suite_id']} | {cell['split']} | {cell['candidate']} | {cell['attempts']} | "
                     f"{cell['measured_samples']} | {rate_text} | {e2e_text} | {ci_text} | {infra_text} | {trunc_text} |")

    lines += ["", "## Qualification thresholds", ""]
    if report.qualification:
        lines += ["| Suite | Candidate | Measured | End-to-end | Truncation | Wilson lower | Threshold | Satisfies? |",
                  "|---|---|---:|---:|---:|---:|---:|---|"]
        for q in report.qualification:
            lines.append(f"| {q['suite_id']} | {q['candidate']} | {q['measured_pass_rate']:.3f} | "
                         f"{q['end_to_end_success_rate']:.3f} | {q['truncation_rate']:.3f} | "
                         f"{q['wilson_95ci_lower']:.3f} | {q['threshold']['min_pass_rate']:.3f} | "
                         f"{'yes' if q['satisfies_threshold'] else 'no'} |")
    else:
        lines.append("_No threshold configured, or no measurements yet._")

    lines += ["", "## Reliability", ""]
    rel = report.reliability
    lines.append(f"- Provider failure rate: {rel['provider_failure_rate']}")
    lines.append(f"- Verifier failure rate: {rel['verifier_failure_rate']}")
    lines.append(f"- Overall infrastructure failure rate: {rel['overall_infrastructure_failure_rate']}")

    lines += ["", "## Cost and resources", ""]
    cost = report.cost_and_resources
    lines.append(f"- API cost: ${cost['api_cost_usd']:.2f} ({cost['cost_basis']})")
    lines.append(f"- {cost['resource_use_note']}")

    lines += ["", "## Per-item consistency (within each candidate)", ""]
    for suite_id, candidates in report.item_consistency.items():
        for candidate, consistency in candidates.items():
            lines.append(
                f"- {suite_id} / {candidate}: "
                f"{consistency['items_with_inconsistent_pass_fail']} of "
                f"{consistency['items_with_multiple_measured_samples']} multiply-measured items "
                "flipped between verified pass and verified fail; "
                f"{consistency['items_with_inconsistent_terminal_outcomes']} of "
                f"{consistency['items_with_multiple_terminal_samples']} multiply-terminal items "
                "changed terminal outcome (including truncation/invalid output)."
            )

    lines += ["", "## Cross-model disagreement (not within-model instability)", ""]
    for suite_id, disagreement in report.cross_model_disagreement.items():
        lines.append(
            f"- {suite_id}: {disagreement['items_with_cross_model_disagreement']} of "
            f"{disagreement['items_comparable_across_models']} comparable items had different "
            "candidate-majority pass/fail verdicts."
        )

    return "\n".join(lines) + "\n"


def write_report(report: CampaignReport, *, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    md_path = out_dir / "report.md"
    json_path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return json_path, md_path
