"""Canonical client/project identity registries.

Manifests are operator-owned data. The loader accepts aliases for matching,
but the classifier only receives stable canonical IDs. Missing or ambiguous
identities are review states, never automatic directory creation requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .contract import Candidate, normalized_slug


@dataclass(frozen=True)
class ClientManifest:
    client_id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    active: bool = True
    notes: str = ""

    def candidate(self) -> Candidate:
        return Candidate(self.client_id, self.display_name, self.aliases)


@dataclass(frozen=True)
class ProjectManifest:
    project_id: str
    client_id: str
    display_name: str
    date: str | None = None
    aliases: tuple[str, ...] = ()
    status: str = "active"

    def candidate(self) -> Candidate:
        return Candidate(self.project_id, self.display_name, self.aliases)


class ManifestError(ValueError):
    pass


def _strings(raw: Any, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ManifestError(f"{field} must be a list of strings")
    return tuple(item.strip() for item in raw if item.strip())


def _read(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"manifest must be a mapping: {path}")
    return value


def _client(path: Path, raw: dict[str, Any]) -> ClientManifest:
    client_id = normalized_slug(str(raw.get("client_id", path.parent.name)))
    display_name = str(raw.get("display_name", "")).strip()
    if not display_name:
        raise ManifestError(f"{path}: display_name is required")
    return ClientManifest(
        client_id=client_id,
        display_name=display_name,
        aliases=_strings(raw.get("aliases"), "aliases"),
        active=bool(raw.get("active", True)),
        notes=str(raw.get("notes", "")),
    )


def _project(path: Path, raw: dict[str, Any], default_client: str | None = None) -> ProjectManifest:
    project_id = normalized_slug(str(raw.get("project_id", "")))
    client_id = normalized_slug(str(raw.get("client_id", default_client or "")))
    display_name = str(raw.get("display_name", raw.get("name", ""))).strip()
    if not display_name:
        raise ManifestError(f"{path}: project display_name/name is required")
    date = raw.get("date")
    if date is not None:
        date = str(date)
        if len(date) != 10 or date[4] != "-" or date[7] != "-":
            raise ManifestError(f"{path}: date must be YYYY-MM-DD")
    return ProjectManifest(
        project_id=project_id,
        client_id=client_id,
        display_name=display_name,
        date=date,
        aliases=_strings(raw.get("aliases"), "aliases"),
        status=str(raw.get("status", "active")),
    )


class ManifestRegistry:
    def __init__(self, clients: tuple[ClientManifest, ...], projects: tuple[ProjectManifest, ...]):
        self.clients = clients
        self.projects = projects
        self._client_aliases = self._aliases((item.client_id, item.display_name, *item.aliases) for item in clients)
        self._project_aliases = self._aliases((item.project_id, item.display_name, *item.aliases) for item in projects)

    @staticmethod
    def _aliases(groups: Any) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for group in groups:
            canonical = str(group[0])
            for alias in group:
                key = str(alias).strip().casefold()
                if not key:
                    continue
                previous = aliases.get(key)
                if previous is not None and previous != canonical:
                    raise ManifestError(f"ambiguous alias {alias!r}: {previous!r} and {canonical!r}")
                aliases[key] = canonical
        return aliases

    @classmethod
    def load(cls, clients_root: Path) -> "ManifestRegistry":
        clients: list[ClientManifest] = []
        projects: list[ProjectManifest] = []
        if not clients_root.exists():
            return cls((), ())
        for client_dir in sorted(path for path in clients_root.iterdir() if path.is_dir()):
            manifest = client_dir / "manifest.yaml"
            if not manifest.exists():
                continue
            client = _client(manifest, _read(manifest))
            clients.append(client)
            raw = _read(manifest)
            listed = raw.get("projects", [])
            if isinstance(listed, list):
                for item in listed:
                    if isinstance(item, dict):
                        projects.append(_project(manifest, item, client.client_id))
            for project_manifest in sorted(client_dir.glob("projects/*/manifest.yaml")):
                projects.append(_project(project_manifest, _read(project_manifest), client.client_id))
            for project_manifest in sorted(client_dir.glob("*/manifest.yaml")):
                if project_manifest.parent.name != "projects":
                    projects.append(_project(project_manifest, _read(project_manifest), client.client_id))
        return cls(tuple(clients), tuple(projects))

    def client(self, value: str) -> ClientManifest | None:
        canonical = self._client_aliases.get(value.strip().casefold())
        return next((item for item in self.clients if item.client_id == canonical), None)

    def project(self, value: str) -> ProjectManifest | None:
        canonical = self._project_aliases.get(value.strip().casefold())
        return next((item for item in self.projects if item.project_id == canonical), None)

    def candidates(self, *, active_only: bool = True) -> tuple[Candidate, ...]:
        return tuple(item.candidate() for item in self.clients if not active_only or item.active)

    def project_candidates(self, *, client_id: str | None = None, active_only: bool = True) -> tuple[Candidate, ...]:
        return tuple(
            item.candidate() for item in self.projects
            if (client_id is None or item.client_id == client_id)
            and (not active_only or item.status == "active")
        )

    def project_manifest(self, project_id: str) -> ProjectManifest | None:
        return next((item for item in self.projects if item.project_id == project_id), None)
