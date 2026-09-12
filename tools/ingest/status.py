"""`organize status` and `organize doctor`.

Two different questions, deliberately kept apart:

* **status** -- what does the pipeline currently know, and what is it waiting
  on? Read from the ledger and the last run record. Answers "is anything stuck,
  and how much is queued for me?".
* **doctor** -- is this machine configured such that the pipeline can run
  safely at all? Answers "why is nothing happening?" and "is the thing I think
  is enabled actually enabled?".

Both are strictly read-only. `doctor` in particular reports the macOS
screenshot location *without* adopting it: a source path the tool inferred is a
source path the tool should not be walking, so the owner copies it into the
config deliberately or not at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.ingest.automation import RunPaths, lock_is_held, read_status
from tools.ingest.catalog import Catalog
from tools.ingest.config import IngestConfig
from tools.ingest.ledger import (
    NAMING_STATE_HUMAN,
    NAMING_STATE_NAMED,
    NAMING_STATE_REVIEW,
    RETENTION_IGNORE,
    RETENTION_INGEST,
    RETENTION_REVIEW,
    LocalLedger,
)
from tools.ingest.evidence.raster import pdfium_available
from tools.ingest.media.probe import ffprobe_available, pypdf_available
from tools.ingest.sources import (
    ScreenshotsNotConfigured,
    resolve_screenshots_root,
    screenshot_location_hint,
    screenshots_root_is_shared,
)
from tools.ingest.t7 import resolve_t7
from tools.ingest.volumes import VolumeError, VolumeProber


@dataclass
class SourceStatus:
    source_class: str
    root: str | None
    configured: bool
    reason: str = ""
    discovered: int = 0
    usefully_named: int = 0
    naming_review: int = 0
    human_named: int = 0
    ingest_candidates: int = 0
    retention_review: int = 0
    ignored: int = 0
    already_ingested: int = 0


@dataclass
class StatusData:
    angel_connected: bool
    angel_reason: str
    angel_root: str | None
    volume_uuid_configured: bool
    catalog_healthy: bool
    catalog_note: str
    ledger_present: bool
    lock_held: bool
    automation_enabled: bool
    auto_ingest_enabled: bool
    naming_enabled: bool
    last_run: dict[str, Any] | None
    last_error: dict[str, Any] | None
    sources: list[SourceStatus] = field(default_factory=list)
    duplicate_observations: int = 0


def _source_status(config: IngestConfig, rows: list[dict[str, Any]], source_class: str) -> SourceStatus:
    if source_class == "screenshots":
        try:
            root: str | None = str(resolve_screenshots_root(config))
            configured, reason = True, ""
        except ScreenshotsNotConfigured as exc:
            root, configured, reason = None, False, str(exc)
    else:
        root, configured, reason = str(config.sources.downloads), True, ""

    mine = [row for row in rows if row["source_class"] == source_class]
    return SourceStatus(
        source_class=source_class,
        root=root,
        configured=configured,
        reason=reason,
        discovered=len(mine),
        usefully_named=sum(1 for row in mine if row["naming_state"] == NAMING_STATE_NAMED),
        naming_review=sum(1 for row in mine if row["naming_state"] == NAMING_STATE_REVIEW),
        human_named=sum(1 for row in mine if row["naming_state"] == NAMING_STATE_HUMAN),
        ingest_candidates=sum(1 for row in mine if row["retention_recommendation"] == RETENTION_INGEST),
        retention_review=sum(1 for row in mine if row["retention_recommendation"] == RETENTION_REVIEW),
        ignored=sum(1 for row in mine if row["retention_recommendation"] == RETENTION_IGNORE),
        already_ingested=sum(1 for row in mine if row["ingested_asset_id"]),
    )


def collect_status(
    config: IngestConfig,
    prober: VolumeProber,
    *,
    allow_dev_storage_root: bool = False,
) -> StatusData:
    """Gather everything both reports need. Opens nothing for writing."""
    angel_root: str | None = None
    angel_reason = ""
    catalog_healthy = False
    catalog_note = "Angel not connected"
    try:
        # `for_apply=False` so a status check never scaffolds directories on the
        # T7 as a side effect of being asked a question.
        resolved = resolve_t7(config, prober, for_apply=False, allow_storage_root_override=allow_dev_storage_root)
        angel_root = str(resolved.root)
        connected = True
        if resolved.catalog_path.exists():
            try:
                with Catalog(resolved.catalog_path) as catalog:
                    count = catalog._conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]  # noqa: SLF001
                catalog_healthy = True
                catalog_note = f"healthy, {count} asset row(s)"
            except Exception as exc:  # noqa: BLE001 - a broken catalog is a status finding, not a crash
                catalog_note = f"unreadable: {exc}"
        else:
            catalog_healthy = True
            catalog_note = "not created yet (no --apply run has landed an asset)"
    except VolumeError as exc:
        connected = False
        angel_reason = str(exc)

    rows: list[dict[str, Any]] = []
    duplicates = 0
    ledger_present = config.local_ledger_path.exists()
    if ledger_present:
        with LocalLedger(config.local_ledger_path) as ledger:
            rows = ledger.local_files()
            duplicates = len(ledger.duplicate_observations())
            last_run = ledger.last_run()
            last_error = ledger.last_failed_run()
    else:
        last_run = None
        last_error = None

    paths = RunPaths.for_state_dir(config.state_dir)
    status_payload = read_status(paths)
    if last_run is None and status_payload is not None:
        last_run = status_payload

    return StatusData(
        angel_connected=connected,
        angel_reason=angel_reason,
        angel_root=angel_root,
        volume_uuid_configured=bool(config.t7.volume_uuid),
        catalog_healthy=catalog_healthy,
        catalog_note=catalog_note,
        ledger_present=ledger_present,
        lock_held=lock_is_held(paths),
        automation_enabled=config.automation.enabled,
        auto_ingest_enabled=config.triage.auto_ingest,
        naming_enabled=config.naming.enabled,
        last_run=last_run,
        last_error=last_error,
        sources=[_source_status(config, rows, name) for name in ("downloads", "screenshots")],
        duplicate_observations=duplicates,
    )


def render_status(data: StatusData) -> str:
    lines: list[str] = []
    lines.append(f"Angel connected:     {'yes' if data.angel_connected else 'no'}" + ("" if data.angel_connected else f"  ({data.angel_reason})"))
    lines.append(f"Volume UUID pinned:  {'yes' if data.volume_uuid_configured else 'NO -- set t7.volume_uuid'}")
    lines.append(f"Catalog:             {data.catalog_note}")
    lines.append(f"Ingestion lock:      {'HELD by another run' if data.lock_held else 'free'}")
    lines.append(f"Local ledger:        {'present' if data.ledger_present else 'not created yet'}")
    lines.append(
        f"Automation:          {'enabled' if data.automation_enabled else 'disabled'}"
        f"   auto-ingest: {'on' if data.auto_ingest_enabled else 'off'}"
        f"   naming: {'on' if data.naming_enabled else 'off'}"
    )

    for source in data.sources:
        lines.append("")
        lines.append(f"{source.source_class.title()}:")
        if not source.configured:
            lines.append(f"  not configured -- {source.reason}")
            continue
        lines.append(f"  {source.root}")
        lines.append(f"  {source.discovered} known")
        lines.append(f"  {source.usefully_named} usefully named")
        if source.human_named:
            lines.append(f"  {source.human_named} named by you (never auto-renamed again)")
        lines.append(f"  {source.naming_review} naming review")
        lines.append(f"  {source.ingest_candidates} Angel candidates")
        lines.append(f"  {source.retention_review} retention review")
        lines.append(f"  {source.ignored} ignored for Angel (still on the Mac)")
        lines.append(f"  {source.already_ingested} already ingested")

    lines.append("")
    lines.append(f"Duplicate relationships observed: {data.duplicate_observations} (never acted on automatically)")
    lines.append(f"Last run:   {_describe_run(data.last_run)}")
    lines.append(f"Last error: {_describe_error(data.last_error)}")
    return "\n".join(lines)


def _describe_run(run: dict[str, Any] | None) -> str:
    if not run:
        return "never"
    started = run.get("started_at") or run.get("finished_at") or "unknown time"
    kind = run.get("kind", "triage")
    status = run.get("status", "unknown")
    targets = run.get("targets", "")
    return f"{started}  {kind} {targets} -> {status}"


def _describe_error(run: dict[str, Any] | None) -> str:
    if not run:
        return "none"
    return f"{run.get('finished_at') or run.get('started_at')}  {run.get('error_message') or run.get('status')}"


def _ocr_backend_note(backend: str) -> str:
    """Whether the configured OCR backend can actually run on this machine.

    Reported rather than acted on, in the same spirit as the screenshot
    location: `doctor` answers "is the thing I think is enabled actually
    enabled", and a backend named in config that is not installed is exactly
    the kind of silent gap that question exists to surface.
    """
    if backend == "null":
        return "  (performs no OCR; the safe default)"
    import shutil

    if backend == "tesseract":
        return "" if shutil.which("tesseract") else "  -- NOT INSTALLED; OCR will record its own absence"
    return ""


def render_doctor(config: IngestConfig, data: StatusData) -> str:
    """Configuration checks, phrased as what to do about each one."""
    lines: list[str] = ["Configuration"]
    lines.append(f"  config file:            {config.config_path or 'none (built-in defaults)'}")
    lines.append(f"  t7.volume_uuid:         {'set' if config.t7.volume_uuid else 'NOT SET -- --apply will refuse'}")
    if config.t7.storage_root_override is not None:
        lines.append(
            "  t7.storage_root_override: SET -- this bypasses volume-UUID verification and is "
            "honored only with --allow-dev-storage-root. Remove it on a production machine."
        )
    lines.append(f"  settle_seconds:         {config.settle_seconds} (manual runs)")
    lines.append(
        f"  watcher settle window:  {config.effective_settle_seconds(automated=True)}"
        f" (automated runs; never shorter than downloads_watcher_min_settle_seconds)"
    )

    lines.append("")
    lines.append("Sources")
    lines.append(f"  downloads:              {config.sources.downloads}")
    screenshots = config.sources.screenshots
    if screenshots.enabled and screenshots.path is not None:
        lines.append(f"  screenshots:            {screenshots.path}")
    else:
        lines.append("  screenshots:            not configured -- naming will not run for screenshots")
        hint = screenshot_location_hint()
        if hint:
            lines.append(f"     macOS currently saves screenshots to: {hint}")
            lines.append("     Put that path in sources.screenshots.path if you want it managed.")
            lines.append("     It is never adopted automatically: an inferred source is one nobody chose.")
    lines.append("  desktop/documents:      manual ingestion only; never evaluated automatically")

    lines.append("")
    lines.append("Policy")
    lines.append(f"  naming.enabled:         {config.naming.enabled}")
    lines.append(f"  naming source classes:  {', '.join(sorted(config.naming.source_classes))}")
    lines.append(f"  naming.min_confidence:  {config.naming.min_confidence}")
    lines.append(f"  triage.auto_ingest:     {config.triage.auto_ingest}"
                 f" (nothing is transferred automatically while this is false)")
    lines.append(f"  triage.min_ingest_confidence: {config.triage.min_ingest_confidence}")
    lines.append(f"  automation.enabled:     {config.automation.enabled}"
                 f" (the scheduled agent refuses to run while this is false)")

    lines.append("")
    lines.append("Environment")
    lines.append(f"  Angel:                  {'connected at ' + str(data.angel_root) if data.angel_connected else 'not connected -- ' + data.angel_reason}")
    lines.append(f"  local ledger:           {config.local_ledger_path}")
    lines.append(f"  state directory:        {config.state_dir}")
    lines.append(f"  ffprobe (video facts):  {'available' if ffprobe_available() else 'not installed -- video metadata will be skipped'}")
    lines.append(f"  pypdf (PDF facts):      {'available' if pypdf_available() else 'not installed -- PDF page counts/titles will be skipped'}")

    lines.append("")
    lines.append("Perception (evidence layer -- reads files, decides nothing)")
    evidence = config.evidence
    lines.append(f"  evidence:               {'enabled' if evidence.enabled else 'disabled'}")
    lines.append(
        f"  OCR backend:            {evidence.ocr.backend}"
        f"{'' if evidence.ocr.enabled else '  (disabled in config)'}"
        f"{_ocr_backend_note(evidence.ocr.backend)}"
    )
    lines.append(
        f"  vision backend:         {evidence.vision.backend}"
        f"{'' if evidence.vision.enabled else '  (disabled in config)'}"
    )
    lines.append(
        f"  pypdfium2 (rasterize):  "
        f"{'available' if pdfium_available() else 'not installed -- scanned PDFs cannot be rendered for OCR'}"
    )
    lines.append("  evidence never renames, moves, deletes, or ingests anything (D-029).")

    lines.append("")
    lines.append("Scheduling")
    agent = Path.home() / "Library/LaunchAgents/com.ai-homelab.file-intelligence.plist"
    lines.append(f"  LaunchAgent installed:  {'yes' if agent.exists() else 'no'}  ({agent})")
    lines.append("  install:                scripts/install-file-intelligence-launchd")
    lines.append("  remove:                 scripts/uninstall-file-intelligence-launchd")
    return "\n".join(lines)
