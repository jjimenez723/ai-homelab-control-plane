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


def short_hash(value: str, length: int = 8) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
