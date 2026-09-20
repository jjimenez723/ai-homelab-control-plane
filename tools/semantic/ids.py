"""Stable IDs for Phase 3 append-only records."""

from __future__ import annotations

import uuid

from tools.ingest.identity import ASSET_NAMESPACE


def semantic_classification_id_for(asset_id: str, sequence: int) -> str:
    return str(uuid.uuid5(ASSET_NAMESPACE, f"semantic-classification:{asset_id}:{sequence}"))


def semantic_correction_id_for(asset_id: str, field: str, sequence: int) -> str:
    return str(uuid.uuid5(ASSET_NAMESPACE, f"semantic-correction:{asset_id}:{field}:{sequence}"))


def embedding_id_for(asset_id: str, input_sha256: str, model_id: str, model_version: str, schema_version: str) -> str:
    return str(uuid.uuid5(ASSET_NAMESPACE, f"embedding:{asset_id}:{input_sha256}:{model_id}:{model_version}:{schema_version}"))
