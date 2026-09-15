"""Orchestrates a campaign run: enumerate, skip-done, execute, checkpoint.

No AI agent needs to sit here. `run_campaign()` is ordinary, deterministic
software: it reads a manifest, reads what has already been measured, runs
what is left through a small bounded worker pool, and appends one durable
record per finished attempt. Interrupting the process and re-running the
identical command resumes -- there is no separate "resume mode" to get
wrong, because every run already skips whatever the store says is done
(see `tools/campaign/store.py`'s terminal/retryable split).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tools.campaign.execution import WorkUnit, build_provider, enumerate_work_units, execute_work_unit
from tools.campaign.fingerprint import Candidate
from tools.campaign.manifest import CampaignManifest
from tools.campaign.store import ResultRecord, ResultStore

DEFAULT_STATE_ROOT = Path(".state/campaigns")

# A rough, explicitly-labeled-as-rough per-record size, for the dry-run's
# "estimated disk impact when reasonably knowable" requirement. Real records
# vary with prompt/response length; this is a planning number, not a promise.
_APPROX_BYTES_PER_RECORD = 900


class ManifestDrift(ValueError):
    """The manifest for this campaign_id changed since it was first run.

    A campaign's identity is its fingerprint, not merely its id string
    (this task's own brief: "changing a meaningful input should produce a
    different campaign/configuration identity instead of contaminating
    prior results"). Reusing a campaign_id under a materially different
    manifest is refused rather than silently mixing the two runs' results;
    pick a new `campaign_id` or pass `allow_manifest_change=True` having
    understood that prior rows under the old manifest remain in the same
    file, now describing a different configuration than new rows will.
    """


def state_dir(campaign_id: str, *, root: Path = DEFAULT_STATE_ROOT) -> Path:
    return root / campaign_id


def fingerprint_path(campaign_id: str, *, root: Path = DEFAULT_STATE_ROOT) -> Path:
    return state_dir(campaign_id, root=root) / "manifest_fingerprint.txt"


def result_store_path(campaign_id: str, *, root: Path = DEFAULT_STATE_ROOT) -> Path:
    return state_dir(campaign_id, root=root) / "results.jsonl"


def check_manifest_drift(manifest: CampaignManifest, *, root: Path = DEFAULT_STATE_ROOT,
                         allow_manifest_change: bool = False) -> None:
    fp_path = fingerprint_path(manifest.campaign_id, root=root)
    current = manifest.manifest_fingerprint()
    if not fp_path.exists():
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        fp_path.write_text(current, encoding="utf-8")
        return
    recorded = fp_path.read_text(encoding="utf-8").strip()
    if recorded != current and not allow_manifest_change:
        raise ManifestDrift(
            f"campaign {manifest.campaign_id!r} was previously run with a different manifest "
            f"(recorded fingerprint {recorded[:12]}..., current {current[:12]}...); use a new "
            "campaign_id for a materially different configuration, or pass allow_manifest_change=True"
        )
    if recorded != current:
        fp_path.write_text(current, encoding="utf-8")


@dataclass
class DryRunReport:
    campaign_id: str
    manifest_fingerprint: str
    suites: list[dict[str, Any]]
    candidates: list[str]
    samples_per_item: int
    total_work_units: int
    already_complete: int
    would_run: int
    estimated_new_disk_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id, "manifest_fingerprint": self.manifest_fingerprint,
            "suites": self.suites, "candidates": self.candidates,
            "samples_per_item": self.samples_per_item, "total_work_units": self.total_work_units,
            "already_complete": self.already_complete, "would_run": self.would_run,
            "estimated_new_disk_bytes": self.estimated_new_disk_bytes,
        }


def dry_run(manifest: CampaignManifest, *, root: Path = DEFAULT_STATE_ROOT) -> DryRunReport:
    """Static enumeration only. Never touches a provider or runs inference."""
    units = enumerate_work_units(manifest)
    store = ResultStore(result_store_path(manifest.campaign_id, root=root))
    done = store.completed_work_unit_ids()
    pending = [u for u in units if u.work_unit_id not in done]
    suite_summaries: dict[tuple[str, str], int] = {}
    for unit in units:
        key = (unit.suite_id, unit.split)
        suite_summaries[key] = suite_summaries.get(key, 0) + 1
    return DryRunReport(
        campaign_id=manifest.campaign_id, manifest_fingerprint=manifest.manifest_fingerprint(),
        suites=[{"suite_id": s, "split": sp, "work_units": n} for (s, sp), n in sorted(suite_summaries.items())],
        candidates=[c.name for c in manifest.candidates],
        samples_per_item=manifest.samples_per_item, total_work_units=len(units),
        already_complete=len(units) - len(pending), would_run=len(pending),
        estimated_new_disk_bytes=len(pending) * _APPROX_BYTES_PER_RECORD,
    )


@dataclass
class CandidateAvailability:
    candidate_name: str
    available: bool
    reason: str | None = None


def check_candidate_availability(manifest: CampaignManifest) -> list[CandidateAvailability]:
    """Detect installed candidates without downloading anything.

    A provider's `available()` is a loopback reachability/tag check
    (`tools.models.ollama_local.OllamaLocalClient.available`), never a pull
    -- model acquisition is a separate, explicit concern this runner never
    performs itself (this task's "model installation boundary" requirement).
    """
    results: list[CandidateAvailability] = []
    for candidate in manifest.candidates:
        try:
            provider = build_provider(candidate)
            ok = provider.available()
            results.append(CandidateAvailability(candidate.name, ok,
                            None if ok else f"{candidate.model} not reachable at {candidate.endpoint}"))
        except Exception as exc:  # noqa: BLE001
            results.append(CandidateAvailability(candidate.name, False, f"{type(exc).__name__}: {exc}"))
    return results


@dataclass
class CampaignRunSummary:
    campaign_id: str
    total_work_units: int
    skipped_already_done: int
    executed: int
    status_counts: dict[str, int] = field(default_factory=dict)
    skipped_candidates: list[str] = field(default_factory=list)


def _smoke_trim(units: list[WorkUnit], *, limit_per_pair: int = 1) -> list[WorkUnit]:
    seen: dict[tuple[str, str], int] = {}
    trimmed: list[WorkUnit] = []
    for unit in units:
        key = (unit.candidate.name, unit.suite_id)
        count = seen.get(key, 0)
        if unit.sample_index != 0:
            continue  # smoke mode never needs more than one sample per item
        if count >= limit_per_pair:
            continue
        seen[key] = count + 1
        trimmed.append(unit)
    return trimmed


def run_campaign(
    manifest: CampaignManifest, *,
    root: Path = DEFAULT_STATE_ROOT,
    resume: bool = True,
    smoke: bool = False,
    smoke_items_per_pair: int = 1,
    strict_candidates: bool = False,
    allow_manifest_change: bool = False,
    max_workers: int | None = None,
    on_result: Callable[[ResultRecord], None] | None = None,
) -> CampaignRunSummary:
    check_manifest_drift(manifest, root=root, allow_manifest_change=allow_manifest_change)
    store = ResultStore(result_store_path(manifest.campaign_id, root=root))

    availability = check_candidate_availability(manifest)
    unavailable = {a.candidate_name for a in availability if not a.available}
    if unavailable and strict_candidates:
        reasons = "; ".join(f"{a.candidate_name}: {a.reason}" for a in availability if not a.available)
        raise RuntimeError(f"candidate(s) unavailable under --strict: {reasons}")

    units = enumerate_work_units(manifest)
    units = [u for u in units if u.candidate.name not in unavailable]
    if smoke:
        units = _smoke_trim(units, limit_per_pair=smoke_items_per_pair)

    done_ids = store.completed_work_unit_ids() if resume else set()
    pending = [u for u in units if u.work_unit_id not in done_ids]

    providers: dict[str, Any] = {}
    for candidate in manifest.candidates:
        if candidate.name not in unavailable:
            providers[candidate.name] = build_provider(candidate)

    status_counts: dict[str, int] = {}
    workers = max_workers or manifest.concurrency or 1
    workers = max(1, workers)

    def _run_one(unit: WorkUnit) -> ResultRecord:
        return execute_work_unit(unit, providers[unit.candidate.name])

    if workers == 1:
        for unit in pending:
            record = _run_one(unit)
            store.append(record)
            status_counts[record.status] = status_counts.get(record.status, 0) + 1
            if on_result:
                on_result(record)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_one, unit): unit for unit in pending}
            for future in as_completed(futures):
                record = future.result()
                store.append(record)
                status_counts[record.status] = status_counts.get(record.status, 0) + 1
                if on_result:
                    on_result(record)

    return CampaignRunSummary(
        campaign_id=manifest.campaign_id, total_work_units=len(units),
        skipped_already_done=len(units) - len(pending), executed=len(pending),
        status_counts=status_counts, skipped_candidates=sorted(unavailable),
    )
