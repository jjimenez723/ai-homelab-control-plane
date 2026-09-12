"""The one deterministic transactional move primitive.

Every destructive ingestion path (`organize desktop/documents/downloads/sd
--apply`) routes through `transfer_file`. No AI/LLM code calls this directly
or is ever given the authority to unlink a source path -- see D-016.

State machine (persisted per-asset so retries resume instead of restarting):

    pending -> copying -> copied -> verified -> committed -> source_unlinked

A crash, unplug, hash mismatch, or database failure at any point before the
`committed` catalog write leaves the source file untouched. `transfer_file`
never raises for expected failure modes; it returns a `TransferOutcome` so
callers can aggregate results without a crashed batch losing earlier work.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tools.ingest.catalog import (
    STATE_COMMITTED,
    STATE_COPYING,
    STATE_FAILED_ERROR,
    STATE_FAILED_HASH_MISMATCH,
    STATE_NEEDS_REVIEW,
    STATE_PENDING,
    STATE_SOURCE_UNLINKED,
    STATE_VERIFIED,
    Catalog,
    now_iso,
)
from tools.ingest.exclusions import is_settled
from tools.ingest.hashing import CHUNK_SIZE, sha256_file
from tools.ingest.identity import asset_id_for, source_ref_id_for
from tools.ingest.t7 import ResolvedT7


@dataclass
class TransferFaultHooks:
    """Injection points for fault-injection tests. All default to no-op."""

    mid_copy: Callable[[int, int], None] | None = None
    after_copy: Callable[[Path], None] | None = None
    after_dest_hash: Callable[[str], None] | None = None
    before_verify_commit: Callable[[], None] | None = None
    before_finalize_rename: Callable[[], None] | None = None
    before_final_commit: Callable[[], None] | None = None
    before_unlink: Callable[[], None] | None = None


@dataclass
class PlannedTransfer:
    source_path: Path
    rel_path: str
    source_volume_id: str
    source_class: str


@dataclass
class TransferOutcome:
    asset_id: str
    state: str
    success: bool
    final_path: Path | None = None
    error_message: str | None = None
    skipped_reason: str | None = None
    source_unlink_failed: bool = False


def _copy_with_fsync(source_path: Path, staging_path: Path, hooks: TransferFaultHooks) -> None:
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    total = source_path.stat().st_size
    written = 0
    with open(source_path, "rb") as src, open(staging_path, "wb") as dst:
        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            dst.write(chunk)
            written += len(chunk)
            if hooks.mid_copy is not None:
                hooks.mid_copy(written, total)
        dst.flush()
        os.fsync(dst.fileno())


def _resolve_final_path(catalog: Catalog, asset_id: str, inbox_dir: Path, source_class: str, rel_path: str, source_sha256: str) -> Path:
    base = inbox_dir / source_class / rel_path
    if not base.exists():
        return base

    owner_asset_id = catalog.get_asset_id_by_final_path(str(base))
    if owner_asset_id == asset_id:
        return base  # this asset already owns this path (resuming a prior attempt)

    if owner_asset_id is None:
        # No catalog row claims this path -- a foreign, pre-existing file.
        # Content equality here means it's a genuine duplicate, safe to reuse.
        try:
            if base.is_file() and sha256_file(base) == source_sha256:
                return base
        except OSError:
            pass

    # A different asset already owns this path (possibly from a different
    # source volume with the same relative path), or content differs:
    # disambiguate using this asset's own identity, not just content, so two
    # unrelated assets never end up sharing one final path.
    return base.parent / f"{base.stem}__{asset_id[:8]}{base.suffix}"


def _staging_path(resolved_t7: ResolvedT7, asset_id: str) -> Path:
    return resolved_t7.staging_dir / f"{asset_id}.partial"


def _fsync_dir(directory: Path) -> None:
    """Flush a directory entry so a completed rename survives power loss.

    Without this the catalog can reach `committed` while the rename itself is
    still only in the page cache -- and a `committed` row is what authorises
    deleting the source on the next run.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def source_is_the_recorded_file(source_path: Path, expected_sha256: str | None) -> bool:
    """Whether the file at the source path is the one this row describes.

    Asset identity includes the verified content hash, but the pathname may
    still be replaced after that hash was sampled. Verifying the destination
    proves the recorded bytes arrived; it says nothing about what is sitting
    at the source path now.

    Without this, a single failed unlink (a locked file, a read-only card) was
    enough: the next run saw a `committed` row, confirmed the destination still
    held the *first* file, and deleted the *second* one, which had never been
    copied anywhere.
    """
    if not expected_sha256:
        return False
    try:
        if not source_path.is_file():
            return False
        return sha256_file(source_path) == expected_sha256
    except OSError:
        return False


def destination_holds(final_path: Path, expected_sha256: str | None) -> bool:
    """Whether `final_path` provably contains the bytes we recorded.

    Existence is not evidence. A resumed transfer reaches the source-unlink
    step on the strength of this answer, so it hashes the file rather than
    trusting either the catalog row or the presence of *a* file at the path --
    the destination may have been deleted, truncated, or replaced by something
    unrelated since the row was written.
    """
    if not expected_sha256:
        return False
    try:
        if not final_path.is_file():
            return False
        return sha256_file(final_path) == expected_sha256
    except OSError:
        return False


def _stat_fingerprint(source_path: Path) -> tuple[int, int, int, int]:
    stat = source_path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _missing_source_outcome(
    catalog: Catalog,
    planned: PlannedTransfer,
    resolved_t7: ResolvedT7,
    hooks: TransferFaultHooks,
) -> TransferOutcome:
    """Reconcile a retry when there are no bytes left at the source path.

    Path lookup is allowed only in this branch because there is nothing to hash.
    It can finish bookkeeping for the latest known occurrence, but it never
    authorizes deleting another file.
    """
    existing = catalog.get_latest_asset_for_source(
        planned.source_volume_id, planned.rel_path
    )
    if existing is None:
        return TransferOutcome(
            asset_id=source_ref_id_for(planned.source_volume_id, planned.rel_path),
            state=STATE_FAILED_ERROR,
            success=False,
            skipped_reason="source_vanished",
        )

    asset_id = existing.row["asset_id"]
    state = existing.row["transfer_state"]
    expected_sha256 = existing.row["sha256"]
    final_path = Path(existing.row["final_path"]) if existing.row["final_path"] else None
    staging_path = _staging_path(resolved_t7, asset_id)

    if state == STATE_SOURCE_UNLINKED:
        return TransferOutcome(
            asset_id=asset_id,
            state=STATE_SOURCE_UNLINKED,
            success=True,
            final_path=final_path,
            skipped_reason="already_completed",
        )

    if state == STATE_COMMITTED:
        if final_path is not None and destination_holds(final_path, expected_sha256):
            return _finish_unlink(
                catalog,
                asset_id,
                str(final_path),
                planned.source_path,
                resolved_t7,
                hooks,
                expected_sha256=expected_sha256,
            )
        catalog.update_asset(
            asset_id,
            transfer_state=STATE_NEEDS_REVIEW,
            error_state="destination_lost",
            error_message=f"committed asset no longer verifiable at {existing.row['final_path']}",
        )
        return TransferOutcome(
            asset_id=asset_id,
            state=STATE_NEEDS_REVIEW,
            success=False,
            final_path=final_path,
            error_message="committed destination is missing or altered and the source is gone",
        )

    if state == STATE_VERIFIED:
        if staging_path.exists():
            try:
                if sha256_file(staging_path) == expected_sha256:
                    if final_path is None:
                        raise ValueError("verified asset has no final_path")
                    return _finalize_and_commit(
                        catalog,
                        asset_id,
                        staging_path,
                        final_path,
                        planned.source_path,
                        resolved_t7,
                        hooks,
                        expected_sha256=expected_sha256,
                    )
            except OSError:
                pass
        if final_path is not None and destination_holds(final_path, expected_sha256):
            return _finalize_and_commit(
                catalog,
                asset_id,
                staging_path,
                final_path,
                planned.source_path,
                resolved_t7,
                hooks,
                rename_needed=False,
                expected_sha256=expected_sha256,
            )
        catalog.update_asset(
            asset_id,
            transfer_state=STATE_NEEDS_REVIEW,
            error_state="verified_copy_lost",
            error_message="verified asset has neither a valid staging copy nor destination and the source is gone",
        )
        return TransferOutcome(
            asset_id=asset_id,
            state=STATE_NEEDS_REVIEW,
            success=False,
            final_path=final_path,
            error_message="verified copy is missing or altered and the source is gone",
        )

    return TransferOutcome(
        asset_id=asset_id,
        state=STATE_FAILED_ERROR,
        success=False,
        skipped_reason="source_vanished",
    )


def transfer_file(
    catalog: Catalog,
    planned: PlannedTransfer,
    resolved_t7: ResolvedT7,
    *,
    settle_seconds: int,
    hooks: TransferFaultHooks | None = None,
) -> TransferOutcome:
    hooks = hooks or TransferFaultHooks()

    if not planned.source_path.exists():
        return _missing_source_outcome(catalog, planned, resolved_t7, hooks)

    if not is_settled(planned.source_path, settle_seconds):
        return TransferOutcome(
            asset_id=source_ref_id_for(planned.source_volume_id, planned.rel_path),
            state=STATE_PENDING,
            success=False,
            skipped_reason="still_changing",
        )

    # Identity is established from bytes, not from a pathname. Stat on both
    # sides of hashing so a writer cannot hand us a hash for a moving target.
    try:
        stat_before = _stat_fingerprint(planned.source_path)
        source_sha256 = sha256_file(planned.source_path)
        stat_after = _stat_fingerprint(planned.source_path)
    except OSError:
        return TransferOutcome(
            asset_id=source_ref_id_for(planned.source_volume_id, planned.rel_path),
            state=STATE_FAILED_ERROR,
            success=False,
            skipped_reason="source_vanished",
        )

    if stat_before != stat_after:
        return TransferOutcome(
            asset_id=source_ref_id_for(planned.source_volume_id, planned.rel_path),
            state=STATE_PENDING,
            success=False,
            skipped_reason="source_changed_during_hash",
            error_message="source metadata changed while hashing; retry after it settles",
        )

    # Existing pre-migration rows keep their old IDs. If one already describes
    # these exact bytes at this exact source path, resume it rather than fork
    # the history. New rows use the content-aware ID.
    existing = catalog.get_asset_for_source_content(
        planned.source_volume_id, planned.rel_path, source_sha256
    )
    asset_id = (
        existing.row["asset_id"]
        if existing is not None
        else asset_id_for(planned.source_volume_id, planned.rel_path, source_sha256)
    )
    staging_path = _staging_path(resolved_t7, asset_id)

    if existing is not None and existing.row["transfer_state"] == STATE_SOURCE_UNLINKED:
        final_path = Path(existing.row["final_path"]) if existing.row["final_path"] else None
        if final_path is not None and destination_holds(final_path, source_sha256):
            return _finish_unlink(
                catalog,
                asset_id,
                str(final_path),
                planned.source_path,
                resolved_t7,
                hooks,
                expected_sha256=source_sha256,
                expected_stat=stat_after,
            )
        # The archived copy disappeared or changed. The source still exists, so
        # repair the archive from it rather than deleting or parking it.

    if existing is not None and existing.row["transfer_state"] == STATE_COMMITTED:
        final_path = Path(existing.row["final_path"]) if existing.row["final_path"] else None
        if final_path is not None and destination_holds(final_path, source_sha256):
            return _finish_unlink(
                catalog,
                asset_id,
                str(final_path),
                planned.source_path,
                resolved_t7,
                hooks,
                expected_sha256=source_sha256,
                expected_stat=stat_after,
            )
        # Destination is no longer trustworthy but the source survives: redo.

    if existing is not None and existing.row["transfer_state"] == STATE_VERIFIED:
        final_path = Path(existing.row["final_path"]) if existing.row["final_path"] else None
        if final_path is not None and staging_path.exists():
            try:
                if sha256_file(staging_path) == source_sha256:
                    return _finalize_and_commit(
                        catalog,
                        asset_id,
                        staging_path,
                        final_path,
                        planned.source_path,
                        resolved_t7,
                        hooks,
                        expected_sha256=source_sha256,
                        expected_stat=stat_after,
                    )
            except OSError:
                pass
        if final_path is not None and destination_holds(final_path, source_sha256):
            return _finalize_and_commit(
                catalog,
                asset_id,
                staging_path,
                final_path,
                planned.source_path,
                resolved_t7,
                hooks,
                rename_needed=False,
                expected_sha256=source_sha256,
                expected_stat=stat_after,
            )
        # Neither persisted copy is trustworthy. Source survives, so recopy.

    stat_size = stat_after[2]
    catalog.ensure_pending_asset(
        asset_id,
        source_volume_id=planned.source_volume_id,
        source_class=planned.source_class,
        original_path=planned.rel_path,
        original_filename=planned.source_path.name,
        size=stat_size,
    )

    mime, _ = mimetypes.guess_type(planned.source_path.name)
    catalog.upsert_blob(source_sha256, size=stat_size, mime=mime)
    catalog.update_asset(
        asset_id,
        sha256=source_sha256,
        size=stat_size,
        mime=mime,
        transfer_started_at=now_iso(),
        transfer_state=STATE_COPYING,
        error_state=None,
        error_message=None,
    )

    try:
        _copy_with_fsync(planned.source_path, staging_path, hooks)
    except OSError as exc:
        catalog.update_asset(
            asset_id,
            transfer_state=STATE_FAILED_ERROR,
            error_state="copy_failed",
            error_message=str(exc),
        )
        return TransferOutcome(
            asset_id=asset_id,
            state=STATE_FAILED_ERROR,
            success=False,
            error_message=str(exc),
        )

    if hooks.after_copy is not None:
        hooks.after_copy(staging_path)
    catalog.update_asset(asset_id, transferred_at=now_iso(), transfer_state="copied")

    try:
        dest_sha256 = sha256_file(staging_path)
    except OSError as exc:
        catalog.update_asset(
            asset_id,
            transfer_state=STATE_FAILED_ERROR,
            error_state="dest_hash_failed",
            error_message=str(exc),
        )
        return TransferOutcome(
            asset_id=asset_id,
            state=STATE_FAILED_ERROR,
            success=False,
            error_message=str(exc),
        )

    if hooks.after_dest_hash is not None:
        hooks.after_dest_hash(dest_sha256)

    if dest_sha256 != source_sha256:
        catalog.update_asset(
            asset_id,
            transfer_state=STATE_FAILED_HASH_MISMATCH,
            error_state="hash_mismatch",
            error_message=f"source={source_sha256} dest={dest_sha256}",
        )
        return TransferOutcome(
            asset_id=asset_id,
            state=STATE_FAILED_HASH_MISMATCH,
            success=False,
            error_message="destination hash did not match source after copy",
        )

    final_path = _resolve_final_path(
        catalog,
        asset_id,
        resolved_t7.inbox_dir,
        planned.source_class,
        planned.rel_path,
        source_sha256,
    )

    try:
        if hooks.before_verify_commit is not None:
            hooks.before_verify_commit()
        catalog.update_asset(
            asset_id,
            verified_at=now_iso(),
            transfer_state=STATE_VERIFIED,
            final_path=str(final_path),
            final_filename=final_path.name,
        )
    except Exception as exc:  # noqa: BLE001 - any DB failure here must not touch source
        return TransferOutcome(
            asset_id=asset_id,
            state=STATE_COPYING,
            success=False,
            error_message=f"catalog write failed before commit: {exc}",
        )

    return _finalize_and_commit(
        catalog,
        asset_id,
        staging_path,
        final_path,
        planned.source_path,
        resolved_t7,
        hooks,
        expected_sha256=source_sha256,
        expected_stat=stat_after,
    )


def _finalize_and_commit(
    catalog: Catalog,
    asset_id: str,
    staging_path: Path,
    final_path: Path,
    source_path: Path,
    resolved_t7: ResolvedT7,
    hooks: TransferFaultHooks,
    *,
    rename_needed: bool = True,
    expected_sha256: str | None = None,
    expected_stat: tuple[int, int, int, int] | None = None,
) -> TransferOutcome:
    if rename_needed:
        try:
            if hooks.before_finalize_rename is not None:
                hooks.before_finalize_rename()
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_path, final_path)
            _fsync_dir(final_path.parent)
        except OSError as exc:
            catalog.update_asset(asset_id, transfer_state=STATE_VERIFIED, error_state="finalize_failed", error_message=str(exc))
            return TransferOutcome(asset_id=asset_id, state=STATE_VERIFIED, success=False, error_message=str(exc))

    try:
        if hooks.before_final_commit is not None:
            hooks.before_final_commit()
        catalog.update_asset(asset_id, transfer_state=STATE_COMMITTED)
    except Exception as exc:  # noqa: BLE001 - destination is verified+renamed; only bookkeeping is outstanding
        return TransferOutcome(asset_id=asset_id, state=STATE_VERIFIED, success=False, final_path=final_path, error_message=f"catalog commit failed: {exc}")

    catalog.write_sidecar(asset_id, resolved_t7.sidecars_dir)
    return _finish_unlink(
        catalog, asset_id, str(final_path), source_path, resolved_t7, hooks,
        expected_sha256=expected_sha256, expected_stat=expected_stat,
    )


def _refuse_source_mismatch(
    catalog: Catalog,
    asset_id: str,
    final_path: Path,
    source_path: Path,
    reason: str,
) -> TransferOutcome:
    """Stop rather than choose between two destructive options.

    Deleting the source would destroy a file that was never copied; overwriting
    the destination would destroy one that was. Neither is ours to pick, so the
    asset is parked for a human with both paths named. A file waiting on a
    decision costs a glance; a file that no longer exists costs everything.
    """
    message = f"{reason}: source={source_path} destination={final_path}"
    catalog.update_asset(
        asset_id,
        transfer_state=STATE_NEEDS_REVIEW,
        error_state="source_identity_mismatch",
        error_message=message,
    )
    return TransferOutcome(
        asset_id=asset_id, state=STATE_NEEDS_REVIEW, success=False,
        final_path=final_path, error_message=message,
    )


def _finish_unlink(
    catalog: Catalog,
    asset_id: str,
    final_path_str: str,
    source_path: Path,
    resolved_t7: ResolvedT7,
    hooks: TransferFaultHooks,
    *,
    expected_sha256: str | None = None,
    expected_stat: tuple[int, int] | None = None,
) -> TransferOutcome:
    """Delete the source, having proved it is the file we actually copied.

    This is the only function in the system that removes a user's file, so it
    re-proves the precondition itself rather than trusting whichever branch
    called it. Two callers previously reached here having verified only the
    *destination* -- which says nothing about what is sitting at the source
    path now. A single failed unlink is enough to set that up: the row stays
    `committed`, the owner moves on, and a different file later takes the same
    name.

    `expected_stat` is `(device, inode, size, mtime_ns)` sampled around the
    source hash. Re-checking it catches both an in-place writer and a different
    file being swapped into the pathname. The content hash is still checked
    immediately before deletion because metadata alone is never identity.
    """
    final_path = Path(final_path_str)

    if expected_sha256 is None or not destination_holds(final_path, expected_sha256):
        return _refuse_source_mismatch(
            catalog,
            asset_id,
            final_path,
            source_path,
            "the destination no longer contains the verified bytes",
        )

    if source_path.exists():
        # Cheap check first, and it also gives the more useful diagnosis: size
        # or mtime moving means something wrote to this file after we read it,
        # which is a different problem from an unrelated file taking the name.
        if expected_stat is not None:
            try:
                if _stat_fingerprint(source_path) != expected_stat:
                    return _refuse_source_mismatch(
                        catalog, asset_id, final_path, source_path,
                        "the source file changed after it was read; a writer may still hold it open",
                    )
            except OSError:
                pass
        if expected_sha256 is not None and not source_is_the_recorded_file(source_path, expected_sha256):
            return _refuse_source_mismatch(
                catalog, asset_id, final_path, source_path,
                "the file at the source path is not the one that was copied",
            )

    try:
        if hooks.before_unlink is not None:
            hooks.before_unlink()
        source_path.unlink()
    except FileNotFoundError:
        pass  # source already gone (a previous run's unlink succeeded before its own commit landed)
    except OSError as exc:
        catalog.update_asset(asset_id, error_state="source_unlink_failed", error_message=str(exc))
        return TransferOutcome(
            asset_id=asset_id, state=STATE_COMMITTED, success=True, final_path=final_path,
            source_unlink_failed=True, error_message=str(exc),
        )

    catalog.update_asset(asset_id, completed_at=now_iso(), transfer_state=STATE_SOURCE_UNLINKED, error_state=None, error_message=None)
    catalog.write_sidecar(asset_id, resolved_t7.sidecars_dir)
    return TransferOutcome(asset_id=asset_id, state=STATE_SOURCE_UNLINKED, success=True, final_path=final_path)
