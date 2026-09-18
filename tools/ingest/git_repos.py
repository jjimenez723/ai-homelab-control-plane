"""Read-only Git repository inspection for the exclusion report.

Detected repositories are inventoried, never moved, merged, or pushed by
this tool (docs/13-dump-first-ingestion.md, review §11). All git invocations
are read-only and hooks are disabled.
"""

from __future__ import annotations

import os
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


@dataclass(frozen=True)
class GitRepoCounts:
    """Counted detail for the development-repo inventory report.

    Strictly additive over `GitRepoDetails`: same read-only `_run_git`
    invocations, same never-raising contract. `inspect_git_repo` keeps its
    boolean shape because `organize <source>` prints flags, not counts.
    """

    details: GitRepoDetails
    dirty_count: int | None
    unpushed_count: int | None
    stash_count: int | None
    last_commit_iso: str | None
    default_remote_branch: str | None
    head_in_default_remote: bool | None
    git_dir_bytes: int | None
    work_tree_bytes: int | None
    is_bare: bool


def resolve_git_dir(directory: Path) -> Path | None:
    """The real repository directory for `directory`, following a `.git` file.

    A submodule (and a linked worktree) stores `.git` as a file pointing at
    the directory that actually holds the objects. Measuring the `.git`
    *file* would report a few dozen bytes, and falling back to the work tree
    would report the checkout -- neither answers "how big is this history".
    """
    marker = directory / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        try:
            content = marker.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in content.splitlines():
            if line.startswith("gitdir:"):
                target = Path(line.removeprefix("gitdir:").strip())
                resolved = target if target.is_absolute() else (directory / target)
                return resolved if resolved.is_dir() else None
        return None
    if (directory / "HEAD").exists() and (directory / "objects").is_dir():
        return directory
    return None


def is_linked_worktree(directory: Path) -> bool:
    """True for a linked `git worktree` checkout, not a repository of its own.

    Both a linked worktree and a submodule have a `.git` *file* holding a
    `gitdir:` pointer, so the marker's type alone cannot tell them apart. The
    pointer's target does: a worktree's target sits under the parent's
    `.git/worktrees/`, while a submodule's sits under `.git/modules/`. That
    distinction matters to an inventory -- a worktree is a second view of
    history already counted, a submodule is a distinct repository with its
    own remote and its own commits.
    """
    marker = directory / ".git"
    if not marker.is_file():
        return False
    try:
        content = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in content.splitlines():
        if line.startswith("gitdir:"):
            target = line.removeprefix("gitdir:").strip().replace("\\", "/")
            parts = target.split("/")
            return "worktrees" in parts and ".git" in parts
    return False


def _count_lines(value: str | None) -> int | None:
    if value is None:
        return None
    return len([line for line in value.splitlines() if line.strip()])


def _dir_bytes(path: Path) -> int | None:
    """Apparent size of a directory tree; `None` if it cannot be walked."""
    if not path.exists():
        return None
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
            for name in filenames:
                candidate = os.path.join(dirpath, name)
                try:
                    stat = os.lstat(candidate)
                except OSError:
                    continue
                if not os.path.islink(candidate):
                    total += stat.st_size
    except OSError:
        return None
    return total


def inspect_git_repo_counts(repo_dir: Path, *, measure_size: bool = True) -> GitRepoCounts:
    """Read-only inspection with counts, ahead/behind, and on-disk size.

    Never moves, fetches, pushes, prunes, or writes anything. `origin/HEAD`
    is read from the existing local ref only -- no network call is made, so
    an offline or unreachable remote yields `None` rather than a stall.
    """
    details = inspect_git_repo(repo_dir)
    dirty_count = _count_lines(_run_git(repo_dir, "status", "--porcelain"))
    stash_count = _count_lines(_run_git(repo_dir, "stash", "list"))
    last_commit_iso = _run_git(repo_dir, "log", "-1", "--format=%cI")

    # Count commits reachable from HEAD that no remote-tracking ref holds.
    # This is deliberately stricter than `branch...origin/branch`, which
    # reports 0 for a local-only branch (no `origin/<branch>` ref exists, so
    # the comparison fails and reads as "nothing to push"). A local-only
    # branch's commits are exactly the ones a Git remote would not preserve,
    # which is the question this inventory exists to answer.
    unpushed_count: int | None = None
    raw = _run_git(repo_dir, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if raw and raw.isdigit():
        unpushed_count = int(raw)

    default_remote_branch = _run_git(repo_dir, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if default_remote_branch:
        default_remote_branch = default_remote_branch.removeprefix("refs/remotes/")

    head_in_default_remote: bool | None = None
    if details.head and default_remote_branch:
        merge_base = _run_git(repo_dir, "merge-base", "--is-ancestor", details.head, default_remote_branch)
        # `--is-ancestor` communicates through the exit code, which `_run_git`
        # collapses to None on failure; an empty string means "yes".
        head_in_default_remote = merge_base == ""

    git_dir = resolve_git_dir(repo_dir)
    is_bare = git_dir is not None and git_dir == repo_dir
    git_dir_bytes = _dir_bytes(git_dir) if (measure_size and git_dir is not None) else None
    work_tree_bytes = _dir_bytes(repo_dir) if measure_size else None

    return GitRepoCounts(
        details=details,
        dirty_count=dirty_count,
        unpushed_count=unpushed_count,
        stash_count=stash_count,
        last_commit_iso=last_commit_iso,
        default_remote_branch=default_remote_branch,
        head_in_default_remote=head_in_default_remote,
        git_dir_bytes=git_dir_bytes,
        work_tree_bytes=work_tree_bytes,
        is_bare=is_bare,
    )
