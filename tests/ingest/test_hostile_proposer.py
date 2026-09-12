"""What a malicious or broken proposer can actually do.

`tests/ingest/test_mutation_authority.py` proves the *structure*: a proposer is
handed no `Path`, and only two modules may rename. This proves the *behavior*
with a proposer that is actively trying to escape — because the whole design
rests on the claim that a model's output is inert until deterministic code has
finished with it, and that claim deserves to be attacked rather than asserted.

The proposer here is the worst case: it returns whatever string it likes with
confidence 1.0 from a source class that is enabled for naming.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.ingest.ledger import DECISION_RENAMED, LocalLedger
from tools.ingest.naming.proposer import NameProposal
from tools.ingest.pipeline import run_source

from tests.ingest.fi_helpers import fi_config, make_png

# Path traversal, absolute paths, hidden files, shell metacharacters, a
# right-to-left override that makes `...gpj.exe` read as `...exe.jpg`, an
# embedded NUL, newlines, and a name far past any filesystem limit.
HOSTILE_PROPOSALS = (
    "../../../../etc/passwd",
    "/etc/passwd",
    "..",
    ".",
    ".DS_Store",
    ".hidden-file",
    "‮gpj.exe",
    "a" * 500,
    "   ",
    "photo.jpg\x00.png",
    "name\nwith\nnewlines",
    "$(rm -rf /)",
    "`rm -rf /`",
    "; rm -rf / #",
    "~/Desktop/evil",
    "....//....//escape",
    "photo.jpg/../../../escape.jpg",
)


class _HostileProposer:
    """Returns an attacker-chosen string with maximum confidence."""

    model = "hostile-test-proposer"
    model_version = "1"

    def __init__(self, payload):
        self.payload = payload

    def propose(self, evidence) -> NameProposal:  # noqa: ANN001
        return NameProposal(
            proposed_filename=self.payload,
            confidence=1.0,
            model=self.model,
            model_version=self.model_version,
            reason="hostile",
        )


class HostileProposerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.screenshots = self.root / "Screenshots"
        self.downloads = self.root / "Downloads"
        for directory in (self.screenshots, self.downloads):
            directory.mkdir()
        self.sentinel = self.root / "SENTINEL.txt"
        self.sentinel.write_text("intact", encoding="utf-8")
        self.config = fi_config(self.root, downloads=self.downloads, screenshots=self.screenshots)

    def _run(self, payload, filename="Screenshot 2026-09-12 at 10.14.32 AM.png"):
        victim = make_png(self.screenshots / filename)
        content = victim.read_bytes()
        with LocalLedger(self.config.local_ledger_path) as ledger:
            report = run_source(
                self.config, ledger,
                source_class="screenshots",
                source_volume_id="mac-local",
                apply=True,
                settle_seconds=1,
                resolved_t7=None,
                proposer=_HostileProposer(payload),
            )
        return report, content

    def test_no_hostile_proposal_escapes_the_source_directory(self):
        for payload in HOSTILE_PROPOSALS:
            with self.subTest(payload=payload):
                report, content = self._run(payload)

                files = [path for path in self.screenshots.iterdir() if path.is_file()]
                self.assertEqual(len(files), 1, "exactly one file must remain, whatever was proposed")
                self.assertEqual(files[0].read_bytes(), content, "the bytes must be untouched")

                stray = sorted(p.name for p in self.root.iterdir() if p.is_file() and p.name != "SENTINEL.txt")
                self.assertEqual(stray, [], f"{payload!r} wrote outside the source directory")
                self.assertEqual(self.sentinel.read_text(encoding="utf-8"), "intact")

                for outcome in report.outcomes:
                    applied = outcome.applied_filename
                    if applied is None:
                        continue
                    self.assertNotIn("/", applied)
                    self.assertNotIn("\\", applied)
                    self.assertNotIn("\x00", applied)
                    self.assertNotIn("\n", applied)
                    self.assertFalse(applied.startswith("."), "a proposal must not be able to hide a file")
                    self.assertLessEqual(len(applied.encode("utf-8")), 255)

                for path in list(self.screenshots.iterdir()):
                    path.unlink()

    def test_a_hostile_proposal_cannot_change_the_file_type(self):
        """`‮gpj.exe` and `photo.jpg\\x00.png` are both attempts to make a
        file read as something it is not. The extension comes from disk."""
        for payload in ("‮gpj.exe", "photo.jpg\x00.png", "invoice.pdf", "report.exe"):
            with self.subTest(payload=payload):
                report, _ = self._run(payload)
                for outcome in report.outcomes:
                    if outcome.applied_filename:
                        self.assertTrue(
                            outcome.applied_filename.endswith(".png"),
                            f"{payload!r} changed the extension to {outcome.applied_filename}",
                        )
                for path in list(self.screenshots.iterdir()):
                    path.unlink()

    def test_a_proposer_that_raises_does_not_end_the_run(self):
        """One broken proposal must not stop a scan over thousands of files."""

        class Exploding:
            model = "exploding"
            model_version = "1"

            def propose(self, evidence):  # noqa: ANN001
                raise RuntimeError("proposer exploded")

        first = make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png")
        second = make_png(self.screenshots / "Screenshot 2026-09-12 at 11.00.00 AM.png")
        with LocalLedger(self.config.local_ledger_path) as ledger:
            report = run_source(
                self.config, ledger,
                source_class="screenshots",
                source_volume_id="mac-local",
                apply=True,
                settle_seconds=1,
                resolved_t7=None,
                proposer=Exploding(),
            )
        self.assertEqual(report.discovered, 2)
        self.assertTrue(first.exists() and second.exists(), "both files must survive a broken proposer")
        self.assertTrue(report.errors, "the failure must be reported, not swallowed silently")

    def test_a_proposer_returning_a_non_string_is_refused(self):
        class Wrong:
            model = "wrong-type"
            model_version = "1"

            def propose(self, evidence):  # noqa: ANN001
                return NameProposal(
                    proposed_filename=b"bytes-not-str",  # type: ignore[arg-type]
                    confidence=1.0,
                    model=self.model,
                    model_version=self.model_version,
                    reason="wrong type",
                )

        victim = make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png")
        with LocalLedger(self.config.local_ledger_path) as ledger:
            report = run_source(
                self.config, ledger,
                source_class="screenshots",
                source_volume_id="mac-local",
                apply=True,
                settle_seconds=1,
                resolved_t7=None,
                proposer=Wrong(),
            )
        self.assertTrue(victim.exists(), "a badly typed proposal must leave the file alone")
        for outcome in report.outcomes:
            self.assertNotEqual(outcome.naming_decision, DECISION_RENAMED)


if __name__ == "__main__":
    unittest.main()
