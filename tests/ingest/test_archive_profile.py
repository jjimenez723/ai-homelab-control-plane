from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tools.ingest.archive_profile import (
    EMPTY_ARCHIVE_PROFILE,
    archive_category_for_prefix,
    archive_destination_for_category,
    archive_filename_proposal,
    archive_prefix_for_category,
    archive_profile_for,
    normalize_archive_category,
    parse_archive_profile,
    resolve_archive_destination,
)
from tests.ingest.archive_fixtures import EXAMPLE_PROFILE_DOC, example_profile, write_example_profile
from tests.ingest.helpers import build_config


class ArchiveProfileTests(unittest.TestCase):
    def test_archive_name_uses_only_prefix_date_and_collision_suffix(self):
        self.assertEqual(
            archive_filename_proposal(
                "IMG_0001.CR3",
                category_prefix="wed",
                service_date="2026-10-25",
                collision_suffix="a1b2c3d4",
            ),
            "WED_2026-10-25_a1b2c3d4.CR3",
        )
        self.assertEqual(
            archive_filename_proposal(
                "IMG_0001.CR3",
                category_prefix="WED",
                service_date=None,
                collision_suffix="001",
            ),
            "IMG_0001.CR3",
        )

    def test_categories_normalize_through_names_and_aliases(self):
        profile = example_profile()
        self.assertEqual(profile.canonical_categories, ("Weddings", "Portraits", "Events", "Commercial"))
        self.assertEqual(normalize_archive_category("weddings", profile), "Weddings")
        self.assertEqual(normalize_archive_category("Wedding", profile), "Weddings")
        self.assertEqual(normalize_archive_category("head-shots", profile), None)
        self.assertEqual(normalize_archive_category("Headshots", profile), "Portraits")
        self.assertIsNone(normalize_archive_category("Unverified Event", profile))
        self.assertEqual(archive_category_for_prefix("wed", profile), "Weddings")
        self.assertIsNone(archive_category_for_prefix("UNKNOWN", profile))

    def test_prefixes_and_destinations_come_from_the_profile(self):
        profile = example_profile()
        self.assertEqual(archive_prefix_for_category("Wedding", profile), "WED")
        self.assertIsNone(archive_prefix_for_category("Portraits", profile))
        self.assertEqual(archive_destination_for_category("Events", profile), "Archive/2026/3. Events")
        self.assertEqual(archive_destination_for_category("Events", profile, year="2027"), "Archive/2027/3. Events")
        self.assertIsNone(archive_destination_for_category("Unknown", profile))

    def test_resolver_preserves_live_names_and_review_states(self):
        profile = example_profile()
        weddings = resolve_archive_destination("Weddings", "Pics", profile)
        self.assertTrue(weddings.ready_for_copy)
        self.assertEqual(weddings.media_folder_id, "pics-weddings")
        self.assertEqual(weddings.relative_path, "Archive/2026/1. Weddings/Pics")

        wedding_video = resolve_archive_destination("Weddings", "Vids", profile)
        self.assertEqual(wedding_video.media_folder_name, "Videos")
        self.assertEqual(wedding_video.relative_path, "Archive/2026/1. Weddings/Videos")

        portraits = resolve_archive_destination("Portraits", "Pics", profile)
        self.assertEqual(portraits.status, "review_prefix")
        self.assertEqual(portraits.media_folder_name, "Pictures")

        events = resolve_archive_destination("Events", "Pics", profile)
        self.assertEqual(events.status, "review_legacy_root")
        self.assertIsNone(events.media_folder_id)
        self.assertEqual(events.relative_path, "Archive/2026/3. Events/ROOT-REVIEW/Pics")

        spring = resolve_archive_destination("Commercial", "Pics", profile, nested_path=("Spring Launch",))
        self.assertEqual(spring.status, "review_nested_media")
        self.assertEqual(spring.nested_folder_ids, ("folder-commercial-spring",))
        self.assertEqual(spring.archive_folder_path, "Archive/2026/4. Commercial/Spring Launch")

    def test_resolver_rejects_unknown_inputs(self):
        profile = example_profile()
        with self.assertRaises(ValueError):
            resolve_archive_destination("Unknown", "Pics", profile)
        with self.assertRaises(ValueError):
            resolve_archive_destination("Weddings", "Docs", profile)
        with self.assertRaises(ValueError):
            resolve_archive_destination("Commercial", "Pics", profile, nested_path=("Missing",))

    def test_empty_profile_resolves_nothing(self):
        self.assertEqual(EMPTY_ARCHIVE_PROFILE.canonical_categories, ())
        self.assertIsNone(normalize_archive_category("Weddings", EMPTY_ARCHIVE_PROFILE))
        self.assertIsNone(archive_prefix_for_category("Weddings", EMPTY_ARCHIVE_PROFILE))
        self.assertIsNone(archive_destination_for_category("Weddings", EMPTY_ARCHIVE_PROFILE))

    def test_malformed_profiles_fail_loudly(self):
        with self.assertRaises(ValueError):
            parse_archive_profile({"categories": {"Weddings": {"folder_id": "x"}}})
        with self.assertRaises(ValueError):
            parse_archive_profile({**EXAMPLE_PROFILE_DOC, "aliases": {"ghost": "Nowhere"}})
        with self.assertRaises(ValueError):
            parse_archive_profile({"categories": ["Weddings"]})

    def test_profile_loads_only_when_archive_routing_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_example_profile(Path(tmp))
            config = build_config(desktop=Path(tmp))
            self.assertIs(archive_profile_for(config), EMPTY_ARCHIVE_PROFILE)
            self.assertIs(archive_profile_for(None), EMPTY_ARCHIVE_PROFILE)
            enabled = replace(config, archive=replace(config.archive, enabled=True, profile_path=path))
            self.assertEqual(archive_profile_for(enabled).canonical_categories, example_profile().canonical_categories)


if __name__ == "__main__":
    unittest.main()
