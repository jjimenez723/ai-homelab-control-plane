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
from tools.ingest.identity import asset_id_for
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

    Asset identity is (source volume, relative path), so one row covers that
    path *over time* -- and `invoice.pdf` downloaded twice is two unrelated
    files sharing it. Verifying the destination proves the recorded bytes
    arrived; it says nothing about what is sitting at the source now.

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


def transfer_file(
    catalog: Catalog,
    planned: PlannedTransfer,
    resolved_t7: ResolvedT7,
    *,
    settle_seconds: int,
    hooks: TransferFaultHooks | None = None,
) -> TransferOutcome:
    hooks = hooks or TransferFaultHooks()
    asset_id = asset_id_for(planned.source_volume_id, planned.rel_path)
    staging_path = _staging_path(resolved_t7, asset_id)
    existing = catalog.get_asset(asset_id)

    if existing is not None and existing.row["transfer_state"] == STATE_SOURCE_UNLINKED:
        if not planned.source_path.exists():
            return TransferOutcome(asset_id=asset_id, state=STATE_SOURCE_UNLINKED, success=True, skipped_reason="already_completed")
        # Something is at a path this row says was emptied. Either the same file
        # came back, or -- far more likely -- an unrelated file now has that
        # name. Claiming success would leave a file that was never copied
        # neither archived nor reported.
        if source_is_the_recorded_file(planned.source_path, existing.row["sha256"]):
            return _finish_unlink(catalog, asset_id, existing.row["final_path"], planned.source_path, resolved_t7, hooks)
        return _refuse_identity_collision(catalog, asset_id, existing, planned)

    if existing is not None and existing.row["transfer_state"] == STATE_COMMITTED:
        # A prior run copied and committed but could not unlink (read-only card,
        # locked file). Two separate things have to be true before finishing
        # that unlink: the destination still holds the exact bytes, *and* the
        # file at the source path is the one they came from.
        final_path = Path(existing.row["final_path"]) if existing.row["final_path"] else None
        if final_path is not None and destination_holds(final_path, existing.row["sha256"]):
            if source_is_the_recorded_file(planned.source_path, existing.row["sha256"]):
                return _finish_unlink(catalog, asset_id, str(final_path), planned.source_path, resolved_t7, hooks)
            if planned.source_path.exists():
                return _refuse_identity_collision(catalog, asset_id, existing, planned)
            # Source already gone and the destination verified: the previous
            # run's unlink did land, only its bookkeeping did not.
            return _finish_unlink(catalog, asset_id, str(final_path), planned.source_path, resolved_t7, hooks)
        if not planned.source_path.exists():
            # Destination gone *and* source gone: nothing left to recover here,
            # and nothing this function may delete. Surface it loudly.
            catalog.update_asset(
                asset_id,
                transfer_state=STATE_NEEDS_REVIEW,
                error_state="destination_lost",
                error_message=f"committed asset no longer verifiable at {existing.row['final_path']}",
            )
            return TransferOutcome(
                asset_id=asset_id, state=STATE_NEEDS_REVIEW, success=False,
                error_message="committed destination is missing or altered and the source is gone",
            )
        # Source survives: redo the transfer from scratch rather than delete it.

    if existing is not None and existing.row["transfer_state"] == STATE_VERIFIED:
        final_path = Path(existing.row["final_path"])
        if staging_path.exists():
            try:
                if sha256_file(staging_path) != existing.row["sha256"]:
                    # corrupted partial from an interrupted prior attempt; redo the copy.
                    pass
                else:
                    return _finalize_and_commit(catalog, asset_id, staging_path, final_path, planned.source_path, resolved_t7, hooks)
            except OSError:
                pass
        elif destination_holds(final_path, existing.row["sha256"]):
            # The rename did land before the crash; only the DB commit is
            # outstanding. Existence alone would not be enough: an unrelated
            # file at this path would otherwise authorise deleting the source.
            return _finalize_and_commit(catalog, asset_id, staging_path, final_path, planned.source_path, resolved_t7, hooks, rename_needed=False)
        # neither a valid partial nor a verified destination exists: source is still safe, redo from scratch.

    if not planned.source_path.exists():
        return TransferOutcome(asset_id=asset_id, state=STATE_FAILED_ERROR, success=False, skipped_reason="source_vanished")

    if not is_settled(planned.source_path, settle_seconds):
        return TransferOutcome(asset_id=asset_id, state=STATE_PENDING, success=False, skipped_reason="still_changing")

    try:
        stat = planned.source_path.stat()
    except OSError:
        # Vanished in the window between the exists() check above and here
        # (unplugged card, concurrent delete) -- source was never touched.
        return TransferOutcome(asset_id=asset_id, state=STATE_FAILED_ERROR, success=False, skipped_reason="source_vanished")
    catalog.ensure_pending_asset(
        asset_id,
        source_volume_id=planned.source_volume_id,
        source_class=planned.source_class,
        original_path=planned.rel_path,
        original_filename=planned.source_path.name,
        size=stat.st_size,
    )

    try:
        source_sha256 = sha256_file(planned.source_path)
    except OSError as exc:
        catalog.update_asset(asset_id, transfer_state=STATE_FAILED_ERROR, error_state="hash_failed", error_message=str(exc))
        return TransferOutcome(asset_id=asset_id, state=STATE_FAILED_ERROR, success=False, error_message=str(exc))

    mime, _ = mimetypes.guess_type(planned.source_path.name)
    catalog.upsert_blob(source_sha256, size=stat.st_size, mime=mime)
    catalog.update_asset(
        asset_id,
        sha256=source_sha256,
        mime=mime,
        transfer_started_at=now_iso(),
        transfer_state=STATE_COPYING,
    )

    try:
        _copy_with_fsync(planned.source_path, staging_path, hooks)
    except OSError as exc:
        catalog.update_asset(asset_id, transfer_state=STATE_FAILED_ERROR, error_state="copy_failed", error_message=str(exc))
        return TransferOutcome(asset_id=asset_id, state=STATE_FAILED_ERROR, success=False, error_message=str(exc))

    if hooks.after_copy is not None:
        hooks.after_copy(staging_path)
    catalog.update_asset(asset_id, transferred_at=now_iso(), transfer_state="copied")

    try:
        dest_sha256 = sha256_file(staging_path)
    except OSError as exc:
        catalog.update_asset(asset_id, transfer_state=STATE_FAILED_ERROR, error_state="dest_hash_failed", error_message=str(exc))
        return TransferOutcome(asset_id=asset_id, state=STATE_FAILED_ERROR, success=False, error_message=str(exc))
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

    final_path = _resolve_final_path(catalog, asset_id, resolved_t7.inbox_dir, planned.source_class, planned.rel_path, source_sha256)

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
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any DB failure here must not touch source
        return TransferOutcome(asset_id=asset_id, state=STATE_COPYING, success=False, error_message=f"catalog write failed before commit: {exc}")

    return _finalize_and_commit(catalog, asset_id, staging_path, final_path, planned.source_path, resolved_t7, hooks)


def _refuse_identity_collision(catalog: Catalog, asset_id: str, existing, planned: PlannedTransfer) -> TransferOutcome:
    """Two different files share one catalog row. Refuse, loudly, and touch nothing.

    Identity is (source volume, relative path), so the schema cannot currently
    hold two files that have occupied the same path. Both available actions are
    destructive -- deleting the new source, or overwriting the archived asset --
    so this does neither and puts the file in front of a person instead.

    Conservative on purpose: the cost is a file that waits for a decision, and
    the alternative cost is a file that no longer exists. Content-addressed
    asset identity is the real fix and needs its own migration.
    """
    message = (
        f"{planned.rel_path} is a different file from the one already recorded at this path "
        f"(recorded sha256 {existing.row['sha256']}). Refusing to touch either. "
        "Rename or move one of them, or resolve the catalog row by hand."
    )
    catalog.update_asset(asset_id, error_state="source_identity_collision", error_message=message)
    return TransferOutcome(
        asset_id=asset_id,
        state=STATE_NEEDS_REVIEW,
        success=False,
        error_message=message,
        skipped_reason="source_identity_collision",
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
    return _finish_unlink(catalog, asset_id, str(final_path), source_path, resolved_t7, hooks)


def _finish_unlink(
    catalog: Catalog,
    asset_id: str,
    final_path_str: str,
    source_path: Path,
    resolved_t7: ResolvedT7,
    hooks: TransferFaultHooks,
) -> TransferOutcome:
    final_path = Path(final_path_str)
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
