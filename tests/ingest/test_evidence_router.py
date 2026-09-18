"""The router decides *whether to look*, and only ever from measured facts.

`route_for` is a pure function: no I/O, no config file reads, no logging --
so a hostile or malformed file cannot turn "should we perceive this" into an
attack surface of its own (see the AST-enforced purity guard in
`test_mutation_authority.py`, which lists `router.py` in `PURE_MODULES`). This
suite is the behavioural half of that claim: every rule id in the spec (r0
through r18) fires under exactly the documented conditions, `refine_route`
and `refine_route_for_native_text` only ever *add* a stage and never remove
one or override a disabled config, and the same inputs always produce an
equal `EvidenceRoute` (the router's only claim to purity that actually
matters to a caller).
"""

from __future__ import annotations

import unittest

from tools.ingest.config import EvidenceConfig, OCRConfig, TranscriptionConfig, VisionConfig
from tools.ingest.evidence.router import (
    MIN_ADEQUATE_NATIVE_CHARS,
    refine_route,
    refine_route_for_native_text,
    route_for,
)
from tools.ingest.evidence.types import (
    IMAGE_KIND_ORDINARY_PHOTO,
    IMAGE_KIND_SCREENSHOT,
    VisualEvidence,
)
from tools.ingest.media.probe import (
    MEDIA_ARCHIVE,
    MEDIA_AUDIO,
    MEDIA_DOCUMENT,
    MEDIA_IMAGE,
    MEDIA_INSTALLER,
    MEDIA_OTHER,
    MEDIA_TEXT,
    MEDIA_VIDEO,
)


def _route(**overrides):
    args = dict(
        media_kind=MEDIA_TEXT, mime=None, extension=".txt", size=100, facts={},
        screenshot_by_name=False, config=EvidenceConfig(),
    )
    args.update(overrides)
    return route_for(**args)


def _visual(image_kind: str) -> VisualEvidence:
    return VisualEvidence(
        image_kind=image_kind, description="", entities=(), context="", confidence=0.6,
        backend="fake", model="fake", model_version="1",
    )


class RuleFiringTests(unittest.TestCase):
    def test_r0_evidence_disabled_beats_every_other_rule(self):
        route = _route(config=EvidenceConfig(enabled=False), media_kind=MEDIA_IMAGE)
        self.assertEqual(route.rules, ("r0-evidence-disabled",))
        self.assertFalse(route.native_text or route.ocr or route.vision)

    def test_r1_too_large_beats_everything_after_it(self):
        route = _route(config=EvidenceConfig(max_bytes=100), size=101, media_kind=MEDIA_TEXT)
        self.assertEqual(route.rules, ("r1-too-large",))
        self.assertFalse(route.native_text or route.ocr or route.vision)
        self.assertIn("100", route.reason)

    def test_r2_text_is_native_only(self):
        route = _route(media_kind=MEDIA_TEXT)
        self.assertEqual(route.rules, ("r2-text",))
        self.assertTrue(route.native_text)
        self.assertFalse(route.ocr or route.vision)

    def test_r3_pdf_with_embedded_text_skips_ocr(self):
        route = _route(media_kind=MEDIA_DOCUMENT, extension=".pdf", facts={"text_available": True})
        self.assertEqual(route.rules, ("r3-pdf-embedded-text",))
        self.assertTrue(route.native_text)
        self.assertFalse(route.ocr)
        self.assertFalse(route.vision)

    def test_r4_pdf_without_embedded_text_gets_native_attempt_plus_ocr(self):
        for facts in ({"text_available": False}, {}):
            with self.subTest(facts=facts):
                route = _route(media_kind=MEDIA_DOCUMENT, extension=".pdf", facts=facts)
                self.assertEqual(route.rules, ("r4-pdf-no-embedded-text",))
                self.assertTrue(route.native_text)
                self.assertTrue(route.ocr)
                self.assertFalse(route.vision)

    def test_r5_office_document_is_native_only(self):
        # .pptx/.xlsx are claimed by r17 (below) before this branch is ever
        # reached -- see RuleFiringTests.test_r17_office_native_text_...
        for extension in (".docx", ".rtf", ".epub"):
            with self.subTest(extension=extension):
                route = _route(media_kind=MEDIA_DOCUMENT, extension=extension, facts={})
                self.assertEqual(route.rules, ("r5-office-document",))
                self.assertTrue(route.native_text)
                self.assertFalse(route.ocr or route.vision)

    def test_r17_office_native_text_covers_spreadsheets_presentations_and_csv(self):
        """`.xlsx`/`.xlsm`/`.pptx`/`.csv`/`.tsv`: `tools/ingest/evidence/office.py`'s
        native-text extractors (an extension of D-029, not a new decision --
        see docs/17). Fires by extension, ahead of the `media_kind` dispatch,
        so it applies the same way regardless of exactly how the extension
        happened to be bucketed."""
        for extension, media_kind in (
            (".xlsx", MEDIA_DOCUMENT),
            (".xlsm", MEDIA_DOCUMENT),
            (".pptx", MEDIA_DOCUMENT),
            (".csv", MEDIA_TEXT),
            (".tsv", MEDIA_TEXT),
        ):
            with self.subTest(extension=extension):
                route = _route(media_kind=media_kind, extension=extension, facts={})
                self.assertEqual(route.rules, ("r18-office-native-text",))
                self.assertTrue(route.native_text)
                self.assertFalse(route.ocr or route.vision)

    def test_r17_fires_by_extension_even_if_media_kind_classification_disagrees(self):
        """Belt and suspenders: this rule does not depend on `media_kind`
        having bucketed the extension correctly, unlike every other rule."""
        route = _route(media_kind=MEDIA_OTHER, extension=".xlsm", facts={})
        self.assertEqual(route.rules, ("r18-office-native-text",))
        self.assertTrue(route.native_text)

    def test_r17_still_respects_evidence_disabled_and_too_large(self):
        disabled = _route(config=EvidenceConfig(enabled=False), media_kind=MEDIA_DOCUMENT, extension=".xlsx")
        self.assertEqual(disabled.rules, ("r0-evidence-disabled",))

        too_large = _route(config=EvidenceConfig(max_bytes=100), size=101, media_kind=MEDIA_DOCUMENT, extension=".xlsx")
        self.assertEqual(too_large.rules, ("r1-too-large",))

    def test_r6_screenshot_by_filename_gets_ocr_and_vision(self):
        route = _route(media_kind=MEDIA_IMAGE, screenshot_by_name=True, extension=".png")
        self.assertEqual(route.rules, ("r6-screenshot",))
        self.assertTrue(route.ocr and route.vision)
        self.assertFalse(route.native_text)

    def test_r6_screenshot_by_probe_facts_also_fires(self):
        route = _route(media_kind=MEDIA_IMAGE, facts={"is_probably_screenshot": True}, extension=".png")
        self.assertEqual(route.rules, ("r6-screenshot",))
        self.assertTrue(route.ocr and route.vision)

    def test_r7_camera_photo_is_vision_only(self):
        route = _route(media_kind=MEDIA_IMAGE, facts={"camera_make": "Apple"}, extension=".jpg")
        self.assertEqual(route.rules, ("r7-camera-photo",))
        self.assertTrue(route.vision)
        self.assertFalse(route.ocr or route.native_text)

    def test_r8_image_of_unknown_origin_is_vision_only(self):
        route = _route(media_kind=MEDIA_IMAGE, facts={}, extension=".png")
        self.assertEqual(route.rules, ("r8-image-other",))
        self.assertTrue(route.vision)
        self.assertFalse(route.ocr or route.native_text)

    def test_r9_opaque_media_kinds_get_nothing(self):
        # MEDIA_AUDIO left this list with `r16-audio-transcript` (D-058): a
        # recording's speech is perceivable evidence, so "opaque" was a
        # statement about the tooling that shipped, not about the bytes.
        for media_kind in (MEDIA_ARCHIVE, MEDIA_INSTALLER, MEDIA_OTHER):
            with self.subTest(media_kind=media_kind):
                route = _route(media_kind=media_kind, extension=".bin")
                self.assertEqual(route.rules, ("r9-opaque",))
                self.assertFalse(route.native_text or route.ocr or route.vision)
                self.assertFalse(route.transcript)
                self.assertIn(media_kind, route.reason)

    def test_r9_covers_an_unrecognised_media_kind_too(self):
        route = _route(media_kind="something-new", extension=".xyz")
        self.assertEqual(route.rules, ("r9-opaque",))
        self.assertIn("unrecognised media kind", route.reason)

    def test_r14_video_gets_vision_and_ocr_on_sampled_frames(self):
        route = _route(media_kind=MEDIA_VIDEO, extension=".mp4")
        self.assertEqual(route.rules, ("r14-video-frame",))
        self.assertTrue(route.vision)
        self.assertTrue(route.ocr)
        self.assertFalse(route.native_text)

    def test_r15_video_with_an_audio_stream_adds_transcription(self):
        route = _route(
            media_kind=MEDIA_VIDEO, extension=".mp4", facts={"audio_present": True},
            config=EvidenceConfig(transcription=TranscriptionConfig(enabled=True)),
        )
        self.assertEqual(route.rules, ("r14-video-frame", "r15-video-transcript"))
        self.assertTrue(route.transcript)
        self.assertTrue(route.vision and route.ocr)

    def test_r15_does_not_fire_for_a_silent_video(self):
        route = _route(
            media_kind=MEDIA_VIDEO, extension=".mp4", facts={"audio_present": False},
            config=EvidenceConfig(transcription=TranscriptionConfig(enabled=True)),
        )
        self.assertEqual(route.rules, ("r14-video-frame",))
        self.assertFalse(route.transcript)

    def test_r15_does_not_fire_when_the_probe_could_not_say(self):
        """An absent `audio_present` fact is "unknown", and must not read as "yes".

        Spending an ffmpeg run plus a speech engine on a guess is exactly the
        kind of blind expensive inference this router exists to prevent.
        """
        route = _route(
            media_kind=MEDIA_VIDEO, extension=".mp4", facts={},
            config=EvidenceConfig(transcription=TranscriptionConfig(enabled=True)),
        )
        self.assertEqual(route.rules, ("r14-video-frame",))
        self.assertFalse(route.transcript)

    def test_r15_does_not_fire_when_transcription_is_disabled(self):
        route = _route(
            media_kind=MEDIA_VIDEO, extension=".mp4", facts={"audio_present": True},
            config=EvidenceConfig(),  # transcription disabled by default
        )
        self.assertEqual(route.rules, ("r14-video-frame",))
        self.assertFalse(route.transcript)

    def test_r16_audio_routes_to_transcription_only(self):
        route = _route(
            media_kind=MEDIA_AUDIO, extension=".m4a",
            config=EvidenceConfig(transcription=TranscriptionConfig(enabled=True)),
        )
        self.assertEqual(route.rules, ("r16-audio-transcript",))
        self.assertTrue(route.transcript)
        self.assertFalse(route.native_text or route.ocr or route.vision)

    def test_r17_transcription_disabled_is_visible_rather_than_silent(self):
        """A disabled gate must not be indistinguishable from "opaque kind"."""
        route = _route(media_kind=MEDIA_AUDIO, extension=".m4a", config=EvidenceConfig())
        self.assertEqual(route.rules, ("r16-audio-transcript", "r17-transcription-disabled"))
        self.assertFalse(route.transcript)

    def test_r14_video_respects_ocr_disabled_independently_of_vision(self):
        route = _route(
            media_kind=MEDIA_VIDEO, extension=".mp4", config=EvidenceConfig(ocr=OCRConfig(enabled=False)),
        )
        self.assertEqual(route.rules, ("r14-video-frame", "r10-ocr-disabled"))
        self.assertFalse(route.ocr)
        self.assertTrue(route.vision)

    def test_r14_video_respects_vision_disabled(self):
        route = _route(
            media_kind=MEDIA_VIDEO, extension=".mp4", config=EvidenceConfig(vision=VisionConfig(enabled=False)),
        )
        self.assertEqual(route.rules, ("r14-video-frame", "r11-vision-disabled"))
        self.assertFalse(route.vision)

    def test_r10_ocr_disabled_clears_ocr_but_not_vision(self):
        route = _route(
            media_kind=MEDIA_IMAGE, screenshot_by_name=True, extension=".png",
            config=EvidenceConfig(ocr=OCRConfig(enabled=False)),
        )
        self.assertEqual(route.rules, ("r6-screenshot", "r10-ocr-disabled"))
        self.assertFalse(route.ocr)
        self.assertTrue(route.vision)

    def test_r11_vision_disabled_clears_vision_but_not_ocr(self):
        route = _route(
            media_kind=MEDIA_IMAGE, screenshot_by_name=True, extension=".png",
            config=EvidenceConfig(vision=VisionConfig(enabled=False)),
        )
        self.assertEqual(route.rules, ("r6-screenshot", "r11-vision-disabled"))
        self.assertTrue(route.ocr)
        self.assertFalse(route.vision)

    def test_r10_and_r11_can_both_fire(self):
        route = _route(
            media_kind=MEDIA_IMAGE, screenshot_by_name=True, extension=".png",
            config=EvidenceConfig(ocr=OCRConfig(enabled=False), vision=VisionConfig(enabled=False)),
        )
        self.assertEqual(route.rules, ("r6-screenshot", "r10-ocr-disabled", "r11-vision-disabled"))
        self.assertFalse(route.ocr or route.vision)


class PurityTests(unittest.TestCase):
    def test_identical_inputs_always_produce_an_equal_route(self):
        kwargs = dict(
            media_kind=MEDIA_IMAGE, mime="image/png", extension=".png", size=1234,
            facts={"is_probably_screenshot": True}, screenshot_by_name=False, config=EvidenceConfig(),
        )
        self.assertEqual(route_for(**kwargs), route_for(**kwargs))


class RefineRouteTests(unittest.TestCase):
    """A vision result may only ever *add* OCR, never remove anything, and
    never override `config.ocr.enabled`."""

    def test_a_text_bearing_vision_result_turns_ocr_on(self):
        route = _route(media_kind=MEDIA_IMAGE, facts={}, extension=".png")  # r8, vision only
        refined = refine_route(route, _visual(IMAGE_KIND_SCREENSHOT), config=EvidenceConfig())
        self.assertTrue(refined.ocr)
        self.assertEqual(refined.rules[-1], "r12-vision-says-text-bearing")

    def test_a_non_text_bearing_vision_result_changes_nothing(self):
        route = _route(media_kind=MEDIA_IMAGE, facts={}, extension=".png")
        refined = refine_route(route, _visual(IMAGE_KIND_ORDINARY_PHOTO), config=EvidenceConfig())
        self.assertEqual(refined, route)

    def test_no_visual_result_changes_nothing(self):
        route = _route(media_kind=MEDIA_IMAGE, facts={}, extension=".png")
        self.assertEqual(refine_route(route, None, config=EvidenceConfig()), route)

    def test_ocr_already_on_is_left_alone_no_duplicate_rule(self):
        route = _route(media_kind=MEDIA_IMAGE, screenshot_by_name=True, extension=".png")  # r6: ocr already True
        refined = refine_route(route, _visual(IMAGE_KIND_SCREENSHOT), config=EvidenceConfig())
        self.assertEqual(refined, route)
        self.assertNotIn("r12-vision-says-text-bearing", refined.rules)

    def test_never_turns_on_ocr_when_config_disables_it(self):
        route = _route(media_kind=MEDIA_IMAGE, facts={}, extension=".png")
        refined = refine_route(route, _visual(IMAGE_KIND_SCREENSHOT), config=EvidenceConfig(ocr=OCRConfig(enabled=False)))
        self.assertFalse(refined.ocr)
        self.assertEqual(refined, route)

    def test_never_clears_vision_even_though_it_only_ever_adds(self):
        route = _route(media_kind=MEDIA_IMAGE, screenshot_by_name=True, extension=".png")
        refined = refine_route(route, _visual(IMAGE_KIND_ORDINARY_PHOTO), config=EvidenceConfig())
        self.assertTrue(refined.vision)


class RefineRouteForNativeTextTests(unittest.TestCase):
    """§ r13: a document's own text layer can turn out to be a watermark."""

    def test_adequate_native_text_does_not_escalate(self):
        route = _route(media_kind=MEDIA_DOCUMENT, extension=".pdf", facts={"text_available": True})
        refined = refine_route_for_native_text(
            route, char_count=MIN_ADEQUATE_NATIVE_CHARS, extracted=True, config=EvidenceConfig()
        )
        self.assertEqual(refined, route)
        self.assertFalse(refined.ocr)

    def test_thin_native_text_escalates_to_ocr(self):
        route = _route(media_kind=MEDIA_DOCUMENT, extension=".pdf", facts={"text_available": True})
        refined = refine_route_for_native_text(
            route, char_count=MIN_ADEQUATE_NATIVE_CHARS - 1, extracted=True, config=EvidenceConfig()
        )
        self.assertTrue(refined.ocr)
        self.assertEqual(refined.rules[-1], "r13-native-text-inadequate")

    def test_a_failed_extraction_escalates_even_with_char_count_zero(self):
        route = _route(media_kind=MEDIA_DOCUMENT, extension=".pdf", facts={"text_available": True})
        refined = refine_route_for_native_text(route, char_count=0, extracted=False, config=EvidenceConfig())
        self.assertTrue(refined.ocr)
        self.assertEqual(refined.rules[-1], "r13-native-text-inadequate")

    def test_never_escalates_when_ocr_already_on(self):
        route = _route(media_kind=MEDIA_DOCUMENT, extension=".pdf", facts={"text_available": False})  # r4: ocr already True
        refined = refine_route_for_native_text(route, char_count=0, extracted=True, config=EvidenceConfig())
        self.assertEqual(refined, route)

    def test_never_escalates_when_ocr_is_disabled(self):
        route = _route(media_kind=MEDIA_DOCUMENT, extension=".pdf", facts={"text_available": True})
        refined = refine_route_for_native_text(
            route, char_count=0, extracted=True, config=EvidenceConfig(ocr=OCRConfig(enabled=False))
        )
        self.assertFalse(refined.ocr)
        self.assertEqual(refined, route)

    def test_never_escalates_when_native_text_was_never_attempted(self):
        route = _route(media_kind=MEDIA_IMAGE, facts={}, extension=".png")  # r8: native_text False
        refined = refine_route_for_native_text(route, char_count=0, extracted=False, config=EvidenceConfig())
        self.assertEqual(refined, route)


if __name__ == "__main__":
    unittest.main()
