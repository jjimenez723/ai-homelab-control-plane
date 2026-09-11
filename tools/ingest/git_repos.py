"""Read-only Git repository inspection for the exclusion report.

Detected repositories are inventoried, never moved, merged, or pushed by
this tool (docs/13-dump-first-ingestion.md, review §11). All git invocations
are read-only and hooks are disabled.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitRepoDetails:
    path: Path
    remote_url: str | None
    head: str | None
    branch: str | None
    dirty: bool | None
    unpushed: bool | None
    stash: bool | None


def _run_git(repo_dir: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "--git-dir", str(repo_dir / ".git"), "--work-tree", str(repo_dir), "-c", "core.hooksPath=/dev/null", "--no-optional-locks", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def inspect_git_repo(repo_dir: Path) -> GitRepoDetails:
    """Best-effort, never-raising read-only inspection of a detected repo."""
    remote_url = _run_git(repo_dir, "remote", "get-url", "origin")
    head = _run_git(repo_dir, "rev-parse", "HEAD")
    branch = _run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    status = _run_git(repo_dir, "status", "--porcelain")
    dirty = None if status is None else bool(status)
    stash_list = _run_git(repo_dir, "stash", "list")
    stash = None if stash_list is None else bool(stash_list)

    unpushed: bool | None = None
    if branch and branch != "HEAD":
        counts = _run_git(repo_dir, "rev-list", "--left-right", "--count", f"{branch}...origin/{branch}")
        if counts:
            parts = counts.split()
            if len(parts) == 2 and parts[0].isdigit():
                unpushed = int(parts[0]) > 0

    return GitRepoDetails(path=repo_dir, remote_url=remote_url, head=head, branch=branch, dirty=dirty, unpushed=unpushed, stash=stash)
