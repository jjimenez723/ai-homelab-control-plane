"""Operator-supplied archive vocabulary and deterministic destination routing.

An archive profile is a local YAML file (``archive.profile_path`` in
ingest.yaml) that lists the archive's categories, their filename prefixes, and
the verified folder IDs for each branch. The profile is operator data, not
code: it is never committed, and with no profile configured every category
resolves to ``None`` so analysis falls back to plain review.

This module performs no Drive or network I/O. It only maps a category the
model proposed onto the operator's verified tree, and anything it cannot
verify stays an explicit review state rather than being guessed.

Profile shape::

    root_name: "Archive"          # top-level archive folder
    year: "2026"                  # default year branch
    categories:
      Weddings:
        prefix: WED               # null when no prefix is assigned yet
        archive_folder_name: "1. Weddings"
        folder_id: "<drive folder id>"
        media_folder_ids: {Pics: "<id>", Vids: "<id>"}
        media_folder_names: {Pics: "Pictures"}   # optional live-name variants
        nested_paths:                             # optional sub-branches
          "Spring": {folder_id: "<id>", media_folder_ids: {}}
        keywords: [ceremony, reception]
    aliases:                       # normalized alias -> canonical category
      wedding: Weddings
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import yaml

if TYPE_CHECKING:
    from tools.ingest.config import IngestConfig


@dataclass(frozen=True)
class ArchiveProfile:
    root_name: str = ""
    year: str = ""
    categories: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    aliases: Mapping[str, str] = field(default_factory=dict)

    @property
    def canonical_categories(self) -> tuple[str, ...]:
        return tuple(self.categories)


EMPTY_ARCHIVE_PROFILE = ArchiveProfile()


@dataclass(frozen=True)
class ArchiveDestination:
    root_name: str
    category: str
    year: str
    prefix: str | None
    category_folder_name: str
    category_folder_id: str
    media_folder: str
    media_folder_name: str | None
    media_folder_id: str | None
    nested_path: tuple[str, ...]
    nested_folder_ids: tuple[str, ...]
    status: str
    reason: str | None = None

    @property
    def archive_folder_path(self) -> str:
        parts = (self.root_name, self.year, self.category_folder_name, *self.nested_path)
        return "/".join(part for part in parts if part)

    @property
    def category_relative_path(self) -> str:
        parts = (self.category_folder_name, *self.nested_path)
        if self.media_folder_id and self.media_folder_name:
            parts += (self.media_folder_name,)
        else:
            parts += ("ROOT-REVIEW", self.media_folder)
        return "/".join(parts)

    @property
    def relative_path(self) -> str:
        if self.media_folder_id and self.media_folder_name:
            return f"{self.archive_folder_path}/{self.media_folder_name}"
        return f"{self.archive_folder_path}/ROOT-REVIEW/{self.media_folder}"

    @property
    def ready_for_copy(self) -> bool:
        return self.status == "ready"


def _normalize_label(value: str) -> str:
    return " ".join(
        value.casefold().replace("_", " ").replace("-", " ").replace("/", " ").replace(",", " ").split()
    )


def parse_archive_profile(raw: Mapping[str, Any]) -> ArchiveProfile:
    """Validate a decoded profile document; malformed profiles fail loudly."""

    categories = raw.get("categories") or {}
    if not isinstance(categories, Mapping):
        raise ValueError("archive profile 'categories' must be a mapping")
    for name, category in categories.items():
        if not isinstance(category, Mapping):
            raise ValueError(f"archive category {name!r} must be a mapping")
        for required in ("archive_folder_name", "folder_id"):
            if not category.get(required):
                raise ValueError(f"archive category {name!r} is missing {required}")
    aliases = raw.get("aliases") or {}
    if not isinstance(aliases, Mapping):
        raise ValueError("archive profile 'aliases' must be a mapping")
    for alias, target in aliases.items():
        if target not in categories:
            raise ValueError(f"archive alias {alias!r} points at unknown category {target!r}")
    return ArchiveProfile(
        root_name=str(raw.get("root_name") or "").strip(),
        year=str(raw.get("year") or "").strip(),
        categories={str(name): dict(category) for name, category in categories.items()},
        aliases={_normalize_label(str(alias)): str(target) for alias, target in aliases.items()},
    )


@lru_cache(maxsize=8)
def _load_archive_profile(path: str, mtime_ns: int) -> ArchiveProfile:
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"archive profile {path} must be a YAML mapping")
    return parse_archive_profile(raw)


def load_archive_profile(path: Path) -> ArchiveProfile:
    return _load_archive_profile(str(path), path.stat().st_mtime_ns)


def archive_profile_for(config: IngestConfig | None) -> ArchiveProfile:
    """The configured profile, or the empty profile when archiving is off."""

    if config is None or not config.archive.enabled or config.archive.profile_path is None:
        return EMPTY_ARCHIVE_PROFILE
    path = config.archive.profile_path
    if not path.is_absolute():
        path = config.repo_root / path
    return load_archive_profile(path)


def normalize_archive_category(value: str | None, profile: ArchiveProfile) -> str | None:
    if not value:
        return None
    normalized = _normalize_label(value)
    for category in profile.categories:
        if _normalize_label(category) == normalized:
            return category
    return profile.aliases.get(normalized)


def archive_category_for_prefix(prefix: str, profile: ArchiveProfile) -> str | None:
    normalized = prefix.strip().upper()
    for category, entry in profile.categories.items():
        if entry.get("prefix") == normalized:
            return category
    return None


def archive_prefix_for_category(category: str | None, profile: ArchiveProfile) -> str | None:
    """Resolve a canonical category to its established prefix, if one exists."""

    canonical = normalize_archive_category(category, profile)
    if canonical is None:
        return None
    return profile.categories[canonical].get("prefix")


def archive_destination_for_category(
    category: str | None,
    profile: ArchiveProfile,
    *,
    year: str | None = None,
) -> str | None:
    """Return the deterministic archive path for a category."""

    canonical = normalize_archive_category(category, profile)
    if canonical is None:
        return None
    folder_name = profile.categories[canonical]["archive_folder_name"]
    parts = (profile.root_name, year or profile.year, folder_name)
    return "/".join(part for part in parts if part)


def resolve_archive_destination(
    category: str | None,
    media_folder: str,
    profile: ArchiveProfile,
    *,
    nested_path: tuple[str, ...] = (),
    year: str | None = None,
) -> ArchiveDestination:
    """Resolve a full archive branch without performing Drive I/O.

    The resolver preserves live folder-name variants (Pictures/Videos and
    lowercase pics/vids) while exposing canonical Pics/Vids semantics. Branches
    without verified media children, or without an established filename prefix,
    remain explicit review states rather than being discarded or guessed.
    """

    canonical = normalize_archive_category(category, profile)
    if canonical is None:
        raise ValueError(f"unknown archive category: {category}")
    if media_folder not in {"Pics", "Vids"}:
        raise ValueError(f"archive media folder must be Pics or Vids: {media_folder}")
    entry = profile.categories[canonical]
    nested_ids: list[str] = []
    selected = entry
    for nested_name in nested_path:
        nested = selected.get("nested_paths", {}).get(nested_name)
        if nested is None:
            raise ValueError(f"unknown nested archive path for {canonical}: {nested_name}")
        nested_ids.append(nested["folder_id"])
        selected = nested

    media_ids = selected.get("media_folder_ids", {})
    media_names = selected.get("media_folder_names", {})
    media_id = media_ids.get(media_folder)
    media_name = media_names.get(media_folder, media_folder) if media_id else None
    prefix = entry.get("prefix")
    if nested_path and not media_id:
        status = "review_nested_media"
        reason = "nested archive folder has no verified Pics/Vids child ID"
    elif not media_id:
        status = "review_legacy_root"
        reason = "archive branch has no verified Pics/Vids child; loose root media requires legacy/root review"
    elif prefix is None:
        status = "review_prefix"
        reason = "category destination is verified but filename prefix is not assigned"
    else:
        status = "ready"
        reason = None
    return ArchiveDestination(
        root_name=profile.root_name,
        category=canonical,
        year=year or profile.year,
        prefix=prefix,
        category_folder_name=entry["archive_folder_name"],
        category_folder_id=entry["folder_id"],
        media_folder=media_folder,
        media_folder_name=media_name,
        media_folder_id=media_id,
        nested_path=tuple(nested_path),
        nested_folder_ids=tuple(nested_ids),
        status=status,
        reason=reason,
    )


def archive_filename_proposal(
    current_filename: str,
    *,
    category_prefix: str,
    service_date: str | None,
    collision_suffix: str | None = None,
) -> str:
    """Build the category-resolved name while preserving the source suffix.

    Visual subject and variant stay in metadata. The suffix is deterministic
    asset identity, not a model-generated sequence.
    """

    if not collision_suffix or not service_date:
        return current_filename
    safe_prefix = "".join(char for char in category_prefix.upper() if char.isalnum())
    safe_date = service_date if len(service_date) == 10 and service_date[4] == "-" and service_date[7] == "-" else ""
    safe_suffix = "".join(char for char in collision_suffix.lower() if char.isalnum())
    if not safe_prefix or not safe_date or not safe_suffix:
        return current_filename
    return f"{safe_prefix}_{safe_date}_{safe_suffix}{Path(current_filename).suffix}"
