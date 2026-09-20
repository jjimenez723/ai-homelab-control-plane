"""Safe derived organized-view planning and materialization.

The view is never canonical. A proposal must pass identity, review, conflict,
drift, path, and free-space checks before this module copies bytes. Copies are
ordinary verified copies; hardlinks and clonefile are intentionally absent.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
import re

from .contract import normalized_slug

DRIFT_LIMIT = 0.20
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class MaterializationRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class PlacementProposal:
    asset_id: str
    source_path: str
    source_sha256: str
    size: int
    client_id: str | None
    project_id: str | None
    filename: str | None
    relative_path: str | None
    tags: tuple[str, ...] = ()
    review_required: bool = False
    reason: str = ""

    def signature(self) -> tuple[Any, ...]:
        return (
            self.client_id, self.project_id, self.relative_path,
            tuple(sorted(self.tags)),
        )


@dataclass(frozen=True)
class ProposalDrift:
    total: int
    changed: int
    fraction: float
    abort: bool
    changed_assets: tuple[str, ...]


@dataclass(frozen=True)
class MaterializationPlan:
    run_id: str
    scope: str
    proposals: tuple[PlacementProposal, ...]
    drift: ProposalDrift
    estimated_bytes: int
    review_count: int
    conflicts: tuple[str, ...]
    available_free_bytes: int
    safe: bool
    dry_run: bool = True

    @property
    def materializable(self) -> tuple[PlacementProposal, ...]:
        return tuple(item for item in self.proposals if not item.review_required and item.relative_path and not item.reason)

    def summary(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "assets_considered": len(self.proposals),
            "would_materialize": len(self.materializable),
            "review_required": self.review_count,
            "conflicts": len(self.conflicts),
            "estimated_additional_bytes": self.estimated_bytes,
            "proposal_drift": self.drift.fraction,
            "drift_aborted": self.drift.abort,
            "safe": self.safe,
        }


def _safe_component(value: str) -> str:
    return normalized_slug(value)


def _safe_filename(value: str) -> str:
    if not _FILENAME_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"invalid filename component: {value!r}")
    return value


def _safe_relative(*components: str) -> str:
    checked = [_safe_component(component) for component in components]
    return "/".join(checked)


def build_placement_proposal(asset: Mapping[str, Any], semantic: Mapping[str, Any], *, canonical_filename: str | None,
                             project_date: str | None = None) -> PlacementProposal:
    client_id = semantic.get("client_id")
    project_id = semantic.get("project_id")
    tags = tuple(sorted(str(tag) for tag in semantic.get("tags", ())))
    base = dict(
        asset_id=str(asset["asset_id"]), source_path=str(asset.get("final_path") or ""),
        source_sha256=str(asset.get("sha256") or ""), size=int(asset.get("size") or 0),
        client_id=client_id, project_id=project_id, filename=canonical_filename,
        relative_path=None, tags=tags,
    )
    if not base["source_path"] or not base["source_sha256"]:
        return PlacementProposal(**base, review_required=True, reason="canonical source identity is incomplete")
    if not client_id or not project_id:
        return PlacementProposal(**base, review_required=True, reason="unknown client/project")
    if not canonical_filename or "/" in canonical_filename or "\\" in canonical_filename or canonical_filename in {".", ".."}:
        return PlacementProposal(**base, review_required=True, reason="invalid canonical filename")
    filename = Path(canonical_filename).name
    if filename != canonical_filename or not filename:
        return PlacementProposal(**base, review_required=True, reason="canonical filename is not one component")
    try:
        _safe_filename(filename)
    except ValueError:
        return PlacementProposal(**base, review_required=True, reason="invalid canonical filename")
    project_path = _safe_component(str(project_id))
    year = (project_date or str(project_id)[:4] or "unknown")
    if not (len(year) == 4 and year.isdigit()):
        year = "unknown-year"
    relative = _safe_relative(str(client_id), "30-media", year, project_path, "00-raw", filename)
    base["filename"] = filename
    base["relative_path"] = relative
    base["review_required"] = semantic.get("human_review_state") == "review_required"
    return PlacementProposal(**base)


def compare_proposals(previous: Iterable[PlacementProposal], current: Iterable[PlacementProposal]) -> ProposalDrift:
    old = {item.asset_id: item for item in previous}
    new = {item.asset_id: item for item in current}
    ids = sorted(set(old) | set(new))
    changed = tuple(asset_id for asset_id in ids if asset_id not in old or asset_id not in new or old[asset_id].signature() != new[asset_id].signature())
    fraction = len(changed) / len(ids) if ids else 0.0
    return ProposalDrift(len(ids), len(changed), fraction, fraction > DRIFT_LIMIT, changed)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_no_symlink_components(root: Path, relative: str, *, create: bool) -> Path:
    if root.exists() and root.is_symlink():
        raise MaterializationRefused(f"view root is a symlink: {root}")
    root = root.resolve()
    target = root.joinpath(*relative.split("/"))
    current = root
    components = relative.split("/")
    for component in components[:-1]:
        current = current / component
        if current.is_symlink():
            raise MaterializationRefused(f"symlink in derived view path: {current}")
        if create and not current.exists():
            current.mkdir()
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise MaterializationRefused("derived path escapes view root")
    return target


def plan_materialization(root: Path, proposals: Iterable[PlacementProposal], *, scope: str,
                         previous: Iterable[PlacementProposal] = (), min_free_bytes: int = 0,
                         run_id: str | None = None) -> MaterializationPlan:
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise MaterializationRefused(f"view root must be an existing non-symlink directory: {root}")
    root = root.resolve()
    rows = tuple(proposals)
    previous_rows = tuple(previous)
    drift = compare_proposals(previous_rows, rows) if previous_rows else ProposalDrift(0, 0, 0.0, False, ())
    conflicts: list[str] = []
    destinations: dict[str, str] = {}
    estimated = 0
    for item in rows:
        if item.review_required or not item.relative_path:
            continue
        other = destinations.get(item.relative_path)
        if other is not None and other != item.asset_id:
            conflicts.append(f"destination collision: {item.relative_path} ({other}, {item.asset_id})")
        destinations[item.relative_path] = item.asset_id
        destination = root / item.relative_path
        if destination.exists():
            if not destination.is_file() or _sha256(destination) != item.source_sha256:
                conflicts.append(f"destination exists with different bytes: {item.relative_path}")
        else:
            estimated += item.size
    free = shutil.disk_usage(root).free
    safe = not conflicts and not drift.abort and free - estimated >= min_free_bytes
    if free - estimated < min_free_bytes:
        conflicts.append(f"free-space safety floor: need {min_free_bytes}, have {free - estimated} after estimate")
        safe = False
    return MaterializationPlan(
        run_id=run_id or str(uuid.uuid4()), scope=scope, proposals=rows, drift=drift,
        estimated_bytes=estimated, review_count=sum(1 for item in rows if item.review_required),
        conflicts=tuple(conflicts), available_free_bytes=free, safe=safe,
    )


def materialize(
    root: Path,
    plan: MaterializationPlan,
    *,
    min_free_bytes: int = 0,
    canonical_root: Path | None = None,
) -> dict[str, Any]:
    """Apply an already-reviewed plan using ordinary verified copies only."""
    if not plan.safe or plan.drift.abort or plan.conflicts:
        raise MaterializationRefused("materialization plan is not safe to apply")
    if plan.available_free_bytes - plan.estimated_bytes < min_free_bytes:
        raise MaterializationRefused("free-space safety floor would be crossed")
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise MaterializationRefused(f"view root must be an existing non-symlink directory: {root}")
    root = root.resolve()
    resolved_canonical_root = canonical_root.resolve() if canonical_root is not None else None
    added = unchanged = 0
    for item in plan.materializable:
        source = Path(item.source_path).resolve()
        if not source.exists() or not source.is_file():
            raise MaterializationRefused(f"canonical source is unavailable: {item.asset_id}")
        if resolved_canonical_root is not None:
            try:
                source.relative_to(resolved_canonical_root)
            except ValueError as exc:
                raise MaterializationRefused(
                    f"canonical source escapes trusted root: {item.asset_id}"
                ) from exc
        destination = _check_no_symlink_components(root, item.relative_path or "", create=True)
        if destination.exists():
            if _sha256(destination) != item.source_sha256:
                raise MaterializationRefused(f"destination collision: {destination}")
            unchanged += 1
            continue
        fd = None
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_handle:
                fd = None
                shutil.copyfileobj(input_handle, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if _sha256(destination) != item.source_sha256:
                raise MaterializationRefused(f"destination hash mismatch: {destination}")
            added += 1
        except Exception:
            if fd is not None:
                os.close(fd)
            if destination.exists():
                destination.unlink()
            raise
    return {"run_id": plan.run_id, "added": added, "unchanged": unchanged, "rewrites": 0, "moves": 0}
