"""HEIC/HEIF -> JPEG compatibility derivative.

Covers required scenarios: #27 HEIC->JPEG quality 100 derivative, #28
original HEIC survives, #29 EXIF/capture timestamp preservation, #30
orientation preservation, #31 ICC profile behavior, #32 corrupted HEIC, #33
conversion-tool failure, #34 HEIC conversion rerun/idempotency, #35
derivative relationship in catalog.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tools.ingest.catalog import STATE_COMMITTED, Catalog
from tools.ingest.config import HeicToJpegConfig
from tools.ingest.derivatives import maybe_generate_heic_derivative
from tools.ingest.hashing import sha256_file
from tools.ingest.identity import derivative_asset_id_for
from tools.ingest.media.heic import HeicToJpegOptions, convert_heic_to_jpeg

from tests.ingest.helpers import build_config, make_resolved_t7
from tests.ingest.heic_fixtures import FAKE_ICC_PROFILE, make_corrupt_heic, make_heic


class ConverterUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

    def test_default_quality_is_100(self):
        self.assertEqual(HeicToJpegOptions().jpeg_quality, 100)

    def test_conversion_produces_jpeg_at_quality_100_and_original_survives(self):
        source = make_heic(self.root / "photo.heic")
        original_bytes = source.read_bytes()
        output = self.root / "photo.jpg"

        result = convert_heic_to_jpeg(source, output, HeicToJpegOptions(jpeg_quality=100))

        self.assertTrue(result.success)
        self.assertTrue(output.exists())
        with Image.open(output) as im:
            self.assertEqual(im.format, "JPEG")
        # The original HEIC is opened read-only and never modified.
        self.assertEqual(source.read_bytes(), original_bytes)

    def test_exif_capture_timestamp_is_preserved(self):
        source = make_heic(self.root / "photo.heic", capture_time="2021:03:04 09:15:30")
        output = self.root / "photo.jpg"
        result = convert_heic_to_jpeg(source, output, HeicToJpegOptions())

        self.assertTrue(result.success)
        with Image.open(output) as im:
            exif = im.getexif()
        self.assertEqual(exif.get(0x9003), "2021:03:04 09:15:30")

    def test_orientation_is_normalized(self):
        # orientation=6 on a landscape (64x48) source displays as portrait (48x64).
        source = make_heic(self.root / "photo.heic", size=(64, 48), orientation=6)
        output = self.root / "photo.jpg"
        result = convert_heic_to_jpeg(source, output, HeicToJpegOptions(normalize_orientation=True))

        self.assertTrue(result.success)
        with Image.open(output) as im:
            im.load()
            self.assertEqual(im.size, (48, 64))  # pixels are already physically rotated
            exif = im.getexif()
        # Orientation tag is normalized to 1 (upright) since pixels are already correct.
        self.assertIn(exif.get(0x0112), (1, None))

    def test_icc_profile_round_trips_when_present(self):
        source = make_heic(self.root / "photo.heic", icc_profile=FAKE_ICC_PROFILE)
        output = self.root / "photo.jpg"
        result = convert_heic_to_jpeg(source, output, HeicToJpegOptions(preserve_icc_profile=True))

        self.assertTrue(result.success)
        with Image.open(output) as im:
            self.assertEqual(im.info.get("icc_profile"), FAKE_ICC_PROFILE)

    def test_icc_profile_is_dropped_when_disabled(self):
        source = make_heic(self.root / "photo.heic", icc_profile=FAKE_ICC_PROFILE)
        output = self.root / "photo.jpg"
        result = convert_heic_to_jpeg(source, output, HeicToJpegOptions(preserve_icc_profile=False))

        self.assertTrue(result.success)
        with Image.open(output) as im:
            self.assertIsNone(im.info.get("icc_profile"))

    def test_missing_source_has_no_icc_profile_when_none_embedded(self):
        source = make_heic(self.root / "photo.heic", icc_profile=None)
        output = self.root / "photo.jpg"
        result = convert_heic_to_jpeg(source, output, HeicToJpegOptions())
        self.assertTrue(result.success)
        with Image.open(output) as im:
            self.assertIsNone(im.info.get("icc_profile"))

    def test_corrupted_heic_input_fails_cleanly_without_touching_original(self):
        source = make_corrupt_heic(self.root / "bad.heic")
        original_bytes = source.read_bytes()
        output = self.root / "bad.jpg"

        result = convert_heic_to_jpeg(source, output, HeicToJpegOptions())

        self.assertFalse(result.success)
        self.assertIsNotNone(result.error_message)
        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), original_bytes)

    def test_converter_failure_due_to_unwritable_destination_fails_cleanly(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits are not enforced for root")
        source = make_heic(self.root / "photo.heic")
        original_bytes = source.read_bytes()
        locked_dir = self.root / "locked-out"
        locked_dir.mkdir()
        output = locked_dir / "photo.jpg"
        os.chmod(locked_dir, 0o500)
        try:
            result = convert_heic_to_jpeg(source, output, HeicToJpegOptions())
        finally:
            os.chmod(locked_dir, 0o700)

        self.assertFalse(result.success)
        self.assertIsNotNone(result.error_message)
        self.assertEqual(source.read_bytes(), original_bytes)

    def test_rerun_overwrites_deterministically_without_numbered_suffixes(self):
        source = make_heic(self.root / "photo.heic")
        output = self.root / "photo.jpg"

        first = convert_heic_to_jpeg(source, output, HeicToJpegOptions())
        second = convert_heic_to_jpeg(source, output, HeicToJpegOptions())

        self.assertTrue(first.success and second.success)
        self.assertEqual(first.output_path, second.output_path)
        siblings = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(siblings, ["photo.heic", "photo.jpg"])  # no photo-2.jpg


class DerivativeOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.t7 = self.root / "T7"
        self.t7.mkdir()
        self.config = build_config(desktop=self.root / "Desktop", t7_root=self.t7)
        self.resolved_t7 = make_resolved_t7(self.config)
        self.catalog = Catalog(self.resolved_t7.catalog_path)
        self.addCleanup(self.catalog.close)

    def _seed_parent_asset(self, final_path: Path, asset_id: str = "parent-asset-1") -> Path:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        make_heic(final_path)
        self.catalog.upsert_blob(sha256_file(final_path), size=final_path.stat().st_size, mime="image/heic")
        self.catalog._conn.execute(  # noqa: SLF001 - direct seed is appropriate for this test
            """
            INSERT INTO assets(
                asset_id, sha256, source_volume_id, source_class, original_path, original_filename,
                size, transfer_state, final_path, final_filename, human_reviewed, created_at, updated_at
            ) VALUES (?, ?, 'vol', 'desktop', 'IMG_0001.heic', 'IMG_0001.heic', ?, ?, ?, ?, 0, 'now', 'now')
            """,
            (asset_id, sha256_file(final_path), final_path.stat().st_size, STATE_COMMITTED, str(final_path), final_path.name),
        )
        return final_path

    def test_derivative_relationship_recorded_in_catalog(self):
        final_path = self._seed_parent_asset(self.resolved_t7.inbox_dir / "desktop" / "IMG_0001.heic")

        result = maybe_generate_heic_derivative(
            self.catalog, self.resolved_t7,
            parent_asset_id="parent-asset-1", parent_final_path=final_path,
            source_volume_id="vol", source_class="desktop", config=HeicToJpegConfig(),
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.success)

        derivative_row = self.catalog.get_derivative("parent-asset-1", "heic_to_jpeg")
        self.assertIsNotNone(derivative_row)
        self.assertEqual(derivative_row["status"], "ok")
        self.assertEqual(derivative_row["converter"], "pillow-heif")
        self.assertEqual(derivative_row["quality"], 100)

        child_asset_id = derivative_asset_id_for("parent-asset-1", "heic_to_jpeg")
        child_asset = self.catalog.get_asset(child_asset_id)
        self.assertIsNotNone(child_asset)
        self.assertEqual(child_asset.row["derived_from"], "parent-asset-1")
        self.assertEqual(child_asset.row["transfer_state"], STATE_COMMITTED)
        self.assertEqual(Path(child_asset.row["final_path"]), final_path.with_suffix(".jpg"))
        self.assertEqual(child_asset.row["sha256"], sha256_file(final_path.with_suffix(".jpg")))

        # The parent's sidecar lists the derivative relationship too.
        import json

        sidecar = json.loads((self.resolved_t7.sidecars_dir / "parent-asset-1.json").read_text())
        self.assertEqual(len(sidecar["derivatives"]), 1)
        self.assertEqual(sidecar["derivatives"][0]["status"], "ok")

    def test_rerun_is_idempotent_and_does_not_reencode(self):
        final_path = self._seed_parent_asset(self.resolved_t7.inbox_dir / "desktop" / "IMG_0002.heic")

        first = maybe_generate_heic_derivative(
            self.catalog, self.resolved_t7,
            parent_asset_id="parent-asset-1", parent_final_path=final_path,
            source_volume_id="vol", source_class="desktop", config=HeicToJpegConfig(),
        )
        self.assertTrue(first.success)
        jpg_path = final_path.with_suffix(".jpg")
        first_mtime = jpg_path.stat().st_mtime_ns

        second = maybe_generate_heic_derivative(
            self.catalog, self.resolved_t7,
            parent_asset_id="parent-asset-1", parent_final_path=final_path,
            source_volume_id="vol", source_class="desktop", config=HeicToJpegConfig(),
        )
        self.assertIsNone(second)  # no-op: already converted
        self.assertEqual(jpg_path.stat().st_mtime_ns, first_mtime)

        siblings = sorted(p.name for p in final_path.parent.iterdir())
        self.assertEqual(siblings, ["IMG_0002.heic", "IMG_0002.jpg"])

    def test_conversion_failure_does_not_touch_or_fail_the_verified_original(self):
        final_path = self.resolved_t7.inbox_dir / "desktop" / "corrupt.heic"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        make_corrupt_heic(final_path)
        self._seed_parent_asset_row_only(final_path)
        original_bytes = final_path.read_bytes()

        result = maybe_generate_heic_derivative(
            self.catalog, self.resolved_t7,
            parent_asset_id="parent-asset-1", parent_final_path=final_path,
            source_volume_id="vol", source_class="desktop", config=HeicToJpegConfig(),
        )

        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertEqual(final_path.read_bytes(), original_bytes)  # original untouched

        parent = self.catalog.get_asset("parent-asset-1")
        self.assertEqual(parent.row["transfer_state"], STATE_COMMITTED)  # ingestion outcome unaffected

        derivative_row = self.catalog.get_derivative("parent-asset-1", "heic_to_jpeg")
        self.assertEqual(derivative_row["status"], "failed")
        self.assertIsNotNone(derivative_row["error_message"])

    def _seed_parent_asset_row_only(self, final_path: Path, asset_id: str = "parent-asset-1") -> None:
        self.catalog._conn.execute(  # noqa: SLF001
            """
            INSERT INTO assets(
                asset_id, source_volume_id, source_class, original_path, original_filename,
                size, transfer_state, final_path, final_filename, human_reviewed, created_at, updated_at
            ) VALUES (?, 'vol', 'desktop', 'corrupt.heic', 'corrupt.heic', ?, ?, ?, ?, 0, 'now', 'now')
            """,
            (asset_id, final_path.stat().st_size, STATE_COMMITTED, str(final_path), final_path.name),
        )

    def test_disabled_config_skips_conversion_entirely(self):
        final_path = self._seed_parent_asset(self.resolved_t7.inbox_dir / "desktop" / "IMG_0003.heic")
        result = maybe_generate_heic_derivative(
            self.catalog, self.resolved_t7,
            parent_asset_id="parent-asset-1", parent_final_path=final_path,
            source_volume_id="vol", source_class="desktop", config=HeicToJpegConfig(enabled=False),
        )
        self.assertIsNone(result)
        self.assertFalse(final_path.with_suffix(".jpg").exists())


if __name__ == "__main__":
    unittest.main()
