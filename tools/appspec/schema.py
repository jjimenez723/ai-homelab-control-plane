"""Loads and caches the canonical application-spec schema.

`templates/app-spec/schema-v1.json` is the single source of truth for the
shape described in `docs/28-application-spec.md`. This module's only job is
finding it, parsing it once, and handing back the same dict to every
caller -- `tools/appspec/validate.py`, `cli.py`, and the tests all go
through here rather than each hard-coding a path.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "templates" / "app-spec" / "schema-v1.json"
SCHEMA_VERSION = "1.0"


class SchemaNotFound(FileNotFoundError):
    """The canonical schema file is missing from its expected location."""


@lru_cache(maxsize=1)
def load_schema() -> dict:
    """Return the parsed schema document, parsed once and cached."""
    if not SCHEMA_PATH.exists():
        raise SchemaNotFound(f"expected schema at {SCHEMA_PATH}")
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)
