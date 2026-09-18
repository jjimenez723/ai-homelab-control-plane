"""The Angel layout contract: docs/25 and scripts/bootstrap-t7.sh agree.

Everything here runs the real `scripts/bootstrap-t7.sh` against a temporary
directory. No test touches a mounted volume, and the script's UUID gate is
itself under test (a root under `/Volumes` without `--expect-uuid` must be
refused), so a passing suite is evidence the gate exists rather than a
promise that it does.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.angel_sync import (
    ARCHIVES_RELATIVE,
    MIRRORS_RELATIVE,
    TRUSTED_SKILLS_RELATIVE,
    AngelSyncError,
    ResolvedTarget,
    export_trusted_skills,
    resolve_target,
)
from tools.ingest.volumes import VolumeError

from tests.ingest.helpers import T7_TEST_UUID, build_config, fake_prober, make_volume

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap-t7.sh"

# Directories docs/25 commits to. A rename breaks config, code, tests, and a
# live restore drill (the D-023 reasoning), so the names are asserted.
REQUIRED_DIRS = (
    "00-inbox/organized",
    "00-inbox/legacy-dump/macbook",
    "10-code/mirrors",
    "10-code/archives",
    "10-code/generated-apps",
    "20-clients",
    "30-shared-media/stock-owned",
    "40-ai/model-cache/ollama",
    "40-ai/model-cache/whisper",
    "40-ai/datasets",
    "40-ai/derived",
    "40-ai/evaluation/benchmarks/legacy-20",
    "40-ai/evaluation/campaigns",
    "40-ai/evaluation/telemetry",
    "40-ai/skills/trusted",
    "40-ai/skills/candidates",
    "50-research/knowledge",
    "60-exports/handoff",
    "70-archives",
    "90-system/migration-reports",
    "90-system/staging",
    "90-system/cache",
)

REQUIRED_READMES = (
    "README.md",
    "00-inbox/README.md",
    "10-code/README.md",
    "10-code/mirrors/README.md",
    "10-code/archives/README.md",
    "20-clients/README.md",
    "30-shared-media/README.md",
    "40-ai/README.md",
    "40-ai/skills/README.md",
    "40-ai/skills/candidates/README.md",
    "50-research/README.md",
    "50-research/knowledge/README.md",
    "60-exports/README.md",
    "70-archives/README.md",
    "90-system/README.md",
)


def run_bootstrap(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(BOOTSTRAP), str(root), *extra],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


class BootstrapStructureTests(unittest.TestCase):
    def test_creates_every_directory_the_layout_commits_to(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = run_bootstrap(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            missing = [name for name in REQUIRED_DIRS if not (root / name).is_dir()]
            self.assertEqual(missing, [], f"bootstrap did not create: {missing}")

    def test_writes_a_readme_at_the_root_and_each_top_level(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_bootstrap(root)
            missing = [name for name in REQUIRED_READMES if not (root / name).is_file()]
            self.assertEqual(missing, [], f"bootstrap did not write: {missing}")

    def test_every_top_level_readme_answers_the_four_questions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_bootstrap(root)
            for name in (
                "00-inbox/README.md",
                "10-code/README.md",
                "20-clients/README.md",
                "30-shared-media/README.md",
                "40-ai/README.md",
                "50-research/README.md",
                "60-exports/README.md",
                "70-archives/README.md",
                "90-system/README.md",
            ):
                text = (root / name).read_text(encoding="utf-8")
                for question in ("What belongs here", "What never belongs here", "Who writes here", "Backup status"):
                    self.assertIn(question, text, f"{name} does not answer '{question}'")

    def test_second_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_bootstrap(root)
            before = {
                path.relative_to(root): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }
            second = run_bootstrap(root)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("directories created: 0", second.stdout)
            self.assertIn("documents written:   0", second.stdout)
            after = {
                path.relative_to(root): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_a_hand_edited_readme_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_bootstrap(root)
            edited = root / "20-clients" / "README.md"
            edited.write_text("# my own notes about this client tree\n", encoding="utf-8")
            result = run_bootstrap(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(edited.read_text(encoding="utf-8"), "# my own notes about this client tree\n")
            self.assertTrue((root / "20-clients" / "README.md.new").is_file())
            self.assertIn("were NOT overwritten", result.stdout)
            self.assertIn(str(edited), result.stdout)

    def test_no_temporary_files_are_left_behind(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_bootstrap(root)
            run_bootstrap(root)
            leftovers = [str(path) for path in root.rglob(".bootstrap-t7.*")]
            self.assertEqual(leftovers, [])


class BootstrapManifestTests(unittest.TestCase):
    def test_manifest_is_written_and_lists_the_backup_exclusions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_bootstrap(root)
            manifest = root / "90-system" / "angel-manifest.yaml"
            self.assertTrue(manifest.is_file())
            text = manifest.read_text(encoding="utf-8")
            self.assertIn("schema_version: 1", text)
            self.assertIn('authoritative_doc: "docs/25-angel-volume-layout.md"', text)
            for excluded in ("90-system/staging/**", "90-system/cache/**", "40-ai/model-cache/**", "**/*.partial"):
                self.assertIn(excluded, text)

    def test_manifest_excludes_match_the_configured_backup_excludes(self):
        """The manifest must not drift from what restic is actually told."""
        from tools.ingest.config import DEFAULTS

        configured = set(DEFAULTS["backup"]["exclude_patterns"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_bootstrap(root)
            text = (root / "90-system" / "angel-manifest.yaml").read_text(encoding="utf-8")
            block = text.split("excluded_paths:", 1)[1].split("\n\n", 1)[0]
            declared = {line.strip().removeprefix("- ").strip('"') for line in block.splitlines() if line.strip().startswith("- ")}
            # The manifest lists volume paths; macOS metadata excludes are an
            # operational workaround rather than part of the layout.
            self.assertTrue(declared)
            self.assertTrue(declared.issubset(configured), f"manifest declares excludes restic does not honour: {declared - configured}")

    def test_manifest_records_a_null_uuid_for_a_dev_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_bootstrap(root)
            text = (root / "90-system" / "angel-manifest.yaml").read_text(encoding="utf-8")
            self.assertIn("volume_uuid: null", text)

    def test_every_manifest_entry_declares_owner_writer_backup_and_class(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_bootstrap(root)
            text = (root / "90-system" / "angel-manifest.yaml").read_text(encoding="utf-8")
            entries = text.split("entries:", 1)[1]
            blocks = [block for block in entries.split("  - path:") if block.strip()]
            self.assertGreaterEqual(len(blocks), 20)
            for block in blocks:
                for field in ("purpose:", "owner:", "writer:", "backup:", "class:"):
                    self.assertIn(field, block, f"manifest entry missing {field}: {block[:80]}")
                self.assertTrue(
                    'backup: "included"' in block or 'backup: "excluded"' in block,
                    f"manifest entry has an unrecognised backup value: {block[:80]}",
                )


class BootstrapUuidGateTests(unittest.TestCase):
    def test_a_volume_root_without_expect_uuid_is_refused(self):
        """A name is not an identity (D-019), and a scaffolder is not exempt."""
        result = run_bootstrap(Path("/Volumes"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("--expect-uuid is required", result.stderr)

    def test_a_mismatched_uuid_is_refused_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = run_bootstrap(root, "--expect-uuid", "00000000-0000-0000-0000-000000000000")
            self.assertEqual(result.returncode, 3)
            self.assertEqual(sorted(path.name for path in root.iterdir()), [])

    def test_a_missing_root_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_bootstrap(Path(temp) / "nope")
            self.assertEqual(result.returncode, 2)

    def test_expect_uuid_without_a_value_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_bootstrap(Path(temp), "--expect-uuid")
            self.assertEqual(result.returncode, 2)


class AngelSyncGateTests(unittest.TestCase):
    """The UUID gate on the two write paths, and what they refuse."""

    def test_resolve_target_refuses_an_absent_volume(self):
        with tempfile.TemporaryDirectory() as temp:
            config = build_config(desktop=Path(temp) / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            with self.assertRaises(VolumeError):
                resolve_target(config, fake_prober([]))

    def test_resolve_target_refuses_a_read_only_volume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Angel"
            root.mkdir()
            config = build_config(desktop=Path(temp) / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            volume = make_volume(T7_TEST_UUID, root, writable=False, label="Angel")
            with self.assertRaisesRegex(VolumeError, "read-only or locked"):
                resolve_target(config, fake_prober([volume]))

    def test_resolve_target_refuses_a_name_matched_volume_with_the_wrong_uuid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Angel"
            root.mkdir()
            config = build_config(desktop=Path(temp) / "Desktop", t7_volume_uuid=T7_TEST_UUID)
            volume = make_volume("99999999-9999-9999-9999-999999999999", root, label="Angel")
            with self.assertRaisesRegex(VolumeError, "not mounted"):
                resolve_target(config, fake_prober([volume]))

    def test_resolve_target_refuses_a_dev_override_unless_asked_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "dev-root"
            root.mkdir()
            config = build_config(desktop=Path(temp) / "Desktop", t7_root=root)
            with self.assertRaisesRegex(VolumeError, "storage_root_override"):
                resolve_target(config, fake_prober([]))

    def test_relative_paths_are_the_documented_ones(self):
        self.assertEqual(MIRRORS_RELATIVE, Path("10-code/mirrors"))
        self.assertEqual(ARCHIVES_RELATIVE, Path("10-code/archives"))
        self.assertEqual(TRUSTED_SKILLS_RELATIVE, Path("40-ai/skills/trusted"))


class TrustedSkillExportTests(unittest.TestCase):
    def _target(self, root: Path) -> ResolvedTarget:
        return ResolvedTarget(root=root, volume_uuid=T7_TEST_UUID)

    def test_export_copies_the_tree_and_writes_a_hash_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Angel"
            source = Path(temp) / "skills"
            (source / "example-skill").mkdir(parents=True)
            (source / "example-skill" / "SKILL.md").write_text("---\nname: example\n---\n", encoding="utf-8")
            (source / "README.md").write_text("# trusted\n", encoding="utf-8")
            root.mkdir()
            manifest, exported, stale = export_trusted_skills(self._target(root), skills_dir=source)
            self.assertEqual(sorted(str(path) for path in exported), ["README.md", "example-skill/SKILL.md"])
            self.assertEqual(stale, [])
            text = manifest.read_text(encoding="utf-8")
            self.assertIn("trust: trusted-reviewed", text)
            self.assertIn("executable_from_here: false", text)
            self.assertIn("source_git_sha:", text)
            self.assertIn("stale_files: []", text)
            self.assertIn("sha256:", text)
            self.assertTrue((root / TRUSTED_SKILLS_RELATIVE / "example-skill" / "SKILL.md").is_file())

    def test_export_reports_a_stale_file_and_never_deletes_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Angel"
            source = Path(temp) / "skills"
            (source / "kept").mkdir(parents=True)
            (source / "kept" / "SKILL.md").write_text("kept\n", encoding="utf-8")
            root.mkdir()
            export_trusted_skills(self._target(root), skills_dir=source)
            orphan = root / TRUSTED_SKILLS_RELATIVE / "retired" / "SKILL.md"
            orphan.parent.mkdir(parents=True)
            orphan.write_text("no longer in git\n", encoding="utf-8")
            manifest, _exported, stale = export_trusted_skills(self._target(root), skills_dir=source)
            self.assertEqual([str(path) for path in stale], ["retired/SKILL.md"])
            self.assertTrue(orphan.is_file(), "the export must never delete from the volume")
            self.assertIn("retired/SKILL.md", manifest.read_text(encoding="utf-8"))

    def test_export_refuses_when_there_is_no_skills_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Angel"
            root.mkdir()
            with self.assertRaises(AngelSyncError):
                export_trusted_skills(self._target(root), skills_dir=Path(temp) / "absent")

    def test_export_is_deterministic_for_unchanged_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Angel"
            source = Path(temp) / "skills"
            (source / "a").mkdir(parents=True)
            (source / "a" / "SKILL.md").write_text("a\n", encoding="utf-8")
            root.mkdir()
            from datetime import datetime, timezone

            fixed = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
            first = export_trusted_skills(self._target(root), skills_dir=source, now=fixed)[0].read_text()
            second = export_trusted_skills(self._target(root), skills_dir=source, now=fixed)[0].read_text()
            self.assertEqual(first, second)


class RealSkillTreeTests(unittest.TestCase):
    def test_the_trusted_tree_documents_itself(self):
        readme = REPO_ROOT / "skills" / "README.md"
        self.assertTrue(readme.is_file(), "skills/README.md states what the trusted tree is")
        text = readme.read_text(encoding="utf-8")
        self.assertIn("trusted", text.lower())
        self.assertIn("40-ai/skills/candidates", text)

    def test_no_candidate_directory_exists_inside_the_trusted_tree(self):
        self.assertFalse((REPO_ROOT / "skills" / "candidates").exists())

    def test_gitignore_keeps_the_candidate_tripwire(self):
        text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("skills/candidates/", text)


if __name__ == "__main__":
    unittest.main()
