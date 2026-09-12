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
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.ingest.catalog import (
    STATE_COMMITTED,
    STATE_FAILED_ERROR,
    STATE_FAILED_HASH_MISMATCH,
    STATE_SOURCE_UNLINKED,
    Catalog,
)
from tools.ingest.config import IngestConfig, load_config
from tools.ingest.derivatives import maybe_generate_heic_derivative
from tools.ingest.exclusions import ExclusionReason
from tools.ingest.git_repos import inspect_git_repo
from tools.ingest.identity import asset_id_for
from tools.ingest.scanner import ScanResult, scan_source
from tools.ingest.sources import (
    ALL_SOURCE_CLASSES,
    MANUAL_STATIC_SOURCES,
    SOURCE_SD,
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

SOURCE_LABELS = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "sd": "SD",
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
    prober = prober or diskutil_volume_prober
    config = config if config is not None else load_config(args.config)
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
    with Catalog(catalog_path) as catalog:
        for target in targets:
            catalog.upsert_volume(target.volume_uuid, label=target.label, mount_point=str(target.root))
            for f in scans[target.key].eligible:
                planned = PlannedTransfer(
                    source_path=f.path,
                    rel_path=f.rel_path,
                    source_volume_id=target.volume_uuid,
                    source_class=target.source_class,
                )
                try:
                    outcome = transfer_file(catalog, planned, resolved_t7, settle_seconds=config.settle_seconds)
                except Exception as exc:  # noqa: BLE001 - one bad file must not abort the whole batch
                    print(f"  ERROR: {f.path}: {exc}")
                    outcome = TransferOutcome(
                        asset_id=asset_id_for(target.volume_uuid, f.rel_path),
                        state=STATE_FAILED_ERROR,
                        success=False,
                        error_message=str(exc),
                    )
                outcomes.append(outcome)
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

    skipped_reasons = ("still_changing", "source_vanished")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="organize", description="Manual-first daily ingestion into the T7 (PLAN.md Phase 1A).")
    parser.add_argument("target", choices=list(ALL_SOURCE_CLASSES) + ["all"])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only. Zero writes. Default.")
    mode.add_argument("--apply", action="store_true", help="Actually transfer eligible files through the verified transaction primitive.")
    parser.add_argument("--config", type=Path, default=None, help="Path to ingest.yaml (default: config/ingest.yaml).")
    parser.add_argument(
        "--allow-dev-storage-root",
        action="store_true",
        help=(
            "Honor t7.storage_root_override. Development only: it bypasses volume-UUID "
            "verification for every write, including the source unlink."
        ),
    )
    return parser


def main(argv: list[str] | None = None, prober: VolumeProber | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return cmd_organize(args, prober=prober)


if __name__ == "__main__":
    sys.exit(main())
