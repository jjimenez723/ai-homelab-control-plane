"""The V1 adversarial review (2026-09-18): what an attacker can actually do.

`test_mutation_authority.py` proves the *structure* of the boundary,
`test_hostile_proposer.py` attacks it with path-shaped proposals, and
`test_evidence_security.py` proves perception cannot acquire authority. This
file is the pass that assumed all three were already passing and went looking
for the cases nobody had written down -- so most of what is here is a
regression guard for a defect that was real when the test was written, and the
comment on each one says what it was.

Grouped by the boundary each class attacks, because a failure here should name
the invariant it broke rather than the function it called:

* `FilenameSanitizerTests` -- `naming/policy.py` is the *whole* filesystem
  authority of the naming pipeline (D-022). A proposer is untrusted by
  construction, so every character class it could emit has to die here.
* `NonLatinNameTests` -- invariant 7 ("a useful name is left alone") held only
  for names written in the Latin alphabet.
* `VolumeRootContainmentTests` -- D-019/D-026: a caller-supplied string and a
  config file must not be able to move a write outside the UUID-verified root.
* `TerminalOutputTests` -- file- and model-derived text reaches a terminal
  through exactly one helper, and that helper must be safe regardless of who
  called it.
* `UntrustedCodeLoadTests` -- docs/26: nothing in `tools/` loads executable
  code from outside the reviewed tree.
* `CorruptMediaFuzzTests` -- `collect_evidence` never raises, proved by
  mutation rather than asserted.
"""

from __future__ import annotations

import hashlib
import random
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path

from tools.ingest.config import EvidenceConfig, load_config
from tools.ingest.evidence.extract import collect_evidence
from tools.ingest.evidence.types import EvidenceSource
from tools.ingest.ledger import DECISION_RENAMED, LocalLedger
from tools.ingest.media.probe import probe_file
from tools.ingest.naming import policy, quality
from tools.ingest.naming.proposer import NameProposal
from tools.ingest.pipeline import run_source

from tests.ingest.evidence_fakes import make_text_pdf
from tests.ingest.fi_helpers import fi_config, make_png
from tests.ingest.media_fixtures import ffmpeg_available, make_tone_audio_file, make_tone_video


# ---------------------------------------------------------------------------
# naming/policy.py -- the filename sanitizer
# ---------------------------------------------------------------------------


class _FixedProposer:
    """Returns one attacker-chosen string at maximum confidence.

    The same shape as `test_hostile_proposer._HostileProposer`; restated here
    so this file stands alone and so a change to that file's payload list
    cannot quietly change what this one is testing.
    """

    model = "adversarial-v1-proposer"
    model_version = "1"

    def __init__(self, payload):
        self.payload = payload

    def propose(self, evidence):  # noqa: ANN001 - protocol takes NamingEvidence
        return NameProposal(
            proposed_filename=self.payload,
            confidence=1.0,
            model=self.model,
            model_version=self.model_version,
            reason="adversarial",
        )


# Every character class that was invisible, or terminal-active, or unencodable,
# and survived `sanitize_stem` into an applied filename. The hand-written
# `_INVISIBLE_CHARS`/`_CONTROL_CHARS` sets named some of the bidi and
# zero-width characters and none of these.
INVISIBLE_AND_ACTIVE_CHARACTERS = {
    # C1 controls. U+009B is CSI: the single byte that starts an 8-bit ANSI
    # escape sequence in a terminal that still honours one, and `organize`
    # prints filenames to a terminal.
    "c1-csi": "",
    "c1-osc": "",
    "c1-nel": "",
    # Format characters the hand-written set missed. `_INVISIBLE_CHARS` listed
    # U+202A/B/D/E and not U+202C, and U+200B/C/D and not U+200E/F.
    "bidi-lrm": "‎",
    "bidi-rlm": "‏",
    "bidi-pop": "‬",
    "soft-hyphen": "­",
    "invisible-times": "⁢",
    "word-joiner": "⁠",
    "arabic-letter-mark": "؜",
    "mongolian-vowel-separator": "᠎",
    # A lone surrogate cannot be encoded to UTF-8 at all, so it reached
    # `len(filename.encode("utf-8"))` and raised `UnicodeEncodeError` -- which
    # is not a `NamePolicyError`, so `rename.py` did not catch it and no
    # `naming_history` row was written. D-057 says a refused proposal is
    # recorded precisely so it is not indistinguishable from a rename nobody
    # noticed.
    "lone-surrogate-high": "\ud800",
    "lone-surrogate-low": "\udfff",
    # Private use: renders as whatever the reader's font decides.
    "private-use": "",
}


class FilenameSanitizerTests(unittest.TestCase):
    """`policy.py` holds the whole filesystem authority, so it is tested alone."""

    def test_no_invisible_or_terminal_active_character_reaches_a_filename(self):
        for label, character in INVISIBLE_AND_ACTIVE_CHARACTERS.items():
            with self.subTest(character=label):
                validated = policy.validate_proposal(
                    f"quarterly{character}revenue{character}report",
                    original_filename="IMG_0001.pdf",
                    max_filename_length=120,
                )
                self.assertNotIn(character, validated.filename)
                self.assertEqual(validated.filename, "quarterlyrevenuereport.pdf")

    def test_no_unicode_control_format_or_surrogate_category_survives(self):
        """The categorical claim, not just the sample above.

        A hand-written list of hostile characters is a list of the ones
        somebody thought of. `sanitize_stem` filters by Unicode category, so
        this asserts the category rather than the members.
        """
        offenders = []
        for codepoint in range(0x11000):
            character = chr(codepoint)
            if unicodedata.category(character) not in ("Cc", "Cf", "Cs", "Co"):
                continue
            if character in policy.sanitize_stem(f"ab{character}cd", max_length=80):
                offenders.append(hex(codepoint))
        self.assertEqual(offenders, [], "these Cc/Cf/Cs/Co characters survived sanitize_stem")

    def test_a_surrogate_is_a_recorded_policy_rejection_not_an_encoding_crash(self):
        """It must reach `NamePolicyError` or a clean name -- never UnicodeError.

        `rename.py` catches `NamePolicyError`; a `UnicodeEncodeError` escaped
        past it to the per-file handler in `pipeline.py`, which records a bare
        error string and no naming-history row.
        """
        for payload in ("\ud800", "report\ud800", "𐏿"):
            with self.subTest(payload=payload):
                try:
                    policy.validate_proposal(
                        payload, original_filename="IMG_0001.pdf", max_filename_length=120
                    )
                except policy.NamePolicyError:
                    pass
                except UnicodeError as exc:  # pragma: no cover - the defect
                    self.fail(f"{payload!r} escaped as {type(exc).__name__}: {exc}")

    def test_a_sanitized_name_always_fits_the_apfs_component_ceiling(self):
        """Trim to the byte ceiling; do not refuse because of it.

        `max_length` counts characters and APFS counts bytes, and the two
        diverge 3x for CJK and 4x for emoji -- so a name that cleared the
        readability cap blew the 255-byte ceiling and `validate_proposal`
        refused it outright.
        """
        scripts = {
            "cjk": "二零二六年度財務報告與董事會決議摘要附件",
            "cyrillic": "отчет-о-финансовых-результатах-за-год-приложение",
            "greek": "συμβόλαιο-ενοικίασης-και-παραρτήματα",
            "emoji": "kitchen" + "\U0001f600" * 40,
            "latin": "rapport-financiere-annuel-avec-annexes",
        }
        for label, text in scripts.items():
            for cap in (8, 40, 120, 200):
                with self.subTest(script=label, max_filename_length=cap):
                    stem = policy.sanitize_stem(text * 6, max_length=cap)
                    self.assertLessEqual(
                        len(stem.encode("utf-8")),
                        policy.MAX_COMPONENT_BYTES,
                        "sanitize_stem must respect the hard byte ceiling",
                    )
                    # Truncation must land on a character boundary: a cut
                    # inside a multi-byte sequence is not a filename.
                    self.assertEqual(stem.encode("utf-8").decode("utf-8"), stem)

    def test_a_multibyte_title_is_named_rather_than_refused(self):
        validated = policy.validate_proposal(
            "отчет-о-финансовых-результатах-" * 8,
            original_filename="download (4).pdf",
            max_filename_length=200,
        )
        self.assertTrue(validated.filename.endswith(".pdf"))
        self.assertLessEqual(len(validated.filename.encode("utf-8")), policy.MAX_COMPONENT_BYTES)

    def test_a_second_extension_cannot_change_what_the_file_is(self):
        """`.pdf.exe`, `.jpg.command`, and a bare type swap."""
        for payload in ("quarterly-report.pdf.exe", "invoice.jpg.command", "installer.exe", "notes.txt"):
            with self.subTest(payload=payload):
                validated = policy.validate_proposal(
                    payload, original_filename="Screenshot 2026-09-12.png", max_filename_length=120
                )
                self.assertTrue(validated.filename.endswith(".png"))
                # The real extension is last; nothing after it.
                self.assertEqual(Path(validated.filename).suffix, ".png")

    def test_a_reserved_device_name_cannot_be_applied_bare(self):
        for stem in ("con", "PRN", "aux", "nul", "com1", "LPT9"):
            with self.subTest(stem=stem):
                validated = policy.validate_proposal(
                    stem, original_filename="report-draft.txt", max_filename_length=120
                )
                self.assertNotEqual(Path(validated.filename).stem.lower(), stem.lower())
                self.assertLessEqual(len(validated.filename.encode("utf-8")), policy.MAX_COMPONENT_BYTES)

    def test_a_hostile_proposal_never_lands_outside_the_source_directory(self):
        """End to end, through a real `run_source --apply`.

        The unit assertions above are about the string. This is about the
        filesystem: whatever the proposer said, one file exists, its bytes are
        unchanged, and nothing was created anywhere else.
        """
        payloads = [
            f"quarterly{character}report" for character in INVISIBLE_AND_ACTIVE_CHARACTERS.values()
        ] + ["[2J[H", "../" * 40 + "escape", "\ud800"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            screenshots = root / "Screenshots"
            screenshots.mkdir()
            sentinel = root / "SENTINEL.txt"
            sentinel.write_text("intact", encoding="utf-8")
            config = fi_config(root, screenshots=screenshots)

            for payload in payloads:
                with self.subTest(payload=repr(payload)):
                    victim = make_png(screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png")
                    content = victim.read_bytes()
                    with LocalLedger(config.local_ledger_path) as ledger:
                        report = run_source(
                            config, ledger,
                            source_class="screenshots",
                            source_volume_id="mac-local",
                            apply=True,
                            settle_seconds=1,
                            resolved_t7=None,
                            proposer=_FixedProposer(payload),
                        )
                    files = [path for path in screenshots.iterdir() if path.is_file()]
                    self.assertEqual(len(files), 1, "exactly one file must remain")
                    self.assertEqual(files[0].read_bytes(), content)
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "intact")
                    self.assertEqual(
                        sorted(p.name for p in root.iterdir() if p.is_file()), ["SENTINEL.txt"]
                    )
                    for character in INVISIBLE_AND_ACTIVE_CHARACTERS.values():
                        self.assertNotIn(character, files[0].name)
                    # An applied name must be encodable and within the ceiling.
                    self.assertLessEqual(len(files[0].name.encode("utf-8")), 255)
                    for outcome in report.outcomes:
                        if outcome.naming_decision == DECISION_RENAMED:
                            self.assertEqual(Path(outcome.path).suffix, ".png")
                    files[0].unlink()


class RenameRaceTests(unittest.TestCase):
    """`safe_rename` must never overwrite, including under a lost race."""

    def test_a_target_created_between_the_check_and_the_rename_is_not_clobbered(self):
        """`collision_free_name` checks, then `os.link` acts. Win or lose the
        race, the pre-existing file's bytes survive: `os.link` fails with
        `FileExistsError` rather than replacing anything, and `os.rename` is
        never used for a distinct inode."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "IMG_0001.png"
            source.write_bytes(b"source-bytes")
            victim = directory / "kitchen-layout.png"
            victim.write_bytes(b"victim-bytes")

            original_exists = Path.exists
            calls = {"n": 0}

            def racing_exists(self):  # noqa: ANN001
                # Report the target free the first time it is asked about, so
                # `collision_free_name` returns the occupied name.
                if self == victim:
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return False
                return original_exists(self)

            Path.exists = racing_exists
            try:
                with self.assertRaises(OSError):
                    policy.safe_rename(source, "kitchen-layout.png", disambiguator="abc123")
            finally:
                Path.exists = original_exists

            self.assertEqual(victim.read_bytes(), b"victim-bytes", "the target was clobbered")
            self.assertEqual(source.read_bytes(), b"source-bytes", "the source was lost")

    def test_a_directory_occupying_the_target_name_is_not_written_into(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "IMG_0002.png"
            source.write_bytes(b"bytes")
            (directory / "kitchen-layout.png").mkdir()
            result = policy.safe_rename(source, "kitchen-layout.png", disambiguator="abc123")
            self.assertTrue(result.is_file())
            self.assertNotEqual(result.name, "kitchen-layout.png")
            self.assertTrue((directory / "kitchen-layout.png").is_dir())

    def test_a_symlink_at_the_source_path_is_refused_at_the_moment_of_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            real = directory / "outside.png"
            real.write_bytes(b"not-ours")
            link = directory / "IMG_0003.png"
            link.symlink_to(real)
            with self.assertRaises(policy.NamePolicyError):
                policy.safe_rename(link, "kitchen-layout.png", disambiguator="abc123")
            self.assertTrue(link.is_symlink())
            self.assertEqual(real.read_bytes(), b"not-ours")


# ---------------------------------------------------------------------------
# naming/quality.py -- invariant 7 outside the Latin alphabet
# ---------------------------------------------------------------------------


class NonLatinNameTests(unittest.TestCase):
    """A name the owner wrote is a good name, whatever script it is in.

    `quality.tokenize` split on `[^A-Za-z0-9]+`, which treats every non-ASCII
    letter as a *separator*. So `отчет-о-финансовых-результатах.pdf` produced
    zero tokens and scored POOR -- meaning eligible for automatic renaming --
    and `café-menu` tokenized to `caf`. Both halves of the consequence are
    tested: the owner's name is left alone, and a proposal in the same script
    is not refused by the quality re-check either.
    """

    OWNER_AUTHORED_NAMES = (
        "отчет-о-финансовых-результатах.pdf",   # Russian
        "финансовый-отчет.pdf",
        "確定申告書.pdf",                          # Japanese
        "二零二六年度財務報告.pdf",                  # Chinese
        "ملخص-الاجتماع-الشهري.pdf",                # Arabic
        "συμβόλαιο-ενοικίασης.pdf",               # Greek
        "café-menu-final.pdf",                    # accented Latin
        "報告.pdf",                                # two ideographs are a word
    )

    def test_an_owner_authored_non_latin_name_is_good(self):
        for name in self.OWNER_AUTHORED_NAMES:
            with self.subTest(name=name):
                verdict, reason = quality.assess(name)
                self.assertEqual(verdict, quality.QUALITY_GOOD, f"{name}: {reason}")

    def test_accented_words_are_not_split_at_the_accent(self):
        self.assertIn("café", quality.tokenize("café-menu-final"))
        self.assertNotIn("caf", quality.tokenize("café-menu-final"))

    def test_a_non_latin_proposal_is_not_refused_by_the_quality_recheck(self):
        for proposal in ("отчет-о-финансовых-результатах", "確定申告書", "συμβόλαιο-ενοικίασης"):
            with self.subTest(proposal=proposal):
                validated = policy.validate_proposal(
                    proposal, original_filename="download (4).pdf", max_filename_length=120
                )
                self.assertTrue(validated.filename.endswith(".pdf"))

    def test_ascii_classification_is_unchanged(self):
        """The fix must be a no-op on ASCII, which is the whole existing corpus."""
        unchanged = {
            "Fine-Line-Group-2026-SOW.pdf": quality.QUALITY_GOOD,
            "IMG_6909.HEIC": quality.QUALITY_POOR,
            "Screenshot 2026-09-12 at 10.14.32 AM.png": quality.QUALITY_POOR,
            "download (7).zip": quality.QUALITY_POOR,
            "untitled.pdf": quality.QUALITY_POOR,
            "new-file-copy-final.pdf": quality.QUALITY_POOR,
            "7f3a9c2e1b8d4f6a0c5e.dat": quality.QUALITY_POOR,
            "kitchen-layout-revision.png": quality.QUALITY_GOOD,
            "download (7).zip": quality.QUALITY_POOR,
        }
        for name, expected in unchanged.items():
            with self.subTest(name=name):
                self.assertEqual(quality.assess(name)[0], expected)


# ---------------------------------------------------------------------------
# D-019 / D-026 -- nothing is written outside the UUID-verified volume root
# ---------------------------------------------------------------------------


class VolumeRootContainmentTests(unittest.TestCase):
    def _repo(self, directory: Path) -> Path:
        """A one-commit git repository, so a mirror attempt is a real one."""
        flags = ["-c", "user.email=a@b", "-c", "user.name=a"]
        subprocess.run(["git", "init", "-q", str(directory)], check=True, capture_output=True)
        (directory / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(directory), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(directory), *flags, "commit", "-qm", "seed"],
            check=True, capture_output=True,
        )
        return directory

    def test_a_mirror_name_cannot_steer_a_clone_out_of_the_volume(self):
        """`angel-sync archive --name` puts a caller string into a path.

        Before this was validated, `--name sub/../../../../tmp/evil` had
        `git clone` create the bare repository outside the resolved root (git
        creates leading directories), and the rename that followed raised an
        uncaught `FileNotFoundError` out of `main()`.
        """
        from tools.angel_sync import AngelSyncError, refresh_mirror
        from tools.ingest.repos_report import MirrorPlan

        hostile = (
            "../../../../OUTSIDE/pwned",
            "sub/../../../../OUTSIDE/pwned",
            "nested/deep",
            "..",
            ".",
            "/absolute",
            ".hidden",
            ".git",
            "trailing.",
            "name\x00nul",
            "name with spaces",
            "a" * 200,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._repo(root / "src")
            volume = root / "Angel"
            archives = volume / "10-code" / "archives"
            archives.mkdir(parents=True)
            outside = root / "OUTSIDE"
            outside.mkdir()

            for name in hostile:
                with self.subTest(name=name):
                    plan = MirrorPlan(mirror_name=name, source_path=source, remote_url="", git_dir_bytes=0)
                    with self.assertRaises(AngelSyncError):
                        refresh_mirror(plan, archives, verify=False)

            self.assertEqual(list(outside.iterdir()), [], "a clone landed outside the volume root")
            self.assertEqual(list(archives.iterdir()), [], "a refused name still created something")
            self.assertEqual(
                sorted(p.name for p in volume.rglob("*") if p.is_dir()),
                ["10-code", "archives"],
                "a refused name created a directory under the volume root",
            )

    def test_a_legitimate_mirror_name_still_works(self):
        """The guard is an allowlist, so it has to be proved non-vacuous."""
        from tools.angel_sync import refresh_mirror
        from tools.ingest.repos_report import MirrorPlan

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._repo(root / "src")
            mirrors = root / "Angel" / "10-code" / "mirrors"
            mirrors.mkdir(parents=True)
            plan = MirrorPlan(
                mirror_name="ai-homelab-control-plane", source_path=source, remote_url="", git_dir_bytes=0
            )
            result = refresh_mirror(plan, mirrors, verify=False)
            self.assertEqual(result.action, "created")
            self.assertTrue((mirrors / "ai-homelab-control-plane.git").is_dir())
            # And a second run refreshes rather than failing or duplicating.
            again = refresh_mirror(plan, mirrors, verify=False)
            self.assertEqual(again.action, "refreshed")
            self.assertEqual(
                sorted(p.name for p in mirrors.iterdir()), ["ai-homelab-control-plane.git"]
            )

    def test_a_non_git_directory_at_the_mirror_path_is_never_written_into(self):
        from tools.angel_sync import refresh_mirror
        from tools.ingest.repos_report import MirrorPlan

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._repo(root / "src")
            mirrors = root / "Angel" / "10-code" / "mirrors"
            mirrors.mkdir(parents=True)
            occupied = mirrors / "photos.git"
            occupied.mkdir()
            (occupied / "holiday.jpg").write_bytes(b"owner-bytes")

            plan = MirrorPlan(mirror_name="photos", source_path=source, remote_url="", git_dir_bytes=0)
            result = refresh_mirror(plan, mirrors, verify=False)
            self.assertEqual(result.action, "skipped")
            self.assertIn("not a git repository", result.detail)
            self.assertEqual(
                sorted(p.name for p in occupied.iterdir()), ["holiday.jpg"],
                "the owner's directory was written into",
            )
            self.assertEqual((occupied / "holiday.jpg").read_bytes(), b"owner-bytes")

    def test_a_symlink_at_the_mirror_path_is_not_fetched_through(self):
        """The UUID gate resolves a *root*. Following a link out of it would
        make that resolution decorative."""
        from tools.angel_sync import refresh_mirror
        from tools.ingest.repos_report import MirrorPlan

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._repo(root / "src")
            elsewhere = self._repo(root / "elsewhere")
            mirrors = root / "Angel" / "10-code" / "mirrors"
            mirrors.mkdir(parents=True)
            (mirrors / "sneaky.git").symlink_to(elsewhere / ".git")

            plan = MirrorPlan(mirror_name="sneaky", source_path=source, remote_url="", git_dir_bytes=0)
            result = refresh_mirror(plan, mirrors, verify=False)
            self.assertEqual(result.action, "skipped")
            self.assertIn("symlink", result.detail)

    def test_angel_sync_contains_no_delete_and_no_push(self):
        """Two of the three invariants in `angel_sync`'s own docstring.

        Checked over the parsed AST rather than the text, because the module
        *documents* both invariants in prose -- a substring search finds
        `git push` in the sentence promising it never happens, which is how a
        grep-shaped test manages to be both failing and vacuous at once.
        """
        import ast

        import tools.angel_sync as angel_sync

        tree = ast.parse(Path(angel_sync.__file__).read_text(encoding="utf-8"))
        forbidden_attributes = {"rmtree", "unlink", "remove", "rmdir", "removedirs", "truncate"}
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Attribute) and target.attr in forbidden_attributes:
                    offenders.append(f"line {node.lineno}: .{target.attr}()")
                if isinstance(target, ast.Name) and target.id == "_git":
                    literals = [
                        argument.value
                        for argument in node.args
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                    ]
                    for verb in ("push", "prune", "gc", "reset", "filter-branch"):
                        if verb in literals:
                            offenders.append(f"line {node.lineno}: git {verb}")
        self.assertEqual(offenders, [], "angel_sync must neither delete nor push")

    def test_a_config_cannot_relocate_the_catalog_off_the_verified_volume(self):
        """`Path(root) / "/tmp/x"` is `/tmp/x`, and `..` walks out.

        `t7.inbox_relative_path` and `t7.system_relative_path` are joined to
        the UUID-verified root by `resolve_t7`, and neither was validated --
        while `automation.state_relative_path` has been validated for exactly
        this reason since it was added. A config file must never be able to
        move a write target outside the volume the UUID check resolved (D-026).
        """
        hostile = (
            "/tmp/angel-escape",
            "../../../../tmp/angel-escape",
            "90-system/../../../tmp/angel-escape",
            "..",
        )
        for key in ("inbox_relative_path", "system_relative_path"):
            for value in hostile:
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / "ingest.yaml"
                    path.write_text(f"t7:\n  {key}: {value!r}\n", encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_config(path)

    def test_the_documented_relative_paths_still_load(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ingest.yaml"
            path.write_text(
                "t7:\n  inbox_relative_path: '00-inbox/organized'\n  system_relative_path: '90-system'\n",
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.t7.inbox_relative_path, "00-inbox/organized")
            self.assertEqual(config.t7.system_relative_path, "90-system")

    def test_the_bootstrap_script_resolves_its_root_before_the_volumes_check(self):
        """`/tmp/angel -> /Volumes/Angel` must not skip `--expect-uuid`.

        The gate is a string test on the argument, so the argument has to be
        the real path. Asserted on the script's text rather than by running it,
        because running it means writing a volume layout somewhere.
        """
        script = (Path(__file__).resolve().parents[2] / "scripts" / "bootstrap-t7.sh").read_text(
            encoding="utf-8"
        )
        resolve_at = script.index('pwd -P')
        check_at = script.index('"$ROOT" == /Volumes')
        self.assertLess(resolve_at, check_at, "ROOT must be resolved before the /Volumes check")


# ---------------------------------------------------------------------------
# The terminal is an output boundary
# ---------------------------------------------------------------------------


class TerminalOutputTests(unittest.TestCase):
    def test_the_excerpt_helper_strips_escapes_whatever_it_is_handed(self):
        """`_print_excerpt` is where attacker-influenced text meets a terminal.

        Its docstring used to justify printing raw on the grounds that the
        caller had already sanitized. One caller had not: `raw_output` is
        bounded at 500 characters and stored *verbatim* -- deliberately, so a
        refused answer reads back exactly as the model produced it -- so a
        model persuaded by injected document text to echo an escape sequence
        reached the owner's terminal through the very command that exists to
        inspect hostile proposals.
        """
        import io
        from contextlib import redirect_stdout

        from tools.ingest.cli import _print_excerpt

        payloads = (
            "\x1b]0;pwned\x07harmless",
            "\x9b2J\x9bHcleared",
            "before\x1b[31mred\x1b[0mafter",
            "bidi‮evil",
            "zero​width",
            "nul\x00byte",
        )
        for payload in payloads:
            with self.subTest(payload=repr(payload)):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    _print_excerpt(payload)
                printed = buffer.getvalue()
                for character in printed:
                    category = unicodedata.category(character)
                    if character in ("\n", "\t"):
                        continue
                    self.assertNotIn(
                        category, ("Cc", "Cf", "Cs", "Co"),
                        f"{payload!r} printed a {category} character ({hex(ord(character))})",
                    )

    def test_the_excerpt_helper_still_prints_the_text(self):
        """Non-vacuous: the strip must not have turned it into a no-op."""
        import io
        from contextlib import redirect_stdout

        from tools.ingest.cli import _print_excerpt

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _print_excerpt("\x1b[31mQuarterly revenue report\x1b[0m")
        self.assertIn("Quarterly revenue report", buffer.getvalue())

    def test_the_raw_output_column_stays_bounded(self):
        from tools.ingest.naming.semantic import MAX_RAW_OUTPUT_CHARS, _bounded_raw

        self.assertEqual(len(_bounded_raw("x" * 100_000)), MAX_RAW_OUTPUT_CHARS)
        # And verbatim within the bound -- the audit fidelity
        # `test_semantic_proposer.py` relies on.
        self.assertEqual(_bounded_raw("roof​framing.pdf"), "roof​framing.pdf")


# ---------------------------------------------------------------------------
# docs/26 -- nothing in tools/ loads executable code from an untrusted tree
# ---------------------------------------------------------------------------


class UntrustedCodeLoadTests(unittest.TestCase):
    def test_a_task_id_cannot_point_the_probe_loader_outside_the_fixtures_tree(self):
        """`load_fixture` resolves the `probes.py` that `build_probes` execs.

        Every other name in a fixture manifest was checked against `..` and an
        absolute path; `task_id` -- which arrives from a corpus JSON a caller
        may load from anywhere (`load_corpus(path)`) -- was not. That made it
        the one path in `tools/` that could execute Python from outside the
        reviewed tree, including from a mirror or from
        `40-ai/skills/candidates/`.

        The planted bundle is *structurally valid*, so nothing but the
        `task_id` check can stop it: a manifest that failed validation would
        make this test pass for the wrong reason. Its `probes.py` writes a
        marker file on import, so "was it executed?" is a fact on disk rather
        than an inference from an exception type.
        """
        import json as json_module

        from tools.fixtures.probes import build_probes
        from tools.fixtures.spec import SUPPORTED_MANIFEST_VERSION, load_fixture

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "fixtures"
            (root / "legit").mkdir(parents=True)
            marker = Path(temp) / "EXECUTED"

            planted = Path(temp) / "candidates"
            (planted / "starter").mkdir(parents=True)
            (planted / "starter" / "solution.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (planted / "task.md").write_text("# planted\n", encoding="utf-8")
            (planted / "fixture.json").write_text(
                json_module.dumps(
                    {
                        "version": SUPPORTED_MANIFEST_VERSION,
                        "task_id": "../candidates",
                        "starter_files": ["solution.py"],
                        "editable_files": ["solution.py"],
                        "probes": "probes.py",
                        "instructions": "task.md",
                    }
                ),
                encoding="utf-8",
            )
            (planted / "probes.py").write_text(
                f"from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
                f"def build(rng):\n    return []\n",
                encoding="utf-8",
            )

            for task_id in ("../candidates", "../../candidates", "/etc", "legit/../../candidates", ".."):
                with self.subTest(task_id=task_id):
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        spec = load_fixture(task_id, root=root)
                        build_probes(spec.probes_path, task_id)
                    self.assertFalse(
                        marker.exists(),
                        f"task_id {task_id!r} executed a probes module outside the fixtures tree",
                    )

    def test_the_shipped_fixtures_still_load(self):
        from tools.fixtures.spec import available_fixtures, load_fixture

        names = available_fixtures()
        self.assertTrue(names, "there should be fixtures to load")
        for task_id in names:
            with self.subTest(task_id=task_id):
                spec = load_fixture(task_id)
                self.assertTrue(spec.probes_path.is_file())

    def test_no_tool_reads_from_an_untrusted_tree_as_code(self):
        """A grep-shaped invariant, kept because the tree is the control.

        `40-ai/skills/candidates/` and `50-research/knowledge/` are untrusted
        by construction (docs/26). Nothing under `tools/` may name either as
        something to import, exec, or load a module from.
        """
        tools_root = Path(__file__).resolve().parents[2] / "tools"
        offenders = []
        for path in sorted(tools_root.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                bare = line.strip()
                if bare.startswith("#") or '"""' in bare:
                    continue
                names_untrusted = "skills/candidates" in bare or "50-research/knowledge" in bare
                loads_code = any(
                    token in bare
                    for token in ("exec(", "eval(", "spec_from_file_location", "exec_module", "__import__")
                )
                if names_untrusted and loads_code:
                    offenders.append(f"{path.relative_to(tools_root)}: {bare}")
        self.assertEqual(offenders, [])


# ---------------------------------------------------------------------------
# transaction.py -- the only code that removes a user's file
# ---------------------------------------------------------------------------


class SourceSwapTests(unittest.TestCase):
    """`_finish_unlink` re-proves the source is the file that was copied.

    `test_transaction.py` covers the source being *rewritten* between the hash
    and the unlink. These are the two variants it does not name: the pathname
    being replaced by a **symlink**, and by a **hard link** to something else.
    Both keep `st_size` plausible and both are a way to aim an `unlink` at a
    file that was never copied anywhere.
    """

    def setUp(self):
        from tools.ingest.catalog import Catalog

        from tests.ingest.helpers import build_config, make_resolved_t7

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.desktop = self.root / "Desktop"
        self.t7 = self.root / "T7"
        self.t7.mkdir()
        self.config = build_config(desktop=self.desktop, t7_root=self.t7, settle_seconds=1)
        self.resolved_t7 = make_resolved_t7(self.config)
        self.catalog = Catalog(self.resolved_t7.catalog_path)
        self.addCleanup(self.catalog.close)

    def _transfer(self, source: Path, hooks=None):
        from tools.ingest.transaction import PlannedTransfer, transfer_file

        return transfer_file(
            self.catalog,
            PlannedTransfer(
                source_path=source,
                rel_path=source.name,
                source_volume_id="local-test-volume",
                source_class="desktop",
            ),
            self.resolved_t7,
            settle_seconds=1,
            hooks=hooks,
        )

    def test_a_source_replaced_by_a_symlink_is_refused_by_the_identity_check(self):
        """Swapped *before* `_finish_unlink` runs its checks.

        `before_final_commit` is the last hook that fires ahead of the
        identity re-check, so this is the window the check exists for.
        `_stat_fingerprint` follows the link, so the swapped-in target's
        (device, inode) will not match what was sampled around the hash.
        """
        from tools.ingest.catalog import STATE_NEEDS_REVIEW
        from tools.ingest.transaction import TransferFaultHooks

        from tests.ingest.helpers import write_file

        source = write_file(self.desktop / "report.bin", b"the real bytes", age_seconds=3600)
        bystander = self.root / "PRIVATE-KEY"
        bystander.write_bytes(b"not ours to delete")

        def swap_in_a_symlink() -> None:
            source.unlink()
            source.symlink_to(bystander)

        outcome = self._transfer(source, hooks=TransferFaultHooks(before_final_commit=swap_in_a_symlink))

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.state, STATE_NEEDS_REVIEW)
        self.assertTrue(bystander.exists(), "the symlink's target was deleted")
        self.assertEqual(bystander.read_bytes(), b"not ours to delete")
        asset = self.catalog.get_asset(outcome.asset_id)
        self.assertEqual(asset.row["error_state"], "source_identity_mismatch")

    def test_a_source_replaced_by_a_hard_link_is_refused_by_the_identity_check(self):
        from tools.ingest.catalog import STATE_NEEDS_REVIEW
        from tools.ingest.transaction import TransferFaultHooks

        from tests.ingest.helpers import write_file

        source = write_file(self.desktop / "notes.bin", b"the real bytes", age_seconds=3600)
        bystander = self.root / "TAX-RETURN"
        bystander.write_bytes(b"also not ours")

        def swap_in_a_hard_link() -> None:
            import os as os_module

            source.unlink()
            os_module.link(bystander, source)

        outcome = self._transfer(source, hooks=TransferFaultHooks(before_final_commit=swap_in_a_hard_link))

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.state, STATE_NEEDS_REVIEW)
        self.assertTrue(bystander.exists())
        self.assertEqual(bystander.read_bytes(), b"also not ours")

    def test_a_symlink_swapped_in_after_the_checks_costs_only_the_link(self):
        """The residual window, documented rather than claimed away.

        `before_unlink` fires after every identity check has passed, so this
        is the irreducible TOCTOU: nothing can be verified between the last
        check and the syscall. What bounds the damage is that `unlink` removes
        the *link*, never what it points at -- so the worst case is a lost
        symlink, not a lost file.
        """
        from tools.ingest.transaction import TransferFaultHooks

        from tests.ingest.helpers import write_file

        source = write_file(self.desktop / "late.bin", b"the real bytes", age_seconds=3600)
        bystander = self.root / "SSH-KEY"
        bystander.write_bytes(b"still not ours")

        def swap_in_a_symlink() -> None:
            source.unlink()
            source.symlink_to(bystander)

        self._transfer(source, hooks=TransferFaultHooks(before_unlink=swap_in_a_symlink))
        self.assertTrue(bystander.exists(), "unlink followed a symlink to its target")
        self.assertEqual(bystander.read_bytes(), b"still not ours")

    def test_the_volume_vanishing_mid_run_costs_the_copy_and_not_the_source(self):
        """Angel unplugged between the plan and the copy.

        Not the same as the volume being *swapped* for a different one at the
        same path, which `resolve_t7` cannot see because it resolves the UUID
        once per run -- see docs/08's V1 adversarial review section for why
        that is accepted rather than fixed. What must hold either way is that
        a destination that is not there costs the transfer, never the file.
        """
        import shutil

        from tools.ingest.transaction import TransferFaultHooks

        from tests.ingest.helpers import write_file

        source = write_file(self.desktop / "holiday.bin", b"irreplaceable", age_seconds=3600)

        def unplug(written: int, total: int) -> None:
            if written < total:
                return
            shutil.rmtree(self.resolved_t7.staging_dir, ignore_errors=True)

        outcome = self._transfer(source, hooks=TransferFaultHooks(mid_copy=unplug))
        self.assertTrue(source.exists(), "the source must survive a vanished destination")
        self.assertEqual(source.read_bytes(), b"irreplaceable")
        if outcome.success:
            self.assertIsNotNone(outcome.final_path)


class InterruptedMigrationTests(unittest.TestCase):
    """A migration killed between steps re-applies harmlessly.

    `user_version` is advanced only after a step returns, so a crash leaves it
    unchanged and the next run re-applies the whole step over the parts that
    already exist. That is only safe because every step is additive and
    idempotent -- which is the property this test attacks rather than trusts.
    """

    def test_a_migration_killed_part_way_through_re_applies_without_loss(self):
        import sqlite3

        from tools.ingest.catalog import MIGRATIONS, Catalog

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "catalog.sqlite"
            catalog = Catalog(path)
            asset_id = "11111111-1111-1111-1111-111111111111"
            catalog.ensure_pending_asset(
                asset_id,
                source_volume_id="v",
                source_class="desktop",
                original_path="a.txt",
                original_filename="a.txt",
                size=3,
            )
            catalog.close()

            # Kill every step in turn: run the real DDL, then roll
            # `user_version` back as a crash before the marker would.
            total = len(MIGRATIONS)
            for stop_after in range(1, total + 1):
                connection = sqlite3.connect(path)
                try:
                    connection.execute(f"PRAGMA user_version = {stop_after - 1}")
                    connection.commit()
                finally:
                    connection.close()
                reopened = Catalog(path)
                try:
                    row = reopened.get_asset(asset_id)
                    self.assertIsNotNone(row, f"re-applying from step {stop_after} lost a row")
                    self.assertEqual(row.row["original_filename"], "a.txt")
                    self.assertEqual(reopened.schema_version, total)
                finally:
                    reopened.close()


# ---------------------------------------------------------------------------
# Corrupted media must never raise out of the perception layer
# ---------------------------------------------------------------------------


class CorruptMediaFuzzTests(unittest.TestCase):
    """`collect_evidence` never raises -- proved by mutation, not asserted.

    Bounded by wall-clock rather than by a mutation count, because the point
    is to spend a fixed budget attacking real decoders (`ffprobe`, `pypdf`,
    `zipfile`, Pillow) rather than to run a fixed number of cheap cases. The
    seed is fixed so a failure is reproducible, and a crashing input is
    reported with its hash and its bytes' length.
    """

    TIME_BUDGET_SECONDS = 25.0
    SEED = 20260918

    def test_no_byte_mutation_of_a_real_media_file_escapes_collect_evidence(self):
        config = EvidenceConfig()
        rng = random.Random(self.SEED)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            seeds: dict[str, bytes] = {
                ".pdf": make_text_pdf(root / "seed.pdf", ["Quarterly report", "body"]).read_bytes(),
            }
            if ffmpeg_available():
                seeds[".mp4"] = make_tone_video(root / "seed.mp4", duration=1.0).read_bytes()
                seeds[".m4a"] = make_tone_audio_file(root / "seed.m4a", duration=1.0).read_bytes()
            try:
                from tests.ingest.test_evidence_office import make_xlsx

                seeds[".xlsx"] = make_xlsx(root / "seed.xlsx", {"Sheet1": [["h"], ["v"]]}).read_bytes()
            except Exception:  # pragma: no cover - fixture builder unavailable
                pass

            work = root / "work"
            work.mkdir()
            deadline = time.monotonic() + self.TIME_BUDGET_SECONDS
            attempts = 0
            while time.monotonic() < deadline:
                extension, data = rng.choice(sorted(seeds.items()))
                mutated = bytearray(data)
                for _ in range(rng.randint(1, 12)):
                    if not mutated:
                        break
                    mutated[rng.randrange(len(mutated))] = rng.randrange(256)
                if rng.random() < 0.3 and len(mutated) > 64:
                    mutated = mutated[: rng.randrange(16, len(mutated))]
                payload = bytes(mutated)
                path = work / f"fuzz{extension}"
                path.write_bytes(payload)
                digest = hashlib.sha256(payload).hexdigest()
                attempts += 1
                try:
                    facts = probe_file(path)
                    collect_evidence(
                        source=EvidenceSource(
                            path=path,
                            sha256=digest,
                            mime=None,
                            media_kind=facts.media_kind,
                            size=path.stat().st_size,
                        ),
                        facts=facts.facts,
                        screenshot_by_name=False,
                        config=config,
                        now="2026-09-18T00:00:00+00:00",
                        ledger=None,
                    )
                except Exception as exc:  # noqa: BLE001 - that is the assertion
                    self.fail(
                        f"{extension} mutation sha256={digest} ({len(payload)} bytes) "
                        f"escaped as {type(exc).__name__}: {exc}"
                    )
            self.assertGreater(attempts, 50, "the fuzz budget produced too few cases to mean anything")


if __name__ == "__main__":
    unittest.main()
