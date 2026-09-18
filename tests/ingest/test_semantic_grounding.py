"""Findings from the first live semantic-naming drill (qwen3.5:9b, 2026-09-18):

1. The heuristic vision backend guessed `ui_capture` for a flat synthetic
   image and the model echoed it: `IMG_6821.jpg` -> `ui-capture.jpg`, worse
   than the deterministic `photo-apple-iphone-15-pro-4032x3024`. A guess is
   now labelled as one, and words grounded only in a low-confidence guess
   do not count toward derived confidence -- so the fallback speaks.
2. `download (4).zip` -> `archive.zip`: the archive's own central directory
   never reached the prompt. It does now, and `archive` is a generic stem.
3. `2026-05-04-maple-street...` -- a model wrote a dash after the date and
   policy let two spellings of one grammar coexist. Normalized to `_`.
"""

from __future__ import annotations

import unittest

from tools.ingest.naming.evidence import NamingEvidence, PerceivedEvidence
from tools.ingest.naming.policy import sanitize_stem, validate_proposal
from tools.ingest.naming.semantic import build_task_text, derive_confidence


def _photo(perceived: PerceivedEvidence | None) -> NamingEvidence:
    return NamingEvidence(
        source_class="downloads", original_filename="IMG_6821.jpg", extension=".jpg", size=191_241,
        mime="image/jpeg", media_kind="image", name_quality="poor", name_quality_reason="x",
        modified_date="2026-09-18",
        facts={"width": 4032, "height": 3024, "camera_make": "Apple", "camera_model": "iPhone 15 Pro", "capture_timestamp": "2026-05-04T13:22:11"},
        perceived=perceived,
    )


class LowConfidenceVisualIsNotGroundTests(unittest.TestCase):
    def test_a_heuristic_kind_guess_cannot_ground_a_name(self):
        perceived = PerceivedEvidence(
            visual_descriptions=("A flat, uniformly coloured image with no visible structure",),
            visual_image_kind="ui_capture", visual_confidence=0.3, sources=("visual",),
        )
        confidence, notes = derive_confidence("2026-05-04-ui-capture.jpg", evidence=_photo(perceived), extension=".jpg")
        self.assertLess(confidence, 0.6, notes)
        self.assertIn("generic-stem", notes)
        self.assertIn("no-evidence-overlap", notes)

    def test_a_confident_vision_description_still_grounds(self):
        perceived = PerceivedEvidence(
            visual_descriptions=("Kitchen island with quartz countertop and pendant lights",),
            visual_image_kind="ordinary_photo", visual_confidence=0.9, sources=("visual",),
        )
        confidence, notes = derive_confidence("2026-05-04_kitchen-island-quartz-countertop.jpg", evidence=_photo(perceived), extension=".jpg")
        self.assertGreaterEqual(confidence, 0.6, notes)
        self.assertIn("grounded-in-evidence", notes)

    def test_the_prompt_labels_a_low_confidence_guess_as_one(self):
        perceived = PerceivedEvidence(visual_descriptions=("flat colour",), visual_image_kind="ui_capture", visual_confidence=0.3, sources=("visual",))
        text = build_task_text(_photo(perceived))
        self.assertIn("visual_guess_low_confidence:", text)
        self.assertIn("low-confidence heuristic", text)
        self.assertNotIn("visual_description:", text)
        confident = PerceivedEvidence(visual_descriptions=("kitchen",), visual_confidence=0.9, sources=("visual",))
        self.assertIn("visual_description:", build_task_text(_photo(confident)))


class ArchiveContentsReachThePromptTests(unittest.TestCase):
    def _zip(self) -> NamingEvidence:
        return NamingEvidence(
            source_class="downloads", original_filename="download (4).zip", extension=".zip", size=262,
            mime="application/zip", media_kind="archive", name_quality="poor", name_quality_reason="x",
            modified_date="2026-09-18",
            facts={"top_level_entries": ["brand-kit/logo.svg", "brand-kit/palette.json"], "entry_count": 2, "dominant_extension": ".json"},
        )

    def test_top_level_entries_are_in_the_task_text(self):
        text = build_task_text(self._zip())
        self.assertIn("archive_top_level_entries: brand-kit/logo.svg, brand-kit/palette.json", text)
        self.assertIn("entry_count: 2", text)

    def test_archive_alone_is_a_generic_stem_and_entries_ground_a_real_name(self):
        low, notes = derive_confidence("2026-09-18_archive.zip", evidence=self._zip(), extension=".zip")
        self.assertLess(low, 0.6, notes)
        high, notes = derive_confidence("2026-09-18_brand-kit-logo-palette.zip", evidence=self._zip(), extension=".zip")
        self.assertGreaterEqual(high, 0.6, notes)


class DateSeparatorGrammarTests(unittest.TestCase):
    def test_a_dash_or_space_after_the_date_becomes_the_underscore(self):
        for spelled in ("2026-05-04-maple-street-scope", "2026-05-04 maple street scope", "2026-05-04.maple-street-scope", "2026-05-04_maple-street-scope"):
            self.assertEqual(sanitize_stem(spelled, max_length=120), "2026-05-04_maple-street-scope", spelled)

    def test_validate_proposal_applies_it_end_to_end(self):
        validated = validate_proposal("2026-05-04-maple-street-kitchen-renovation.pdf", original_filename="document (3).pdf", max_filename_length=120)
        self.assertEqual(validated.filename, "2026-05-04_maple-street-kitchen-renovation.pdf", validated)
        self.assertTrue(validated.changed, validated)

    def test_a_bare_date_is_not_a_prefix(self):
        self.assertEqual(sanitize_stem("2026-05-04", max_length=120), "2026-05-04")


if __name__ == "__main__":
    unittest.main()
