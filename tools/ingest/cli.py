"""The `organize` CLI (PLAN.md Phase 1A).

    organize desktop | documents | downloads | sd | all [--dry-run | --apply] [--config PATH]

Default (no flag) is `--dry-run`: nothing is ever destructive by accident.
Desktop, Documents, and Downloads only run when this CLI is invoked by hand;
SD cards are auto-detected by UUID but still require explicit `--apply`.
`Organize Files.command` and `scripts/organize` both call this module --
there is exactly one implementation of the ingestion logic.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.ingest.automation import (
    RunLockBusy,
    RunLockLost,
    RunPaths,
    new_run_id,
    now_iso,
    run_lock,
    write_status,
)
from tools.ingest.catalog import (
    STATE_COMMITTED,
    STATE_FAILED_ERROR,
    STATE_FAILED_HASH_MISMATCH,
    STATE_SOURCE_UNLINKED,
    Catalog,
)
from tools.ingest.config import IngestConfig, load_config
from tools.ingest.identity import local_file_id_for
from tools.ingest.naming.evidence import sanitize_perceived_text
from tools.ingest.ledger import (
    DECISION_PROPOSED,
    DECISION_RENAMED,
    NAMING_STATE_REVIEW,
    RETENTION_IGNORE,
    RETENTION_INGEST,
    RETENTION_REVIEW,
    LocalLedger,
)
from tools.ingest.pipeline import PipelineReport, run_pipeline
from tools.ingest.provenance import attach_local_provenance, read_local_provenance
from tools.ingest.status import collect_status, render_doctor, render_status
from tools.ingest.derivatives import maybe_generate_heic_derivative
from tools.ingest.enrichment import enrich_asset
from tools.ingest.exclusions import ExclusionReason
from tools.ingest.git_repos import inspect_git_repo
from tools.ingest.identity import source_ref_id_for
from tools.ingest.repos_report import cmd_repos_report as cmd_repos_report_impl
from tools.ingest.scanner import ScanResult, scan_source
from tools.ingest.sources import (
    ALL_SOURCE_CLASSES,
    MANUAL_STATIC_SOURCES,
    SOURCE_SD,
    TRIAGE_SOURCE_CLASSES,
    detect_sd_sources,
    resolve_volume_for_path,
    static_source_root,
)
from tools.ingest.t7 import ResolvedT7, resolve_t7
from tools.ingest.transaction import PlannedTransfer, TransferOutcome, transfer_file
from tools.ingest.volumes import MountedVolume, VolumeError, VolumeProber, diskutil_volume_prober

EXCLUSION_LABELS = {
    ExclusionReason.GIT_REPO: "Git repositories",
    ExclusionReason.DIR_NAME: "excluded directories (node_modules/venv/etc.)",
    ExclusionReason.BUNDLE: "application/package bundles",
    ExclusionReason.INCOMPLETE_DOWNLOAD: "active downloads",
    ExclusionReason.STILL_CHANGING: "files still changing",
    ExclusionReason.SYMLINK_ESCAPE: "symlinks escaping the source root",
    ExclusionReason.SYMLINK: "symlinks",
    ExclusionReason.KEEP_LOCAL: "keep-local paths",
    ExclusionReason.EXTRA_PATH: "excluded paths",
}

LOCAL_VOLUME_ID = "mac-local"

SOURCE_LABELS = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "sd": "SD",
    "screenshots": "Screenshots",
    "all": "every manual source",
}


@dataclass(frozen=True)
class SourceTarget:
    source_class: str
    label: str
    root: Path
    volume_uuid: str

    @property
    def key(self) -> tuple[str, str]:
        # Display labels aren't guaranteed unique (two SD cards can share a
        # generic label; Desktop/Documents can share a volume_uuid), but the
        # (source_class, volume_uuid) pair always is.
        return (self.source_class, self.volume_uuid)


def gather_targets(config: IngestConfig, requested: list[str], volumes: list[MountedVolume]) -> list[SourceTarget]:
    """Build the list of scan targets from one already-taken volume snapshot,
    so a card ejecting mid-run can't make different call sites disagree about
    what's mounted."""
    targets: list[SourceTarget] = []

    for source_class in MANUAL_STATIC_SOURCES:
        if source_class not in requested:
            continue
        root = static_source_root(config, source_class)
        volume = resolve_volume_for_path(root, volumes)
        volume_uuid = volume.volume_uuid if volume is not None else f"unknown-local:{root}"
        targets.append(SourceTarget(source_class, SOURCE_LABELS[source_class], root, volume_uuid))

    if SOURCE_SD in requested:
        for sd in detect_sd_sources(volumes, config):
            label = f"SD ({sd.volume.label})"
            targets.append(SourceTarget(SOURCE_SD, label, sd.root, sd.volume.volume_uuid))

    return targets


def _requested_classes(target_arg: str) -> list[str]:
    if target_arg == "all":
        return list(ALL_SOURCE_CLASSES)
    return [target_arg]


def _print_exclusions(counts: dict[str, int]) -> None:
    nonzero = {k: v for k, v in counts.items() if v}
    if not nonzero:
        return
    print("\nExcluded:")
    for reason, count in sorted(nonzero.items(), key=lambda kv: -kv[1]):
        label = EXCLUSION_LABELS.get(reason, reason)
        print(f"  {count} {label}")


def _print_git_repos(scans: dict[tuple[str, str], ScanResult], limit: int = 10) -> None:
    all_repos = [repo for scan in scans.values() for repo in scan.git_repos]
    if not all_repos:
        return
    print(f"\nGit repositories found (inventoried, not moved): {len(all_repos)}")
    for repo in all_repos[:limit]:
        print(f"  {repo.path}")
    if len(all_repos) > limit:
        print(f"  ... and {len(all_repos) - limit} more")


def cmd_organize(
    args: argparse.Namespace,
    prober: VolumeProber | None = None,
    config: IngestConfig | None = None,
) -> int:
    """Ingest one or more sources into the T7, under the same lock as `triage`.

    `--apply` transfers files, writes the catalog, and unlinks verified
    sources -- and since `triage --apply` now transfers too, the hourly agent
    and a hand-run `organize all --apply` would otherwise reach the same
    sources, the same catalog, and the same `staging/{asset_id}.partial` paths
    at the same time. This was the unlocked destructive command.

    A dry run deliberately takes no lock: it reads and reports, and making the
    one safe command the one the owner cannot get an answer from while the
    agent is running would be backwards.
    """
    prober = prober or diskutil_volume_prober
    config = config if config is not None else load_config(args.config)
    if not args.apply:
        return _organize(args, prober, config)

    paths = RunPaths.for_state_dir(config.state_dir)
    try:
        # Before the scan, not merely before the first transfer: there is no
        # point hashing sources another run is already moving out from under us.
        with run_lock(paths):
            return _organize(args, prober, config)
    except RunLockBusy as exc:
        # Same stance as `triage`: the work is picked up by whoever holds the
        # lock or by the next run, so a skip is not a failure.
        print(f"Skipped: {exc}")
        return 0
    except RunLockLost as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _organize(args: argparse.Namespace, prober: VolumeProber, config: IngestConfig) -> int:
    requested = _requested_classes(args.target)
    allow_dev_root = bool(getattr(args, "allow_dev_storage_root", False))

    # Probe exactly once: gathering targets and resolving the T7 both need a
    # consistent view of what's mounted, not two independent diskutil calls
    # that could disagree if a card is inserted/ejected mid-invocation.
    volumes = prober()
    snapshot_prober: VolumeProber = lambda: volumes  # noqa: E731
    targets = gather_targets(config, requested, volumes)

    t7_error: str | None = None
    resolved_t7: ResolvedT7 | None = None
    try:
        resolved_t7 = resolve_t7(
            config, snapshot_prober, for_apply=args.apply, allow_storage_root_override=allow_dev_root
        )
    except VolumeError as exc:
        t7_error = str(exc)

    if args.apply and resolved_t7 is None:
        print(f"T7: not connected ({t7_error})")
        print("Refusing to --apply: no destructive action taken.")
        return 1

    print(f"T7: connected ({resolved_t7.root})" if resolved_t7 is not None else f"T7: not connected ({t7_error})")

    repo_root = config.repo_root
    extra_excludes = (repo_root,) + ((resolved_t7.root,) if resolved_t7 is not None else ())

    scans: dict[tuple[str, str], ScanResult] = {}
    for target in targets:
        scan = scan_source(target.root, config=config, extra_exclude_paths=extra_excludes)
        scans[target.key] = scan

    for source_class in MANUAL_STATIC_SOURCES:
        if source_class not in requested:
            continue
        matching = [t for t in targets if t.source_class == source_class]
        scan = scans[matching[0].key]
        print(f"{SOURCE_LABELS[source_class]}: {'missing' if scan.missing else f'{len(scan.eligible)} eligible'}")

    sd_targets = [t for t in targets if t.source_class == SOURCE_SD]
    if SOURCE_SD in requested:
        if not sd_targets:
            print("SD: no registered/mounted card detected")
        else:
            total_sd_eligible = sum(len(scans[t.key].eligible) for t in sd_targets)
            print(f"SD: {total_sd_eligible} eligible")

    combined_counts: dict[str, int] = {}
    for scan in scans.values():
        for reason, count in scan.exclusion_counts.items():
            combined_counts[reason] = combined_counts.get(reason, 0) + count
    _print_exclusions(combined_counts)
    _print_git_repos(scans)

    all_eligible = [(target, f) for target in targets for f in scans[target.key].eligible]
    total_bytes = sum(f.size for _, f in all_eligible)
    print(f"\nPlanned: {len(all_eligible)} files / {total_bytes / 1e9:.1f} GB")

    if not args.apply:
        print("\nDry run. No files changed.")
        return 0

    assert resolved_t7 is not None
    catalog_path = resolved_t7.catalog_path
    outcomes: list[TransferOutcome] = []
    total_removed_bytes = 0
    # One stack so every handle closes on any path out of here, including a
    # transfer that raises: the ledger used to be closed after the catalog's
    # `with` block, which an exception skipped entirely.
    with contextlib.ExitStack() as stack:
        catalog = stack.enter_context(Catalog(catalog_path))
        # A file that `organize triage` renamed earlier is about to lose the
        # only name the owner recognises, because the transfer unlinks the
        # source. The ledger is opened read-mostly here purely to carry that
        # lineage across (invariant 11); its absence is not an error.
        ledger = (
            stack.enter_context(LocalLedger(config.local_ledger_path))
            if config.local_ledger_path.exists()
            else None
        )
        for target in targets:
            catalog.upsert_volume(target.volume_uuid, label=target.label, mount_point=str(target.root))
            for f in scans[target.key].eligible:
                planned = PlannedTransfer(
                    source_path=f.path,
                    rel_path=f.rel_path,
                    source_volume_id=target.volume_uuid,
                    source_class=target.source_class,
                )
                # Read before the transfer: identity is (device, inode) and the
                # source path stops existing once the transfer succeeds.
                provenance = read_local_provenance(ledger, f.path) if ledger is not None else None
                try:
                    outcome = transfer_file(catalog, planned, resolved_t7, settle_seconds=config.settle_seconds)
                except Exception as exc:  # noqa: BLE001 - one bad file must not abort the whole batch
                    print(f"  ERROR: {f.path}: {exc}")
                    outcome = TransferOutcome(
                        asset_id=source_ref_id_for(target.volume_uuid, f.rel_path),
                        state=STATE_FAILED_ERROR,
                        success=False,
                        error_message=str(exc),
                    )
                outcomes.append(outcome)
                if outcome.final_path is not None and outcome.state in (STATE_COMMITTED, STATE_SOURCE_UNLINKED):
                    # Deterministic enrichment of the copy that is now on Angel.
                    # Reads the destination, because the source is gone by now.
                    enrich_asset(
                        catalog,
                        asset_id=outcome.asset_id,
                        final_path=outcome.final_path,
                        sidecars_dir=resolved_t7.sidecars_dir,
                    )
                if provenance is not None and outcome.state in (STATE_COMMITTED, STATE_SOURCE_UNLINKED):
                    try:
                        attach_local_provenance(
                            catalog, ledger, provenance,
                            asset_id=outcome.asset_id,
                            sidecars_dir=resolved_t7.sidecars_dir,
                        )
                    except Exception as exc:  # noqa: BLE001 - bookkeeping must never undo a verified transfer
                        print(f"  WARNING: could not record local naming history for {f.path.name}: {exc}")
                if outcome.state == STATE_SOURCE_UNLINKED:
                    total_removed_bytes += f.size
                if outcome.final_path is not None and outcome.state in (STATE_COMMITTED, STATE_SOURCE_UNLINKED):
                    try:
                        maybe_generate_heic_derivative(
                            catalog,
                            resolved_t7,
                            parent_asset_id=outcome.asset_id,
                            parent_final_path=outcome.final_path,
                            source_volume_id=target.volume_uuid,
                            source_class=target.source_class,
                            config=config.media.heic_to_jpeg,
                        )
                    except Exception as exc:  # noqa: BLE001 - a derivative failure never blocks ingestion
                        print(f"  ERROR: derivative for {outcome.final_path}: {exc}")

            for repo in scans[target.key].git_repos:
                details = inspect_git_repo(repo.path)
                catalog.record_repo(
                    f"{target.volume_uuid}:{repo.rel_path}",
                    source_volume_id=target.volume_uuid,
                    original_path=repo.rel_path,
                    remote_url=details.remote_url,
                    head=details.head,
                    branch=details.branch,
                    dirty=details.dirty,
                    unpushed=details.unpushed,
                    stash=details.stash,
                )

    skipped_reasons = ("still_changing", "source_vanished", "source_changed_during_hash")
    attempted = [o for o in outcomes if o.skipped_reason not in skipped_reasons]
    skipped = [o for o in outcomes if o.skipped_reason in skipped_reasons]
    transferred = [o for o in attempted if o.state in (STATE_FAILED_HASH_MISMATCH, STATE_COMMITTED, STATE_SOURCE_UNLINKED)]
    verified = [o for o in attempted if o.state in (STATE_COMMITTED, STATE_SOURCE_UNLINKED)]
    organized = [o for o in attempted if o.state == STATE_SOURCE_UNLINKED and not o.source_unlink_failed]
    organized_ids = {o.asset_id for o in organized}
    needs_review = [o for o in attempted if o.asset_id not in organized_ids]

    print(f"\n{len(transferred)}/{len(attempted)} transferred")
    print(f"{len(verified)}/{len(attempted)} verified")
    print(f"{len(organized)} organized/staged")
    if needs_review:
        print(f"{len(needs_review)} need review")
    if skipped:
        print(f"{len(skipped)} skipped (not settled yet)")
    print(f"{total_removed_bytes / 1e9:.1f} GB removed from source volumes after verification")

    return 0


def _resolve_t7_or_reason(
    config: IngestConfig, prober: VolumeProber, *, for_apply: bool, allow_dev_root: bool
) -> tuple[ResolvedT7 | None, str]:
    try:
        return resolve_t7(config, prober, for_apply=for_apply, allow_storage_root_override=allow_dev_root), ""
    except VolumeError as exc:
        return None, str(exc)


def cmd_triage(args: argparse.Namespace, prober: VolumeProber | None = None, config: IngestConfig | None = None) -> int:
    """Name and triage local files.

    Naming runs whether or not Angel is connected: a screenshot deserves a
    useful filename regardless of whether it will ever be archived (invariants
    4 and 5). Ingestion is the only part that needs the T7, and its absence
    degrades this command rather than failing it -- under the hard rule that no
    source file is removed while Angel is missing.
    """
    prober = prober or diskutil_volume_prober
    config = config if config is not None else load_config(args.config)
    allow_dev_root = bool(getattr(args, "allow_dev_storage_root", False))
    automated = bool(getattr(args, "automated", False))
    apply = bool(args.apply)
    targets = list(TRIAGE_SOURCE_CLASSES) if args.target == "all" else [args.target]

    if automated and not config.automation.enabled:
        print("automation.enabled is false in config/ingest.yaml; refusing to run as a scheduled agent.")
        return 1

    paths = RunPaths.for_state_dir(config.state_dir)
    run_id = new_run_id()
    report: PipelineReport | None = None

    try:
        with run_lock(paths):
            resolved_t7: ResolvedT7 | None = None
            t7_reason = "ingestion not requested (dry run)"
            if apply:
                resolved_t7, t7_reason = _resolve_t7_or_reason(
                    config, prober, for_apply=True, allow_dev_root=allow_dev_root
                )

            catalog = Catalog(resolved_t7.catalog_path) if resolved_t7 is not None else None
            try:
                with LocalLedger(config.local_ledger_path) as ledger:
                    ledger.start_run(
                        run_id,
                        kind="triage",
                        trigger="automated" if automated else "manual",
                        targets=",".join(targets),
                        apply=apply,
                        now=now_iso(),
                    )
                    try:
                        report = run_pipeline(
                            config,
                            targets=targets,
                            apply=apply,
                            automated=automated,
                            run_id=run_id,
                            ledger=ledger,
                            catalog=catalog,
                            resolved_t7=resolved_t7,
                            t7_reason=t7_reason,
                        )
                    except Exception as exc:  # noqa: BLE001 - a failed run must still be recorded
                        ledger.finish_run(run_id, status="error", summary=None, now=now_iso(), error_message=str(exc))
                        write_status(paths, {"run_id": run_id, "status": "error", "error": str(exc), "finished_at": now_iso()})
                        raise
                    summary = report.summary()
                    ledger.finish_run(run_id, status="ok", summary=summary, now=now_iso())
            finally:
                if catalog is not None:
                    catalog.close()
            write_status(paths, {**summary, "status": "ok", "finished_at": now_iso()})
    except RunLockBusy as exc:
        # Not an error: the next scheduled firing picks the work up. Queuing
        # scans behind each other would be worse than skipping one.
        print(f"Skipped: {exc}")
        return 0
    except RunLockLost as exc:
        # The run finished, but it can no longer claim it was the only one.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    _print_triage_reports(report, apply=apply)
    return 0


def _print_triage_reports(report: PipelineReport, *, apply: bool) -> None:
    print(
        f"Mode: {'apply' if apply else 'dry run'}   "
        f"settle window: {report.settle_seconds}s ({'automated' if report.automated else 'manual'})"
    )
    if report.t7_connected:
        print("Angel: connected")
    else:
        print(f"Angel: not connected -- {report.t7_reason}")

    for source in report.sources:
        print()
        print(f"{source.source_class.title()}:")
        if not source.available:
            print(f"  unavailable -- {source.unavailable_reason}")
            continue
        print(f"  {source.root}")
        print(f"  {source.discovered} discovered, {source.evaluated} evaluated")
        print(f"  {source.renamed} {'renamed' if apply else 'would be renamed'}")
        print(f"  {source.ingest_candidates} Angel candidates")
        print(f"  {source.needs_review} need review")
        print(f"  {source.ignored} ignored for Angel (left on the Mac)")
        if source.ingested:
            print(f"  {source.ingested} ingested through the verified transfer path")
        for reason, count in sorted(source.skipped.items(), key=lambda item: -item[1]):
            print(f"  {count} skipped ({reason})")
        for outcome in source.outcomes:
            if outcome.naming_decision in (DECISION_RENAMED, DECISION_PROPOSED):
                target = outcome.applied_filename or outcome.proposed_filename
                verb = "->" if apply else "would become"
                print(f"    {outcome.original_filename} {verb} {target}")
        for error in source.errors:
            print(f"  ERROR: {error}")

    if not apply:
        print("\nDry run. No files were renamed, moved, or removed.")


def cmd_repos_report(args: argparse.Namespace, prober: VolumeProber | None = None, config: IngestConfig | None = None) -> int:
    """Inventory development repositories; write two reports and nothing else.

    The default report directory is on the volume, so it goes through the
    same UUID resolver every other write does. `--report-dir` writes wherever
    it points -- useful for a local copy -- and never needs the volume.
    """
    config = config if config is not None else load_config(args.config)
    report_dir: Path | None = getattr(args, "report_dir", None)
    if report_dir is None:
        resolved = resolve_t7(
            config,
            prober or diskutil_volume_prober,
            for_apply=False,
            allow_storage_root_override=bool(getattr(args, "allow_dev_storage_root", False)),
        )
        report_dir = resolved.system_dir / "migration-reports"
    roots = tuple(getattr(args, "roots", None) or ()) or None
    return cmd_repos_report_impl(report_dir, config=config, roots=roots)


def cmd_status(args: argparse.Namespace, prober: VolumeProber | None = None, config: IngestConfig | None = None) -> int:
    config = config if config is not None else load_config(args.config)
    data = collect_status(
        config, prober or diskutil_volume_prober,
        allow_dev_storage_root=bool(getattr(args, "allow_dev_storage_root", False)),
    )
    print(render_status(data))
    return 0


def cmd_doctor(args: argparse.Namespace, prober: VolumeProber | None = None, config: IngestConfig | None = None) -> int:
    config = config if config is not None else load_config(args.config)
    data = collect_status(
        config, prober or diskutil_volume_prober,
        allow_dev_storage_root=bool(getattr(args, "allow_dev_storage_root", False)),
    )
    print(render_doctor(config, data))
    return 0


def cmd_review(args: argparse.Namespace, prober: VolumeProber | None = None, config: IngestConfig | None = None) -> int:
    """The queue of files a person needs to look at. Nothing here has moved."""
    config = config if config is not None else load_config(args.config)
    if not config.local_ledger_path.exists():
        print("No local ledger yet. Run `organize triage all` first.")
        return 0
    limit = int(getattr(args, "limit", 40))
    with LocalLedger(config.local_ledger_path) as ledger:
        rows = [
            row for row in ledger.local_files()
            if row["retention_recommendation"] == RETENTION_REVIEW or row["naming_state"] == NAMING_STATE_REVIEW
        ]
    if not rows:
        print("Nothing is waiting for review.")
        return 0
    print(f"{len(rows)} file(s) waiting for a decision. None have been moved or renamed without one.\n")
    for row in rows[:limit]:
        print(f"  {row['current_path']}")
        print(f"    naming: {row['naming_state']}   retention: {row['retention_recommendation'] or 'not evaluated'}")
        if row["retention_reason"]:
            print(f"    why: {row['retention_reason']}")
    if len(rows) > limit:
        print(f"\n  ... and {len(rows) - limit} more (use --limit)")
    return 0


def cmd_duplicates(args: argparse.Namespace, prober: VolumeProber | None = None, config: IngestConfig | None = None) -> int:
    """Report duplicate relationships. This command never deletes anything."""
    config = config if config is not None else load_config(args.config)
    if not config.local_ledger_path.exists():
        print("No local ledger yet. Run `organize triage all` first.")
        return 0
    limit = int(getattr(args, "limit", 40))
    with LocalLedger(config.local_ledger_path) as ledger:
        observations = ledger.duplicate_observations()
        lookup = {row["local_file_id"]: row for row in ledger.local_files()}
    if not observations:
        print("No duplicates observed.")
        return 0
    print(f"{len(observations)} duplicate relationship(s) observed. Nothing is removed automatically.\n")
    for observation in observations[:limit]:
        left = lookup.get(observation["left_ref"], {})
        print(f"  [{observation['kind']}] {left.get('current_path', observation['left_ref'])}")
        print(f"    matches {observation['right_scope']} {observation['right_ref']}")
        if observation["detail"]:
            print(f"    {observation['detail']}")
    if len(observations) > limit:
        print(f"\n  ... and {len(observations) - limit} more (use --limit)")
    return 0


def cmd_decide(args: argparse.Namespace, prober: VolumeProber | None = None, config: IngestConfig | None = None) -> int:
    """Record a retention decision the owner made themselves.

    `organize keep PATH` / `organize ignore PATH`. This is what makes the
    review queue a queue rather than a list: a file the system could not judge
    gets judged by the person who can, once, and the decision stands.

    It does not move or delete anything. `keep` marks a file as an ingest
    candidate; the transfer still happens through the same verified path on the
    next `organize triage ... --apply`, and `ignore` simply means the file stays
    on the Mac and stops being asked about.
    """
    config = config if config is not None else load_config(args.config)
    path = args.path.expanduser()
    if not path.is_file():
        print(f"Not a file: {path}")
        return 1
    if not config.local_ledger_path.exists():
        print("No local ledger yet. Run `organize triage all` first so the file is known.")
        return 1
    try:
        stat = path.stat()
    except OSError as exc:
        print(f"Cannot read {path}: {exc}")
        return 1

    recommendation = RETENTION_INGEST if args.command == "keep" else RETENTION_IGNORE
    with LocalLedger(config.local_ledger_path) as ledger:
        local_file_id = local_file_id_for(LOCAL_VOLUME_ID, stat.st_dev, stat.st_ino)
        row = ledger.get_local_file(local_file_id)
        if row is None:
            print(f"{path.name} is not tracked yet. Run `organize triage all` first.")
            return 1
        reason = args.reason or f"decided by the owner via `organize {args.command}`"
        run_id = ledger.append_classification(
            {
                "local_file_id": local_file_id,
                "asset_id": row["ingested_asset_id"],
                "source_volume_id": row["source_volume_id"],
                "source_class": row["source_class"],
                "sha256": row["sha256"],
                "recommendation": recommendation,
                "confidence": 1.0,
                "reason": reason,
                "model": "human",
                "model_version": "-",
                "policy_outcome": recommendation,
                "human_state": "decided",
                "human_corrected": 1,
                "created_at": now_iso(),
            }
        )
        ledger.update_local_file(
            local_file_id,
            now=now_iso(),
            retention_recommendation=recommendation,
            retention_confidence=1.0,
            retention_reason=reason,
            retention_run_id=run_id,
        )

    if recommendation == RETENTION_INGEST:
        print(
            f"Marked '{path.name}' to keep. It moves to Angel on the next "
            "`organize triage ... --apply`, through the verified transfer path."
        )
    else:
        print(
            f"Marked '{path.name}' as not for Angel. It stays exactly where it is "
            "on the Mac; this is not a delete."
        )
    return 0


def cmd_correct_name(args: argparse.Namespace, prober: VolumeProber | None = None, config: IngestConfig | None = None) -> int:
    """Record that the owner chose this file's name.

    Use after renaming a file by hand. From then on the pipeline leaves it
    alone, permanently -- invariant 12. This command does not rename anything
    itself; it records a name that already exists on disk.
    """
    config = config if config is not None else load_config(args.config)
    path = args.path.expanduser()
    if not path.is_file():
        print(f"Not a file: {path}")
        return 1
    if not config.local_ledger_path.exists():
        print("No local ledger yet. Run `organize triage all` first so the file is known.")
        return 1

    try:
        stat = path.stat()
    except OSError as exc:
        print(f"Cannot read {path}: {exc}")
        return 1

    with LocalLedger(config.local_ledger_path) as ledger:
        local_file_id = local_file_id_for("mac-local", stat.st_dev, stat.st_ino)
        if ledger.get_local_file(local_file_id) is None:
            print(f"{path.name} is not tracked yet. Run `organize triage all` first.")
            return 1
        ledger.record_human_name(
            local_file_id,
            filename=path.name,
            directory=str(path.parent),
            now=now_iso(),
            note="recorded via `organize correct-name`",
        )
    print(f"Recorded '{path.name}' as an owner-chosen name. Automatic naming will not change it again.")
    return 0


def cmd_naming_history(
    args: argparse.Namespace, prober: VolumeProber | None = None, config: IngestConfig | None = None
) -> int:
    """Why is this file called what it is called? Strictly read-only.

    `organize status` and `organize review` answer "what is waiting on me".
    This answers the audit question instead, and it is the reason the D-057
    columns exist: for one file, every naming decision ever recorded against
    it, in order, with the prompt that was rendered, the evidence kinds that
    were in front of the proposer, the content hash of that evidence, and --
    where a model was involved -- its raw answer before any cleanup.

    The raw answer is printed because that is the whole point: a proposal that
    was refused by policy is the most interesting row in this table, and a
    refusal nobody can inspect is indistinguishable from a rename nobody
    noticed. It is file-derived text, so it is printed through the same
    bounded, clearly-marked excerpt helper `organize evidence --show-text`
    uses, and it is already bounded at the point it was stored.

    Writes nothing, renames nothing, moves nothing.
    """
    config = config if config is not None else load_config(args.config)
    if not config.local_ledger_path.exists():
        print("No local ledger yet. Run `organize triage all` first.")
        return 1

    path = args.path.expanduser()
    with LocalLedger(config.local_ledger_path) as ledger:
        row = _local_file_row(ledger, path)
        if row is None:
            print(f"Not in the ledger: {path}")
            print("Run `organize triage all` over its source folder first.")
            return 1
        rows = ledger.naming_history(row["local_file_id"])
        print(f"{row['current_path']}")
        print(f"  first seen as: {row['first_seen_filename'] or '(unknown)'}")
        print(f"  content sha256: {row['sha256'] or '(none recorded)'}")
        print(f"  naming state: {row['naming_state']}")
        if not rows:
            print("\nNo naming decisions recorded for this file yet.")
            return 0
        print(f"\n{len(rows)} naming decision(s), oldest first:")
        for record in rows:
            _print_naming_history_row(record)

    print(
        "\nEvery filename above was produced by a proposal that "
        "tools/ingest/naming/policy.py validated before it could be applied."
    )
    return 0


def _print_naming_history_row(record: dict) -> None:
    print(f"\n  [{record['created_at']}] {record['decision']}")
    print(f"    from: {record['original_filename']}")
    if record["proposed_filename"]:
        print(f"    proposed: {record['proposed_filename']}")
    if record["applied_filename"]:
        print(f"    applied: {record['applied_filename']}")
    confidence = record["confidence"]
    model = record["model"] or "(no proposer)"
    print(f"    proposer: {model} {record['model_version'] or ''}".rstrip()
          + (f"   confidence {float(confidence):.2f}" if confidence is not None else ""))
    print(f"    approved by: {record['approved_by']}"
          + ("  (human correction)" if record["human_corrected"] else ""))
    if record["reason"]:
        print(f"    reason: {record['reason']}")

    prompt_id = record.get("prompt_id")
    if prompt_id:
        print(f"    prompt: {prompt_id} v{record.get('prompt_version') or '?'}")
        print(f"      rendered sha256: {record.get('prompt_sha256') or '(none)'}")
    sources = record.get("evidence_sources")
    if sources:
        try:
            kinds = ", ".join(str(kind) for kind in json.loads(sources))
        except (TypeError, ValueError):
            kinds = str(sources)
        print(f"    evidence kinds: {kinds or '(none)'}")
    if record.get("evidence_sha256"):
        print(f"    evidence sha256: {record['evidence_sha256']}")
    raw = record.get("raw_output")
    if raw:
        print("    raw model output (untrusted, recorded as produced):")
        _print_excerpt(str(raw), limit=400)


def cmd_evidence(args: argparse.Namespace, prober: VolumeProber | None = None, config: IngestConfig | None = None) -> int:
    """Report what the perception layer has seen. Strictly read-only.

    Two modes, because there are two different questions. With no path it
    summarizes the whole corpus: how much evidence exists, from which
    producers, at which statuses. With a path it explains one file: the
    content hash the evidence describes, which routing rules fired and why,
    which producer and version answered each stage, and what went wrong.

    What it deliberately does not print is the extracted text. A terminal
    dump of every contract, medical letter, and tax return on the disk is a
    far larger disclosure than this feature is worth, and an `organize` run
    piped to a log file would persist it. `--show-text` is the explicit,
    per-invocation act of asking for a bounded excerpt anyway.
    """
    config = config if config is not None else load_config(args.config)
    if not config.local_ledger_path.exists():
        print("No local ledger yet. Run `organize triage all` first.")
        return 0

    path: Path | None = getattr(args, "path", None)
    with LocalLedger(config.local_ledger_path) as ledger:
        if path is None:
            return _print_evidence_summary(ledger, config)
        return _print_evidence_for_path(ledger, path, show_text=bool(getattr(args, "show_text", False)))


def _print_evidence_summary(ledger: LocalLedger, config: IngestConfig) -> int:
    summary = ledger.evidence_summary()
    print(f"Evidence layer: {'enabled' if config.evidence.enabled else 'disabled'}")
    print(f"  OCR backend:    {config.evidence.ocr.backend}"
          f"{'' if config.evidence.ocr.enabled else '  (disabled)'}")
    print(f"  Vision backend: {config.evidence.vision.backend}"
          f"{'' if config.evidence.vision.enabled else '  (disabled)'}")
    print(f"  Transcription:  {config.evidence.transcription.backend}"
          f"{'' if config.evidence.transcription.enabled else '  (disabled)'}")
    print()
    if not summary["total"]:
        print("No evidence recorded yet.")
        return 0
    print(f"{summary['total']} cached evidence record(s), keyed by content hash.\n")
    for label, counts in (
        ("by kind", summary["by_kind"]),
        ("by producer", summary["by_producer"]),
        ("by status", summary["by_status"]),
    ):
        print(f"  {label}:")
        for name, count in sorted(counts.items()):
            print(f"    {name}: {count}")
    print("\nEvidence is perception only. Nothing here renames, moves, or deletes a file.")
    return 0


def _print_evidence_for_path(ledger: LocalLedger, path: Path, *, show_text: bool) -> int:
    row = _local_file_row(ledger, path)
    if row is None:
        print(f"Not in the ledger: {path}")
        print("Run `organize triage all` over its source folder first.")
        return 1
    sha256 = row["sha256"]
    if not sha256:
        print(f"{path}: no verified content hash recorded yet.")
        return 1

    records = ledger.file_evidence_for_content(sha256)
    print(f"{row['current_path']}")
    print(f"  content sha256: {sha256}")
    if not records:
        print("  no evidence recorded for these bytes")
        return 0

    for record in records:
        model = f"  model={record['model']}/{record['model_version']}" if record["model"] else ""
        print(f"\n  [{record['evidence_kind']}] {record['producer']} v{record['producer_version']}{model}")
        print(f"    pipeline: {record['pipeline_version']}   status: {record['status']}")
        print(f"    generated: {record['generated_at']}")
        if record["route_reason"]:
            print(f"    routed because: {record['route_reason']}")
        for warning in record["warnings"]:
            print(f"    warning [{warning.get('stage')}]: {warning.get('code')} {warning.get('detail', '')}".rstrip())
        _print_evidence_payload(record, show_text=show_text)

    print("\nEvidence is perception only. Nothing here renames, moves, or deletes a file.")
    return 0


def _print_evidence_payload(record: dict, *, show_text: bool) -> None:
    """Describe one cached record's payload without reproducing its content."""
    payload = record.get("payload") or {}
    native = payload.get("native_text")
    if native:
        print(f"    text: {native.get('char_count', 0)} chars"
              f"{', truncated' if native.get('truncated') else ''}"
              f", {len(native.get('sections') or [])} section(s)")
        if show_text:
            _print_excerpt(native.get("text") or "")
    structure = payload.get("structure")
    if structure:
        print(f"    structure: {len(structure.get('headings') or [])} heading(s), "
              f"{len(structure.get('regions') or [])} region(s), "
              f"{len(structure.get('tables') or [])} table(s)")
    ocr = payload.get("ocr")
    if ocr:
        blocks = ocr.get("blocks") or []
        confidences = [float(block.get("confidence", 0.0)) for block in blocks]
        mean = sum(confidences) / len(confidences) if confidences else 0.0
        print(f"    ocr: {len(blocks)} block(s), mean confidence {mean:.2f}")
        if show_text:
            ordered = sorted(blocks, key=lambda block: (block.get("page", 1), block.get("reading_order", 0)))
            _print_excerpt("\n".join(str(block.get("text", "")) for block in ordered))
    transcript = payload.get("transcript")
    if transcript:
        # Presence, language, coverage and segment count -- never the words,
        # unless `--show-text` was asked for. A transcript is the most
        # sensitive thing this layer produces: it is a verbatim record of
        # what was said in the owner's own recordings, and a terminal dump of
        # it piped to a log file would persist that (the same reasoning
        # `cmd_evidence`'s docstring gives for extracted document text).
        segments = transcript.get("segments") or []
        covered = float(transcript.get("duration_covered_seconds", 0.0) or 0.0)
        print(f"    transcript: present, {len(segments)} segment(s), "
              f"{len(transcript.get('text') or '')} chars, "
              f"language {transcript.get('language') or 'unknown'}, "
              f"{covered:.1f}s of audio covered"
              f"{', truncated' if transcript.get('truncated') else ''}")
        print(f"      engine: {transcript.get('engine', '?')} "
              f"{transcript.get('engine_version', '?')}"
              f"{'  model=' + str(transcript.get('model')) if transcript.get('model') else ''}"
              f"   confidence {float(transcript.get('confidence', 0.0) or 0.0):.2f}")
        if show_text:
            _print_excerpt(transcript.get("text") or "")
    visual = payload.get("visual")
    if visual:
        print(f"    visual: {visual.get('image_kind')} (confidence {float(visual.get('confidence', 0.0)):.2f})")
        if visual.get("description"):
            print(f"      {visual['description']}")
        if visual.get("context"):
            print(f"      context: {visual['context']}")
        if visual.get("entities"):
            print(f"      entities: {', '.join(visual['entities'])}")


def _print_excerpt(text: str, *, limit: int = 800) -> None:
    """Print a bounded excerpt of stored text, indented and clearly marked.

    Text reaching here is normally already sanitized at the point it was
    stored (`evidence/types.py::sanitize_extracted_text` strips the control
    and bidi characters that let a terminal be driven by file content). It is
    re-stripped here anyway, for the same reason
    `naming/evidence.py::_sanitize_excerpt` re-sanitizes at its own boundary:
    this is the function that hands attacker-influenced text to a terminal,
    and it must be safe by construction rather than by the provenance of its
    caller. The `raw_output` column proved the point -- it was bounded but
    never sanitized, so rows written before that was fixed still contain
    whatever a model emitted.
    """
    if not text.strip():
        print("      (no text)")
        return
    excerpt = sanitize_perceived_text(text, max_chars=limit)
    if not excerpt.strip():
        print("      (no printable text)")
        return
    print("      --- extracted text (untrusted file content) ---")
    for line in excerpt.splitlines():
        print(f"      | {line}")
    if len(text) > limit:
        print(f"      | ... ({len(text) - limit} more characters)")


def _local_file_row(ledger: LocalLedger, path: Path) -> dict | None:
    """The ledger row for a path, by the identity that survives a rename."""
    try:
        stat = path.stat()
    except OSError:
        # The file may have been renamed or removed since it was scanned; the
        # ledger still knows it by its recorded path.
        target = str(path)
        return next((row for row in ledger.local_files() if row["current_path"] == target), None)
    return ledger.get_local_file(local_file_id_for(LOCAL_VOLUME_ID, stat.st_dev, stat.st_ino))


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="Path to ingest.yaml (default: config/ingest.yaml).")
    parser.add_argument(
        "--allow-dev-storage-root",
        action="store_true",
        help=(
            "Honor t7.storage_root_override. Development only: it bypasses volume-UUID "
            "verification for every write, including the source unlink."
        ),
    )


def _add_mode_arguments(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only. Zero writes. Default.")
    mode.add_argument("--apply", action="store_true", help="Actually make the reported changes.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="organize",
        description=(
            "Manual-first ingestion into the T7, plus local file naming and retention triage. "
            "Every subcommand defaults to a dry run."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # The Phase 1A ingestion targets keep their exact existing spelling
    # (`organize all --apply`), so scripts, the .command launcher, and muscle
    # memory all keep working.
    for target in list(ALL_SOURCE_CLASSES) + ["all"]:
        sub = subparsers.add_parser(target, help=f"Ingest {SOURCE_LABELS.get(target, target)} into the T7.")
        sub.set_defaults(command="organize", target=target)
        _add_mode_arguments(sub)
        _add_common_arguments(sub)

    triage = subparsers.add_parser(
        "triage",
        help="Name and triage local files. Naming does not imply ingestion.",
        description=(
            "Evaluate filenames and retention for Downloads and Screenshots. Renaming a file "
            "never moves it; moving a file requires triage.auto_ingest and a connected Angel."
        ),
    )
    triage.add_argument("target", choices=list(TRIAGE_SOURCE_CLASSES) + ["all"])
    _add_mode_arguments(triage)
    triage.add_argument(
        "--automated",
        action="store_true",
        help=(
            "Run as the scheduled agent: use the watcher settle window "
            "(downloads_watcher_min_settle_seconds) instead of the shorter manual one."
        ),
    )
    _add_common_arguments(triage)

    status = subparsers.add_parser("status", help="What the pipeline currently knows and would do.")
    _add_common_arguments(status)

    doctor = subparsers.add_parser("doctor", help="Configuration and environment checks.")
    _add_common_arguments(doctor)

    review = subparsers.add_parser("review", help="Files waiting on a human decision.")
    review.add_argument("--limit", type=int, default=40)
    _add_common_arguments(review)

    duplicates = subparsers.add_parser("duplicates", help="Observed duplicate relationships. Never acted on.")
    duplicates.add_argument("--limit", type=int, default=40)
    _add_common_arguments(duplicates)

    for verb, blurb in (
        ("keep", "Mark a reviewed file as one to keep in Angel. Your decision, not the model's."),
        ("ignore", "Mark a reviewed file as not for Angel. It stays on the Mac -- this is not a delete."),
    ):
        decide = subparsers.add_parser(verb, help=blurb)
        decide.add_argument("path", type=Path, help="The file, as it is currently named on disk.")
        decide.add_argument("--reason", default="", help="Why, for the audit trail.")
        _add_common_arguments(decide)

    evidence = subparsers.add_parser(
        "evidence",
        help="What the perception layer has seen. Read-only; never prints file text by default.",
    )
    evidence.add_argument(
        "path", type=Path, nargs="?", default=None,
        help="A file to explain. Omit for a corpus-wide summary.",
    )
    evidence.add_argument(
        "--show-text", action="store_true",
        help="Also print a bounded excerpt of the extracted text. Off by default: "
             "the evidence corpus contains the contents of the owner's documents.",
    )
    _add_common_arguments(evidence)

    naming_history = subparsers.add_parser(
        "naming-history",
        help="Why this file is called what it is called. Read-only audit trail.",
        description=(
            "Every naming decision recorded for one file, oldest first: the proposer, its "
            "confidence, the prompt that was rendered, which kinds of perceived evidence were "
            "in front of it, and a model's raw answer where one was involved. Writes nothing."
        ),
    )
    naming_history.add_argument("path", type=Path, help="The file, as it is currently named on disk.")
    _add_common_arguments(naming_history)

    correct = subparsers.add_parser(
        "correct-name",
        help="Record a name you chose yourself. Nothing automatic overrides it afterwards.",
    )
    correct.add_argument("path", type=Path, help="The file as it is now named on disk.")
    _add_common_arguments(correct)

    repos = subparsers.add_parser(
        "repos-report",
        help="Inventory development repositories. Read-only: nothing is moved, fetched, or pushed.",
    )
    repos.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Where to write repos-inventory.yaml and unpushed-repos.md. "
             "Defaults to 90-system/migration-reports/ on the UUID-verified volume.",
    )
    repos.add_argument(
        "--root",
        type=Path,
        action="append",
        default=None,
        dest="roots",
        help="A source root to inventory. Repeatable. Defaults to ~/Documents, ~/Projects, ~/OpenMontage.",
    )
    _add_common_arguments(repos)

    return parser


def main(argv: list[str] | None = None, prober: VolumeProber | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "organize":
        return cmd_organize(args, prober=prober)
    handler = {
        "triage": cmd_triage,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "review": cmd_review,
        "duplicates": cmd_duplicates,
        "evidence": cmd_evidence,
        "naming-history": cmd_naming_history,
        "correct-name": cmd_correct_name,
        "repos-report": cmd_repos_report,
        "keep": cmd_decide,
        "ignore": cmd_decide,
    }[args.command]
    return handler(args, prober=prober)


if __name__ == "__main__":
    sys.exit(main())
