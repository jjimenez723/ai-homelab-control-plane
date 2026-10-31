from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from dataclasses import replace

from tests.ingest.archive_fixtures import write_example_profile
from tests.ingest.helpers import build_config
from tools.ingest.plan import attach_preflight_analysis
from tools.ingest.modeling import PreflightAnalysis
from tools.ingest.plan import PlanFile, PreviewPlan


class PreviewAnalysisPlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profile_path = write_example_profile(Path(self._tmp.name))

    def _archive_config(self):
        base = build_config(desktop=Path("/tmp/archive-test-desktop"))
        return replace(
            base,
            archive=replace(
                base.archive,
                enabled=True,
                profile_path=self.profile_path,
                media_only_sd=True,
                service_date="2026-10-25",
            ),
        )

    def _single_file_plan(self, name: str) -> PreviewPlan:
        return PreviewPlan(
            fingerprint="stable",
            files=(
                PlanFile(
                    source_path=f"/Volumes/CARD/DCIM/{name}",
                    relative_path=f"DCIM/{name}",
                    source_class="sd",
                    source_volume="CARD",
                    current_filename=name,
                    proposed_filename=name,
                    landing_destination=f"/tmp/archive/{name}",
                    status="ready",
                    warning=None,
                    size=100,
                    mtime_ns=123,
                ),
            ),
            total_bytes=100,
            warnings=(),
            destination_root="/tmp/archive",
        )

    def _analyze(self, plan: PreviewPlan, payload: dict, config) -> PlanFile:
        analyzed = attach_preflight_analysis(
            plan,
            {
                plan.files[0].source_path: PreflightAnalysis(
                    source_path=plan.files[0].source_path,
                    payload={"people": [], "animals": [], **payload},
                )
            },
            profiles=[],
            config=config,
        )
        return analyzed.files[0]

    def test_analysis_attaches_filename_people_animal_and_review_destination(self):
        plan = PreviewPlan(
            fingerprint="stable",
            files=(
                PlanFile(
                    source_path="/Volumes/CARD/DCIM/IMG_0001.JPG",
                    relative_path="DCIM/IMG_0001.JPG",
                    source_class="sd",
                    source_volume="CARD",
                    current_filename="IMG_0001.JPG",
                    proposed_filename="IMG_0001.JPG",
                    landing_destination="/Volumes/T7/00-inbox/organized/sd/DCIM/IMG_0001.JPG",
                    status="ready",
                    warning=None,
                    size=100,
                    mtime_ns=123,
                ),
            ),
            total_bytes=100,
            warnings=(),
            destination_root="/Volumes/T7/00-inbox/organized",
        )
        analyzed = attach_preflight_analysis(
            plan,
            {
                "/Volumes/CARD/DCIM/IMG_0001.JPG": PreflightAnalysis(
                    source_path="/Volumes/CARD/DCIM/IMG_0001.JPG",
                    payload={
                        "summary": "Alice with a dog",
                        "tags": ["dog", "portrait"],
                        "people": [
                            {
                                "person_profile_id": "person-alice",
                                "confidence": 0.92,
                                "evidence": "clear face",
                            }
                        ],
                        "animals": [
                            {
                                "animal_type": "dog",
                                "count": 1,
                                "confidence": 0.98,
                                "evidence": "visible dog",
                            }
                        ],
                        "filename_proposal": {
                            "subject": "alice-dog",
                            "variant": "portrait",
                            "confidence": 0.8,
                        },
                    },
                )
            },
            profiles=[
                {
                    "profile_id": "person-alice",
                    "display_name": "Alice",
                    "scope": "global",
                }
            ],
        )

        item = analyzed.files[0]
        self.assertEqual(analyzed.fingerprint, "stable")
        self.assertEqual(item.proposed_filename, "alice-dog__portrait.JPG")
        self.assertEqual(item.analysis["profile_matches"][0]["display_name"], "Alice")
        self.assertEqual(item.analysis["animals"][0]["animal_type"], "dog")
        self.assertEqual(item.analysis["destination_relative_path"], "people-and-animals/alice-dog__portrait.JPG")
        self.assertIn("people-and-animals", item.landing_destination)

    def test_duplicate_model_names_get_deterministic_unique_destinations(self):
        files = tuple(
            PlanFile(
                source_path=f"/Volumes/CARD/DCIM/IMG_000{i}.JPG",
                relative_path=f"DCIM/IMG_000{i}.JPG",
                source_class="sd",
                source_volume="CARD",
                current_filename=f"IMG_000{i}.JPG",
                proposed_filename=f"IMG_000{i}.JPG",
                landing_destination=f"/Volumes/T7/00-inbox/organized/sd/DCIM/IMG_000{i}.JPG",
                status="ready",
                warning=None,
                size=100,
                mtime_ns=123 + i,
            )
            for i in (1, 2)
        )
        plan = PreviewPlan(
            fingerprint="stable",
            files=files,
            total_bytes=200,
            warnings=(),
            destination_root="/Volumes/T7/00-inbox/organized",
        )
        results = {
            item.source_path: PreflightAnalysis(
                source_path=item.source_path,
                payload={
                    "summary": "Same scene",
                    "people": [],
                    "animals": [],
                    "filename_proposal": {
                        "subject": "same-scene",
                        "variant": "wide",
                    },
                },
            )
            for item in files
        }

        analyzed = attach_preflight_analysis(plan, results, profiles=[])

        destinations = [item.analysis["destination_relative_path"] for item in analyzed.files]
        filenames = [item.proposed_filename for item in analyzed.files]
        self.assertEqual(len(set(destination.casefold() for destination in destinations)), 2)
        self.assertEqual(filenames[0], "same-scene__wide.JPG")
        self.assertTrue(filenames[1].startswith("same-scene__wide__"))
        self.assertIn("Duplicate model names", analyzed.files[1].warning)
        self.assertIn("collision_resolution", analyzed.files[1].analysis)

    def test_archive_resolves_known_category_prefix_and_destination(self):
        item = self._analyze(
            self._single_file_plan("IMG_0001.JPG"),
            {"summary": "couple at the altar", "archive_category": "Weddings", "archive_confidence": 0.9},
            self._archive_config(),
        )
        self.assertEqual(item.proposed_filename, "WED_2026-10-25_c2d91d0c.JPG")
        self.assertEqual(item.analysis["archive_category"], "Weddings")
        self.assertEqual(item.analysis["archive_prefix"], "WED")
        self.assertEqual(item.analysis["archive_category_status"], "destination_review")
        self.assertEqual(item.analysis["archive_destination"], "Archive/2026/1. Weddings")

    def test_archive_resolves_aliases_to_the_canonical_prefix(self):
        item = self._analyze(
            self._single_file_plan("IMG_0002.JPG"),
            {"summary": "first dance", "archive_category": "wedding"},
            self._archive_config(),
        )
        self.assertEqual(item.analysis["archive_category"], "Weddings")
        self.assertEqual(item.analysis["archive_prefix"], "WED")
        self.assertEqual(item.analysis["archive_category_status"], "destination_review")
        self.assertEqual(item.proposed_filename, "WED_2026-10-25_b0c1ecf5.JPG")

    def test_archive_category_without_prefix_stays_review_gated(self):
        item = self._analyze(
            self._single_file_plan("IMG_0003.JPG"),
            {"summary": "studio headshot", "archive_category": "Portraits"},
            self._archive_config(),
        )
        self.assertEqual(item.analysis["archive_category"], "Portraits")
        self.assertIsNone(item.analysis["archive_prefix"])
        self.assertEqual(item.analysis["archive_destination"], "Archive/2026/2. Portraits")
        self.assertEqual(item.analysis["archive_category_status"], "prefix_unresolved")
        self.assertEqual(item.proposed_filename, "IMG_0003.JPG")
        self.assertIn("unresolved", item.warning)

    def test_archive_disabled_ignores_model_categories(self):
        item = self._analyze(
            self._single_file_plan("IMG_0004.JPG"),
            {"summary": "couple at the altar", "archive_category": "Weddings"},
            build_config(desktop=Path("/tmp/archive-test-desktop")),
        )
        self.assertIsNone(item.analysis["archive_category"])
        self.assertIsNone(item.analysis["archive_destination"])
        self.assertNotIn("archive_category_status", item.analysis)


if __name__ == "__main__":
    unittest.main()
