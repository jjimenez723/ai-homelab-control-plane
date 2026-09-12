"""Stable identity helpers.

Asset IDs are derived deterministically from (source volume UUID, original
relative path) so that re-running `organize` against the same pending file
always yields the same catalog row -- this is what makes retries, resumes,
and duplicate invocations idempotent instead of creating new rows.
"""

from __future__ import annotations

import hashlib
import uuid

# Fixed, arbitrary namespace for this project's asset IDs (uuid5 requires one).
ASSET_NAMESPACE = uuid.UUID("d3c6a8b0-4b8b-4f2a-9c1a-1f7e3a2b9c11")


def asset_id_for(volume_id: str, original_path: str) -> str:
    """Deterministic asset ID for a physical file at a given source location."""
    key = f"{volume_id}:{original_path}"
    return str(uuid.uuid5(ASSET_NAMESPACE, key))


def derivative_asset_id_for(parent_asset_id: str, kind: str) -> str:
    """Deterministic asset ID for a derivative of a given parent asset."""
    return str(uuid.uuid5(ASSET_NAMESPACE, f"derivative:{kind}:{parent_asset_id}"))


def local_file_id_for(volume_id: str, device: int, inode: int) -> str:
    """Identity for a Mac-side file that has *not* been ingested yet.

    Deliberately not derived from the path: naming renames files, and a
    path-derived identity would make every rename look like a brand-new file,
    losing the audit lineage and the human-correction lock along with it.

    (device, inode) is the property that actually survives a rename on APFS.
    Inode numbers can be reused after a delete, so callers additionally compare
    the recorded content hash before inheriting a lineage -- see
    `LocalLedger.observe_file`.
    """
    return str(uuid.uuid5(ASSET_NAMESPACE, f"local:{volume_id}:{device}:{inode}"))


def naming_id_for(local_file_id: str, sequence: int) -> str:
    """Identity for one row of a file's naming lineage."""
    return str(uuid.uuid5(ASSET_NAMESPACE, f"naming:{local_file_id}:{sequence}"))


def classification_run_id_for(local_file_id: str, sequence: int) -> str:
    """Identity for one retention/classification evaluation of a file."""
    return str(uuid.uuid5(ASSET_NAMESPACE, f"classification:{local_file_id}:{sequence}"))


def duplicate_observation_id_for(kind: str, left_ref: str, right_ref: str) -> str:
    """Stable ID for one observed duplicate relationship, so re-scans don't
    accumulate a new row for the same pair every run."""
    low, high = sorted((left_ref, right_ref))
    return str(uuid.uuid5(ASSET_NAMESPACE, f"duplicate:{kind}:{low}:{high}"))


def short_hash(value: str, length: int = 8) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
