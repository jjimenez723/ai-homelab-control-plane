"""Resource-aware, resumable campaign orchestration.

A real run stages the exact manifest-approved candidates, resolves their local
runtime identity, then schedules candidate-sized batches. Validation may run
several small models concurrently when a conservative unified-memory budget
allows it; frozen-holdout campaigns are always serialized.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from tools.campaign.execution import WorkUnit, build_provider, enumerate_work_units, execute_work_unit
from tools.campaign.fingerprint import Candidate
from tools.campaign.manifest import CampaignManifest
from tools.campaign.store import ResultRecord, ResultStore

DEFAULT_STATE_ROOT = Path(".state/campaigns")
_APPROX_BYTES_PER_RECORD = 900
_GIB = 1024 ** 3
_MIB = 1024 ** 2
_MEMORY_FRACTION = 0.60


class ManifestDrift(ValueError):
    """The source manifest or resolved runtime identity changed under one campaign id."""


def state_dir(campaign_id: str, *, root: Path = DEFAULT_STATE_ROOT) -> Path:
    return root / campaign_id


def fingerprint_path(campaign_id: str, *, root: Path = DEFAULT_STATE_ROOT) -> Path:
    return state_dir(campaign_id, root=root) / "manifest_fingerprint.txt"


def resolved_candidates_path(campaign_id: str, *, root: Path = DEFAULT_STATE_ROOT) -> Path:
    return state_dir(campaign_id, root=root) / "resolved_candidates.json"


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
    """Static enumeration only. Never touches a provider, pulls, loads, or infers."""
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


@dataclass
class StagedCandidate:
    source: Candidate
    resolved: Candidate
    provider: Any
    model_size_bytes: int
    estimated_resident_bytes: int
    pulled: bool


@dataclass
class CandidateBatchOutcome:
    candidate_name: str
    executed: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    unload_error: str | None = None


@dataclass
class CampaignRunSummary:
    campaign_id: str
    total_work_units: int
    skipped_already_done: int
    executed: int
    status_counts: dict[str, int] = field(default_factory=dict)
    skipped_candidates: list[str] = field(default_factory=list)
    pulled_candidates: list[str] = field(default_factory=list)
    candidate_failures: dict[str, str] = field(default_factory=dict)
    memory_budget_bytes: int | None = None
    max_parallel_candidates: int = 1


def _estimate_resident_bytes(model_size_bytes: int) -> int:
    if model_size_bytes <= 0:
        return _GIB
    return int(model_size_bytes * 1.35) + 512 * _MIB


def _physical_memory_bytes() -> int | None:
    if platform.system() == "Darwin":
        try:
            return int(subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], stderr=subprocess.DEVNULL, text=True).strip())
        except Exception:  # noqa: BLE001
            pass
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            return pages * page_size
    except (AttributeError, OSError, ValueError):
        pass
    return None


def _quant_key(value: str | None) -> str | None:
    return value.upper().replace("-", "_") if value else None


def _stage_candidate(candidate: Candidate, *, auto_pull: bool) -> tuple[StagedCandidate | None, CandidateAvailability]:
    try:
        provider = build_provider(candidate)
        pulled = False
        record = None
        ensure = getattr(provider, "ensure_model", None)
        if callable(ensure):
            record, pulled = ensure(auto_pull=auto_pull)
            if record is None:
                return None, CandidateAvailability(
                    candidate.name, False, f"{candidate.model} is not installed and auto-pull is disabled")
        else:
            if not provider.available():
                return None, CandidateAvailability(
                    candidate.name, False, f"{candidate.model} is unavailable at {candidate.endpoint}")

        digest = getattr(record, "digest", None) if record is not None else candidate.model_digest
        quantization = getattr(record, "quantization", None) if record is not None else candidate.quantization
        size_bytes = int(getattr(record, "size_bytes", 0) or 0)

        if candidate.model_digest and digest and candidate.model_digest != digest:
            return None, CandidateAvailability(
                candidate.name, False,
                f"digest mismatch for {candidate.model}: manifest={candidate.model_digest}, local={digest}")
        if candidate.quantization and quantization and _quant_key(candidate.quantization) != _quant_key(quantization):
            return None, CandidateAvailability(
                candidate.name, False,
                f"quantization mismatch for {candidate.model}: manifest={candidate.quantization}, local={quantization}")

        resolved = replace(
            candidate,
            model_digest=digest or candidate.model_digest,
            quantization=quantization or candidate.quantization,
        )
        staged = StagedCandidate(
            source=candidate, resolved=resolved, provider=provider,
            model_size_bytes=size_bytes,
            estimated_resident_bytes=_estimate_resident_bytes(size_bytes),
            pulled=pulled,
        )
        return staged, CandidateAvailability(candidate.name, True)
    except Exception as exc:  # noqa: BLE001
        return None, CandidateAvailability(candidate.name, False, f"{type(exc).__name__}: {exc}")


def _stage_candidates(manifest: CampaignManifest, *, auto_pull: bool) -> tuple[list[StagedCandidate], list[CandidateAvailability]]:
    staged: list[StagedCandidate] = []
    availability: list[CandidateAvailability] = []
    for candidate in manifest.candidates:
        item, status = _stage_candidate(candidate, auto_pull=auto_pull)
        availability.append(status)
        if item is not None:
            staged.append(item)
    return staged, availability


def check_candidate_availability(manifest: CampaignManifest) -> list[CandidateAvailability]:
    """Check exact installed candidates without pulling anything."""
    _staged, availability = _stage_candidates(manifest, auto_pull=False)
    return availability


def _record_resolved_candidates(manifest: CampaignManifest, staged: list[StagedCandidate], *,
                                root: Path) -> None:
    path = resolved_candidates_path(manifest.campaign_id, root=root)
    existing: dict[str, Any] = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ManifestDrift(f"{path}: resolved candidate state is not an object")
        existing = raw

    changed = False
    for item in staged:
        identity = {
            "model": item.resolved.model,
            "model_digest": item.resolved.model_digest,
            "quantization": item.resolved.quantization,
            "runtime": item.resolved.runtime,
            "model_size_bytes": item.model_size_bytes,
        }
        prior = existing.get(item.resolved.name)
        if prior is not None:
            comparable_prior = {
                key: prior.get(key) for key in ("model", "model_digest", "quantization", "runtime")
            }
            comparable_now = {
                key: identity.get(key) for key in ("model", "model_digest", "quantization", "runtime")
            }
            if comparable_prior != comparable_now:
                raise ManifestDrift(
                    f"campaign {manifest.campaign_id!r} candidate {item.resolved.name!r} "
                    f"resolved to a different runtime identity: was {comparable_prior}, now {comparable_now}; "
                    "use a new campaign_id rather than mixing model weights"
                )
        if prior != identity:
            existing[item.resolved.name] = identity
            changed = True

    if changed or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def _reset_candidate_residency(staged: list[StagedCandidate]) -> None:
    """Unload only campaign-owned candidate tags to establish a clean baseline."""
    for item in staged:
        is_loaded = getattr(item.provider, "is_loaded", None)
        unload = getattr(item.provider, "unload", None)
        if callable(is_loaded) and callable(unload):
            try:
                if is_loaded():
                    unload()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"could not establish clean residency for {item.resolved.name}: {type(exc).__name__}: {exc}"
                ) from exc


def _external_resident_bytes(staged: list[StagedCandidate]) -> int:
    if not staged:
        return 0
    running_fn = next(
        (getattr(item.provider, "running_models", None) for item in staged
         if callable(getattr(item.provider, "running_models", None))),
        None,
    )
    if running_fn is None:
        return 0
    try:
        running = running_fn()
    except Exception:  # noqa: BLE001 -- inability to inspect means do not subtract guessed bytes
        return 0
    candidate_models = {item.resolved.model for item in staged}
    return sum(
        int(getattr(record, "size_bytes", 0) or 0)
        for record in running
        if getattr(record, "name", None) not in candidate_models
    )


def _smoke_trim(units: list[WorkUnit], *, limit_per_pair: int = 1) -> list[WorkUnit]:
    seen: dict[tuple[str, str], int] = {}
    trimmed: list[WorkUnit] = []
    for unit in units:
        key = (unit.candidate.name, unit.suite_id)
        count = seen.get(key, 0)
        if unit.sample_index != 0:
            continue
        if count >= limit_per_pair:
            continue
        seen[key] = count + 1
        trimmed.append(unit)
    return trimmed


def _run_candidate_batch(item: StagedCandidate, units: list[WorkUnit], store: ResultStore,
                         on_result: Callable[[ResultRecord], None] | None) -> CandidateBatchOutcome:
    outcome = CandidateBatchOutcome(candidate_name=item.resolved.name)
    preload = getattr(item.provider, "preload", None)
    unload = getattr(item.provider, "unload", None)

    try:
        if callable(preload):
            preload()
    except Exception as exc:  # noqa: BLE001
        try:
            if callable(unload):
                unload()
        except Exception:
            pass
        outcome.error = f"load failed: {type(exc).__name__}: {exc}"
        return outcome

    try:
        for unit in units:
            record = execute_work_unit(unit, item.provider)
            store.append(record)
            outcome.executed += 1
            outcome.status_counts[record.status] = outcome.status_counts.get(record.status, 0) + 1
            if on_result:
                on_result(record)
    finally:
        if callable(unload):
            try:
                unload()
            except Exception as exc:  # noqa: BLE001
                outcome.unload_error = f"unload failed: {type(exc).__name__}: {exc}"
    return outcome


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
    auto_pull: bool = True,
) -> CampaignRunSummary:
    # Refuse source-manifest drift before a changed manifest can trigger new downloads.
    check_manifest_drift(manifest, root=root, allow_manifest_change=allow_manifest_change)
    store = ResultStore(result_store_path(manifest.campaign_id, root=root))

    staged, availability = _stage_candidates(manifest, auto_pull=auto_pull)
    unavailable = {a.candidate_name for a in availability if not a.available}
    if unavailable and strict_candidates:
        reasons = "; ".join(f"{a.candidate_name}: {a.reason}" for a in availability if not a.available)
        raise RuntimeError(f"candidate(s) unavailable under --strict: {reasons}")

    _record_resolved_candidates(manifest, staged, root=root)
    _reset_candidate_residency(staged)

    resolved_candidates = tuple(item.resolved for item in staged)
    units = enumerate_work_units(manifest, candidates=resolved_candidates)
    if smoke:
        units = _smoke_trim(units, limit_per_pair=smoke_items_per_pair)

    done_ids = store.completed_work_unit_ids() if resume else set()
    pending = [unit for unit in units if unit.work_unit_id not in done_ids]
    by_candidate: dict[str, list[WorkUnit]] = {}
    for unit in pending:
        by_candidate.setdefault(unit.candidate.name, []).append(unit)

    total_memory = _physical_memory_bytes()
    external_resident = _external_resident_bytes(staged)
    memory_budget: int | None = None
    if total_memory is not None:
        memory_budget = max(_GIB, int(total_memory * _MEMORY_FRACTION) - external_resident)

    requested_parallel = max(1, int(max_workers or manifest.concurrency or 1))
    is_holdout = any(spec.split == "frozen_holdout" for spec in manifest.suites)
    max_parallel = 1 if is_holdout or total_memory is None else requested_parallel

    queue = [
        item for item in staged
        if by_candidate.get(item.resolved.name)
    ]
    queue.sort(key=lambda item: item.estimated_resident_bytes)

    status_counts: dict[str, int] = {}
    candidate_failures: dict[str, str] = {}
    skipped_candidates = set(unavailable)
    executed = 0
    used_memory = 0

    def _fits(item: StagedCandidate) -> bool:
        return memory_budget is None or used_memory + item.estimated_resident_bytes <= memory_budget

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        active: dict[Any, StagedCandidate] = {}
        while queue or active:
            while queue and len(active) < max_parallel:
                fit_index = next((i for i, item in enumerate(queue) if _fits(item)), None)
                if fit_index is None:
                    if active:
                        break
                    # A single model may exceed the conservative 60% planning
                    # budget. Run it alone rather than deadlocking the campaign;
                    # Ollama still owns the actual allocation and may fail cleanly.
                    fit_index = 0
                item = queue.pop(fit_index)
                used_memory += item.estimated_resident_bytes
                future = pool.submit(
                    _run_candidate_batch, item, by_candidate[item.resolved.name], store, on_result)
                active[future] = item

            if not active:
                continue

            finished, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
            for future in finished:
                item = active.pop(future)
                used_memory = max(0, used_memory - item.estimated_resident_bytes)
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"candidate batch {item.resolved.name} crashed after cleanup: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc

                if outcome.unload_error:
                    # Do not admit another model after memory release failed;
                    # that would defeat the admission controller's safety claim.
                    raise RuntimeError(f"{item.resolved.name}: {outcome.unload_error}")

                if outcome.error:
                    candidate_failures[item.resolved.name] = outcome.error
                    skipped_candidates.add(item.resolved.name)
                    if strict_candidates:
                        raise RuntimeError(f"{item.resolved.name}: {outcome.error}")
                    continue

                executed += outcome.executed
                for status, count in outcome.status_counts.items():
                    status_counts[status] = status_counts.get(status, 0) + count

    return CampaignRunSummary(
        campaign_id=manifest.campaign_id,
        total_work_units=len(units),
        skipped_already_done=len(units) - len(pending),
        executed=executed,
        status_counts=status_counts,
        skipped_candidates=sorted(skipped_candidates),
        pulled_candidates=sorted(item.resolved.name for item in staged if item.pulled),
        candidate_failures=candidate_failures,
        memory_budget_bytes=memory_budget,
        max_parallel_candidates=max_parallel,
    )
