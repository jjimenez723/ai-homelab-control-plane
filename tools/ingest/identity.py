"""Stable identity helpers.

A pathname is an observation, not an identity. Ingested asset IDs therefore
include the verified content hash as well as source provenance. Reusing the
same pathname for different bytes creates a different asset row, while exact
duplicates remain related by the shared `blobs.sha256` content identity.

Mac-side pre-ingest identity still uses (volume, device, inode) because it has
to survive a local rename before the content is committed to Angel.
"""

from __future__ import annotations

import hashlib
import uuid

# Fixed, arbitrary namespace for this project's asset IDs (uuid5 requires one).
ASSET_NAMESPACE = uuid.UUID("d3c6a8b0-4b8b-4f2a-9c1a-1f7e3a2b9c11")


def asset_id_for(volume_id: str, original_path: str, sha256: str) -> str:
    """Deterministic identity for one source occurrence of verified bytes.

    Two files that occupy the same path at different times get different IDs
    when their bytes differ. Two byte-identical files at different paths keep
    distinct provenance rows but point at the same `blobs.sha256` content.
    """
    key = f"asset:{volume_id}:{original_path}:{sha256}"
    return str(uuid.uuid5(ASSET_NAMESPACE, key))


def source_ref_id_for(volume_id: str, original_path: str) -> str:
    """Non-persistent ID for an attempt whose content could not be hashed.

    This is only for reporting failures before an asset can exist. It must
    never be used as a catalog asset ID.
    """
    return str(uuid.uuid5(ASSET_NAMESPACE, f"source-ref:{volume_id}:{original_path}"))


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
