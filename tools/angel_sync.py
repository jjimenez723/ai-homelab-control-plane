"""UUID-gated writes to Angel's `10-code/mirrors` and `40-ai/skills/trusted`.

Two jobs, one gate. Refreshing repository mirrors and exporting the trusted
skill tree both write to the volume, so both resolve it through
`tools.ingest.t7.resolve_t7` first -- the same resolver ingestion and backup
use. A directory that happens to be called `/Volumes/Angel` is not
permission to write (D-019).

Invariants:

- **Nothing on the volume is deleted.** A mirror is created or fetched into;
  a stale file in the trusted export is *reported*, never removed. Deleting
  on a volume that is somebody's primary copy is not this tool's decision.
- **No push, ever.** Mirrors are read from local working copies and written
  to Angel. `git push` appears nowhere in this module.
- **A mirror is cloned from the local copy, not from GitHub.** The local copy
  is a superset: it holds branches that were never pushed, which is exactly
  the class `docs/09-backup-recovery.md` calls irreplaceable.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tools.ingest.config import IngestConfig, load_config
from tools.ingest.repos_report import (
    MIRROR_MAX_GIT_BYTES,
    MirrorPlan,
    build_inventory,
    default_roots,
    plan_mirrors,
)
from tools.ingest.t7 import resolve_t7
from tools.ingest.volumes import VolumeError, VolumeProber, diskutil_volume_prober

MIRRORS_RELATIVE = Path("10-code/mirrors")
ARCHIVES_RELATIVE = Path("10-code/archives")
TRUSTED_SKILLS_RELATIVE = Path("40-ai/skills/trusted")

# The manifest describes the export and is written by the export itself, so
# it is never counted as a stale leftover. Everything else in the destination
# either came from Git `skills/` or does not belong there -- including Git's
# own `skills/README.md`, which is exported like any other file.
EXPORT_RESERVED_NAMES = frozenset({"MANIFEST.yaml"})

GIT_READONLY_FLAGS = ("-c", "core.hooksPath=/dev/null", "--no-optional-locks")


class AngelSyncError(RuntimeError):
    """A refusal. Raised before anything is written, never part-way through."""


@dataclass(frozen=True)
class ResolvedTarget:
    root: Path
    volume_uuid: str


def resolve_target(
    config: IngestConfig,
    prober: VolumeProber,
    *,
    allow_dev_storage_root: bool = False,
) -> ResolvedTarget:
    """Resolve the volume root, or raise. The only way into a write path here."""
    resolved = resolve_t7(config, prober, for_apply=False, allow_storage_root_override=allow_dev_storage_root)
    if not resolved.volume_uuid:
        raise AngelSyncError("writing to Angel requires a UUID-verified volume")
    return ResolvedTarget(root=resolved.root, volume_uuid=resolved.volume_uuid)


def _git(*args: str, cwd: Path | None = None, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *GIT_READONLY_FLAGS, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=str(cwd) if cwd else None,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dir_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())


# ---------------------------------------------------------------------------
# Mirrors
# ---------------------------------------------------------------------------


@dataclass
class MirrorResult:
    name: str
    path: Path
    action: str  # "created" | "refreshed" | "skipped" | "failed"
    size_bytes: int | None
    detail: str


def _record_provenance(mirror: Path, plan: MirrorPlan) -> None:
    """Remember where a mirror came from, in the mirror's own config.

    `angel.sourcePath` is what a refresh fetches from; `angel.sourceRemote`
    is the GitHub URL a human should set as `origin` after cloning out of the
    mirror. Neither is read by Git itself.
    """
    _git("--git-dir", str(mirror), "config", "angel.sourcePath", str(plan.source_path))
    _git("--git-dir", str(mirror), "config", "angel.sourceRemote", plan.remote_url)


def refresh_mirror(plan: MirrorPlan, mirrors_dir: Path, *, verify: bool = True) -> MirrorResult:
    """Create or update one bare mirror. Never deletes, never pushes."""
    mirror = mirrors_dir / f"{plan.mirror_name}.git"
    if not plan.source_path.is_dir():
        return MirrorResult(plan.mirror_name, mirror, "skipped", None, f"source is gone: {plan.source_path}")

    if mirror.exists():
        # A mirror's fetch refspec is `+refs/*:refs/*`. `remote update`
        # without `--prune` deliberately keeps refs the source has deleted:
        # this is backup coverage, so losing history to an upstream deletion
        # would defeat the purpose.
        proc = _git("--git-dir", str(mirror), "remote", "update")
        if proc.returncode != 0:
            return MirrorResult(plan.mirror_name, mirror, "failed", None, (proc.stderr or proc.stdout).strip()[:500])
        _record_provenance(mirror, plan)
        return MirrorResult(plan.mirror_name, mirror, "refreshed", _dir_bytes(mirror), "fetched from the local source")

    staging = mirrors_dir / f".{plan.mirror_name}.git.partial"
    if staging.exists():
        return MirrorResult(
            plan.mirror_name, mirror, "skipped", None,
            f"an interrupted clone is still at {staging}; move it aside by hand before retrying",
        )
    # `--no-hardlinks` matters: a local clone would otherwise share object
    # files with the source when the filesystems allow it, and docs/02 forbids
    # a shared inode between a source and a derived copy on principle.
    proc = _git("clone", "--mirror", "--no-hardlinks", str(plan.source_path), str(staging))
    if proc.returncode != 0:
        return MirrorResult(plan.mirror_name, mirror, "failed", None, (proc.stderr or proc.stdout).strip()[:500])

    if verify:
        check = _git("--git-dir", str(staging), "fsck", "--connectivity-only", "--no-progress")
        if check.returncode != 0:
            return MirrorResult(
                plan.mirror_name, staging, "failed", _dir_bytes(staging),
                "git fsck failed on the fresh clone; left in place as "
                f"{staging.name} for inspection: {(check.stderr or check.stdout).strip()[:300]}",
            )

    staging.rename(mirror)
    _record_provenance(mirror, plan)
    return MirrorResult(plan.mirror_name, mirror, "created", _dir_bytes(mirror), "cloned from the local working copy")


def cmd_mirrors(args: argparse.Namespace, prober: VolumeProber | None = None) -> int:
    config = load_config(args.config)
    target = resolve_target(
        config, prober or diskutil_volume_prober, allow_dev_storage_root=bool(args.allow_dev_storage_root)
    )
    mirrors_dir = target.root / MIRRORS_RELATIVE
    print(f"Volume UUID verified: {target.volume_uuid}")
    print(f"Mirrors: {mirrors_dir}\n")

    records = build_inventory(default_roots(), config=config)
    plans, skips = plan_mirrors(records, max_git_bytes=args.max_git_bytes)
    if args.only:
        wanted = set(args.only)
        plans = [plan for plan in plans if plan.mirror_name in wanted]
        missing = wanted - {plan.mirror_name for plan in plans}
        for name in sorted(missing):
            print(f"  no eligible repository named {name}")

    if args.dry_run:
        print("Dry run. Would mirror:")
        for plan in plans:
            print(f"  {plan.mirror_name}.git  <-  {plan.source_path}  ({plan.git_dir_bytes / 1048576:.1f} MiB of .git)")
        print(f"\nWould skip {len(skips)} repositories:")
        for skip in skips:
            print(f"  {skip.path}: {skip.reason}")
        return 0

    mirrors_dir.mkdir(parents=True, exist_ok=True)
    results = [refresh_mirror(plan, mirrors_dir, verify=not args.no_verify) for plan in plans]
    failures = [result for result in results if result.action == "failed"]
    for result in results:
        size = f"{result.size_bytes / 1048576:.1f} MiB" if result.size_bytes else "-"
        print(f"  {result.action:<10} {result.name}.git  {size}  {result.detail}")
    print(f"\n{len(results)} mirrors processed; {len(failures)} failed; {len(skips)} repositories not mirrored.")
    if not args.quiet_skips:
        for skip in skips:
            print(f"  not mirrored: {skip.path}: {skip.reason}")
    return 1 if failures else 0


def cmd_archive(args: argparse.Namespace, prober: VolumeProber | None = None) -> int:
    """Archive one named repository as a bare mirror under `10-code/archives/`.

    Deliberately explicit rather than inferred: archiving is a statement that
    a repository is no longer worked in, and the owner makes it. The source
    working copy is never touched.
    """
    config = load_config(args.config)
    target = resolve_target(
        config, prober or diskutil_volume_prober, allow_dev_storage_root=bool(args.allow_dev_storage_root)
    )
    source = args.source.expanduser()
    if not (source / ".git").exists():
        print(f"ERROR: not a git repository: {source}", file=sys.stderr)
        return 2
    remote = _git("--git-dir", str(source / ".git"), "remote", "get-url", "origin")
    remote_url = remote.stdout.strip() if remote.returncode == 0 else ""
    head = _git("--git-dir", str(source / ".git"), "rev-parse", "HEAD")
    if head.returncode != 0:
        print(f"ERROR: {source} has no commits; nothing to archive", file=sys.stderr)
        return 2
    head_sha = head.stdout.strip()

    if args.require_contained_in:
        contained = _git(
            "--git-dir", str(source / ".git"), "merge-base", "--is-ancestor", head_sha, args.require_contained_in
        )
        if contained.returncode != 0:
            print(
                f"REFUSING: HEAD {head_sha[:8]} is not contained in {args.require_contained_in}. "
                "Archiving it would imply the history is already in the remote when it is not.",
                file=sys.stderr,
            )
            return 3
        print(f"HEAD {head_sha[:8]} is contained in {args.require_contained_in}")

    archives_dir = target.root / ARCHIVES_RELATIVE
    archives_dir.mkdir(parents=True, exist_ok=True)
    plan = MirrorPlan(mirror_name=args.name, source_path=source, remote_url=remote_url, git_dir_bytes=0)
    print(f"Volume UUID verified: {target.volume_uuid}")
    if args.dry_run:
        print(f"Dry run. Would archive {source} as {archives_dir / (args.name + '.git')}")
        return 0
    result = refresh_mirror(plan, archives_dir, verify=True)
    size = f"{result.size_bytes / 1048576:.1f} MiB" if result.size_bytes else "-"
    print(f"  {result.action:<10} {result.name}.git  {size}  {result.detail}")
    print(f"\nThe original at {source} was not touched.")
    return 1 if result.action == "failed" else 0


# ---------------------------------------------------------------------------
# Trusted skill export
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def export_trusted_skills(
    target: ResolvedTarget,
    *,
    skills_dir: Path | None = None,
    now: datetime | None = None,
) -> tuple[Path, list[Path], list[Path]]:
    """Copy Git `skills/` to the volume with a sha256 + git SHA manifest.

    Returns `(manifest_path, exported_files, stale_files)`. Stale files --
    present in the export but no longer in Git -- are reported, never
    deleted: this module does not remove anything from the volume.
    """
    source = skills_dir or (repo_root() / "skills")
    if not source.is_dir():
        raise AngelSyncError(f"no skills/ directory to export: {source}")
    destination = target.root / TRUSTED_SKILLS_RELATIVE
    destination.mkdir(parents=True, exist_ok=True)

    tracked = sorted(
        path for path in source.rglob("*") if path.is_file() and not path.is_symlink() and ".git" not in path.parts
    )
    exported: list[Path] = []
    entries: list[tuple[str, str, int]] = []
    for path in tracked:
        relative = path.relative_to(source)
        out = destination / relative
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
        exported.append(relative)
        entries.append((str(relative), _sha256(out), out.stat().st_size))

    expected = {str(relative) for relative in exported}
    stale = [
        path.relative_to(destination)
        for path in sorted(destination.rglob("*"))
        if path.is_file()
        and str(path.relative_to(destination)) not in expected
        and path.relative_to(destination).name not in EXPORT_RESERVED_NAMES
    ]

    commit = _git("rev-parse", "HEAD", cwd=repo_root())
    describe = _git("status", "--porcelain", cwd=repo_root())
    git_sha = commit.stdout.strip() if commit.returncode == 0 else "unknown"
    dirty = bool(describe.stdout.strip()) if describe.returncode == 0 else None
    generated = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    lines = [
        "# Trusted skill export. Git is the source of truth; this is a copy.",
        "# Refresh: ./scripts/export-angel-skills",
        "#",
        "# Do not edit anything in this directory. An edit reaches no agent and",
        "# is discarded by the next export. Change a skill in Git, via a",
        "# reviewed pull request (docs/26-skills-and-knowledge-trust.md).",
        "schema_version: 1",
        f'generated_at: "{generated}"',
        f'source_repository: "ai-homelab-control-plane"',
        f'source_git_sha: "{git_sha}"',
        f"source_working_tree_dirty: {'true' if dirty else 'false' if dirty is not None else 'null'}",
        f'volume_uuid: "{target.volume_uuid}"',
        "trust: trusted-reviewed",
        "executable_from_here: false",
        "files:",
    ]
    for relative, digest, size in entries:
        lines.append(f'  - path: "{relative}"')
        lines.append(f'    sha256: "{digest}"')
        lines.append(f"    size_bytes: {size}")
    if stale:
        lines.append("# Present in this export but no longer in Git. Nothing is deleted from")
        lines.append("# the volume by this tool; reconcile by hand.")
        lines.append("stale_files:")
        lines.extend(f'  - "{path}"' for path in stale)
    else:
        lines.append("stale_files: []")

    manifest = destination / "MANIFEST.yaml"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest, exported, stale


def cmd_skills(args: argparse.Namespace, prober: VolumeProber | None = None) -> int:
    config = load_config(args.config)
    target = resolve_target(
        config, prober or diskutil_volume_prober, allow_dev_storage_root=bool(args.allow_dev_storage_root)
    )
    print(f"Volume UUID verified: {target.volume_uuid}")
    destination = target.root / TRUSTED_SKILLS_RELATIVE
    if args.dry_run:
        source = repo_root() / "skills"
        print(f"Dry run. Would export {source} -> {destination}")
        for path in sorted(source.rglob("*")):
            if path.is_file():
                print(f"  {path.relative_to(source)}")
        return 0
    manifest, exported, stale = export_trusted_skills(target)
    print(f"Exported {len(exported)} files to {destination}")
    print(f"Manifest: {manifest}")
    if stale:
        print("\nStale files remain in the export (not deleted by this tool):")
        for path in stale:
            print(f"  {destination / path}")
        print("Reconcile by hand; they are listed in MANIFEST.yaml under stale_files.")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="angel-sync", description=__doc__)
    # Common flags live on every subcommand rather than before it, so the thin
    # `scripts/*` wrappers can pass through whatever the caller typed without
    # having to know which flags argparse wants in front of the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help="Path to ingest.yaml (default: config/ingest.yaml).")
    common.add_argument(
        "--allow-dev-storage-root",
        action="store_true",
        help="Permit t7.storage_root_override, which bypasses UUID verification. Development only.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    mirrors = subparsers.add_parser(
        "mirrors", parents=[common], help="Create or refresh bare mirrors under 10-code/mirrors/."
    )
    mirrors.add_argument("--dry-run", action="store_true", help="Report what would be mirrored; write nothing.")
    mirrors.add_argument("--only", action="append", default=None, help="Mirror name to process. Repeatable.")
    mirrors.add_argument("--max-git-bytes", type=int, default=MIRROR_MAX_GIT_BYTES, help="Skip repos with a larger .git.")
    mirrors.add_argument("--no-verify", action="store_true", help="Skip `git fsck` on a freshly created mirror.")
    mirrors.add_argument("--quiet-skips", action="store_true", help="Do not list skipped repositories.")

    archive = subparsers.add_parser(
        "archive", parents=[common], help="Archive one repository as a bare mirror under 10-code/archives/."
    )
    archive.add_argument("--source", type=Path, required=True, help="The working copy to archive. Never modified.")
    archive.add_argument("--name", required=True, help="Mirror name; `<name>.git` is created.")
    archive.add_argument(
        "--require-contained-in",
        default=None,
        help="Refuse unless HEAD is an ancestor of this ref (e.g. origin/main).",
    )
    archive.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")

    skills = subparsers.add_parser(
        "skills", parents=[common], help="Export Git skills/ to 40-ai/skills/trusted/ with a manifest."
    )
    skills.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")

    return parser


def main(argv: list[str] | None = None, prober: VolumeProber | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"mirrors": cmd_mirrors, "archive": cmd_archive, "skills": cmd_skills}[args.command]
    try:
        return handler(args, prober=prober)
    except (AngelSyncError, VolumeError) as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
