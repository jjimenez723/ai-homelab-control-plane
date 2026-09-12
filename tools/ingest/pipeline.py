"""Discover -> describe -> name -> triage -> (maybe) ingest.

This is the orchestration layer for the file-intelligence pipeline. It owns no
authority of its own: every step that can change the filesystem is delegated to
code that already had that authority and is tested for it --
`naming.policy.safe_rename` for renames, `transaction.transfer_file` for
transfers. Nothing here unlinks a source file, and nothing here lets a model's
output reach either primitive directly.

The two independent questions, in order:

1. **What should this file be called?** Asked of every eligible settled file,
   whether or not it will ever go to Angel.
2. **Does it belong in Angel?** Asked separately. `ignore` here means "not for
   the library" -- the file stays on the Mac either way.

Angel being disconnected disables only step 2. Naming keeps working, because
its ledger lives on the Mac (D-021).
"""

from __future__ import annotations

import dataclasses
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from tools.ingest.catalog import Catalog, now_iso
from tools.ingest.config import IngestConfig
from tools.ingest.exclusions import is_incomplete_download, is_settled
from tools.ingest.hashing import sha256_file
from tools.ingest.ledger import (
    DECISION_PROPOSED,
    DECISION_RENAMED,
    RETENTION_IGNORE,
    RETENTION_INGEST,
    RETENTION_REVIEW,
    LocalLedger,
)
from tools.ingest.media.probe import probe_file
from tools.ingest.naming.evidence import gather_evidence
from tools.ingest.naming.quality import QUALITY_GOOD, assess
from tools.ingest.naming.proposer import DeterministicProposer, DisabledProposer, NameProposer
from tools.ingest.naming.rename import NamingOutcome, evaluate_and_apply
from tools.ingest.sources import (
    SOURCE_DOWNLOADS,
    SOURCE_SCREENSHOTS,
    ScreenshotsNotConfigured,
    is_screenshot_candidate,
    resolve_screenshots_root,
    screenshots_root_is_shared,
)
from tools.ingest.t7 import ResolvedT7
from tools.ingest.transaction import PlannedTransfer, TransferOutcome, transfer_file
from tools.ingest.triage.policy import RetentionDecision, human_decision, may_auto_ingest, triage

@dataclass
class FileOutcome:
    """Everything decided about one file in one run."""

    path: Path
    source_class: str
    local_file_id: str
    sha256: str
    original_filename: str
    naming_decision: str
    name_quality: str
    proposed_filename: str | None
    applied_filename: str | None
    retention: str
    retention_confidence: float
    retention_reason: str
    ingested: bool = False
    transfer: TransferOutcome | None = None
    note: str = ""


@dataclass
class SourceReport:
    source_class: str
    root: Path | None = None
    available: bool = True
    unavailable_reason: str = ""
    discovered: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    outcomes: list[FileOutcome] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def evaluated(self) -> int:
        return len(self.outcomes)

    @property
    def renamed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.naming_decision in (DECISION_RENAMED, DECISION_PROPOSED))

    @property
    def ingest_candidates(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.retention == RETENTION_INGEST)

    @property
    def needs_review(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.retention == RETENTION_REVIEW)

    @property
    def ignored(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.retention == RETENTION_IGNORE)

    @property
    def ingested(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.ingested)

    def summary(self) -> dict[str, object]:
        return {
            "source_class": self.source_class,
            "root": str(self.root) if self.root else None,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "discovered": self.discovered,
            "evaluated": self.evaluated,
            "renamed": self.renamed,
            "ingest_candidates": self.ingest_candidates,
            "needs_review": self.needs_review,
            "ignored": self.ignored,
            "ingested": self.ingested,
            "skipped": dict(self.skipped),
            "errors": list(self.errors),
        }


@dataclass
class PipelineReport:
    run_id: str
    apply: bool
    automated: bool
    settle_seconds: int
    t7_connected: bool
    t7_reason: str = ""
    sources: list[SourceReport] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "apply": self.apply,
            "automated": self.automated,
            "settle_seconds": self.settle_seconds,
            "t7_connected": self.t7_connected,
            "t7_reason": self.t7_reason,
            "sources": [source.summary() for source in self.sources],
        }


def build_proposer(config: IngestConfig) -> NameProposer:
    return DeterministicProposer() if config.naming.enabled else DisabledProposer()


def screenshot_candidates(root: Path, *, require_name_pattern: bool) -> list[Path]:
    """Files in the screenshots folder that are actually screenshots.

    The scan is flat, not recursive: a folder someone put *inside* their
    screenshots folder is theirs, not the pipeline's. Eligibility itself is
    `sources.is_screenshot_candidate`, which narrows to filename patterns when
    the folder is shared with the Desktop.
    """
    candidates: list[Path] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return candidates
    for entry in entries:
        if not entry.is_file() or entry.is_symlink() or entry.name.startswith("."):
            continue
        if not is_screenshot_candidate(entry, require_name_pattern=require_name_pattern):
            continue
        candidates.append(entry)
    return candidates


def downloads_candidates(root: Path, config: IngestConfig) -> list[Path]:
    """Top-level files in Downloads. Directories are left alone entirely.

    Downloads is full of unpacked app folders and project trees; walking into
    them would mean renaming files that something else owns by name. A
    directory is a unit the owner made, so it is out of scope for triage.
    """
    candidates: list[Path] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return candidates
    for entry in entries:
        if not entry.is_file() or entry.is_symlink() or entry.name.startswith("."):
            continue
        if is_incomplete_download(entry, config.exclusions):
            continue
        candidates.append(entry)
    return candidates


def _resolve_source(config: IngestConfig, source_class: str) -> tuple[Path, bool]:
    """(root, require_screenshot_name_pattern) for one intelligence source."""
    if source_class == SOURCE_SCREENSHOTS:
        root = resolve_screenshots_root(config)
        return root, screenshots_root_is_shared(config, root)
    if source_class == SOURCE_DOWNLOADS:
        return config.sources.downloads, False
    raise ScreenshotsNotConfigured(f"{source_class} is not a file-intelligence source")


def run_source(
    config: IngestConfig,
    ledger: LocalLedger,
    *,
    source_class: str,
    source_volume_id: str,
    apply: bool,
    settle_seconds: int,
    resolved_t7: ResolvedT7 | None,
    catalog: Catalog | None = None,
    proposer: NameProposer | None = None,
) -> SourceReport:
    """Run the pipeline over one source. Never raises for an expected failure."""
    report = SourceReport(source_class=source_class)
    proposer = proposer or build_proposer(config)

    try:
        root, restrict = _resolve_source(config, source_class)
    except ScreenshotsNotConfigured as exc:
        report.available = False
        report.unavailable_reason = str(exc)
        return report
    report.root = root

    if not root.exists():
        report.available = False
        report.unavailable_reason = f"source directory does not exist: {root}"
        return report

    if source_class == SOURCE_SCREENSHOTS:
        candidates = screenshot_candidates(root, require_name_pattern=restrict)
    else:
        candidates = downloads_candidates(root, config)

    for path in candidates:
        report.discovered += 1
        try:
            outcome = _process_file(
                config, ledger, path,
                source_class=source_class,
                source_volume_id=source_volume_id,
                apply=apply,
                settle_seconds=settle_seconds,
                resolved_t7=resolved_t7,
                catalog=catalog,
                proposer=proposer,
                report=report,
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not end the run
            report.errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        if outcome is not None:
            report.outcomes.append(outcome)

    return report


def _process_file(
    config: IngestConfig,
    ledger: LocalLedger,
    path: Path,
    *,
    source_class: str,
    source_volume_id: str,
    apply: bool,
    settle_seconds: int,
    resolved_t7: ResolvedT7 | None,
    catalog: Catalog | None,
    proposer: NameProposer,
    report: SourceReport,
) -> FileOutcome | None:
    # An unsettled file is still being written. Hashing it would record bytes
    # that are about to change, and renaming it could break the writer.
    if not is_settled(path, settle_seconds):
        report.skip("not settled yet")
        return None

    try:
        stat = path.stat()
        sha256 = sha256_file(path)
    except OSError as exc:
        report.errors.append(f"{path.name}: unreadable: {exc}")
        return None

    mime, _ = mimetypes.guess_type(path.name)
    facts = probe_file(path, mime=mime)
    now = now_iso()

    observed = ledger.observe_file(
        path=path,
        source_class=source_class,
        source_volume_id=source_volume_id,
        device=stat.st_dev,
        inode=stat.st_ino,
        size=stat.st_size,
        sha256=sha256,
        mime=mime,
        media_kind=facts.media_kind,
        now=now,
    )
    ledger.record_media_metadata(
        observed.local_file_id,
        subject_kind="local_file",
        media_kind=facts.media_kind,
        mime=mime,
        probe=facts.probe,
        probe_version=facts.probe_version,
        facts=facts.facts,
        created_at=now,
    )

    evidence = gather_evidence(
        path,
        source_class=source_class,
        vocabulary=config.naming.vocabulary,
        media_facts=facts,
    )

    # -- 1. naming ----------------------------------------------------------

    naming = evaluate_and_apply(
        ledger,
        local_file_id=observed.local_file_id,
        path=path,
        evidence=evidence,
        proposer=proposer,
        source_volume_id=source_volume_id,
        source_class=source_class,
        sha256=sha256,
        now=now,
        apply=apply and config.naming.enabled and source_class in config.naming.source_classes,
        min_confidence=config.naming.min_confidence,
        max_filename_length=config.naming.max_filename_length,
        protected_extensions=config.naming.protected_extensions,
        vocabulary=config.naming.vocabulary,
    )
    ledger.update_local_file(
        observed.local_file_id,
        now=now,
        name_quality=naming.name_quality,
        naming_state=naming.naming_state,
        current_path=str(naming.path),
        current_filename=naming.path.name,
    )
    current_path = naming.path

    # -- 2. retention triage (independent of the above) ---------------------

    duplicate_asset = _duplicate_in_angel(catalog, sha256)
    duplicate_local = _duplicate_on_mac(ledger, sha256, observed.local_file_id)
    if duplicate_local is not None:
        ledger.record_duplicate(
            kind="identical_bytes",
            left_ref=observed.local_file_id, left_scope="local_file",
            right_ref=duplicate_local, right_scope="local_file",
            detail=f"sha256={sha256}", created_at=now,
        )
    if duplicate_asset is not None:
        ledger.record_duplicate(
            kind="already_in_angel",
            left_ref=observed.local_file_id, left_scope="local_file",
            right_ref=duplicate_asset, right_scope="asset",
            detail=f"sha256={sha256}", created_at=now,
        )

    # Triage judges the file as it now stands. A file that just earned a
    # meaningful filename should be assessed on that name, not on the machine
    # name it arrived with -- the flow is name-then-triage, and re-deriving the
    # quality here is what makes that ordering real rather than nominal.
    triage_evidence = evidence
    if current_path.name != evidence.original_filename:
        quality, quality_reason = assess(current_path.name, vocabulary=config.naming.vocabulary)
        triage_evidence = dataclasses.replace(
            evidence,
            original_filename=current_path.name,
            name_quality=quality,
            name_quality_reason=quality_reason,
        )

    # A retention decision the owner made stands, permanently. Re-deriving one
    # every run would mean a model quietly overruling a person (invariant 12).
    standing = _standing_human_decision(ledger, observed.local_file_id)
    if standing is not None:
        decision = standing
    else:
        decision = triage(
            triage_evidence,
            config=config.triage,
            duplicate_of_asset=duplicate_asset,
            duplicate_of_local=duplicate_local,
            # The name the file arrived with, before this run touched it.
            owner_named=evidence.name_quality == QUALITY_GOOD,
        )
    run_id = ledger.append_classification(
        {
            "local_file_id": observed.local_file_id,
            "asset_id": None,
            "source_volume_id": source_volume_id,
            "source_class": source_class,
            "sha256": sha256,
            "recommendation": decision.recommendation,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "tags": ",".join(decision.tags),
            "canonical_filename_proposal": naming.proposed_filename,
            "model": decision.model,
            "model_version": decision.model_version,
            "policy_outcome": decision.recommendation,
            "created_at": now,
        }
    )
    ledger.update_local_file(
        observed.local_file_id,
        now=now,
        retention_recommendation=decision.recommendation,
        retention_confidence=decision.confidence,
        retention_reason=decision.reason,
        retention_run_id=run_id,
    )

    outcome = FileOutcome(
        path=current_path,
        source_class=source_class,
        local_file_id=observed.local_file_id,
        sha256=sha256,
        original_filename=naming.original_filename,
        naming_decision=naming.decision,
        name_quality=naming.name_quality,
        proposed_filename=naming.proposed_filename,
        applied_filename=naming.applied_filename,
        retention=decision.recommendation,
        retention_confidence=decision.confidence,
        retention_reason=decision.reason,
    )

    # -- 3. ingestion, only where policy explicitly allows it ---------------

    if not may_auto_ingest(decision, config.triage):
        outcome.note = _why_not_ingested(decision.recommendation, config)
        return outcome

    if resolved_t7 is None or catalog is None:
        # Angel absent: naming already happened and stands. Nothing is moved,
        # and above all nothing is removed from the Mac.
        outcome.note = "Angel not connected; retained on the Mac for a later run"
        return outcome

    if not apply:
        outcome.note = "dry run; would ingest"
        return outcome

    transfer = transfer_file(
        catalog,
        PlannedTransfer(
            source_path=current_path,
            rel_path=current_path.name,
            source_volume_id=source_volume_id,
            source_class=source_class,
        ),
        resolved_t7,
        settle_seconds=settle_seconds,
    )
    outcome.transfer = transfer
    outcome.ingested = transfer.success and transfer.final_path is not None

    if outcome.ingested:
        _attach_provenance(
            catalog, ledger,
            asset_id=transfer.asset_id,
            local_file_id=observed.local_file_id,
            first_seen_filename=_first_seen_filename(ledger, observed.local_file_id, current_path.name),
            retention=decision.recommendation,
            retention_run_id=run_id,
            facts=facts,
            now=now,
        )
        ledger.update_local_file(observed.local_file_id, now=now, ingested_asset_id=transfer.asset_id)
    else:
        outcome.note = transfer.error_message or transfer.skipped_reason or "transfer did not complete"
    return outcome


def _why_not_ingested(recommendation: str, config: IngestConfig) -> str:
    if recommendation == RETENTION_IGNORE:
        return "not for Angel; left on the Mac (ignore does not mean delete)"
    if recommendation == RETENTION_REVIEW:
        return "needs review; left on the Mac"
    if recommendation == RETENTION_INGEST and not config.triage.auto_ingest:
        return "ingest candidate, but triage.auto_ingest is off"
    return "ingest candidate below the confidence threshold"


def _duplicate_in_angel(catalog: Catalog | None, sha256: str) -> str | None:
    """An asset already on Angel with these exact bytes, if the catalog is open."""
    if catalog is None:
        return None
    row = catalog._conn.execute(  # noqa: SLF001 - same package, intentional
        "SELECT asset_id FROM assets WHERE sha256 = ? AND final_path IS NOT NULL LIMIT 1",
        (sha256,),
    ).fetchone()
    return row["asset_id"] if row is not None else None


def _duplicate_on_mac(ledger: LocalLedger, sha256: str, local_file_id: str) -> str | None:
    for row in ledger.local_files_with_sha(sha256):
        if row["local_file_id"] != local_file_id:
            return row["local_file_id"]
    return None


def _first_seen_filename(ledger: LocalLedger, local_file_id: str, fallback: str) -> str:
    row = ledger.get_local_file(local_file_id)
    return (row or {}).get("first_seen_filename") or fallback


def _attach_provenance(
    catalog: Catalog,
    ledger: LocalLedger,
    *,
    asset_id: str,
    local_file_id: str,
    first_seen_filename: str,
    retention: str,
    retention_run_id: str,
    facts,
    now: str,
) -> None:
    """Copy the Mac-side lineage onto the durable Angel record.

    The asset's `original_filename` is the name it had at the moment of
    transfer, which after a local rename is *not* the name the owner would
    recognise. So the first-seen name and the whole naming lineage are carried
    across, and the sidecar renders both. Losing the ledger later must not cost
    the provenance of anything that reached Angel.
    """
    catalog.update_asset(
        asset_id,
        first_seen_filename=first_seen_filename,
        local_file_id=local_file_id,
        retention_recommendation=retention,
        retention_run_id=retention_run_id,
    )
    for row in ledger.naming_history(local_file_id):
        record = dict(row)
        record.pop("naming_id", None)
        record["asset_id"] = asset_id
        catalog.append_naming(record)
    for row in ledger.classification_runs(local_file_id):
        record = dict(row)
        record.pop("run_id", None)
        record["asset_id"] = asset_id
        catalog.append_classification(record)
    catalog.record_media_metadata(
        asset_id,
        subject_kind="asset",
        media_kind=facts.media_kind,
        mime=facts.mime,
        probe=facts.probe,
        probe_version=facts.probe_version,
        facts=facts.facts,
        created_at=now,
    )
    ledger.link_naming_to_asset(local_file_id, asset_id)
    ledger.link_classification_to_asset(local_file_id, asset_id)
    catalog.write_sidecar(asset_id, _sidecars_dir(catalog))


def _sidecars_dir(catalog: Catalog) -> Path:
    return catalog.db_path.parent / "sidecars"


def run_pipeline(
    config: IngestConfig,
    *,
    targets: list[str],
    apply: bool,
    automated: bool,
    run_id: str,
    ledger: LocalLedger,
    catalog: Catalog | None,
    resolved_t7: ResolvedT7 | None,
    t7_reason: str = "",
) -> PipelineReport:
    """Run naming and triage across the requested sources.

    Angel's absence is a degraded mode, not a failure: naming still happens and
    is still recorded, and the one thing that cannot happen is a source file
    being removed. That is the whole point of keeping the naming ledger on the
    Mac.
    """
    settle_seconds = config.effective_settle_seconds(automated=automated)
    report = PipelineReport(
        run_id=run_id,
        apply=apply,
        automated=automated,
        settle_seconds=settle_seconds,
        t7_connected=resolved_t7 is not None,
        t7_reason=t7_reason,
    )
    proposer = build_proposer(config)
    # Local files are all on the Mac's own volume as far as identity goes; the
    # per-file identity that matters is (device, inode), which `observe_file`
    # derives. A single stable label here keeps ledger rows comparable.
    source_volume_id = "mac-local"

    for source_class in targets:
        report.sources.append(
            run_source(
                config,
                ledger,
                source_class=source_class,
                source_volume_id=source_volume_id,
                apply=apply,
                settle_seconds=settle_seconds,
                resolved_t7=resolved_t7,
                catalog=catalog,
                proposer=proposer,
            )
        )
    return report


def _standing_human_decision(ledger: LocalLedger, local_file_id: str) -> RetentionDecision | None:
    """The owner's own retention decision for this file, if they made one.

    Read from the append-only classification ledger rather than a mutable flag,
    so the decision keeps its reason and its timestamp alongside every model
    opinion that came before or after it.
    """
    for row in reversed(ledger.classification_runs(local_file_id)):
        if row["human_corrected"]:
            return human_decision(row["recommendation"], reason=row["reason"] or "decided by the owner")
    return None
