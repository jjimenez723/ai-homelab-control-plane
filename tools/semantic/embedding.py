"""Content-addressed local embeddings and deterministic similarity helpers.

The Phase 3 baseline intentionally has no online dependency. It uses a small
hashing vectorizer whose identity is versioned like a model. The catalog API
is ready for sqlite-vec later, but the current vector layer remains inside
SQLite and joins results back to stable ``asset_id`` values.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

MODEL_ID = "angel-hash-embedding"
MODEL_VERSION = "1.0.0"
SCHEMA_VERSION = "1"
DIMENSIONS = 128
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")


@dataclass(frozen=True)
class EmbeddingSpec:
    model_id: str = MODEL_ID
    model_version: str = MODEL_VERSION
    model_digest: str | None = "builtin-sha256-token-hash-v1"
    schema_version: str = SCHEMA_VERSION
    dimensions: int = DIMENSIONS


def input_sha256(text: str, *, spec: EmbeddingSpec = EmbeddingSpec()) -> str:
    return hashlib.sha256((spec.schema_version + "\x1f" + text).encode("utf-8")).hexdigest()


def embed_text(text: str, *, spec: EmbeddingSpec = EmbeddingSpec()) -> tuple[float, ...]:
    values = [0.0] * spec.dimensions
    tokens = _TOKEN_RE.findall(text.casefold())
    for token in tokens:
        digest = hashlib.sha256((spec.model_version + "\x1f" + token).encode()).digest()
        index = int.from_bytes(digest[:4], "big") % spec.dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        values[index] += sign
        # Add a little phrase sensitivity without introducing a dependency.
        if len(token) > 3:
            for start in range(len(token) - 2):
                gram = token[start:start + 3]
                g = hashlib.sha256((spec.model_version + "\x1e" + gram).encode()).digest()
                values[int.from_bytes(g[:4], "big") % spec.dimensions] += sign * 0.15
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return tuple(0.0 for _ in values)
    return tuple(value / norm for value in values)


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    return sum(a * b for a, b in zip(left, right))
