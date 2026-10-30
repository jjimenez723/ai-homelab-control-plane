"""Read-only, reviewable ingestion plans.

The base plan is deterministic; an optional preflight pass can attach
proposal-only model output without changing the source set fingerprint. The
fingerprint covers path, identity, size, and mtime so a later apply can refuse
a changed source instead of silently applying a different preview.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from tools.ingest.config import IngestConfig
from tools.ingest.identity import source_ref_id_for
from tools.ingest.archive_profile import (
    ArchiveProfile,
    archive_destination_for_category,
    archive_filename_proposal,
    archive_prefix_for_category,
    archive_profile_for,
    normalize_archive_category,
    resolve_archive_destination,
)
from tools.ingest.media_kind import classify_media
from tools.ingest.modeling import PreflightAnalysis
from tools.ingest.scanner import EligibleFile, ScanResult
from tools.ingest.t7 import ResolvedT7


PLAN_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PlanFile:
    source_path: str
    relative_path: str
    source_class: str
    source_volume: str
    current_filename: str
    proposed_filename: str
    landing_destination: str
    status: str
    warning: str | None
    size: int
    mtime_ns: int
    analysis: dict[str, Any] | None = None

    def fingerprint_record(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "relative_path": self.relative_path,
            "source_class": self.source_class,
            "source_volume": self.source_volume,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "relative_path": self.relative_path,
            "source_class": self.source_class,
            "source_volume": self.source_volume,
            "current_filename": self.current_filename,
            "proposed_filename": self.proposed_filename,
            "landing_destination": self.landing_destination,
            "status": self.status,
            "warning": self.warning,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "analysis": self.analysis,
        }


@dataclass(frozen=True)
class PreviewPlan:
    fingerprint: str
    files: tuple[PlanFile, ...]
    total_bytes: int
    warnings: tuple[str, ...]
    destination_root: str
    catalog_path: str | None = None
    reviewed_files: tuple[dict[str, str], ...] = ()
    analysis_status: str = "not_run"
    analysis_message: str = (
        "Analysis has not run yet. Select files and run pre-ingest analysis to "
        "see people, animals, profile matches, and move proposals."
    )

    @property
    def warning_count(self) -> int:
        return sum(1 for item in self.files if item.warning) + len(self.warnings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "destination_root": self.destination_root,
            "catalog_path": self.catalog_path,
            "summary": {
                "file_count": len(self.files),
                "total_bytes": self.total_bytes,
                "warning_count": self.warning_count,
                "review_count": len(self.reviewed_files),
            },
            "analysis": {
                "status": self.analysis_status,
                "message": self.analysis_message,
            },
            "warnings": list(self.warnings),
            "files": [item.as_dict() for item in self.files],
            "reviewed_files": list(self.reviewed_files),
        }

    def json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, indent=2) + "\n"


def _destination_root(config: IngestConfig, resolved_t7: ResolvedT7 | None) -> Path:
    if resolved_t7 is not None:
        return resolved_t7.inbox_dir
    if config.t7.storage_root_override is not None:
        return config.t7.storage_root_override / config.t7.inbox_relative_path
    # A stable, clearly non-local placeholder keeps preview useful when the
    # physical destination is not mounted without pretending it is connected.
    return Path("<T7>") / config.t7.inbox_relative_path


def _fingerprint(files: list[PlanFile]) -> str:
    payload = [item.fingerprint_record() for item in files]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_preview_plan(
    config: IngestConfig,
    targets: list[Any],
    scans: dict[tuple[str, str], ScanResult],
    *,
    resolved_t7: ResolvedT7 | None,
    t7_warning: str | None = None,
) -> PreviewPlan:
    destination_root = _destination_root(config, resolved_t7)
    plan_files: list[PlanFile] = []
    reviewed_files: list[dict[str, str]] = []
    global_warnings: list[str] = []
    if t7_warning:
        global_warnings.append(f"T7 destination is not connected: {t7_warning}")

    for target in targets:
        scan = scans[target.key]
        reviewed_files.extend(
            {
                "source_path": str(item.path),
                "relative_path": item.rel_path,
                "reason": item.reason,
            }
            for item in scan.media_review
        )
        for eligible in scan.eligible:
            destination = destination_root / target.source_class / eligible.rel_path
            warning: str | None = None
            status = "ready"
            if t7_warning:
                status = "needs_attention"
                warning = "T7 must be connected and verified before applying this plan."
            elif destination.exists():
                status = "warning"
                warning = (
                    "A file already exists at this landing path; apply will verify content "
                    "and may use a deterministic disambiguated name."
                )
            plan_files.append(
                PlanFile(
                    source_path=str(eligible.path),
                    relative_path=eligible.rel_path,
                    source_class=target.source_class,
                    source_volume=target.volume_uuid,
                    current_filename=eligible.path.name,
                    # Before preflight analysis, preserving the source name is
                    # the safe proposal. Analysis may replace this with an
                    # explicitly reviewable name and destination.
                    proposed_filename=eligible.path.name,
                    landing_destination=str(destination),
                    status=status,
                    warning=warning,
                    size=eligible.size,
                    mtime_ns=eligible.mtime_ns,
                )
            )

    plan_files.sort(key=lambda item: (item.source_class, item.source_volume, item.relative_path))
    return PreviewPlan(
        fingerprint=_fingerprint(plan_files),
        files=tuple(plan_files),
        total_bytes=sum(item.size for item in plan_files),
        warnings=tuple(global_warnings),
        destination_root=str(destination_root),
        catalog_path=str(resolved_t7.catalog_path) if resolved_t7 is not None else None,
        reviewed_files=tuple(sorted(reviewed_files, key=lambda item: item["source_path"])),
    )


def _safe_component(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:80] or fallback


def _proposed_filename(current_filename: str, payload: dict[str, Any]) -> str:
    proposal = payload.get("filename_proposal") or {}
    subject = _safe_component(str(proposal.get("subject") or ""), fallback="")
    variant = _safe_component(str(proposal.get("variant") or ""), fallback="")
    if not subject:
        return current_filename
    suffix = Path(current_filename).suffix
    parts = [subject]
    if variant:
        parts.append(variant)
    return "__".join(parts) + suffix


def _archive_proposed_filename(
    config: IngestConfig,
    archive_profile: ArchiveProfile,
    current_filename: str,
    payload: dict[str, Any],
    *,
    collision_suffix: str,
) -> str:
    category_prefix = archive_prefix_for_category(payload.get("archive_category"), archive_profile)
    if category_prefix is None:
        return current_filename
    return archive_filename_proposal(
        current_filename,
        category_prefix=category_prefix,
        service_date=config.archive.service_date,
        collision_suffix=collision_suffix,
    )


def _materialization_folder(payload: dict[str, Any], profiles: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    profiles_by_id = {profile["profile_id"]: profile for profile in profiles}
    people = payload.get("people") if isinstance(payload.get("people"), list) else []
    animals = payload.get("animals") if isinstance(payload.get("animals"), list) else []
    profile_matches: list[dict[str, Any]] = []
    for person in people:
        if not isinstance(person, dict):
            continue
        profile_id = person.get("person_profile_id")
        profile = profiles_by_id.get(profile_id)
        if profile is not None:
            profile_matches.append(
                {
                    "profile_id": profile_id,
                    "display_name": profile["display_name"],
                    "confidence": person.get("confidence"),
                    "evidence": person.get("evidence", ""),
                }
            )

    if profile_matches and not animals and len(profile_matches) == len(people):
        if len(profile_matches) == 1:
            return f"people/{_safe_component(profile_matches[0]['display_name'], fallback='known-person')}", profile_matches
        return "people/multiple", profile_matches
    if animals and not people:
        animal_type = _safe_component(str(animals[0].get("animal_type") or "animal"), fallback="animal")
        return f"animals/{animal_type}", profile_matches
    if profile_matches and animals:
        return "people-and-animals", profile_matches
    return "needs-review", profile_matches


def attach_preflight_analysis(
    plan: PreviewPlan,
    results: dict[str, PreflightAnalysis | str],
    *,
    profiles: list[dict[str, Any]],
    config: IngestConfig | None = None,
) -> PreviewPlan:
    """Attach read-only model proposals to selected plan records."""
    archive_profile = archive_profile_for(config)
    updated_files: list[PlanFile] = []
    analyzed_count = 0
    failed_count = 0
    for item in plan.files:
        result = results.get(item.source_path)
        if result is None:
            updated_files.append(item)
            continue
        if isinstance(result, str):
            failed_count += 1
            updated_files.append(
                PlanFile(
                    **{**item.__dict__, "status": "analysis_failed", "warning": result,
                       "analysis": {"status": "failed", "error": result}}
                )
            )
            continue

        analyzed_count += 1
        payload = result.payload
        folder, profile_matches = _materialization_folder(payload, profiles)
        archive_category = normalize_archive_category(payload.get("archive_category"), archive_profile)
        archive_prefix = archive_prefix_for_category(archive_category, archive_profile)
        archive_destination = archive_destination_for_category(archive_category, archive_profile)
        archive_route = None
        nested_path = tuple(payload.get("archive_nested_path") or ())
        media_folder = classify_media(Path(item.source_path)).route_type
        if archive_category is not None and media_folder is not None:
            try:
                archive_route = resolve_archive_destination(
                    archive_category,
                    media_folder,
                    archive_profile,
                    nested_path=nested_path,
                )
            except ValueError as exc:
                archive_route = None
                archive_destination_error = str(exc)
            else:
                archive_destination_error = None
        else:
            archive_destination_error = None
        proposed_filename = (
            _archive_proposed_filename(
                config,
                archive_profile,
                item.current_filename,
                payload,
                collision_suffix=source_ref_id_for(item.source_volume, item.relative_path)[:8],
            )
            if config is not None and config.archive.enabled
            else _proposed_filename(item.current_filename, payload)
        )
        destination_relative_path = f"{folder}/{proposed_filename}"
        analysis = {
            "status": "ready_for_review",
            "summary": payload.get("summary", ""),
            "description": payload.get("description", payload.get("summary", "")),
            "capture_date": payload.get("capture_date"),
            "archive_category": archive_category,
            "archive_prefix": archive_prefix,
            "archive_destination": archive_destination,
            "archive_destination_status": archive_route.status if archive_route else "review",
            "archive_destination_reason": (
                archive_route.reason
                if archive_route
                else archive_destination_error or "missing category or media route"
            ),
            "archive_destination_error": archive_destination_error,
            "archive_nested_path": list(nested_path),
            "archive_category_folder_id": archive_route.category_folder_id if archive_route else None,
            "archive_media_folder_id": archive_route.media_folder_id if archive_route else None,
            "archive_media_folder_name": archive_route.media_folder_name if archive_route else None,
            "archive_relative_path": archive_route.relative_path if archive_route else None,
            "archive_confidence": payload.get("archive_confidence"),
            "archive_evidence": payload.get("archive_evidence", ""),
            "keywords": payload.get("keywords", []),
            "people": payload.get("people", []),
            "profile_matches": profile_matches,
            "animals": payload.get("animals", []),
            "tags": payload.get("tags", []),
            "filename_proposal": payload.get("filename_proposal", {}),
            "proposed_filename": proposed_filename,
            "destination_relative_path": destination_relative_path,
        }
        category_warning = None
        if config is not None and config.archive.enabled:
            if archive_category is None:
                category_warning = (
                    "Archive category is unknown or missing; human review is required before archive routing."
                )
                analysis["archive_category_status"] = "unknown"
            elif archive_prefix is None:
                category_warning = (
                    "Archive category is canonical, but its source prefix is unresolved; "
                    "human review is required before archive routing."
                )
                analysis["archive_category_status"] = "prefix_unresolved"
            elif archive_route is None:
                category_warning = (
                    "Archive category is canonical, but the source media route could not be verified; "
                    "human review is required before archive routing."
                )
                analysis["archive_category_status"] = "destination_review"
            elif archive_route.status != "ready":
                category_warning = (
                    f"Archive category is canonical, but destination requires review: "
                    f"{archive_route.reason}"
                )
                analysis["archive_category_status"] = "destination_review"
            else:
                analysis["archive_category_status"] = "matched"
        updated_files.append(
            PlanFile(
                **{
                    **item.__dict__,
                    "proposed_filename": proposed_filename,
                    "landing_destination": str(
                        Path(plan.destination_root) / item.source_class / destination_relative_path
                    ),
                    "status": "ready_for_review",
                    "warning": category_warning or "AI proposal only. Approve this file before moving it.",
                    "analysis": analysis,
                }
            )
        )

    # Model proposals are independent, so several files can legitimately
    # produce the same subject/variant. Resolve those collisions in the
    # review plan rather than waiting until apply, where the user would only
    # see the final name after a move has started. The suffix is derived from
    # the same stable asset identity used by the transfer transaction, making
    # the result deterministic across retries and previews.
    used_destinations: set[str] = set()

    resolved_files: list[PlanFile] = []
    for item in updated_files:
        analysis = item.analysis
        if analysis is None or analysis.get("status") != "ready_for_review":
            resolved_files.append(item)
            continue

        original_destination = str(analysis["destination_relative_path"])
        destination = original_destination
        collision_key = destination.casefold()
        if collision_key in used_destinations:
            destination_path = Path(original_destination)
            asset_id = source_ref_id_for(item.source_volume, item.relative_path)
            suffix = asset_id[:8]
            candidate = f"{destination_path.stem}__{suffix}{destination_path.suffix}"
            attempt = 2
            while candidate.casefold() in used_destinations:
                candidate = f"{destination_path.stem}__{suffix}-{attempt}{destination_path.suffix}"
                attempt += 1
            destination = str(destination_path.with_name(candidate))
            analysis = {
                **analysis,
                "proposed_filename": candidate,
                "destination_relative_path": destination,
                "collision_resolution": (
                    "The model proposed this name for multiple files. "
                    "A deterministic suffix keeps each destination unique."
                ),
            }
            warning = (
                "AI proposal only. Approve this file before moving. "
                "Duplicate model names were resolved with a deterministic suffix."
            )
        else:
            warning = item.warning

        used_destinations.add(destination.casefold())
        resolved_files.append(
            PlanFile(
                **{
                    **item.__dict__,
                    "proposed_filename": Path(destination).name,
                    "landing_destination": str(
                        Path(plan.destination_root) / item.source_class / destination
                    ),
                    "warning": warning,
                    "analysis": analysis,
                }
            )
        )

    updated_files = resolved_files

    if failed_count:
        status = "partial_failed" if analyzed_count else "failed"
        message = f"Analysis completed for {analyzed_count} file(s); {failed_count} failed. Failed files cannot be approved."
    else:
        status = "complete"
        message = f"Analysis completed for {analyzed_count} file(s). Review each proposal before moving."
    return PreviewPlan(
        fingerprint=plan.fingerprint,
        files=tuple(updated_files),
        total_bytes=plan.total_bytes,
        warnings=plan.warnings,
        destination_root=plan.destination_root,
        catalog_path=plan.catalog_path,
        reviewed_files=plan.reviewed_files,
        analysis_status=status,
        analysis_message=message,
    )
