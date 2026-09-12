"""Deterministic enrichment of assets that reached Angel.

Every ingested asset should carry measured facts about itself -- dimensions,
capture time, codecs, page counts -- without a model having been consulted.
These record that it happens on the ordinary ingestion path, that it reaches
the sidecar as well as the catalog, and above all that it can never damage a
transfer that already succeeded.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.ingest.catalog import Catalog
from tools.ingest.cli import cmd_organize
from tools.ingest.enrichment import enrich_asset
from tools.ingest.t7 import resolve_t7

from tests.ingest.fi_helpers import fi_config, make_blob, make_camera_jpeg, make_png


class _OrganizeArgs:
    def __init__(self, target: str, *, apply: bool = True):
        self.target = target
        self.apply = apply
        self.dry_run = not apply
        self.config = None
        self.allow_dev_storage_root = True


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.downloads = self.root / "Downloads"
        self.t7 = self.root / "T7"
        for directory in (self.downloads, self.t7):
            directory.mkdir()
        self.config = fi_config(
            self.root, downloads=self.downloads, screenshots=None, screenshots_enabled=False, t7_root=self.t7
        )

    def resolved_t7(self):
        return resolve_t7(self.config, prober=lambda: [], for_apply=True, allow_storage_root_override=True)

    def organize(self) -> int:
        return cmd_organize(_OrganizeArgs("downloads"), prober=lambda: [], config=self.config)

    def assets(self):
        with Catalog(self.resolved_t7().catalog_path) as catalog:
            rows = catalog._conn.execute(
                "SELECT asset_id, final_filename FROM assets WHERE final_path IS NOT NULL"
            ).fetchall()
            return [(row["asset_id"], row["final_filename"], catalog.get_media_metadata(row["asset_id"], "asset")) for row in rows]

    def test_an_ingested_photo_carries_its_measured_facts(self):
        make_camera_jpeg(self.downloads / "vacation-sunset-tenerife.jpg", size=(4032, 3024))
        self.assertEqual(self.organize(), 0)

        (_, _, metadata), = self.assets()
        self.assertIsNotNone(metadata, "an ingested asset must carry deterministic facts")
        self.assertEqual(metadata["media_kind"], "image")
        self.assertEqual(metadata["facts"]["width"], 4032)
        self.assertEqual(metadata["facts"]["height"], 3024)
        self.assertEqual(metadata["facts"]["camera_model"], "iPhone 15 Pro")
        self.assertTrue(metadata["facts"]["capture_timestamp"].startswith("2026-05-04"))

    def test_the_facts_reach_the_sidecar_not_just_the_catalog(self):
        """The sidecar is the durable human-readable record; it has to agree."""
        make_png(self.downloads / "roof-survey-north-elevation.png", size=(1600, 900))
        self.assertEqual(self.organize(), 0)

        (asset_id, _, _), = self.assets()
        sidecar = json.loads((self.resolved_t7().sidecars_dir / f"{asset_id}.json").read_text())
        self.assertIsNotNone(sidecar["media_metadata"])
        self.assertEqual(sidecar["media_metadata"]["facts"]["width"], 1600)

    def test_enrichment_is_recorded_against_the_destination_not_the_source(self):
        """The source is unlinked by the transfer; only the copy on Angel is left."""
        source = make_camera_jpeg(self.downloads / "beach-day.jpg", size=(800, 600))
        self.assertEqual(self.organize(), 0)
        self.assertFalse(source.exists())
        (_, filename, metadata), = self.assets()
        self.assertEqual(filename, "beach-day.jpg")
        self.assertEqual(metadata["facts"]["width"], 800)

    def test_a_probe_failure_never_undoes_a_verified_transfer(self):
        document = make_blob(self.downloads / "scope-of-work-2026.pdf", b"%PDF-1.4 " + b"z" * 5000)
        expected = document.read_bytes()

        with mock.patch("tools.ingest.enrichment.probe_file", side_effect=RuntimeError("probe exploded")):
            self.assertEqual(self.organize(), 0)

        (asset_id, filename, metadata), = self.assets()
        self.assertIsNone(metadata, "a failed probe records nothing rather than something wrong")
        landed = self.resolved_t7().inbox_dir / "downloads" / filename
        self.assertEqual(landed.read_bytes(), expected, "the transfer itself must be untouched")
        self.assertFalse(document.exists())

    def test_enrich_asset_returns_false_rather_than_raising_on_a_broken_catalog(self):
        make_png(self.downloads / "diagram-of-the-thing.png")
        self.assertEqual(self.organize(), 0)
        (asset_id, filename, _), = self.assets()
        final_path = self.resolved_t7().inbox_dir / "downloads" / filename

        with Catalog(self.resolved_t7().catalog_path) as catalog:
            with mock.patch.object(catalog, "record_media_metadata", side_effect=RuntimeError("db gone")):
                self.assertFalse(
                    enrich_asset(catalog, asset_id=asset_id, final_path=final_path, sidecars_dir=self.resolved_t7().sidecars_dir)
                )

    def test_an_unreadable_file_type_is_not_an_error(self):
        make_blob(self.downloads / "firmware-blob-for-the-router.bin", b"\x00\x01\x02" * 5000)
        self.assertEqual(self.organize(), 0)
        (_, _, metadata), = self.assets()
        # Facts are thin, but recording "we looked and this is all there is" is
        # different from recording nothing.
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["media_kind"], "other")


if __name__ == "__main__":
    unittest.main()
