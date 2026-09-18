"""Evidence reaching production naming, end to end (D-057).

`test_perception_bridge.py` proves the conversion. `test_semantic_proposer.py`
proves what a model can and cannot do. This proves the thing the owner
actually asked for: a real `run_source` over a real temp folder produces a
*better filename* because something was perceived about the file -- and every
property that held before still holds.

The positive cases first, because without them the safety tests could all
pass by the feature simply never working:

* a machine-named PDF is named from its own first heading;
* a screenshot is named from the title line of its OCR;
* a video is *not* named from a vision description, deterministically, however
  confident the description sounds.

Then the properties that must survive it: rerunning changes nothing and hits
the evidence cache, Angel being unplugged still names, an evidence stage that
explodes does not cost a name, and two files that perceive the same title get
two distinct policy-safe names.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import unittest
import tempfile
from pathlib import Path
from unittest import mock

import yaml

from tools.ingest.config import load_config
from tools.ingest.evidence.types import OCRBlock, Polygon
from tools.ingest.ledger import (
    DECISION_ALREADY_NAMED,
    DECISION_RENAMED,
    LocalLedger,
)
from tools.ingest.pipeline import run_source

from tests.ingest.evidence_fakes import FakeOCRBackend, make_text_pdf
from tests.ingest.fi_helpers import aged, fi_config, make_png
from tests.ingest.test_evidence_security import assert_policy_legal_filename


def _ocr_blocks(lines, *, page: int = 1):
    return tuple(
        OCRBlock(
            text=text,
            polygon=Polygon.from_bbox(0, index * 40, 900, index * 40 + 30),
            page=page,
            confidence=0.93,
            reading_order=index,
            engine="fake-ocr",
            engine_version="1",
        )
        for index, text in enumerate(lines)
    )


class _PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.downloads = self.root / "Downloads"
        self.screenshots = self.root / "Screenshots"
        for directory in (self.downloads, self.screenshots):
            directory.mkdir()
        self.config = fi_config(self.root, downloads=self.downloads, screenshots=self.screenshots)

    def _run(self, *, source_class: str, apply: bool = True, config=None, resolved_t7=None):
        config = config if config is not None else self.config
        with LocalLedger(config.local_ledger_path) as ledger:
            return run_source(
                config, ledger, source_class=source_class, source_volume_id="mac-local",
                apply=apply, settle_seconds=1, resolved_t7=resolved_t7,
            )

    def _run_with_ocr(self, lines, *, source_class: str = "screenshots", apply: bool = True, config=None):
        backend = FakeOCRBackend(blocks_by_page={1: _ocr_blocks(lines)})
        with mock.patch("tools.ingest.evidence.extract.build_ocr_backend", return_value=backend):
            report = self._run(source_class=source_class, apply=apply, config=config)
        return report, backend


class NamedFromNativeTextTests(_PipelineTestCase):
    def test_a_machine_named_pdf_is_named_from_its_own_first_heading(self):
        path = make_text_pdf(
            self.downloads / "document (12).pdf",
            ["Kitchen Cabinet Elevation Schedule", "Sheet A-501 of the permit set.", "Revision three."],
        )
        aged(path)

        report = self._run(source_class="downloads")
        outcome = report.outcomes[0]

        self.assertEqual(outcome.naming_decision, DECISION_RENAMED)
        applied = outcome.applied_filename
        self.assertIsNotNone(applied)
        assert_policy_legal_filename(self, applied, extension=".pdf")
        for word in ("kitchen", "cabinet", "elevation"):
            self.assertIn(word, applied.lower())
        self.assertTrue((self.downloads / applied).exists())
        self.assertFalse(path.exists(), "the old name is gone; the bytes are not")

    def test_the_same_pdf_with_no_text_layer_falls_back_to_a_factual_generic_name(self):
        """The trade docs/16 states, still intact.

        Without perceived text there is nothing to name the file *about*, and
        the proposer says what the file is rather than inventing a subject.
        """
        from tests.ingest.evidence_fakes import make_scanned_pdf

        path = make_scanned_pdf(self.downloads / "document (13).pdf")
        aged(path)
        report = self._run(source_class="downloads")
        applied = report.outcomes[0].applied_filename or report.outcomes[0].proposed_filename
        if applied:
            assert_policy_legal_filename(self, applied, extension=".pdf")
            self.assertIn("document", applied.lower())

    def test_embedded_title_metadata_still_outranks_a_perceived_heading(self):
        """Order of evidence strength, asserted rather than assumed.

        A title field is a claim the authoring tool made deliberately; a first
        heading is an inference from position. When a file carries both, the
        title must win -- otherwise adding perception would have *downgraded*
        naming for every file that states its own title.
        """
        from tools.ingest.naming.evidence import NamingEvidence, PerceivedEvidence
        from tools.ingest.naming.proposer import CONFIDENCE_EMBEDDED_TITLE, DeterministicProposer

        evidence = NamingEvidence(
            source_class="downloads", original_filename="document (1).pdf", extension=".pdf",
            size=4096, mime="application/pdf", media_kind="document",
            name_quality="poor", name_quality_reason="machine-generated",
            facts={"metadata_title": "Signed Vendor Agreement", "page_count": 12},
            perceived=PerceivedEvidence(
                native_text_excerpt="Kitchen Cabinet Elevation Schedule\nSheet A-501",
                headings=("Kitchen Cabinet Elevation Schedule",),
                sources=("native_text", "structure"),
            ),
        )
        proposal = DeterministicProposer().propose(evidence)
        self.assertEqual(proposal.confidence, CONFIDENCE_EMBEDDED_TITLE)
        self.assertIn("vendor", proposal.proposed_filename.lower())
        self.assertNotIn("kitchen", proposal.proposed_filename.lower())


class NamedFromOcrTests(_PipelineTestCase):
    def test_a_screenshot_is_named_from_the_title_line_of_its_ocr(self):
        make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(1600, 900))

        report, backend = self._run_with_ocr([
            "Angel Naming Lab — Review Queue",
            "12 files awaiting a decision",
            "Filter: screenshots only",
        ])
        self.assertTrue(backend.calls, "the fake OCR backend must actually have run")

        outcome = report.outcomes[0]
        self.assertEqual(outcome.naming_decision, DECISION_RENAMED)
        applied = outcome.applied_filename
        assert_policy_legal_filename(self, applied, extension=".png")
        self.assertIn("screen-capture", applied.lower())
        for word in ("angel", "naming", "lab"):
            self.assertIn(word, applied.lower())

    def test_ocr_is_only_trusted_for_a_known_screen_capture(self):
        """A shop sign in a photograph must not become the filename.

        `screenshot_by_name` (the filename or the configured folder says so),
        not the probe's `is_probably_screenshot` pixel guess, is what licenses
        the OCR-title builder. A camera photo with text in it gets the device
        name it always got.
        """
        from tools.ingest.naming.evidence import NamingEvidence, PerceivedEvidence
        from tools.ingest.naming.proposer import DeterministicProposer

        base = dict(
            source_class="downloads", original_filename="IMG_6909.JPG", extension=".jpg",
            size=400_000, mime="image/jpeg", media_kind="image",
            name_quality="poor", name_quality_reason="camera-generated",
            facts={"width": 4032, "height": 3024, "camera_make": "Apple", "camera_model": "iPhone 15 Pro"},
            perceived=PerceivedEvidence(
                ocr_text_excerpt="ACME HARDWARE AND SUPPLY\nOpen 7 days",
                sources=("ocr",),
            ),
        )
        photo = DeterministicProposer().propose(NamingEvidence(**base, screenshot_by_name=False))
        self.assertNotIn("acme", photo.proposed_filename.lower())
        self.assertIn("iphone", photo.proposed_filename.lower())

        capture = DeterministicProposer().propose(NamingEvidence(**base, screenshot_by_name=True))
        self.assertIn("acme", capture.proposed_filename.lower())


class VisionAndTranscriptAreNotUsedDeterministicallyTests(unittest.TestCase):
    """The line the deterministic proposer does not cross.

    A vision description and a speech transcript are summaries somebody (or
    something) else wrote *about* the file, not text the file contains --
    and `HeuristicVisionBackend` is explicitly a guess from colour
    distribution and flatness. Turning either into a filename without a model
    weighing it against everything else would be asserting certainty the
    evidence does not carry. `SemanticProposer` may read them; this may not.
    """

    def _evidence(self, **perceived_kwargs):
        from tools.ingest.naming.evidence import NamingEvidence, PerceivedEvidence

        return NamingEvidence(
            source_class="downloads", original_filename="movie (3).mp4", extension=".mp4",
            size=8_000_000, mime="video/mp4", media_kind="video",
            name_quality="poor", name_quality_reason="machine-generated",
            facts={"duration_seconds": 42.0, "width": 1920, "height": 1080},
            perceived=PerceivedEvidence(**perceived_kwargs),
        )

    def test_a_confident_visual_description_does_not_reach_the_filename(self):
        from tools.ingest.naming.proposer import DeterministicProposer

        proposal = DeterministicProposer().propose(self._evidence(
            visual_descriptions=("a hawk perched on a cedar fence in bright sunlight",),
            visual_entities=("hawk", "fence"),
            visual_image_kind="ordinary_photo",
            sources=("visual",),
        ))
        lowered = proposal.proposed_filename.lower()
        for word in ("hawk", "cedar", "fence", "sunlight"):
            self.assertNotIn(word, lowered)
        self.assertIn("video-clip", lowered)

    def test_a_transcript_does_not_reach_the_filename(self):
        from tools.ingest.naming.proposer import DeterministicProposer

        proposal = DeterministicProposer().propose(self._evidence(
            transcript_excerpt="so this is the kitchen island we discussed with the Fineline team",
            sources=("transcript",),
        ))
        lowered = proposal.proposed_filename.lower()
        for word in ("kitchen", "island", "fineline"):
            self.assertNotIn(word, lowered)

    def test_the_semantic_proposer_does_see_both(self):
        """The counterpart claim: they are withheld from one proposer, not lost."""
        from tools.ingest.naming.semantic import build_task_text

        task_text = build_task_text(self._evidence(
            visual_descriptions=("a hawk perched on a cedar fence",),
            transcript_excerpt="the kitchen island we discussed",
            sources=("visual", "transcript"),
        ))
        self.assertIn("hawk perched on a cedar fence", task_text)
        self.assertIn("the kitchen island we discussed", task_text)


class PropertiesThatMustSurviveTests(_PipelineTestCase):
    def test_a_second_run_renames_nothing_and_hits_the_evidence_cache(self):
        """Idempotency (invariant 7) plus D-031's content-keyed cache.

        The rename happens on run one. On run two the file is at a *new path*
        with the same bytes, which is exactly the case the cache key was
        designed for: no pathname appears in it, so the perception is reused
        rather than re-derived, and the naming gate returns `already_named`
        without asking a proposer anything.
        """
        make_text_pdf(
            self.downloads / "document (21).pdf",
            ["Roof Framing Plan Revision Three", "Sheet A-201 of the permit set."],
        )
        aged(self.downloads / "document (21).pdf")

        first = self._run(source_class="downloads")
        self.assertEqual(first.renamed, 1)
        applied = first.outcomes[0].applied_filename
        self.assertIsNotNone(applied)
        self.assertGreaterEqual(first.evidence_collected, 1)

        aged(self.downloads / applied)
        second = self._run(source_class="downloads")
        self.assertEqual(second.renamed, 0, "a second run must rename nothing")
        self.assertEqual(second.outcomes[0].naming_decision, DECISION_ALREADY_NAMED)
        self.assertGreater(
            second.evidence_cache_hits, 0,
            "the same bytes at a new path must hit the content-keyed evidence cache",
        )
        self.assertEqual(
            sorted(p.name for p in self.downloads.iterdir()), [applied],
            "exactly one file, under the name run one chose",
        )

    def test_naming_works_with_angel_disconnected(self):
        """D-021: the naming ledger is on the Mac, so naming does not need the T7."""
        make_text_pdf(
            self.downloads / "document (22).pdf", ["Quarterly Revenue Summary", "Twelve months to March."]
        )
        aged(self.downloads / "document (22).pdf")

        report = self._run(source_class="downloads", resolved_t7=None)
        outcome = report.outcomes[0]
        self.assertEqual(outcome.naming_decision, DECISION_RENAMED)
        self.assertIn("revenue", outcome.applied_filename.lower())
        self.assertFalse(outcome.ingested)

    def test_an_evidence_stage_that_explodes_does_not_cost_a_name(self):
        """Perception is allowed to fail; naming is not allowed to stop.

        The proposer then sees `perceived=None` and names from measured facts
        alone -- the pre-D-057 behaviour, reached as a degradation rather than
        as a separate code path.
        """
        make_text_pdf(self.downloads / "document (23).pdf", ["Roof Framing Plan Revision Three"])
        aged(self.downloads / "document (23).pdf")

        with mock.patch(
            "tools.ingest.pipeline.collect_evidence", side_effect=RuntimeError("perception exploded")
        ):
            report = self._run(source_class="downloads")

        outcome = report.outcomes[0]
        self.assertTrue(report.errors, "the failure must be reported, not swallowed")
        self.assertIn("perception exploded", "\n".join(report.errors))
        self.assertIsNotNone(outcome.proposed_filename or outcome.applied_filename)
        self.assertEqual(outcome.evidence_kinds, (), "no evidence was collected")
        name = outcome.applied_filename or outcome.proposed_filename
        assert_policy_legal_filename(self, name, extension=".pdf")
        self.assertNotIn("roof", name.lower(), "the text was never perceived, so it cannot be in the name")

    def test_evidence_disabled_names_exactly_as_it_did_before(self):
        make_text_pdf(self.downloads / "document (24).pdf", ["Roof Framing Plan Revision Three"])
        aged(self.downloads / "document (24).pdf")

        config = dataclasses.replace(
            self.config, evidence=dataclasses.replace(self.config.evidence, enabled=False)
        )
        report = self._run(source_class="downloads", config=config)
        name = report.outcomes[0].applied_filename or report.outcomes[0].proposed_filename
        if name:
            self.assertNotIn("roof", name.lower())

    def test_two_files_perceiving_the_same_title_get_two_safe_distinct_names(self):
        """Collision handling, now reachable through perception.

        Two different screenshots of the same window produce the same OCR
        title and therefore the same proposal. `collision_free_name` resolves
        the second from *content* (`-<sha8>`), so the result is deterministic
        across reruns rather than a climbing counter -- and neither file is
        overwritten.
        """
        make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(1600, 900), color=(10, 20, 30))
        make_png(self.screenshots / "Screenshot 2026-09-12 at 10.15.00 AM.png", size=(1600, 900), color=(200, 40, 40))

        report, _ = self._run_with_ocr(["Angel Review Queue", "two files"])

        self.assertEqual(len(report.outcomes), 2)
        names = sorted(path.name for path in self.screenshots.iterdir() if path.is_file())
        self.assertEqual(len(names), 2, "neither file may be overwritten")
        self.assertEqual(len(set(names)), 2, "the two names must differ")
        for name in names:
            assert_policy_legal_filename(self, name, extension=".png")
            self.assertIn("angel", name.lower())

    def test_retention_is_unchanged_by_a_better_name(self):
        """Naming and retention stay separate questions (D-020).

        A screenshot that finally has a meaningful name is still a screenshot
        whose retention nobody can decide without a human, and it must not be
        promoted to an ingest candidate by having been renamed.
        """
        make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png", size=(1600, 900))
        report, _ = self._run_with_ocr(["Angel Review Queue", "12 files awaiting a decision"])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.naming_decision, DECISION_RENAMED)
        self.assertEqual(outcome.retention, "review")
        self.assertFalse(outcome.ingested)


class NamingHistoryCommandTests(_PipelineTestCase):
    """`organize naming-history PATH`: the audit question, answerable."""

    def _cli(self, argv):
        from tools.ingest import cli as cli_module

        original = cli_module.load_config
        cli_module.load_config = lambda _path: self.config
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli_module.main(argv)
            return code, buffer.getvalue()
        finally:
            cli_module.load_config = original

    def test_it_prints_the_evidence_that_produced_the_filename(self):
        make_text_pdf(
            self.downloads / "document (31).pdf",
            ["Kitchen Cabinet Elevation Schedule", "Sheet A-501 of the permit set."],
        )
        aged(self.downloads / "document (31).pdf")
        report = self._run(source_class="downloads")
        applied = report.outcomes[0].applied_filename
        self.assertIsNotNone(applied)

        code, output = self._cli(["naming-history", str(self.downloads / applied)])
        self.assertEqual(code, 0)
        self.assertIn("document (31).pdf", output)
        self.assertIn(applied, output)
        self.assertIn("renamed", output)
        self.assertIn("deterministic-proposer", output)
        self.assertIn("evidence kinds:", output)
        self.assertIn("native_text", output)
        self.assertIn("evidence sha256:", output)
        self.assertIn(report.outcomes[0].sha256, output)
        # A PDF's text layer carries no structural outline, so the builder
        # that fired is the first-meaningful-line one -- and the reason says
        # which, which is the point of printing it at all.
        self.assertIn("the document's own text layer", output)

    def test_it_reports_a_model_raw_answer_including_a_refused_one(self):
        from tests.ingest.test_semantic_proposer import _proposer

        make_png(self.screenshots / "Screenshot 2026-09-12 at 10.14.32 AM.png")
        proposer, _ = _proposer("../../etc/passwd")
        with LocalLedger(self.config.local_ledger_path) as ledger:
            run_source(
                self.config, ledger, source_class="screenshots", source_volume_id="mac-local",
                apply=True, settle_seconds=1, resolved_t7=None, proposer=proposer,
            )
        survivor = next(path for path in self.screenshots.iterdir() if path.is_file())

        code, output = self._cli(["naming-history", str(survivor)])
        self.assertEqual(code, 0)
        self.assertIn("prompt: acb-v1-semantic-naming", output)
        self.assertIn("raw model output", output)
        self.assertIn("../../etc/passwd", output)
        self.assertIn("model output refused", output)
        self.assertIn("path separator", output)

    def test_an_untracked_file_is_reported_rather_than_guessed_at(self):
        stranger = self.downloads / "never-scanned.pdf"
        stranger.write_bytes(b"%PDF-1.4\n")
        with LocalLedger(self.config.local_ledger_path):
            pass  # create the ledger so the "no ledger yet" branch is not the one under test
        code, output = self._cli(["naming-history", str(stranger)])
        self.assertEqual(code, 1)
        self.assertIn("Not in the ledger", output)

    def test_the_command_writes_nothing(self):
        make_text_pdf(self.downloads / "document (32).pdf", ["Roof Framing Plan Revision Three"])
        aged(self.downloads / "document (32).pdf")
        report = self._run(source_class="downloads")
        applied = report.outcomes[0].applied_filename
        target = self.downloads / applied

        before = {
            path: path.stat().st_mtime_ns
            for path in sorted(self.downloads.iterdir())
            if path.is_file()
        }
        rows_before = self._naming_row_count()

        self._cli(["naming-history", str(target)])

        after = {
            path: path.stat().st_mtime_ns
            for path in sorted(self.downloads.iterdir())
            if path.is_file()
        }
        self.assertEqual(before, after, "a read-only command must not touch the owner's files")
        self.assertEqual(rows_before, self._naming_row_count(), "it must not append a ledger row either")

    def _naming_row_count(self) -> int:
        with LocalLedger(self.config.local_ledger_path) as ledger:
            return sum(
                len(ledger.naming_history(row["local_file_id"])) for row in ledger.local_files()
            )


class ConfigTests(unittest.TestCase):
    def test_the_new_naming_keys_have_defaults_and_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "ingest.yaml"

            path.write_text(yaml.safe_dump({"sources": {"desktop": str(root)}}), encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config.naming.proposer, "deterministic")
            self.assertEqual(config.naming.semantic.endpoint, "http://127.0.0.1:11434")
            self.assertTrue(config.naming.semantic.fallback_to_deterministic)

            path.write_text(
                yaml.safe_dump({
                    "sources": {"desktop": str(root)},
                    "naming": {
                        "proposer": "semantic",
                        "semantic": {"model": "qwen3.5:9b", "min_confidence": 0.75, "num_predict": 32},
                    },
                }),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.naming.proposer, "semantic")
            self.assertEqual(config.naming.semantic.model, "qwen3.5:9b")
            self.assertEqual(config.naming.semantic.min_confidence, 0.75)
            self.assertEqual(config.naming.semantic.num_predict, 32)

    def test_an_unknown_proposer_is_refused_rather_than_silently_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "ingest.yaml"
            for bad in ("semantics", "model", "", "Deterministic"):
                with self.subTest(proposer=bad):
                    path.write_text(
                        yaml.safe_dump({"sources": {"desktop": str(root)}, "naming": {"proposer": bad}}),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError) as caught:
                        load_config(path)
                    self.assertIn("naming.proposer", str(caught.exception))

    def test_out_of_range_semantic_values_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "ingest.yaml"
            for semantic, fragment in (
                ({"timeout_seconds": 0}, "timeout_seconds"),
                ({"timeout_seconds": 10_000}, "timeout_seconds"),
                ({"num_predict": 0}, "num_predict"),
                ({"min_confidence": 1.5}, "min_confidence"),
                ({"model": "  "}, "model"),
            ):
                with self.subTest(semantic=semantic):
                    path.write_text(
                        yaml.safe_dump({
                            "sources": {"desktop": str(root)},
                            "naming": {"semantic": semantic},
                        }),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError) as caught:
                        load_config(path)
                    self.assertIn(fragment, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
