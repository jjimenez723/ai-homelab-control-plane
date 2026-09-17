"""The development-seed schema and its on-disk store.

A seed is addressed by content hash, never by path (spec section 6/7): moving
or renaming the source file does not lose its human annotations, because
nothing here is keyed on where the file happens to live right now. A
machine-local `path_index.json` remembers the last-seen path purely so the UI
can re-open the file -- it is never treated as part of the portable record.

`split` is hard-pinned to `"development_seed"`. This is a structural
guarantee, not a convention: every seed this module can construct is
supervision/evaluation data for prompt development, never something that
could later be mistaken for clean validation or frozen-holdout evidence
(spec section 13).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from tools.ingest.naming.policy import slugify
from tools.naming_lab.dates import SemanticDateCandidate

SEED_SCHEMA_VERSION = 1
DEVELOPMENT_SEED_SPLIT = "development_seed"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def corpus_id_for(corpus_dir: Path) -> str:
    """A stable, filesystem-safe id for a corpus directory.

    Derived from the resolved absolute path, so pointing the Lab at the same
    folder again reuses the same seed store -- but the id itself never
    appears as portable truth inside a seed record (spec section 7).
    """
    resolved = str(Path(corpus_dir).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    slug = slugify(Path(resolved).name, max_length=40) or "corpus"
    return f"{slug}-{digest}"


@dataclasses.dataclass(frozen=True)
class SeedRecord:
    """One development seed. See `docs/23-naming-lab.md` for the full schema."""

    schema_version: int
    seed_id: str
    sha256: str
    original_filename: str
    media_kind: str
    extension: str
    semantic_date: SemanticDateCandidate | None
    accepted_filenames: tuple[str, ...]
    required_semantics: dict[str, tuple[str, ...]]
    forbidden_semantics: tuple[str, ...]
    human_notes: str | None
    split: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.split != DEVELOPMENT_SEED_SPLIT:
            raise ValueError(
                f"SeedRecord.split must be {DEVELOPMENT_SEED_SPLIT!r}, got {self.split!r} -- "
                "Naming Lab seeds are development supervision, never validation or frozen holdout"
            )
        if len(self.sha256) != 64:
            raise ValueError(f"SeedRecord.sha256 must be a 64-char hex digest, got {self.sha256!r}")

    def with_accepted_filename(self, filename: str) -> "SeedRecord":
        if filename in self.accepted_filenames:
            return self
        return dataclasses.replace(
            self,
            accepted_filenames=self.accepted_filenames + (filename,),
            updated_at=_now_iso(),
        )

    def with_notes(self, notes: str | None) -> "SeedRecord":
        return dataclasses.replace(self, human_notes=notes, updated_at=_now_iso())


def new_seed(
    *,
    sha256: str,
    original_filename: str,
    media_kind: str,
    extension: str,
    semantic_date: SemanticDateCandidate | None = None,
    accepted_filenames: tuple[str, ...] = (),
    required_semantics: dict[str, tuple[str, ...]] | None = None,
    forbidden_semantics: tuple[str, ...] = (),
    human_notes: str | None = None,
) -> SeedRecord:
    now = _now_iso()
    return SeedRecord(
        schema_version=SEED_SCHEMA_VERSION,
        seed_id=f"seed-{sha256[:12]}",
        sha256=sha256,
        original_filename=original_filename,
        media_kind=media_kind,
        extension=extension,
        semantic_date=semantic_date,
        accepted_filenames=tuple(accepted_filenames),
        required_semantics=dict(required_semantics or {}),
        forbidden_semantics=tuple(forbidden_semantics),
        human_notes=human_notes,
        split=DEVELOPMENT_SEED_SPLIT,
        created_at=now,
        updated_at=now,
    )


def seed_to_json(seed: SeedRecord) -> dict[str, Any]:
    return {
        "schema_version": seed.schema_version,
        "seed_id": seed.seed_id,
        "sha256": seed.sha256,
        "original_filename": seed.original_filename,
        "media_kind": seed.media_kind,
        "extension": seed.extension,
        "semantic_date": dataclasses.asdict(seed.semantic_date) if seed.semantic_date else None,
        "accepted_filenames": list(seed.accepted_filenames),
        "required_semantics": {key: list(values) for key, values in seed.required_semantics.items()},
        "forbidden_semantics": list(seed.forbidden_semantics),
        "human_notes": seed.human_notes,
        "split": seed.split,
        "created_at": seed.created_at,
        "updated_at": seed.updated_at,
    }


def seed_from_json(data: dict[str, Any]) -> SeedRecord:
    semantic_date_raw = data.get("semantic_date")
    semantic_date = SemanticDateCandidate(**semantic_date_raw) if semantic_date_raw else None
    return SeedRecord(
        schema_version=int(data["schema_version"]),
        seed_id=str(data["seed_id"]),
        sha256=str(data["sha256"]),
        original_filename=str(data["original_filename"]),
        media_kind=str(data["media_kind"]),
        extension=str(data["extension"]),
        semantic_date=semantic_date,
        accepted_filenames=tuple(data.get("accepted_filenames") or ()),
        required_semantics={
            key: tuple(values) for key, values in (data.get("required_semantics") or {}).items()
        },
        forbidden_semantics=tuple(data.get("forbidden_semantics") or ()),
        human_notes=data.get("human_notes"),
        split=str(data["split"]),
        created_at=str(data["created_at"]),
        updated_at=str(data["updated_at"]),
    )


class SeedStore:
    """One corpus's seeds on disk, under `.state/naming-lab/<corpus_id>/`.

    Never writes source file bytes anywhere -- only the small JSON record per
    seed and a path index mapping content hash to last-seen local path.
    """

    def __init__(self, corpus_root: Path):
        self.corpus_root = Path(corpus_root)
        self.seeds_dir = self.corpus_root / "seeds"
        self.seeds_dir.mkdir(parents=True, exist_ok=True)
        self._path_index_file = self.corpus_root / "path_index.json"

    def _seed_path(self, sha256: str) -> Path:
        return self.seeds_dir / f"{sha256}.json"

    def get(self, sha256: str) -> SeedRecord | None:
        path = self._seed_path(sha256)
        if not path.exists():
            return None
        return seed_from_json(json.loads(path.read_text(encoding="utf-8")))

    def save(self, seed: SeedRecord) -> SeedRecord:
        seed = dataclasses.replace(seed, updated_at=_now_iso())
        self._seed_path(seed.sha256).write_text(
            json.dumps(seed_to_json(seed), indent=2, sort_keys=True), encoding="utf-8"
        )
        return seed

    def get_or_create(
        self,
        *,
        sha256: str,
        original_filename: str,
        media_kind: str,
        extension: str,
        semantic_date: SemanticDateCandidate | None,
    ) -> SeedRecord:
        existing = self.get(sha256)
        if existing is not None:
            return existing
        seed = new_seed(
            sha256=sha256, original_filename=original_filename,
            media_kind=media_kind, extension=extension, semantic_date=semantic_date,
        )
        return self.save(seed)

    def all(self) -> tuple[SeedRecord, ...]:
        records = [
            seed_from_json(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self.seeds_dir.glob("*.json"))
        ]
        return tuple(records)

    # -- machine-local path index --------------------------------------------
    #
    # Deliberately a separate, un-versioned file: a `sha256 -> path` mapping is
    # only ever valid on the machine that wrote it, and must never be read
    # back as if it were part of the portable seed record.

    def _read_path_index(self) -> dict[str, str]:
        if not self._path_index_file.exists():
            return {}
        return json.loads(self._path_index_file.read_text(encoding="utf-8"))

    def record_local_path(self, sha256: str, path: Path) -> None:
        index = self._read_path_index()
        index[sha256] = str(Path(path).expanduser().resolve())
        self._path_index_file.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")

    def local_path_for(self, sha256: str) -> Path | None:
        raw = self._read_path_index().get(sha256)
        return Path(raw) if raw else None
