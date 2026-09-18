"""Read-only development-repository inventory (PLAN.md Phase 1).

What this module is for: answering "where does committed work actually live
on this machine, what of it is not in a remote, and what should its home be"
without touching a single repository.

Three rules hold for every code path here:

1. **Detection is not reimplemented.** Discovery runs
   `tools.ingest.scanner.scan_source`, so the inventory sees exactly the
   directories ingestion sees and honours the same exclusions. Per-repository
   facts come from `tools.ingest.git_repos`, the existing read-only
   inspection, whose git invocations disable hooks and never write.
2. **Nothing is moved.** There is no `--apply`. The module emits a YAML
   inventory and a Markdown report and returns; relocation is an owner
   decision recorded as a recommendation (`docs/25-angel-volume-layout.md`).
3. **A recommendation is not an action.** `recommended_home` is prose for a
   human. No other module reads it, and no tool in this repository acts on
   it.

Source roots are Google-Drive-synced (`~/Documents`) and span trust domains
(`kronos-quant-lab`, D-011), which is the concrete reason for rule 2.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tools.ingest.config import IngestConfig
from tools.ingest.git_repos import GitRepoCounts, inspect_git_repo_counts, is_linked_worktree
from tools.ingest.scanner import scan_source

# A repository whose newest commit is older than this, and which is fully
# pushed and clean, is *reported* as archive-shaped. Nothing acts on it.
ARCHIVE_STALE_DAYS = 180

# The canonical home for active working trees (docs/02, docs/25): the Mac's
# internal disk, never Angel and never a synced folder.
CANONICAL_DEV_HOME = Path("~/Projects")

# Repositories that belong to a separate trust domain (D-011). They are
# inventoried -- knowing where they are is the point -- but never proposed
# for relocation and never mirrored by this repository's tooling.
SEPARATE_TRUST_DOMAIN_NAMES = frozenset({"kronos-quant-lab"})

# Roots that a third party synchronises. Moving anything inside one is an
# owner decision, because the move replicates to Drive.
SYNCED_ROOT_HINTS = ("Documents", "Google Drive", "Dropbox", "OneDrive", "iCloud Drive")

CLASSIFICATIONS = ("active", "archive", "duplicate", "no-remote")


@dataclass(frozen=True)
class RepoRecord:
    """One inventoried repository. Every field is observed or derived, never acted on."""

    path: Path
    name: str
    source_root: Path
    nested_under: Path | None
    remote_url: str | None
    branch: str | None
    head: str | None
    dirty_count: int | None
    unpushed_count: int | None
    stash_count: int | None
    last_commit_iso: str | None
    default_remote_branch: str | None
    head_in_default_remote: bool | None
    git_dir_bytes: int | None
    work_tree_bytes: int | None
    classification: str
    recommended_home: str
    mirror_eligible: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_attention(self) -> bool:
        """True when committed history alone would not preserve this repo."""
        return bool(
            (self.unpushed_count or 0) > 0
            or (self.dirty_count or 0) > 0
            or (self.stash_count or 0) > 0
            or self.remote_url is None
        )


def _is_synced(path: Path) -> bool:
    parts = set(path.parts)
    return any(hint in parts for hint in SYNCED_ROOT_HINTS)


def _normalize_remote(url: str | None) -> str | None:
    """Compare remotes by identity, not by transport spelling."""
    if not url:
        return None
    normalized = url.strip()
    normalized = normalized.removesuffix(".git")
    for prefix in ("git@github.com:", "ssh://git@github.com/", "https://github.com/", "git://github.com/"):
        if normalized.startswith(prefix):
            return "github:" + normalized[len(prefix) :].lower()
    return normalized.lower()


def _age_days(last_commit_iso: str | None, *, now: datetime | None = None) -> float | None:
    if not last_commit_iso:
        return None
    try:
        committed = datetime.fromisoformat(last_commit_iso)
    except ValueError:
        return None
    if committed.tzinfo is None:
        # `git log --format=%cI` always emits an offset, so this is a
        # defensive branch. It uses `datetime.combine` rather than
        # `datetime.replace` on purpose: `tests/ingest/test_mutation_authority.py`
        # treats any non-string-literal `.replace(` under `tools/ingest/` as
        # possible rename authority, and that guard is worth more than the
        # shorter spelling.
        committed = datetime.combine(committed.date(), committed.time(), tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return (reference - committed).total_seconds() / 86400.0


def discover_repositories(
    roots: tuple[Path, ...],
    *,
    config: IngestConfig,
    max_nesting: int = 3,
    skipped_worktrees: list[Path] | None = None,
) -> list[tuple[Path, Path, Path | None]]:
    """Find repositories under `roots` as `(repo, source_root, nested_under)`.

    Uses `scan_source`, the ingestion walker, so exclusions match exactly.
    `scan_source` stops at a repository boundary (a repo is an exclusion, not
    a place to walk into), so this re-runs it rooted at each repository found
    to surface nested repositories such as `dashboard-demos/sources/*`.

    Linked `git worktree` checkouts are appended to `skipped_worktrees` rather
    than inventoried: they are views of a repository this inventory already
    records, so counting them would double-count the same history.
    """
    if skipped_worktrees is None:
        skipped_worktrees = []
    found: list[tuple[Path, Path, Path | None]] = []
    seen: set[Path] = set()

    def walk(root: Path, source_root: Path, nested_under: Path | None, depth: int) -> None:
        if depth > max_nesting:
            return
        scan = scan_source(root, config=config)
        if scan.missing:
            return
        for repo in sorted(scan.git_repos, key=lambda info: str(info.path)):
            resolved = repo.path
            if resolved in seen:
                continue
            seen.add(resolved)
            if is_linked_worktree(resolved):
                # A linked worktree is a view of a repository already in this
                # inventory, not a repository. Its contents are still walked,
                # because a worktree can contain nested clones.
                skipped_worktrees.append(resolved)
            else:
                found.append((resolved, source_root, nested_under))
            walk(resolved, source_root, resolved, depth + 1)

    for root in roots:
        expanded = root.expanduser()
        walk(expanded, expanded, None, 1)
    return found


def classify_repo(
    *,
    path: Path,
    counts: GitRepoCounts,
    duplicate_of: Path | None,
    now: datetime | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Classify one repository. Pure function over observed facts.

    Ordering is deliberate. "No remote" outranks everything, because a repo
    with no remote has no second copy regardless of how tidy it looks.
    "Duplicate" requires the copy to be provably redundant -- clean, nothing
    unpushed, no stashes, and HEAD reachable from the remote's default branch
    -- so a divergent second checkout is never labelled disposable.
    """
    details = counts.details
    notes: list[str] = []

    if details.remote_url is None:
        if counts.last_commit_iso is None:
            notes.append("no origin remote and no commits: an initialized but unused `.git`")
        else:
            notes.append("no origin remote: committed history exists in exactly one place")
        return "no-remote", tuple(notes)

    fully_pushed = (counts.unpushed_count == 0) and (counts.dirty_count == 0) and (counts.stash_count == 0)
    contained = counts.head_in_default_remote is True

    if duplicate_of is not None:
        if fully_pushed and contained:
            notes.append(f"same origin as {duplicate_of}; HEAD is contained in {counts.default_remote_branch}")
            return "duplicate", tuple(notes)
        notes.append(
            f"shares an origin with {duplicate_of} but is not provably redundant "
            "(local work, or HEAD not contained in the remote default branch)"
        )
        return "active", tuple(notes)

    age = _age_days(counts.last_commit_iso, now=now)
    if fully_pushed and contained and age is not None and age > ARCHIVE_STALE_DAYS:
        notes.append(f"clean, fully pushed, last commit {age:.0f} days ago")
        return "archive", tuple(notes)

    if not fully_pushed:
        notes.append("has local state a remote does not hold")
    return "active", tuple(notes)


def recommend_home(
    *,
    path: Path,
    name: str,
    classification: str,
    separate_trust_domain: bool,
    duplicate_of: Path | None,
) -> tuple[str, bool]:
    """Return `(recommended_home, mirror_eligible)` -- prose plus one flag.

    The flag gates `scripts/refresh-angel-mirrors`; the prose gates nothing
    and is addressed to the owner.
    """
    if separate_trust_domain:
        return (
            "stays where it is, inside the separate Kronos trust domain (D-011); "
            "not mirrored by this repository's tooling and not relocated by it",
            False,
        )
    home = f"{CANONICAL_DEV_HOME}/{name}"
    if classification == "no-remote":
        return (
            f"owner decision: create a remote (or accept it as disposable), then {home}; "
            "report-only until then, because there is nothing to mirror",
            False,
        )
    if classification == "duplicate":
        return (
            f"owner decision: archive as a bare mirror under Angel `10-code/archives/` and keep working in {home}; "
            f"the original stays in place untouched (redundant with {duplicate_of})",
            False,
        )
    if classification == "archive":
        return (
            f"owner decision: bare mirror under Angel `10-code/archives/`; "
            f"restore to {home} if it becomes active again",
            True,
        )
    under_synced = _is_synced(path)
    suffix = (
        " -- it currently sits inside a Google-Drive-synced root, so the move is an owner decision, never automatic"
        if under_synced
        else ""
    )
    return (f"{home} (canonical dev home), with a bare mirror under Angel `10-code/mirrors/` for backup coverage{suffix}", True)


def build_inventory(
    roots: tuple[Path, ...],
    *,
    config: IngestConfig,
    now: datetime | None = None,
    measure_size: bool = True,
    skipped_worktrees: list[Path] | None = None,
) -> list[RepoRecord]:
    """Inventory every repository under `roots`. Performs no writes at all."""
    skipped_worktrees = skipped_worktrees if skipped_worktrees is not None else []
    discovered = discover_repositories(roots, config=config, skipped_worktrees=skipped_worktrees)
    inspected: list[tuple[Path, Path, Path | None, GitRepoCounts]] = []
    for path, source_root, nested_under in discovered:
        inspected.append((path, source_root, nested_under, inspect_git_repo_counts(path, measure_size=measure_size)))

    # Duplicate detection needs the whole set, so it happens after inspection.
    by_remote: dict[str, list[tuple[Path, GitRepoCounts]]] = {}
    for path, _root, _nested, counts in inspected:
        key = _normalize_remote(counts.details.remote_url)
        if key:
            by_remote.setdefault(key, []).append((path, counts))

    def canonical_for(key: str) -> Path | None:
        """The copy that owns a shared remote: the one under the canonical dev home."""
        group = by_remote[key]
        if len(group) < 2:
            return None
        canonical_root = CANONICAL_DEV_HOME.expanduser()
        candidates = [path for path, _ in group if canonical_root in path.parents] or [path for path, _ in group]
        # Shallowest first: a copy at `~/Documents/<name>` is the one the owner
        # works in; a copy buried under an agent scratch directory is not.
        return sorted(candidates, key=lambda path: (len(path.parts), str(path)))[0]

    records: list[RepoRecord] = []
    for path, source_root, nested_under, counts in inspected:
        key = _normalize_remote(counts.details.remote_url)
        owner = canonical_for(key) if key else None
        duplicate_of = owner if owner is not None and owner != path else None
        classification, notes = classify_repo(path=path, counts=counts, duplicate_of=duplicate_of, now=now)
        name = path.name
        separate = name in SEPARATE_TRUST_DOMAIN_NAMES
        recommended, mirror_eligible = recommend_home(
            path=path,
            name=name,
            classification=classification,
            separate_trust_domain=separate,
            duplicate_of=duplicate_of,
        )
        extra = list(notes)
        if separate:
            extra.append("separate trust domain (D-011): never mirrored or relocated by this tooling")
        if _is_synced(path):
            extra.append("inside a third-party-synced root; a move here replicates to the sync provider")
        if nested_under is not None:
            extra.append(f"nested inside {nested_under}")
        records.append(
            RepoRecord(
                path=path,
                name=name,
                source_root=source_root,
                nested_under=nested_under,
                remote_url=counts.details.remote_url,
                branch=counts.details.branch,
                head=counts.details.head,
                dirty_count=counts.dirty_count,
                unpushed_count=counts.unpushed_count,
                stash_count=counts.stash_count,
                last_commit_iso=counts.last_commit_iso,
                default_remote_branch=counts.default_remote_branch,
                head_in_default_remote=counts.head_in_default_remote,
                git_dir_bytes=counts.git_dir_bytes,
                work_tree_bytes=counts.work_tree_bytes,
                classification=classification,
                recommended_home=recommended,
                mirror_eligible=mirror_eligible,
                notes=tuple(extra),
            )
        )
    return sorted(records, key=lambda record: str(record.path))


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


def _yaml_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_inventory_yaml(
    records: list[RepoRecord],
    *,
    roots: tuple[Path, ...],
    generated_at: str,
    host: str,
    skipped_worktrees: list[Path] | None = None,
) -> str:
    """Hand-rendered so key order is stable and a diff between runs is readable."""
    lines = [
        "# Development-repository inventory. Generated, read-only, never acted on.",
        "# Regenerate: ./scripts/repos-inventory --report-dir <dir>",
        "schema_version: 1",
        f"generated_at: {_yaml_scalar(generated_at)}",
        f"host: {_yaml_scalar(host)}",
        f"canonical_dev_home: {_yaml_scalar(str(CANONICAL_DEV_HOME))}",
        f"archive_stale_days: {ARCHIVE_STALE_DAYS}",
        "source_roots:",
    ]
    lines.extend(f"  - {_yaml_scalar(str(root))}" for root in roots)
    worktrees = sorted(skipped_worktrees or [], key=str)
    if worktrees:
        lines.append("# Linked `git worktree` checkouts: views of a repository listed below, not repositories.")
        lines.append("skipped_linked_worktrees:")
        lines.extend(f"  - {_yaml_scalar(str(path))}" for path in worktrees)
    else:
        lines.append("skipped_linked_worktrees: []")
    plans, skips = plan_mirrors(records)
    lines.append("angel_mirror_plan:")
    lines.append(f"  max_git_bytes: {MIRROR_MAX_GIT_BYTES}")
    if plans:
        lines.append("  mirrors:")
        for plan in plans:
            lines.append(f"    - name: {_yaml_scalar(plan.mirror_name + '.git')}")
            lines.append(f"      cloned_from: {_yaml_scalar(str(plan.source_path))}")
            lines.append(f"      remote: {_yaml_scalar(plan.remote_url)}")
            lines.append(f"      git_dir_bytes: {plan.git_dir_bytes}")
    else:
        lines.append("  mirrors: []")
    if skips:
        lines.append("  not_mirrored:")
        for skip in skips:
            lines.append(f"    - path: {_yaml_scalar(str(skip.path))}")
            lines.append(f"      reason: {_yaml_scalar(skip.reason)}")
    else:
        lines.append("  not_mirrored: []")
    lines.append("repositories:")
    if not records:
        lines[-1] = "repositories: []"
        return "\n".join(lines) + "\n"
    for record in records:
        lines.append(f"  - path: {_yaml_scalar(str(record.path))}")
        lines.append(f"    name: {_yaml_scalar(record.name)}")
        lines.append(f"    source_root: {_yaml_scalar(str(record.source_root))}")
        lines.append(f"    nested_under: {_yaml_scalar(str(record.nested_under) if record.nested_under else None)}")
        lines.append(f"    remote: {_yaml_scalar(record.remote_url)}")
        lines.append(f"    branch: {_yaml_scalar(record.branch)}")
        lines.append(f"    head: {_yaml_scalar(record.head)}")
        lines.append(f"    dirty_count: {_yaml_scalar(record.dirty_count)}")
        lines.append(f"    unpushed_count: {_yaml_scalar(record.unpushed_count)}")
        lines.append(f"    stash_count: {_yaml_scalar(record.stash_count)}")
        lines.append(f"    last_commit: {_yaml_scalar(record.last_commit_iso)}")
        lines.append(f"    default_remote_branch: {_yaml_scalar(record.default_remote_branch)}")
        lines.append(f"    head_in_default_remote: {_yaml_scalar(record.head_in_default_remote)}")
        lines.append(f"    git_dir_bytes: {_yaml_scalar(record.git_dir_bytes)}")
        lines.append(f"    work_tree_bytes: {_yaml_scalar(record.work_tree_bytes)}")
        lines.append(f"    classification: {_yaml_scalar(record.classification)}")
        lines.append(f"    mirror_eligible: {_yaml_scalar(record.mirror_eligible)}")
        lines.append(f"    recommended_home: {_yaml_scalar(record.recommended_home)}")
        if record.notes:
            lines.append("    notes:")
            lines.extend(f"      - {_yaml_scalar(note)}" for note in record.notes)
        else:
            lines.append("    notes: []")
    return "\n".join(lines) + "\n"


def render_unpushed_markdown(
    records: list[RepoRecord],
    *,
    roots: tuple[Path, ...],
    generated_at: str,
    host: str,
    skipped_worktrees: list[Path] | None = None,
) -> str:
    """The human report: what is at risk first, then the full inventory."""
    attention = [record for record in records if record.needs_attention]
    lines = [
        "# Development repositories: unpushed work and recommended homes",
        "",
        f"Generated {generated_at} on `{host}` by `./scripts/repos-inventory`. Read-only:",
        "nothing in this report was moved, deleted, fetched, or pushed.",
        "",
        "Scanned roots:",
        "",
    ]
    lines.extend(f"- `{root}`" for root in roots)
    lines += [
        "",
        f"{len(records)} repositories found; {len(attention)} hold state that a Git remote does not.",
        "",
        (
            f"{len(skipped_worktrees or [])} linked `git worktree` checkouts were skipped: each is a view of a "
            "repository already listed here, so counting one would double-count the same history."
        ),
        "",
        "## Read this first",
        "",
        "A repository appears below because committed history alone would **not** preserve it:",
        "it has unpushed commits, uncommitted changes, stashes, or no remote at all. Everything",
        "else is recoverable from GitHub (`docs/09-backup-recovery.md`).",
        "",
    ]
    if attention:
        lines += [
            "| Repository | Branch | Dirty | Unpushed | Stashes | Remote | Classification |",
            "|---|---|---|---|---|---|---|",
        ]
        for record in attention:
            remote = record.remote_url or "**none**"
            lines.append(
                f"| `{record.path}` | {record.branch or '-'} | {record.dirty_count if record.dirty_count is not None else '?'} "
                f"| {record.unpushed_count if record.unpushed_count is not None else '?'} "
                f"| {record.stash_count if record.stash_count is not None else '?'} | {remote} | {record.classification} |"
            )
        lines.append("")
    else:
        lines += ["No repository holds unpushed, uncommitted, or stashed work.", ""]

    lines += [
        "## Full inventory",
        "",
        "| Repository | Classification | Branch | HEAD | Remote | Work tree | `.git` | Last commit |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for record in records:
        head = (record.head or "")[:8] or "-"
        lines.append(
            f"| `{record.path}` | {record.classification} | {record.branch or '-'} | `{head}` "
            f"| {record.remote_url or '**none**'} | {_human_bytes(record.work_tree_bytes)} "
            f"| {_human_bytes(record.git_dir_bytes)} | {record.last_commit_iso or '-'} |"
        )

    plans, skips = plan_mirrors(records)
    lines += [
        "",
        "## Angel mirror coverage",
        "",
        f"One bare mirror per distinct GitHub remote, cloned from the local copy with the largest `.git` "
        f"under a {_human_bytes(MIRROR_MAX_GIT_BYTES)} cap. A mirror is not a working tree; refresh with",
        "`./scripts/refresh-angel-mirrors`.",
        "",
        "| Mirror | Cloned from | `.git` |",
        "|---|---|---|",
    ]
    for plan in plans:
        lines.append(f"| `10-code/mirrors/{plan.mirror_name}.git` | `{plan.source_path}` | {_human_bytes(plan.git_dir_bytes)} |")
    lines += ["", "Not mirrored, and why:", ""]
    for skip in skips:
        lines.append(f"- `{skip.path}` — {skip.reason}")
    lines += [
        "",
        "## Recommended home (owner decision, not an action)",
        "",
        "Nothing in this repository moves an owner repository. `~/Documents` is Google-Drive-synced,",
        "so a move there replicates; `kronos-quant-lab` is a separate trust domain (D-011) and is",
        "neither relocated nor mirrored by this tooling. The recommendation below is prose for the",
        "owner to act on by hand, and is restated in `docs/25-angel-volume-layout.md`.",
        "",
    ]
    for record in records:
        lines.append(f"### `{record.path}`")
        lines.append("")
        lines.append(f"- classification: **{record.classification}**")
        lines.append(f"- recommended home: {record.recommended_home}")
        lines.append(f"- eligible for an Angel bare mirror: {'yes' if record.mirror_eligible else 'no'}")
        for note in record.notes:
            lines.append(f"- note: {note}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_reports(
    records: list[RepoRecord],
    *,
    report_dir: Path,
    roots: tuple[Path, ...],
    generated_at: str | None = None,
    host: str | None = None,
    skipped_worktrees: list[Path] | None = None,
) -> tuple[Path, Path]:
    """Write both reports into `report_dir`. The only writes this module makes."""
    generated_at = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    host = host or socket.gethostname()
    report_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = report_dir / "repos-inventory.yaml"
    md_path = report_dir / "unpushed-repos.md"
    yaml_path.write_text(
        render_inventory_yaml(
            records, roots=roots, generated_at=generated_at, host=host, skipped_worktrees=skipped_worktrees or []
        ),
        encoding="utf-8",
    )
    md_path.write_text(
        render_unpushed_markdown(
            records, roots=roots, generated_at=generated_at, host=host, skipped_worktrees=skipped_worktrees or []
        ),
        encoding="utf-8",
    )
    return yaml_path, md_path


def default_roots() -> tuple[Path, ...]:
    """The roots PLAN.md Phase 1 names, filtered to those that exist."""
    candidates = (Path("~/Documents"), Path("~/Projects"), Path("~/OpenMontage"))
    return tuple(path for path in candidates if path.expanduser().is_dir())


MIRROR_MAX_GIT_BYTES = 1024 ** 3


@dataclass(frozen=True)
class MirrorPlan:
    """One bare mirror to create on Angel, and the local copy to clone it from."""

    mirror_name: str
    source_path: Path
    remote_url: str
    git_dir_bytes: int


@dataclass(frozen=True)
class MirrorSkip:
    """A repository deliberately not mirrored, and the reason to report."""

    path: Path
    remote_url: str | None
    git_dir_bytes: int | None
    reason: str


def _ineligibility_reason(record: RepoRecord) -> str:
    """Why a repository is not mirrored, in the inventory's own vocabulary."""
    if record.name in SEPARATE_TRUST_DOMAIN_NAMES:
        return "separate trust domain (D-011): never mirrored by this tooling"
    if record.classification == "no-remote":
        return "no origin remote: there is nothing to name a mirror after, and no upstream to compare against"
    if record.classification == "duplicate":
        return "redundant duplicate: archive it under `10-code/archives/` instead of mirroring it"
    return f"classified {record.classification} and not marked mirror-eligible"


def plan_mirrors(
    records: list[RepoRecord],
    *,
    max_git_bytes: int = MIRROR_MAX_GIT_BYTES,
) -> tuple[list[MirrorPlan], list[MirrorSkip]]:
    """Decide which bare mirrors to create, one per distinct GitHub remote.

    Eligibility is narrow on purpose:

    - a GitHub `origin` (nothing else has a name to mirror under);
    - not a separate trust domain (D-011) and not a redundant duplicate;
    - `.git` no larger than `max_git_bytes`, measured rather than guessed --
      an unmeasurable `.git` is skipped, never estimated.

    One mirror per remote, cloned from the local copy with the **largest**
    `.git` under the cap. Cloning locally rather than from GitHub is the point
    of the exercise: the local copy is a superset of the remote, so the mirror
    captures local-only branches that GitHub does not hold.
    """
    skips: list[MirrorSkip] = []
    by_remote: dict[str, list[RepoRecord]] = {}
    for record in records:
        key = _normalize_remote(record.remote_url)
        if not record.mirror_eligible:
            skips.append(MirrorSkip(record.path, record.remote_url, record.git_dir_bytes, _ineligibility_reason(record)))
            continue
        if not key or not key.startswith("github:") or not record.remote_url:
            skips.append(MirrorSkip(record.path, record.remote_url, record.git_dir_bytes, "origin is not a GitHub remote"))
            continue
        if record.git_dir_bytes is None:
            skips.append(MirrorSkip(record.path, record.remote_url, None, "`.git` size could not be measured"))
            continue
        if record.git_dir_bytes > max_git_bytes:
            skips.append(
                MirrorSkip(
                    record.path,
                    record.remote_url,
                    record.git_dir_bytes,
                    f"`.git` is {_human_bytes(record.git_dir_bytes)}, over the {_human_bytes(max_git_bytes)} cap",
                )
            )
            continue
        by_remote.setdefault(key, []).append(record)

    plans: list[MirrorPlan] = []
    for key in sorted(by_remote):
        group = sorted(by_remote[key], key=lambda record: (-(record.git_dir_bytes or 0), str(record.path)))
        chosen = group[0]
        name = key.split("/")[-1]
        assert chosen.remote_url is not None and chosen.git_dir_bytes is not None
        plans.append(
            MirrorPlan(
                mirror_name=name,
                source_path=chosen.path,
                remote_url=chosen.remote_url,
                git_dir_bytes=chosen.git_dir_bytes,
            )
        )
        for other in group[1:]:
            skips.append(
                MirrorSkip(
                    other.path,
                    other.remote_url,
                    other.git_dir_bytes,
                    f"same remote as `{chosen.path}`, which is mirrored as `{name}.git`",
                )
            )
    return plans, sorted(skips, key=lambda skip: str(skip.path))


def cmd_repos_report(report_dir: Path, *, config: IngestConfig, roots: tuple[Path, ...] | None = None) -> int:
    """`organize repos-report`: inventory the source roots and write both files."""
    roots = roots or default_roots()
    if not roots:
        print("No source root exists on this machine; nothing to inventory.")
        return 0
    print("Inventorying development repositories (read-only; nothing is moved):")
    for root in roots:
        print(f"  {root.expanduser()}")
    skipped_worktrees: list[Path] = []
    records = build_inventory(roots, config=config, skipped_worktrees=skipped_worktrees)
    yaml_path, md_path = write_reports(
        records, report_dir=report_dir, roots=roots, skipped_worktrees=skipped_worktrees
    )
    attention = [record for record in records if record.needs_attention]
    print(f"\n{len(records)} repositories; {len(attention)} hold state no remote has.")
    if skipped_worktrees:
        print(f"  ({len(skipped_worktrees)} linked git worktrees skipped: views of repositories already listed)")
    counts: dict[str, int] = {}
    for record in records:
        counts[record.classification] = counts.get(record.classification, 0) + 1
    for classification in CLASSIFICATIONS:
        if classification in counts:
            print(f"  {classification}: {counts[classification]}")
    print(f"\nWrote {md_path}")
    print(f"Wrote {yaml_path}")
    return 0


__all__ = [
    "ARCHIVE_STALE_DAYS",
    "CANONICAL_DEV_HOME",
    "CLASSIFICATIONS",
    "RepoRecord",
    "SEPARATE_TRUST_DOMAIN_NAMES",
    "build_inventory",
    "classify_repo",
    "cmd_repos_report",
    "default_roots",
    "discover_repositories",
    "MIRROR_MAX_GIT_BYTES",
    "MirrorPlan",
    "MirrorSkip",
    "plan_mirrors",
    "recommend_home",
    "render_inventory_yaml",
    "render_unpushed_markdown",
    "write_reports",
]
