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

A third thing happens, and as of D-057 it feeds exactly one of the two:

3. **What is in this file?** Perception -- native text, OCR, a vision
   description, and in time a speech transcript -- now runs *before* naming
   and is handed to the proposer as `NamingEvidence.perceived`, converted to
   bounded plain data by `perception_bridge.py`. It is still handed to
   **nothing else**. Retention triage receives the same `NamingEvidence` it
   always did minus that field's influence: `triage/policy.py` reads none of
   it, may not import the evidence package at all, and
   `tests/ingest/test_evidence_security.py` proves behaviourally that two
   files differing only in what was perceived about them get the identical
   retention decision.

   The ordering change is the whole of the new authority, and it is narrow by
   construction: what a proposer returns is still a string, still validated by
   `naming/policy.py::validate_proposal`, and still incapable of relocating or
   deleting anything. Perception that fails, is disabled, or is unavailable
   yields `perceived=None` and naming proceeds exactly as it did before --
   evidence can make a name better, never make naming fail.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from tools.ingest.catalog import Catalog, now_iso
from tools.ingest.config import IngestConfig
from tools.ingest.exclusions import is_incomplete_download, is_settled
from tools.ingest.hashing import sha256_file
from tools.ingest.identity import local_file_id_for
from tools.ingest.ledger import (
    DECISION_PROPOSED,
    DECISION_RENAMED,
    RETENTION_IGNORE,
    RETENTION_INGEST,
    RETENTION_REVIEW,
    LocalLedger,
)
from tools.ingest.evidence.extract import collect_evidence
from tools.ingest.evidence.types import EvidenceSource, FileEvidence
from tools.ingest.media.probe import PROBE_VERSION, MediaFacts, probe_file
from tools.ingest.naming.evidence import gather_evidence
from tools.ingest.naming.quality import QUALITY_GOOD, assess
from tools.ingest.naming.proposer import DeterministicProposer, DisabledProposer, NameProposer
from tools.ingest.naming.rename import NamingOutcome, evaluate_and_apply
from tools.ingest.naming.semantic import SemanticProposer
from tools.ingest.perception_bridge import perceived_from_file_evidence
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

LOCAL_VOLUME_ID = "mac-local"


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
    # What was perceived about this file: the route that fired, the kinds
    # produced, the warnings, the cache hits. Reporting only -- the *content*
    # reached the naming proposal (D-057) and nothing else; retention was
    # handed evidence with `perceived` stripped off.
    evidence_route: tuple[str, ...] = ()
    evidence_kinds: tuple[str, ...] = ()
    evidence_warnings: tuple[str, ...] = ()
    evidence_cache_hits: int = 0


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
    evidence_collected: int = 0
    evidence_cache_hits: int = 0

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
            "evidence_collected": self.evidence_collected,
            "evidence_cache_hits": self.evidence_cache_hits,
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
    """The one proposer for a whole run, chosen by config (D-057).

    Built once per run rather than once per file: `SemanticProposer` resolves
    its model's digest at construction, and `ledger.evaluated_by` keys the
    "already answered" check on `model_version`, so a value that changed
    partway through a run would make every remaining file look unevaluated.

    `naming.enabled: false` still wins over `naming.proposer`, and means
    exactly what it always meant -- a `DisabledProposer` that declines
    everything. Turning on the semantic proposer is therefore two deliberate
    settings, not one.
    """
    if not config.naming.enabled:
        return DisabledProposer()
    if config.naming.proposer == "semantic":
        semantic = config.naming.semantic
        return SemanticProposer(
            model=semantic.model,
            endpoint=semantic.endpoint,
            timeout_seconds=semantic.timeout_seconds,
            num_predict=semantic.num_predict,
            min_confidence=semantic.min_confidence,
            fallback_to_deterministic=semantic.fallback_to_deterministic,
            max_filename_length=config.naming.max_filename_length,
        )
    return DeterministicProposer()


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


def _unchanged_since_last_scan(ledger: LocalLedger, path: Path, stat) -> tuple[str, MediaFacts] | None:
    """The stored hash and probe, if this file demonstrably has not changed.

    The test is deliberately strict, because being wrong here means naming and
    triage decisions made against stale bytes:

    * the ledger already knows this (device, inode);
    * it is at the same path under the same name;
    * the size is identical;
    * both mtime and ctime are older than the moment we last looked at it;
    * and we have a stored hash and a probe from *this* probe version.

    Any of those failing means a full read. A writer can restore mtime, but an
    in-place content rewrite still advances ctime on the supported local
    filesystems; checking both prevents a same-size rewrite from inheriting a
    stale cached hash. This cache is only a cost optimization -- `transfer_file`
    still hashes independently before it removes anything.
    """
    row = ledger.get_local_file(local_file_id_for(LOCAL_VOLUME_ID, stat.st_dev, stat.st_ino))
    if row is None or not row["sha256"]:
        return None
    if row["current_path"] != str(path) or row["size"] != stat.st_size:
        return None

    last_seen = row["last_seen_at"]
    if not last_seen:
        return None
    try:
        seen_at = dt.datetime.fromisoformat(last_seen).timestamp()
    except ValueError:
        return None
    if stat.st_mtime >= seen_at or stat.st_ctime >= seen_at:
        return None

    stored = ledger.get_media_metadata(row["local_file_id"], "local_file")
    if stored is None or stored["probe_version"] != PROBE_VERSION:
        return None

    return row["sha256"], MediaFacts(
        media_kind=stored["media_kind"],
        mime=stored["mime"],
        facts=stored["facts"],
        probe=stored["probe"],
        probe_version=stored["probe_version"],
    )


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
    except OSError as exc:
        report.errors.append(f"{path.name}: unreadable: {exc}")
        return None

    mime, _ = mimetypes.guess_type(path.name)
    now = now_iso()

    # Reading every byte of every file on every run is what an hourly agent
    # over a Downloads folder would otherwise do -- forever, to reach a verdict
    # that is almost always "already named". Reuse the stored hash and probe
    # only when the file provably has not been touched since we last read it;
    # anything unclear falls through to the full read.
    cached = _unchanged_since_last_scan(ledger, path, stat)
    if cached is not None:
        sha256, facts = cached
    else:
        try:
            sha256 = sha256_file(path)
        except OSError as exc:
            report.errors.append(f"{path.name}: unreadable: {exc}")
            return None
        facts = probe_file(path, mime=mime)

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

    # -- 0. perception ------------------------------------------------------
    #
    # Runs before naming (D-057) and is handed to the proposer, and to nothing
    # else. Keyed by content hash, so a file this run renames still hits the
    # cache on the next run under its new name -- the rename costs nothing.
    #
    # Everything about this call is failure-tolerant: `_collect_file_evidence`
    # never raises, an unavailable backend yields a warning rather than an
    # error, and a `None` result means naming proceeds on measured facts
    # alone. Evidence can improve a name; it can never prevent one.
    collected = _collect_file_evidence(
        config, ledger, path,
        sha256=sha256,
        mime=mime,
        facts=facts,
        screenshot_by_name=evidence.screenshot_by_name,
        size=stat.st_size,
        now=now,
        report=report,
    )
    perceived = perceived_from_file_evidence(collected)
    if perceived is not None:
        evidence = dataclasses.replace(evidence, perceived=perceived)

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
    # A successful rename changes filesystem ctime even though it does not
    # change the file's bytes. Refresh last_seen_at after our own mutation so
    # the ctime cache guard does not mistake that rename for a later content
    # rewrite on the next scan. A real rewrite after this point still advances
    # ctime beyond this timestamp and forces a full re-read.
    post_naming_now = now_iso() if naming.renamed else now
    ledger.update_local_file(
        observed.local_file_id,
        now=post_naming_now,
        name_quality=naming.name_quality,
        naming_state=naming.naming_state,
        current_path=str(naming.path),
        current_filename=naming.path.name,
        last_seen_at=post_naming_now,
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
    #
    # `perceived=None` is stripped off deliberately rather than merely being
    # ignored by `triage()`. D-057 grants perceived content authority over a
    # *name* and nothing else, and the cheapest way to keep that true a year
    # from now is for the value triage is handed not to contain it: a future
    # change that wants retention to read OCR text has to widen this call
    # visibly instead of finding the field already there.
    triage_evidence = dataclasses.replace(evidence, perceived=None)
    if current_path.name != evidence.original_filename:
        quality, quality_reason = assess(current_path.name, vocabulary=config.naming.vocabulary)
        triage_evidence = dataclasses.replace(
            triage_evidence,
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

    # -- 2a. perception, reported ------------------------------------------
    #
    # The measurement itself happened at step 0, before naming. What is left
    # here is describing it on the outcome: which route fired, which kinds
    # were produced, what was warned about, and how much was served from
    # cache -- the fields `organize triage` prints and the idempotency tests
    # assert against.
    _describe_collected_evidence(collected, outcome=outcome, report=report)

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


def _collect_file_evidence(
    config: IngestConfig,
    ledger: LocalLedger,
    path: Path,
    *,
    sha256: str,
    mime: str | None,
    facts: MediaFacts,
    screenshot_by_name: bool,
    size: int,
    now: str,
    report: SourceReport,
) -> FileEvidence | None:
    """Perceive one file and return the result, or `None` if it could not be.

    Failure here never costs a naming decision. The whole stage is wrapped --
    a backend that crashes, a ledger that is locked, a PDF that is a lie, an
    `evidence.enabled: false` config -- and every one of those outcomes is the
    same one: `None`, plus (for a real failure) a line in the run's errors.
    Naming then proceeds on measured facts alone, exactly as it did before
    D-057. That is what "evidence unavailability must never block naming"
    means as code rather than as a promise: there is no path out of this
    function that raises, and no caller branch that stops.
    """
    if not config.evidence.enabled:
        return None
    try:
        return collect_evidence(
            source=EvidenceSource(
                path=path,
                sha256=sha256,
                mime=mime,
                media_kind=facts.media_kind,
                size=size,
            ),
            facts=facts.facts,
            screenshot_by_name=screenshot_by_name,
            config=config.evidence,
            now=now,
            ledger=ledger,
        )
    except Exception as exc:  # noqa: BLE001 - perception never costs a decision
        report.errors.append(f"{path.name}: evidence: {type(exc).__name__}: {exc}")
        return None


def _describe_collected_evidence(
    collected: FileEvidence | None,
    *,
    outcome: FileOutcome,
    report: SourceReport,
) -> None:
    """Copy what was perceived onto the run's report. Decides nothing."""
    if collected is None:
        return
    kinds = tuple(sorted({record.kind for record in collected.provenance}))
    outcome.evidence_route = collected.route.rules
    outcome.evidence_kinds = kinds
    outcome.evidence_warnings = collected.warning_codes()
    outcome.evidence_cache_hits = sum(1 for record in collected.provenance if record.cache_hit)
    if kinds:
        report.evidence_collected += 1
    report.evidence_cache_hits += outcome.evidence_cache_hits


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
