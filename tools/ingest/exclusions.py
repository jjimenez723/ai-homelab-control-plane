"""Hard-exclusion predicates shared by the scanner.

Every predicate here is pure (path in, answer out) so it can be unit-tested
directly without touching a real filesystem tree beyond the fixture at hand.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from tools.ingest.config import ExclusionsConfig


class ExclusionReason:
    GIT_REPO = "git_repo"
    DIR_NAME = "excluded_dir_name"
    BUNDLE = "bundle"
    INCOMPLETE_DOWNLOAD = "incomplete_download"
    STILL_CHANGING = "still_changing"
    SYMLINK_ESCAPE = "symlink_escape"
    SYMLINK = "symlink"
    KEEP_LOCAL = "keep_local"
    EXTRA_PATH = "excluded_path"
    SELF_REPO = "control_plane_repo"
    DESTINATION = "destination_volume"


def is_git_repo_dir(directory: Path) -> bool:
    """A directory is a git repo/worktree root if it has a `.git` dir or file."""
    git_marker = directory / ".git"
    return git_marker.is_dir() or git_marker.is_file()


def is_bundle_dir(directory: Path, config: ExclusionsConfig) -> bool:
    return directory.suffix.lower() in config.bundle_extensions


def is_excluded_dir_name(directory: Path, config: ExclusionsConfig) -> bool:
    # Case-insensitive: matches macOS's default case-insensitive-but-preserving
    # APFS, where `Node_Modules` and `node_modules` are the same directory.
    name = directory.name.lower()
    return any(name == excluded.lower() for excluded in config.dir_names)


def has_keep_local_marker(directory: Path, config: ExclusionsConfig) -> bool:
    return (directory / config.keep_local_marker).exists()


def is_incomplete_download(path: Path, config: ExclusionsConfig) -> bool:
    """Whether a file or directory is a browser-managed partial download.

    Safari represents an active download as a `.download` directory/package,
    while Chrome and other tools commonly use file suffixes such as
    `.crdownload` and `.part`. The scanner must reject both shapes before
    descending into a directory or considering a file eligible.
    """
    return path.suffix.lower() in config.incomplete_download_suffixes


def is_settled(file_path: Path, settle_seconds: int, *, now: float | None = None) -> bool:
    """True if the file's mtime is old enough to be considered done changing."""
    reference = now if now is not None else time.time()
    try:
        mtime = file_path.stat().st_mtime
    except OSError:
        return False
    return (reference - mtime) >= settle_seconds


def symlink_escapes_root(file_path: Path, root: Path) -> bool:
    """True if `file_path` is a symlink whose real target is outside `root`."""
    if not os.path.islink(file_path):
        return False
    try:
        target = file_path.resolve(strict=True)
    except OSError:
        return True  # unresolvable symlink -- treat as escaping/unsafe
    resolved_root = root.resolve()
    try:
        target.relative_to(resolved_root)
        return False
    except ValueError:
        return True


def under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False
