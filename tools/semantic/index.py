"""Catalog-backed semantic embeddings and search."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tools.ingest.catalog import Catalog

from .embedding import EmbeddingSpec, cosine, embed_text, input_sha256


@dataclass(frozen=True)
class SearchResult:
    score: float
    asset_id: str
    client_id: str | None
    project_id: str | None
    filename: str | None
    path: str | None
    tags: tuple[str, ...]
    mime: str | None
    size: int | None


def semantic_text(*, filename: str, client_id: str | None, project_id: str | None,
                  subject: str | None, tags: tuple[str, ...], evidence_summary: str = "") -> str:
    return " ".join(
        value for value in (
            filename, client_id or "", project_id or "", subject or "",
            " ".join(tags), evidence_summary[:1800],
        ) if value
    )


def ensure_asset_embedding(catalog: Catalog, asset_id: str, text: str, *, spec: EmbeddingSpec = EmbeddingSpec()) -> dict[str, Any]:
    asset = catalog.get_asset(asset_id)
    if asset is None:
        raise KeyError(asset_id)
    content_hash = asset.row["sha256"] or ""
    digest = input_sha256(text, spec=spec)
    cached = catalog.get_embedding(
        asset_id, input_sha256=digest, model_id=spec.model_id,
        model_version=spec.model_version, schema_version=spec.schema_version,
    )
    if cached is not None:
        return {**cached, "cache_hit": True}
    vector = embed_text(text, spec=spec)
    embedding_id = catalog.record_embedding(
        asset_id, content_sha256=content_hash, input_sha256=digest,
        model_id=spec.model_id, model_version=spec.model_version,
        model_digest=spec.model_digest, schema_version=spec.schema_version,
        vector=vector,
    )
    return {
        "embedding_id": embedding_id,
        "asset_id": asset_id,
        "input_sha256": digest,
        "model_id": spec.model_id,
        "model_version": spec.model_version,
        "schema_version": spec.schema_version,
        "vector": vector,
        "cache_hit": False,
    }


def search_catalog(catalog: Catalog, query: str, *, limit: int = 20, spec: EmbeddingSpec = EmbeddingSpec(),
                   client_id: str | None = None, project_id: str | None = None,
                   tag: str | None = None, media_type: str | None = None,
                   date_from: str | None = None, date_to: str | None = None,
                   orientation: str | None = None) -> list[SearchResult]:
    query_vector = embed_text(query, spec=spec)
    results: list[SearchResult] = []
    for row in catalog.embedding_rows(model_id=spec.model_id, model_version=spec.model_version, schema_version=spec.schema_version):
        semantic = catalog.latest_semantic_classification(row["asset_id"])
        if semantic is None:
            continue
        tags = tuple(semantic.get("tags") or ())
        if client_id and semantic.get("client_id") != client_id:
            continue
        if project_id and semantic.get("project_id") != project_id:
            continue
        if tag and tag.lower() not in tags:
            continue
        if media_type and not (row.get("mime") or "").lower().startswith(media_type.lower()):
            continue
        project = str(semantic.get("project_id") or "")
        date_token = project[:10] if len(project) >= 10 and project[4:5] == "-" and project[7:8] == "-" else ""
        if date_from and (not date_token or date_token < date_from):
            continue
        if date_to and (not date_token or date_token > date_to):
            continue
        if orientation:
            metadata = semantic.get("metadata") or {}
            if metadata.get("orientation") != orientation:
                continue
        vector = tuple(json.loads(row["vector_json"]))
        results.append(SearchResult(
            score=cosine(query_vector, vector),
            asset_id=row["asset_id"],
            client_id=semantic.get("client_id"),
            project_id=semantic.get("project_id"),
            filename=row.get("final_filename"),
            path=row.get("final_path"),
            tags=tags,
            mime=row.get("mime"),
            size=row.get("size"),
        ))
    results.sort(key=lambda item: (-item.score, item.asset_id))
    return results[:max(0, limit)]
