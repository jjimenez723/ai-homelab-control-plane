"""Naming: quality judgement, filename policy, and the rename gate.

These encode §16's naming requirements. The through-line is that a rename is a
mutation of the owner's data, so every path to one is gated, and every path
away from one is recorded.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tools.ingest.ledger import (
    DECISION_ALREADY_NAMED,
    DECISION_HUMAN_LOCKED,
    DECISION_KEPT,
    DECISION_LOW_CONFIDENCE,
    DECISION_PROPOSED,
    DECISION_REJECTED_POLICY,
    DECISION_RENAMED,
    LocalLedger,
)
from tools.ingest.naming import policy, quality
from tools.ingest.naming.evidence import NamingEvidence, gather_evidence
from tools.ingest.naming.proposer import DeterministicProposer, NameProposal
from tools.ingest.naming.rename import evaluate_and_apply

from tests.ingest.fi_helpers import LOCAL_VOLUME_ID, make_camera_jpeg, make_png

NOW = "2026-09-12T12:00:00+00:00"


class NameQualityTests(unittest.TestCase):
    """Invariant 7 and invariant 8: leave good names alone, flag useless ones."""

    POOR = (
        "IMG_6909.JPG",
        "DSC_0442.NEF",
        "Screenshot 2026-09-12 at 10.14.32 AM.png",
        "Screen Shot 2026-09-12 at 10.14.32 AM.png",
        "CleanShot 2026-09-12 at 10.14.32.png",
        "image.png",
        "download (7).pdf",
        "Untitled.pdf",
        "untitled 3.pdf",
        "file (2).zip",
        "3f2504e0-4f89-11d3-9a0c-0305e82c3301.png",
        "a3f5c9d1e8b74620ff12.bin",
        "20260912_101432.jpg",
        "PXL_20260912_101432123.jpg",
        "GOPR0042.MP4",
        "DJI_0042.JPG",
        "photo.jpg",
        "scan.pdf",
    )

    GOOD = (
        "Fine-Line-Group-2026-SOW.pdf",
        "Angel-ingestion-architecture-v3.drawio",
        "ENGR350-midterm-review.pdf",
        "GoogleChrome.dmg",
        "FineLine_signed_contract.pdf",
        "invoice.pdf",
        "smith-kitchen-elevation.dwg",
        # Names this pipeline produces must themselves score GOOD, or every run
        # would re-propose them. This is the first line of idempotency.
        "2026-09-12_red-tailed-hawk-on-fence.jpg",
        "2026-09-12_github-actions-python-tests-failing.png",
        "2026-09-12_web-browser-product-page.png",
        "2026-09-12_screen-capture-2560x1440.png",
        "2026-09-12_fine-line-kitchen-renders-archive.zip",
    )

    def test_machine_generated_names_are_poor(self):
        for filename in self.POOR:
            with self.subTest(filename=filename):
                verdict, reason = quality.assess(filename)
                self.assertEqual(verdict, quality.QUALITY_POOR, f"{filename}: {reason}")

    def test_meaningful_names_are_left_alone(self):
        for filename in self.GOOD:
            with self.subTest(filename=filename):
                verdict, reason = quality.assess(filename)
                self.assertEqual(verdict, quality.QUALITY_GOOD, f"{filename}: {reason}")

    def test_owner_vocabulary_rescues_an_otherwise_unremarkable_name(self):
        self.assertEqual(quality.assess("fineline-2026.pdf", vocabulary=("fineline",))[0], quality.QUALITY_GOOD)


class FilenamePolicyTests(unittest.TestCase):
    """A proposal is a string from an untrusted source until this module has
    finished with it."""

    def validate(self, proposed: str, original: str = "IMG_6909.JPG") -> str:
        return policy.validate_proposal(proposed, original_filename=original, max_filename_length=120).filename

    def test_extension_comes_from_the_file_not_the_proposal(self):
        self.assertTrue(self.validate("a red tailed hawk on a fence").endswith(".jpg"))
        # A proposal that names a *different* type must not be able to change it.
        self.assertTrue(self.validate("hawk photograph.png").endswith(".jpg"))
        self.assertTrue(self.validate("scanned contract", "contract.HEIC").endswith(".heic"))

    def test_date_prefix_keeps_its_underscore_and_the_rest_is_kebab_case(self):
        self.assertEqual(
            self.validate("2026-09-12_red tailed hawk on fence"),
            "2026-09-12_red-tailed-hawk-on-fence.jpg",
        )

    def test_path_separators_are_refused_outright(self):
        for proposal in ("../../etc/passwd", "subdir/photo", "back\\slash", ".", ".."):
            with self.subTest(proposal=proposal), self.assertRaises(policy.NamePolicyError):
                self.validate(proposal)

    def test_a_null_byte_is_refused_rather_than_cleaned_up(self):
        # Salvaging a NUL-bearing proposal would mean guessing what a caller
        # that produced one actually meant. Refusing is the honest answer.
        with self.assertRaises(policy.NamePolicyError):
            self.validate("red\x00tailed hawk")

    def test_control_and_invisible_characters_are_stripped(self):
        cleaned = self.validate("red\x07tailed​hawk‮ on a fence")
        self.assertFalse(any(ord(character) < 32 or ord(character) == 127 for character in cleaned))
        self.assertNotIn("​", cleaned)
        self.assertNotIn("‮", cleaned)
        self.assertTrue(cleaned.endswith(".jpg"))

    def test_forbidden_filesystem_characters_do_not_survive(self):
        cleaned = self.validate('hawk: on a fence <2026> | "yes" * ?')
        self.assertFalse(set(cleaned) & set('/\\:*?"<>|'))

    def test_a_name_is_never_hidden_by_accident(self):
        self.assertFalse(self.validate("...a hidden looking hawk photograph").startswith("."))

    def test_length_is_bounded(self):
        long_name = self.validate("hawk " * 200)
        self.assertLessEqual(len(long_name), 120)
        self.assertTrue(long_name.endswith(".jpg"))

    def test_a_proposal_that_would_still_be_poor_is_refused(self):
        # Otherwise the file is eligible again next run: a rename loop.
        with self.assertRaises(policy.NamePolicyError):
            self.validate("untitled")

    def test_empty_and_punctuation_only_proposals_are_refused(self):
        for proposal in ("", "   ", "---", "..."):
            with self.subTest(proposal=proposal), self.assertRaises(policy.NamePolicyError):
                self.validate(proposal)


class SafeRenameTests(unittest.TestCase):
    """The one mutation primitive. It must never destroy anything."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_rename_moves_the_file_and_removes_the_old_name(self):
        source = self.root / "IMG_6909.JPG"
        source.write_bytes(b"photo bytes")
        destination = policy.safe_rename(source, "2026-09-12_hawk.jpg", disambiguator="abc123")
        self.assertEqual(destination.name, "2026-09-12_hawk.jpg")
        self.assertEqual(destination.read_bytes(), b"photo bytes")
        self.assertFalse(source.exists())

    def test_a_collision_never_overwrites(self):
        occupied = self.root / "2026-09-12_hawk.jpg"
        occupied.write_bytes(b"someone else's photograph")
        source = self.root / "IMG_6909.JPG"
        source.write_bytes(b"our photograph")

        destination = policy.safe_rename(source, "2026-09-12_hawk.jpg", disambiguator="abc123")

        self.assertNotEqual(destination, occupied)
        self.assertEqual(occupied.read_bytes(), b"someone else's photograph")
        self.assertEqual(destination.read_bytes(), b"our photograph")

    def test_collision_suffix_is_deterministic_not_a_climbing_counter(self):
        (self.root / "2026-09-12_hawk.jpg").write_bytes(b"occupied")
        first = self.root / "a.jpg"
        first.write_bytes(b"one")
        destination = policy.safe_rename(first, "2026-09-12_hawk.jpg", disambiguator="deadbe")
        self.assertEqual(destination.name, "2026-09-12_hawk-deadbe.jpg")

    def test_rename_cannot_relocate_a_file_into_another_directory(self):
        source = self.root / "IMG_6909.JPG"
        source.write_bytes(b"photo")
        with self.assertRaises(policy.NamePolicyError):
            policy.safe_rename(source, "../escaped.jpg", disambiguator="abc123")
        self.assertTrue(source.exists())

    def test_reserve_fallback_also_refuses_to_clobber(self):
        """Filesystems without hardlinks take the O_EXCL path; it is no laxer."""
        occupied = self.root / "taken.jpg"
        occupied.write_bytes(b"existing")
        source = self.root / "source.jpg"
        source.write_bytes(b"incoming")
        with self.assertRaises(FileExistsError):
            policy._reserve_rename(source, occupied)
        self.assertEqual(occupied.read_bytes(), b"existing")
        self.assertTrue(source.exists())


class _FixedProposer:
    """A stand-in for a model, so gate behavior can be tested independently of
    whatever the deterministic proposer happens to say."""

    model = "test-proposer"
    model_version = "1"

    def __init__(self, filename: str | None, confidence: float = 0.9):
        self._filename = filename
        self._confidence = confidence

    def propose(self, evidence: NamingEvidence) -> NameProposal | None:
        if self._filename is None:
            return None
        return NameProposal(
            proposed_filename=self._filename,
            confidence=self._confidence,
            model=self.model,
            model_version=self.model_version,
            reason="fixed test proposal",
        )


class RenameGateTests(unittest.TestCase):
    """The gate that turns a proposal into (or away from) a rename."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ledger = LocalLedger(self.root / "ledger.sqlite")
        self.addCleanup(self.ledger.close)

    def _observe(self, path: Path):
        stat = path.stat()
        return self.ledger.observe_file(
            path=path,
            source_class="screenshots",
            source_volume_id=LOCAL_VOLUME_ID,
            device=stat.st_dev,
            inode=stat.st_ino,
            size=stat.st_size,
            sha256="0" * 64,
            mime="image/png",
            media_kind="image",
            now=NOW,
        )

    def _evaluate(self, path: Path, proposer, *, apply: bool = True, min_confidence: float = 0.4):
        observed = self._observe(path)
        evidence = gather_evidence(path, source_class="screenshots")
        return observed, evaluate_and_apply(
            self.ledger,
            local_file_id=observed.local_file_id,
            path=path,
            evidence=evidence,
            proposer=proposer,
            source_volume_id=LOCAL_VOLUME_ID,
            source_class="screenshots",
            sha256="0" * 64,
            now=NOW,
            apply=apply,
            min_confidence=min_confidence,
        )

    def test_a_meaningful_existing_name_is_kept(self):
        path = make_png(self.root / "angel-architecture-diagram.png")
        _, outcome = self._evaluate(path, _FixedProposer("2026-09-12_something-else"))
        self.assertEqual(outcome.decision, DECISION_KEPT)
        self.assertTrue(path.exists(), "a good name must not be churned")

    def test_a_generic_camera_filename_gets_renamed(self):
        path = make_camera_jpeg(self.root / "IMG_6909.JPG")
        _, outcome = self._evaluate(path, DeterministicProposer())
        self.assertEqual(outcome.decision, DECISION_RENAMED)
        self.assertFalse(path.exists())
        self.assertTrue(outcome.path.exists())
        self.assertTrue(outcome.path.name.endswith(".jpg"), outcome.path.name)

    def test_a_screenshot_gets_a_useful_proposal(self):
        path = make_png(self.root / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(2560, 1440))
        _, outcome = self._evaluate(path, DeterministicProposer())
        self.assertEqual(outcome.decision, DECISION_RENAMED)
        self.assertEqual(quality.assess(outcome.path.name)[0], quality.QUALITY_GOOD)

    def test_a_dry_run_proposes_without_touching_the_file(self):
        path = make_png(self.root / "IMG_1001.png")
        original_bytes = path.read_bytes()
        _, outcome = self._evaluate(path, _FixedProposer("2026-09-12_a useful description"), apply=False)
        self.assertEqual(outcome.decision, DECISION_PROPOSED)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_a_low_confidence_proposal_is_recorded_and_not_applied(self):
        path = make_png(self.root / "IMG_1002.png")
        _, outcome = self._evaluate(
            path, _FixedProposer("2026-09-12_a useful description", confidence=0.1), min_confidence=0.8
        )
        self.assertEqual(outcome.decision, DECISION_LOW_CONFIDENCE)
        self.assertTrue(path.exists())

    def test_a_proposal_that_is_not_an_improvement_is_rejected(self):
        path = make_png(self.root / "IMG_1003.png")
        _, outcome = self._evaluate(path, _FixedProposer("untitled"))
        self.assertEqual(outcome.decision, DECISION_REJECTED_POLICY)
        self.assertTrue(path.exists())

    def test_no_proposal_leaves_the_file_alone(self):
        path = make_png(self.root / "IMG_1004.png")
        _, outcome = self._evaluate(path, _FixedProposer(None))
        self.assertTrue(path.exists())
        self.assertIsNone(outcome.applied_filename)

    def test_repeated_runs_do_not_keep_renaming(self):
        """Invariant: a second pass is a no-op, and so is a tenth."""
        path = make_camera_jpeg(self.root / "IMG_6909.JPG")
        _, first = self._evaluate(path, DeterministicProposer())
        self.assertEqual(first.decision, DECISION_RENAMED)

        settled = first.path
        for _ in range(3):
            _, again = self._evaluate(settled, DeterministicProposer())
            self.assertIn(again.decision, (DECISION_KEPT, DECISION_ALREADY_NAMED))
            self.assertTrue(settled.exists())
            self.assertEqual(sorted(p.name for p in self.root.glob("*.jpg")), [settled.name])

    def test_a_human_named_file_is_never_renamed_again(self):
        """Invariant 12. The lock survives however poor the name looks."""
        path = make_png(self.root / "IMG_2001.png")
        observed = self._observe(path)
        self.ledger.record_human_name(
            observed.local_file_id, filename=path.name, directory=str(self.root), now=NOW
        )

        _, outcome = self._evaluate(path, _FixedProposer("2026-09-12_a much better name"))
        self.assertEqual(outcome.decision, DECISION_HUMAN_LOCKED)
        self.assertTrue(path.exists(), "an owner-chosen name must survive every later run")

    def test_the_original_filename_stays_auditable_after_a_rename(self):
        path = make_camera_jpeg(self.root / "IMG_6909.JPG")
        observed, outcome = self._evaluate(path, DeterministicProposer())
        history = self.ledger.naming_history(observed.local_file_id)
        originals = {row["original_filename"] for row in history}
        applied = {row["applied_filename"] for row in history if row["applied_filename"]}
        self.assertIn("IMG_6909.JPG", originals)
        self.assertIn(outcome.path.name, applied)

    def test_a_rename_failure_leaves_the_file_untouched(self):
        path = make_png(self.root / "IMG_2002.png")
        # Occupy both the target and its deterministic collision fallback, and
        # make the directory read-only so no further name can be claimed.
        (self.root / "2026-09-12_blocked-name.png").write_bytes(b"occupied")
        os.chmod(self.root, 0o500)
        self.addCleanup(os.chmod, self.root, 0o700)

        _, outcome = self._evaluate(path, _FixedProposer("2026-09-12_blocked name"))

        self.assertTrue(path.exists(), "a failed rename must not lose the file")
        self.assertNotEqual(outcome.decision, DECISION_RENAMED)

    def test_a_protected_extension_is_never_renamed(self):
        path = self.root / "SomeApp-1.2.3-installer.dmg"
        path.write_bytes(b"disk image")
        observed = self._observe(path)
        evidence = gather_evidence(path, source_class="downloads")
        outcome = evaluate_and_apply(
            self.ledger,
            local_file_id=observed.local_file_id,
            path=path,
            evidence=evidence,
            proposer=_FixedProposer("2026-09-12_renamed installer"),
            source_volume_id=LOCAL_VOLUME_ID,
            source_class="downloads",
            sha256="0" * 64,
            now=NOW,
            apply=True,
            min_confidence=0.1,
            protected_extensions=frozenset({".dmg"}),
        )
        self.assertEqual(outcome.decision, DECISION_KEPT)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()


class AdversarialProposalTests(unittest.TestCase):
    """A hostile proposer must not be able to reach past the policy layer.

    These are the inputs a model-backed proposer could produce, deliberately or
    because it was fed attacker-controlled evidence (a zip entry name, an EXIF
    description, a PDF title all come from whoever made the file).
    """

    def _validate(self, proposed: str, original: str = "IMG_1.png"):
        return policy.validate_proposal(
            proposed, original_filename=original, max_filename_length=120
        )

    def test_compound_extensions_are_preserved(self):
        """`.tar.gz` truncated to `.gz` mislabels the file for every tool."""
        self.assertEqual(policy.normalized_extension("download (4).tar.gz"), ".tar.gz")
        self.assertEqual(self._validate("project-backup", "download (4).tar.gz").filename, "project-backup.tar.gz")
        self.assertEqual(self._validate("site-snapshot", "archive.tar.bz2").filename, "site-snapshot.tar.bz2")

    def test_a_dotted_name_does_not_lose_its_last_word(self):
        self.assertEqual(policy.normalized_extension("Fine.Line.Group.pdf"), ".pdf")

    def test_hostile_proposals_are_rejected_or_neutralised(self):
        hostile = [
            "../../../../etc/passwd",
            "/etc/passwd",
            "sub/dir/name",
            "back\\slash",
            "with\x00null",
            ".",
            "..",
            "",
            "   ",
            "‮gnp.exe",          # RTL override
            "zero​width",
            "x" * 10_000,
        ]
        for proposal in hostile:
            with self.subTest(proposal=proposal[:24]):
                try:
                    result = self._validate(proposal)
                except policy.NamePolicyError:
                    continue  # rejected outright is a fine answer
                self.assertNotIn("/", result.filename)
                self.assertNotIn("\\", result.filename)
                self.assertNotIn("\x00", result.filename)
                self.assertFalse(result.filename.startswith("."))
                self.assertTrue(result.filename.endswith(".png"))
                self.assertLessEqual(len(result.filename.encode()), policy.MAX_COMPONENT_BYTES)
                self.assertTrue(all(ord(c) >= 0x20 for c in result.filename))


class SymlinkRenameTests(unittest.TestCase):
    """A symlink must never be renamed, however it got there.

    The scanner screens symlinks at discovery, but discovery, settling, hashing
    and probing all happen before the rename -- and anything can swap a file for
    a symlink in that window. `os.link` follows symlinks by default, so without
    this the swap would hardlink a file from outside the source root into it and
    unlink the owner's symlink.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.downloads = self.root / "Downloads"
        self.private = self.root / "Private"
        self.downloads.mkdir()
        self.private.mkdir()

    def test_a_symlink_planted_before_the_rename_is_refused(self):
        secret = self.private / "wedding-photo.png"
        secret.write_bytes(b"irreplaceable")
        link = self.downloads / "IMG_0007.png"
        link.symlink_to(secret)

        with self.assertRaises(policy.NamePolicyError):
            policy.safe_rename(link, "2026-09-12_photo-640x480.png", disambiguator="abc123")

        self.assertTrue(link.is_symlink(), "the owner's symlink must survive")
        self.assertEqual(secret.read_bytes(), b"irreplaceable")
        self.assertEqual(secret.stat().st_nlink, 1, "nothing outside the source root was hardlinked")
        self.assertEqual([p.name for p in self.downloads.iterdir()], ["IMG_0007.png"])
