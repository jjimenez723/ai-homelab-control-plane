"""Naming defects found by the first real dry run over the owner's folders
during the V1 completion pass (2026-09-18), each pinned so it cannot return.

1. macOS writes `UserComment: Screenshot` into every capture, and the
   embedded-description builder (0.85) turned that into the name
   `2026-09-12_screenshot.png` -- outranking the OCR title line that could
   actually say what the window showed.
2. `Screenshot 2026-09-11 at 9.21.48 PM.png` was dated 2026-09-12: the
   date in its own filename lost to the mtime of a Drive-synced copy.
3. `CP_575_G Notice` by author `IRS` became `notice.pdf`: the identifier
   was shredded into stop-word-length fragments and the author ignored.
"""

from __future__ import annotations

import unittest

from tools.ingest.naming.evidence import NamingEvidence, PerceivedEvidence, date_from_filename
from tools.ingest.naming.proposer import DeterministicProposer


def _image(**overrides) -> NamingEvidence:
    base = dict(
        source_class="screenshots", original_filename="Screenshot 2026-09-11 at 9.21.48 PM.png",
        extension=".png", size=1_479_209, mime="image/png", media_kind="image",
        name_quality="poor", name_quality_reason="1 meaningful word(s) in the filename",
        modified_date="2026-09-12", filename_date="2026-09-11", is_screenshot=True,
        screenshot_by_name=True,
        facts={"width": 1416, "height": 1764, "exif_user_comment": "Screenshot", "is_probably_screenshot": True},
    )
    base.update(overrides)
    return NamingEvidence(**base)


class DateFromFilenameTests(unittest.TestCase):
    def test_common_capture_and_camera_spellings(self):
        self.assertEqual(date_from_filename("Screenshot 2026-09-11 at 9.21.48 PM.png"), "2026-09-11")
        self.assertEqual(date_from_filename("IMG_20260911_212148.jpg"), "2026-09-11")
        self.assertEqual(date_from_filename("report_2026_09_11 final.pdf"), "2026-09-11")
        self.assertEqual(date_from_filename("2026.09.11 invoice.pdf"), "2026-09-11")

    def test_non_dates_are_not_dates(self):
        for name in ("CP_575_G (1).pdf", "invoice-20261399.pdf", "IMG_6821.mp4", "3 Gallon Bottle.png", "phone 5551234567.txt"):
            self.assertIsNone(date_from_filename(name), name)

    def test_implausible_years_are_ignored(self):
        self.assertIsNone(date_from_filename("ref-18500101.txt"))


class ScreenshotUserCommentTests(unittest.TestCase):
    def test_the_exif_kind_word_alone_does_not_become_the_name(self):
        proposal = DeterministicProposer().propose(_image())
        assert proposal is not None
        self.assertNotIn("screenshot", proposal.proposed_filename.lower().replace("screen-capture", ""))
        self.assertTrue(proposal.proposed_filename.startswith("2026-09-11_"), proposal.proposed_filename)

    def test_ocr_title_now_wins_over_the_kind_word(self):
        perceived = PerceivedEvidence(ocr_text_excerpt="Oasis Pure Water - Order #4471\nDelivery Tuesday", sources=("ocr",))
        proposal = DeterministicProposer().propose(_image(perceived=perceived))
        assert proposal is not None
        self.assertEqual(proposal.proposed_filename, "2026-09-11_screen-capture-oasis-pure-water-order.png")

    def test_a_real_description_still_wins(self):
        proposal = DeterministicProposer().propose(_image(facts={"width": 10, "height": 10, "exif_user_comment": "Kitchen island quartz sample"}))
        assert proposal is not None
        self.assertIn("kitchen-island-quartz-sample", proposal.proposed_filename)


class FilenameDatePrecedenceTests(unittest.TestCase):
    def test_capture_date_beats_filename_date_beats_mtime(self):
        with_capture = _image(facts={"capture_timestamp": "2026-09-10T20:00:00", "width": 10, "height": 10})
        self.assertTrue(DeterministicProposer().propose(with_capture).proposed_filename.startswith("2026-09-10_"))
        no_capture = _image(facts={"width": 10, "height": 10})
        self.assertTrue(DeterministicProposer().propose(no_capture).proposed_filename.startswith("2026-09-11_"))
        nothing = _image(facts={"width": 10, "height": 10}, filename_date=None)
        self.assertTrue(DeterministicProposer().propose(nothing).proposed_filename.startswith("2026-09-12_"))


class ThinTitleWithAuthorTests(unittest.TestCase):
    def test_identifier_titles_keep_their_identifier_and_gain_their_author(self):
        evidence = NamingEvidence(
            source_class="downloads", original_filename="CP_575_G (1).pdf", extension=".pdf", size=10_057,
            mime="application/pdf", media_kind="document", name_quality="poor", name_quality_reason="x",
            modified_date="2026-08-11",
            facts={"metadata_title": "CP_575_G Notice", "metadata_author": "IRS", "page_count": 1},
        )
        proposal = DeterministicProposer().propose(evidence)
        assert proposal is not None
        self.assertEqual(proposal.proposed_filename, "2026-08-11_irs-cp-575-g-notice.pdf")

    def test_a_full_title_is_not_prefixed_with_its_author(self):
        evidence = NamingEvidence(
            source_class="downloads", original_filename="doc (2).pdf", extension=".pdf", size=10,
            mime="application/pdf", media_kind="document", name_quality="poor", name_quality_reason="x",
            modified_date="2026-08-11",
            facts={"metadata_title": "Maple Street Kitchen Renovation Scope of Work", "metadata_author": "Jane Doe", "page_count": 4},
        )
        self.assertEqual(DeterministicProposer().propose(evidence).proposed_filename, "2026-08-11_maple-street-kitchen-renovation-scope-work.pdf")

    def test_a_kind_word_only_title_declines_to_the_next_builder(self):
        evidence = NamingEvidence(
            source_class="downloads", original_filename="doc (2).pdf", extension=".pdf", size=10,
            mime="application/pdf", media_kind="document", name_quality="poor", name_quality_reason="x",
            modified_date="2026-08-11", facts={"metadata_title": "Untitled document", "page_count": 4},
        )
        proposal = DeterministicProposer().propose(evidence)
        assert proposal is not None
        self.assertEqual(proposal.proposed_filename, "2026-08-11_document-4p.pdf")


if __name__ == "__main__":
    unittest.main()
