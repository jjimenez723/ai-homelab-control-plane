"""Walk a source root once, applying hard exclusions, to find eligible files.

This module never opens a file for writing and never deletes anything. It is
safe to call during `--dry-run`; the only I/O is `os.walk` and `stat`.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from tools.ingest.config import IngestConfig
from tools.ingest.exclusions import (
    ExclusionReason,
    has_keep_local_marker,
    is_bundle_dir,
    is_camera_metadata_file,
    is_excluded_dir_name,
    is_git_repo_dir,
    is_incomplete_download,
    is_macos_metadata_dir,
    is_macos_metadata_file,
    is_settled,
    symlink_escapes_root,
    under_any,
)
from tools.ingest.media_kind import classify_media


@dataclass(frozen=True)
class EligibleFile:
    path: Path
    rel_path: str
    size: int
    mtime: float
    mtime_ns: int


@dataclass(frozen=True)
class GitRepoInfo:
    path: Path
    rel_path: str


@dataclass(frozen=True)
class MediaReviewFile:
    path: Path
    rel_path: str
    reason: str


@dataclass
class ScanResult:
    root: Path
    eligible: list[EligibleFile] = field(default_factory=list)
    git_repos: list[GitRepoInfo] = field(default_factory=list)
    media_review: list[MediaReviewFile] = field(default_factory=list)
    exclusion_counts: dict[str, int] = field(default_factory=dict)
    missing: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.eligible)


def scan_source(
    root: Path,
    *,
    config: IngestConfig,
    extra_exclude_paths: tuple[Path, ...] = (),
    media_only: bool = False,
) -> ScanResult:
    # Preserve the caller's lexical path in results.  On macOS `/var` is a
    # symlink to `/private/var`; resolving here made a read-only scan report a
    # different source pathname than the one the caller supplied.  Escape
    # checks resolve only the paths they compare.
    root = root.absolute()
    if not root.exists():
        return ScanResult(root=root, missing=True)

    exclude_paths = tuple(config.exclusions.extra_paths) + tuple(extra_exclude_paths)
    counts: dict[str, int] = defaultdict(int)
    eligible: list[EligibleFile] = []
    git_repos: list[GitRepoInfo] = []
    media_review: list[MediaReviewFile] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)

        if exclude_paths and under_any(current, exclude_paths):
            dirnames[:] = []
            continue

        keep_dirs = []
        for name in list(dirnames):
            sub = current / name
            if os.path.islink(sub):
                if symlink_escapes_root(sub, root):
                    counts[ExclusionReason.SYMLINK_ESCAPE] += 1
                else:
                    counts[ExclusionReason.SYMLINK] += 1
                continue
            if is_macos_metadata_dir(sub):
                counts[ExclusionReason.MACOS_METADATA] += 1
                continue
            if exclude_paths and under_any(sub, exclude_paths):
                counts[ExclusionReason.EXTRA_PATH] += 1
                continue
            if is_incomplete_download(sub, config.exclusions):
                counts[ExclusionReason.INCOMPLETE_DOWNLOAD] += 1
                continue
            if is_git_repo_dir(sub):
                git_repos.append(GitRepoInfo(path=sub, rel_path=str(sub.relative_to(root))))
                counts[ExclusionReason.GIT_REPO] += 1
                continue
            if is_bundle_dir(sub, config.exclusions):
                counts[ExclusionReason.BUNDLE] += 1
                continue
            if is_excluded_dir_name(sub, config.exclusions):
                counts[ExclusionReason.DIR_NAME] += 1
                continue
            if has_keep_local_marker(sub, config.exclusions):
                counts[ExclusionReason.KEEP_LOCAL] += 1
                continue
            keep_dirs.append(name)
        dirnames[:] = keep_dirs

        for filename in filenames:
            file_path = current / filename

            if os.path.islink(file_path):
                if symlink_escapes_root(file_path, root):
                    counts[ExclusionReason.SYMLINK_ESCAPE] += 1
                else:
                    counts[ExclusionReason.SYMLINK] += 1
                continue
            if is_macos_metadata_file(file_path):
                counts[ExclusionReason.MACOS_METADATA] += 1
                continue
            if is_camera_metadata_file(file_path, config.exclusions):
                counts[ExclusionReason.CAMERA_METADATA] += 1
                continue
            if is_incomplete_download(file_path, config.exclusions):
                counts[ExclusionReason.INCOMPLETE_DOWNLOAD] += 1
                continue
            if not is_settled(file_path, config.settle_seconds):
                counts[ExclusionReason.STILL_CHANGING] += 1
                continue
            try:
                stat = file_path.stat()
            except OSError:
                continue
            if media_only:
                classification = classify_media(file_path)
                if classification.route_type is None:
                    media_review.append(
                        MediaReviewFile(
                            path=file_path,
                            rel_path=str(file_path.relative_to(root)),
                            reason=classification.reason,
                        )
                    )
                    counts["media_review"] += 1
                    continue
            eligible.append(
                EligibleFile(
                    path=file_path,
                    rel_path=str(file_path.relative_to(root)),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    mtime_ns=stat.st_mtime_ns,
                )
            )

    return ScanResult(
        root=root,
        eligible=eligible,
        git_repos=git_repos,
        media_review=media_review,
        exclusion_counts=dict(counts),
    )
