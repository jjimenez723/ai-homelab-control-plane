"""The development-repository inventory: classification, planning, rendering.

Every test builds real throwaway Git repositories in a temp directory. None
reads `~/Documents`, `~/Projects`, or a mounted volume, and none of them can:
the inventory has no `--apply`, performs no fetch and no push, and its only
writes are the two report files it is asked to produce.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.ingest.git_repos import (
    inspect_git_repo,
    inspect_git_repo_counts,
    is_linked_worktree,
    resolve_git_dir,
)
from tools.ingest.repos_report import (
    ARCHIVE_STALE_DAYS,
    CANONICAL_DEV_HOME,
    CLASSIFICATIONS,
    MIRROR_MAX_GIT_BYTES,
    SEPARATE_TRUST_DOMAIN_NAMES,
    RepoRecord,
    build_inventory,
    discover_repositories,
    plan_mirrors,
    recommend_home,
    render_inventory_yaml,
    render_unpushed_markdown,
    write_reports,
)

from tests.ingest.helpers import build_config

GIT_ENV_ARGS = (
    "-c", "user.name=Test",
    "-c", "user.email=test@example.invalid",
    "-c", "commit.gpgsign=false",
    "-c", "init.defaultBranch=main",
)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_ENV_ARGS, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return proc.stdout.strip()


def make_repo(path: Path, *, commits: int = 1, remote: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", *GIT_ENV_ARGS, "init", "-q", str(path)], check=True, timeout=60)
    for index in range(commits):
        (path / f"file-{index}.txt").write_text(f"content {index}\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"commit {index}")
    if remote:
        git(path, "remote", "add", "origin", remote)
    return path


def publish(source: Path, remote_dir: Path) -> None:
    """Give `source` a real remote whose default branch contains HEAD.

    A bare repository on disk stands in for GitHub: `origin/HEAD` and the
    remote-tracking refs it creates are what the inventory actually reads, and
    nothing here contacts a network.
    """
    subprocess.run(["git", *GIT_ENV_ARGS, "init", "-q", "--bare", str(remote_dir)], check=True, timeout=60)
    git(source, "remote", "add", "origin", str(remote_dir))
    git(source, "push", "-q", "origin", "HEAD:refs/heads/main")
    git(source, "fetch", "-q", "origin")
    git(source, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(source, "branch", "--set-upstream-to=origin/main", "main")


class GitInspectionTests(unittest.TestCase):
    def test_counts_are_counts_not_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = make_repo(Path(temp) / "repo", commits=2)
            (repo / "dirty-a.txt").write_text("a\n", encoding="utf-8")
            (repo / "dirty-b.txt").write_text("b\n", encoding="utf-8")
            counts = inspect_git_repo_counts(repo)
            self.assertEqual(counts.dirty_count, 2)
            self.assertTrue(inspect_git_repo(repo).dirty)
            self.assertEqual(counts.stash_count, 0)
            self.assertIsNotNone(counts.last_commit_iso)

    def test_a_local_only_branch_counts_as_unpushed(self):
        """The bug this replaces: `branch...origin/branch` reads 0 when no
        such remote ref exists, i.e. exactly when everything is unpushed."""
        with tempfile.TemporaryDirectory() as temp:
            repo = make_repo(Path(temp) / "repo", commits=1)
            publish(repo, Path(temp) / "remote.git")
            self.assertEqual(inspect_git_repo_counts(repo).unpushed_count, 0)
            git(repo, "checkout", "-q", "-b", "local-only")
            (repo / "new.txt").write_text("new\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "local only work")
            counts = inspect_git_repo_counts(repo)
            self.assertEqual(counts.unpushed_count, 1)
            # The old boolean cannot see it, which is why the count exists.
            self.assertIsNone(inspect_git_repo(repo).unpushed)

    def test_head_containment_is_reported_against_the_remote_default_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = make_repo(Path(temp) / "repo", commits=1)
            publish(repo, Path(temp) / "remote.git")
            counts = inspect_git_repo_counts(repo)
            self.assertEqual(counts.default_remote_branch, "origin/main")
            self.assertTrue(counts.head_in_default_remote)
            (repo / "later.txt").write_text("later\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "not pushed")
            self.assertFalse(inspect_git_repo_counts(repo).head_in_default_remote)

    def test_a_repository_with_no_commits_reports_no_last_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "empty"
            repo.mkdir()
            subprocess.run(["git", *GIT_ENV_ARGS, "init", "-q", str(repo)], check=True, timeout=60)
            counts = inspect_git_repo_counts(repo)
            self.assertIsNone(counts.last_commit_iso)
            self.assertIsNone(counts.details.remote_url)
            self.assertIsNone(counts.details.head)

    def test_a_linked_worktree_is_distinguished_from_a_submodule(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = make_repo(root / "repo", commits=1)
            worktree = root / "wt"
            git(repo, "worktree", "add", "-q", str(worktree), "-b", "side")
            self.assertTrue(is_linked_worktree(worktree))
            self.assertFalse(is_linked_worktree(repo))

            # A submodule also has a `.git` *file*, and must not be mistaken
            # for a worktree: it is a distinct repository with its own remote.
            fake_submodule = root / "parent" / "sub"
            fake_submodule.mkdir(parents=True)
            (fake_submodule / ".git").write_text("gitdir: ../../repo/.git/modules/sub\n", encoding="utf-8")
            self.assertFalse(is_linked_worktree(fake_submodule))

    def test_resolve_git_dir_follows_a_gitfile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = make_repo(root / "repo", commits=1)
            self.assertEqual(resolve_git_dir(repo), repo / ".git")
            worktree = root / "wt"
            git(repo, "worktree", "add", "-q", str(worktree), "-b", "side")
            resolved = resolve_git_dir(worktree)
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertIn("worktrees", resolved.parts)

    def test_git_dir_size_is_the_history_not_the_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = make_repo(Path(temp) / "repo", commits=1)
            (repo / "big.bin").write_bytes(b"\0" * 400_000)
            counts = inspect_git_repo_counts(repo)
            assert counts.git_dir_bytes is not None and counts.work_tree_bytes is not None
            self.assertLess(counts.git_dir_bytes, counts.work_tree_bytes)


class DiscoveryTests(unittest.TestCase):
    def _config(self, temp: Path):
        return build_config(desktop=temp / "Desktop", settle_seconds=0)

    def test_nested_repositories_are_found(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sources"
            outer = make_repo(root / "outer", commits=1)
            make_repo(outer / "vendor" / "inner", commits=1)
            found = discover_repositories((root,), config=self._config(Path(temp)))
            paths = {path for path, _root, _nested in found}
            self.assertIn(outer, paths)
            self.assertIn(outer / "vendor" / "inner", paths)
            nested = {path: parent for path, _root, parent in found}
            self.assertEqual(nested[outer / "vendor" / "inner"], outer)

    def test_linked_worktrees_are_skipped_and_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sources"
            repo = make_repo(root / "repo", commits=1)
            git(repo, "worktree", "add", "-q", str(root / "repo-side"), "-b", "side")
            skipped: list[Path] = []
            found = discover_repositories((root,), config=self._config(Path(temp)), skipped_worktrees=skipped)
            paths = {path for path, _root, _nested in found}
            self.assertEqual(paths, {repo})
            self.assertEqual(skipped, [root / "repo-side"])

    def test_a_missing_root_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            found = discover_repositories((Path(temp) / "absent",), config=self._config(Path(temp)))
            self.assertEqual(found, [])


class ClassificationTests(unittest.TestCase):
    def _config(self, temp: Path):
        return build_config(desktop=temp / "Desktop", settle_seconds=0)

    def _records(self, temp: Path, root: Path, **kwargs) -> list[RepoRecord]:
        return build_inventory((root,), config=self._config(temp), measure_size=False, **kwargs)

    def test_no_remote_outranks_everything_else(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sources"
            make_repo(root / "orphan", commits=1)
            records = self._records(Path(temp), root)
            self.assertEqual([record.classification for record in records], ["no-remote"])
            self.assertTrue(records[0].needs_attention)
            self.assertFalse(records[0].mirror_eligible)
            self.assertIn("no origin remote", records[0].notes[0])

    def test_a_repository_with_no_commits_is_reported_as_such(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sources"
            empty = root / "no-commits"
            empty.mkdir(parents=True)
            subprocess.run(["git", *GIT_ENV_ARGS, "init", "-q", str(empty)], check=True, timeout=60)
            records = self._records(Path(temp), root)
            self.assertEqual(records[0].classification, "no-remote")
            self.assertIn("no commits", " ".join(records[0].notes))

    def test_a_clean_fully_pushed_recent_repository_is_active(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sources"
            repo = make_repo(root / "live", commits=1)
            publish(repo, Path(temp) / "remote.git")
            records = self._records(Path(temp), root)
            self.assertEqual(records[0].classification, "active")
            self.assertFalse(records[0].needs_attention)
            self.assertTrue(records[0].mirror_eligible)

    def test_a_clean_fully_pushed_stale_repository_is_archive_shaped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sources"
            repo = make_repo(root / "old", commits=1)
            publish(repo, Path(temp) / "remote.git")
            future = datetime.now(timezone.utc) + timedelta(days=ARCHIVE_STALE_DAYS + 30)
            records = self._records(Path(temp), root, now=future)
            self.assertEqual(records[0].classification, "archive")

    def test_a_second_clean_copy_of_the_same_remote_is_a_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sources"
            remote = Path(temp) / "remote.git"
            primary = make_repo(root / "project", commits=1)
            publish(primary, remote)
            copy = root / "nested" / "deeper" / "project"
            copy.parent.mkdir(parents=True)
            subprocess.run(["git", "clone", "-q", str(remote), str(copy)], check=True, timeout=120)
            git(copy, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
            records = {record.path: record for record in self._records(Path(temp), root)}
            self.assertEqual(records[primary].classification, "active")
            self.assertEqual(records[copy].classification, "duplicate")
            self.assertIn("archives", records[copy].recommended_home)

    def test_a_divergent_second_copy_is_never_called_a_duplicate(self):
        """Redundancy has to be proved, not inferred from a shared remote."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sources"
            remote = Path(temp) / "remote.git"
            primary = make_repo(root / "project", commits=1)
            publish(primary, remote)
            copy = root / "nested" / "project"
            copy.parent.mkdir(parents=True)
            subprocess.run(["git", "clone", "-q", str(remote), str(copy)], check=True, timeout=120)
            git(copy, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
            (copy / "unpushed.txt").write_text("local work\n", encoding="utf-8")
            git(copy, "add", "-A")
            git(copy, "commit", "-q", "-m", "work the remote has never seen")
            records = {record.path: record for record in self._records(Path(temp), root)}
            self.assertEqual(records[copy].classification, "active")
            self.assertTrue(records[copy].needs_attention)

    def test_every_classification_is_one_of_the_documented_four(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sources"
            make_repo(root / "orphan", commits=1)
            published = make_repo(root / "published", commits=1)
            publish(published, Path(temp) / "remote.git")
            for record in self._records(Path(temp), root):
                self.assertIn(record.classification, CLASSIFICATIONS)


class RecommendationTests(unittest.TestCase):
    def test_the_separate_trust_domain_is_never_relocated_or_mirrored(self):
        name = sorted(SEPARATE_TRUST_DOMAIN_NAMES)[0]
        home, eligible = recommend_home(
            path=Path.home() / "Documents" / name,
            name=name,
            classification="active",
            separate_trust_domain=True,
            duplicate_of=None,
        )
        self.assertFalse(eligible)
        self.assertIn("D-011", home)
        self.assertIn("stays where it is", home)

    def test_an_active_repository_is_recommended_the_canonical_dev_home(self):
        home, eligible = recommend_home(
            path=Path("/Users/someone/Documents/thing"),
            name="thing",
            classification="active",
            separate_trust_domain=False,
            duplicate_of=None,
        )
        self.assertTrue(eligible)
        self.assertIn(f"{CANONICAL_DEV_HOME}/thing", home)
        self.assertIn("owner decision", home)

    def test_a_synced_root_is_called_out_as_an_owner_decision(self):
        home, _eligible = recommend_home(
            path=Path("/Users/someone/Documents/thing"),
            name="thing",
            classification="active",
            separate_trust_domain=False,
            duplicate_of=None,
        )
        self.assertIn("Google-Drive-synced", home)
        self.assertIn("never automatic", home)

    def test_a_repository_outside_a_synced_root_is_not_called_out(self):
        home, _eligible = recommend_home(
            path=Path("/Users/someone/OpenMontage/thing"),
            name="thing",
            classification="active",
            separate_trust_domain=False,
            duplicate_of=None,
        )
        self.assertNotIn("Google-Drive-synced", home)


def _record(**overrides) -> RepoRecord:
    base = dict(
        path=Path("/Users/someone/Projects/thing"),
        name="thing",
        source_root=Path("/Users/someone/Projects"),
        nested_under=None,
        remote_url="https://github.com/someone/thing.git",
        branch="main",
        head="a" * 40,
        dirty_count=0,
        unpushed_count=0,
        stash_count=0,
        last_commit_iso="2026-09-01T00:00:00+00:00",
        default_remote_branch="origin/main",
        head_in_default_remote=True,
        git_dir_bytes=1024,
        work_tree_bytes=4096,
        classification="active",
        recommended_home="~/Projects/thing",
        mirror_eligible=True,
        notes=(),
    )
    base.update(overrides)
    return RepoRecord(**base)  # type: ignore[arg-type]


class MirrorPlanningTests(unittest.TestCase):
    def test_one_mirror_per_remote_cloned_from_the_largest_copy(self):
        small = _record(path=Path("/a/thing"), git_dir_bytes=1000)
        large = _record(path=Path("/b/thing"), git_dir_bytes=9000)
        plans, skips = plan_mirrors([small, large])
        self.assertEqual([plan.mirror_name for plan in plans], ["thing"])
        self.assertEqual(plans[0].source_path, Path("/b/thing"))
        self.assertEqual([skip.path for skip in skips], [Path("/a/thing")])
        self.assertIn("same remote", skips[0].reason)

    def test_an_oversized_git_directory_is_skipped_with_its_size_named(self):
        plans, skips = plan_mirrors([_record(git_dir_bytes=MIRROR_MAX_GIT_BYTES + 1)])
        self.assertEqual(plans, [])
        self.assertIn("over the", skips[0].reason)
        self.assertIn("cap", skips[0].reason)

    def test_an_unmeasurable_git_directory_is_skipped_never_guessed_at(self):
        plans, skips = plan_mirrors([_record(git_dir_bytes=None)])
        self.assertEqual(plans, [])
        self.assertIn("could not be measured", skips[0].reason)

    def test_a_non_github_remote_is_skipped(self):
        plans, skips = plan_mirrors([_record(remote_url="https://gitlab.example.com/x/y.git")])
        self.assertEqual(plans, [])
        self.assertIn("not a GitHub remote", skips[0].reason)

    def test_an_ineligible_record_is_skipped_with_a_layout_reason(self):
        plans, skips = plan_mirrors(
            [
                _record(name="kronos-quant-lab", mirror_eligible=False),
                _record(path=Path("/c/dupe"), classification="duplicate", mirror_eligible=False),
                _record(path=Path("/d/orphan"), classification="no-remote", remote_url=None, mirror_eligible=False),
            ]
        )
        self.assertEqual(plans, [])
        reasons = {skip.path: skip.reason for skip in skips}
        self.assertIn("D-011", reasons[Path("/Users/someone/Projects/thing")])
        self.assertIn("archives", reasons[Path("/c/dupe")])
        self.assertIn("no origin remote", reasons[Path("/d/orphan")])

    def test_ssh_and_https_spellings_of_one_remote_collapse_to_one_mirror(self):
        https = _record(path=Path("/a/thing"), remote_url="https://github.com/Someone/Thing.git")
        ssh = _record(path=Path("/b/thing"), remote_url="git@github.com:someone/thing")
        plans, _skips = plan_mirrors([https, ssh])
        self.assertEqual(len(plans), 1)


class RenderingTests(unittest.TestCase):
    def test_yaml_is_parseable_and_carries_the_mirror_plan(self):
        import yaml

        text = render_inventory_yaml(
            [_record(), _record(path=Path("/x/orphan"), remote_url=None, classification="no-remote", mirror_eligible=False)],
            roots=(Path("~/Projects"),),
            generated_at="2026-09-17T00:00:00+00:00",
            host="test-host",
            skipped_worktrees=[Path("/some/worktree")],
        )
        data = yaml.safe_load(text)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["host"], "test-host")
        self.assertEqual(data["skipped_linked_worktrees"], ["/some/worktree"])
        self.assertEqual(len(data["repositories"]), 2)
        self.assertEqual(data["angel_mirror_plan"]["max_git_bytes"], MIRROR_MAX_GIT_BYTES)
        self.assertEqual(data["angel_mirror_plan"]["mirrors"][0]["name"], "thing.git")

    def test_yaml_quotes_a_path_with_a_space_or_a_quote(self):
        import yaml

        text = render_inventory_yaml(
            [_record(path=Path('/x/TLC - "Service" at the Park'), name="TLC")],
            roots=(Path("~/Documents"),),
            generated_at="2026-09-17T00:00:00+00:00",
            host="test-host",
        )
        data = yaml.safe_load(text)
        self.assertEqual(data["repositories"][0]["path"], '/x/TLC - "Service" at the Park')

    def test_markdown_leads_with_what_a_remote_does_not_hold(self):
        text = render_unpushed_markdown(
            [
                _record(),
                _record(path=Path("/x/risky"), unpushed_count=3, stash_count=1),
                _record(path=Path("/x/orphan"), remote_url=None, classification="no-remote", mirror_eligible=False),
            ],
            roots=(Path("~/Projects"),),
            generated_at="2026-09-17T00:00:00+00:00",
            host="test-host",
        )
        self.assertIn("## Read this first", text)
        self.assertIn("2 hold state that a Git remote does not", text)
        self.assertIn("/x/risky", text.split("## Full inventory")[0])
        self.assertIn("/x/orphan", text.split("## Full inventory")[0])
        self.assertNotIn("/Users/someone/Projects/thing", text.split("## Read this first")[1].split("## Full inventory")[0])
        self.assertIn("## Angel mirror coverage", text)
        self.assertIn("owner decision, not an action", text)

    def test_markdown_says_so_when_nothing_needs_attention(self):
        text = render_unpushed_markdown(
            [_record()], roots=(Path("~/Projects"),), generated_at="x", host="h"
        )
        self.assertIn("No repository holds unpushed, uncommitted, or stashed work.", text)

    def test_write_reports_writes_exactly_two_files(self):
        with tempfile.TemporaryDirectory() as temp:
            report_dir = Path(temp) / "reports"
            yaml_path, md_path = write_reports(
                [_record()], report_dir=report_dir, roots=(Path("~/Projects"),), generated_at="x", host="h"
            )
            self.assertEqual(
                sorted(path.name for path in report_dir.iterdir()),
                ["repos-inventory.yaml", "unpushed-repos.md"],
            )
            self.assertTrue(yaml_path.is_file())
            self.assertTrue(md_path.is_file())


class ReadOnlinessTests(unittest.TestCase):
    def test_the_inventory_never_mutates_a_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sources"
            repo = make_repo(root / "project", commits=2)
            publish(repo, Path(temp) / "remote.git")
            (repo / "uncommitted.txt").write_text("keep me\n", encoding="utf-8")
            before = {
                path.relative_to(repo): path.stat().st_mtime_ns
                for path in sorted(repo.rglob("*"))
                if path.is_file() and ".git" not in path.parts
            }
            head_before = git(repo, "rev-parse", "HEAD")
            build_inventory((root,), config=build_config(desktop=Path(temp) / "Desktop", settle_seconds=0))
            after = {
                path.relative_to(repo): path.stat().st_mtime_ns
                for path in sorted(repo.rglob("*"))
                if path.is_file() and ".git" not in path.parts
            }
            self.assertEqual(before, after)
            self.assertEqual(git(repo, "rev-parse", "HEAD"), head_before)
            self.assertIn("uncommitted.txt", git(repo, "status", "--porcelain"))

    def test_the_module_contains_no_write_capable_git_verb(self):
        """A grep-level guard: the inventory has no business pushing."""
        source = (Path(__file__).resolve().parent.parent / "tools" / "ingest" / "repos_report.py").read_text()
        for verb in ('"push"', '"fetch"', '"commit"', '"checkout"', '"reset"', '"clean"', '"gc"', '"prune"'):
            self.assertNotIn(verb, source, f"repos_report.py must not invoke git {verb}")


if __name__ == "__main__":
    unittest.main()
