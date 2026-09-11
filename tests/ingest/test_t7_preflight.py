"""T7 (and other destination-volume) preflight checks.

Covers required scenarios: #2 T7 missing, #3 wrong T7 UUID, #4 T7
read-only/unavailable, #5 unknown SD card, #6 registered SD card.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tools.ingest.sources import detect_sd_sources
from tools.ingest.t7 import resolve_t7
from tools.ingest.volumes import VolumeError

from tests.ingest.helpers import (
    SD_TEST_UUID,
    T7_TEST_UUID,
    UNKNOWN_SD_UUID,
    build_config,
    fake_prober,
    make_volume,
)


class T7PreflightTests(unittest.TestCase):
    def test_t7_missing_raises_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            config = build_config(desktop=desktop, t7_volume_uuid=T7_TEST_UUID)
            prober = fake_prober([])  # nothing mounted at all

            with self.assertRaises(VolumeError):
                resolve_t7(config, prober, for_apply=True)

    def test_wrong_t7_uuid_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            t7_mount = Path(tmp) / "SomeOtherDrive"
            t7_mount.mkdir()
            config = build_config(desktop=desktop, t7_volume_uuid=T7_TEST_UUID)
            wrong_volume = make_volume("aaaaaaaa-0000-0000-0000-000000000000", t7_mount)
            prober = fake_prober([wrong_volume])

            with self.assertRaises(VolumeError):
                resolve_t7(config, prober, for_apply=True)

    def test_t7_read_only_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            t7_mount = Path(tmp) / "T7"
            t7_mount.mkdir()
            config = build_config(desktop=desktop, t7_volume_uuid=T7_TEST_UUID)
            locked_volume = make_volume(T7_TEST_UUID, t7_mount, writable=False)
            prober = fake_prober([locked_volume])

            with self.assertRaises(VolumeError):
                resolve_t7(config, prober, for_apply=True)

    def test_t7_directory_not_writable_raises(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits are not enforced for root")
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            t7_mount = Path(tmp) / "T7"
            t7_mount.mkdir()
            os.chmod(t7_mount, 0o500)
            try:
                config = build_config(desktop=desktop, t7_root=t7_mount)
                with self.assertRaises(VolumeError):
                    resolve_t7(config, prober=lambda: [], for_apply=False)
            finally:
                os.chmod(t7_mount, 0o700)

    def test_correct_t7_uuid_resolves_successfully(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            t7_mount = Path(tmp) / "T7"
            t7_mount.mkdir()
            config = build_config(desktop=desktop, t7_volume_uuid=T7_TEST_UUID)
            good_volume = make_volume(T7_TEST_UUID, t7_mount)
            prober = fake_prober([good_volume])

            resolved = resolve_t7(config, prober, for_apply=True)
            self.assertEqual(resolved.root, t7_mount)
            self.assertTrue(resolved.system_dir.exists())
            self.assertTrue(resolved.inbox_dir.exists())


class SDDetectionTests(unittest.TestCase):
    def test_unknown_sd_card_is_never_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            sd_mount = Path(tmp) / "UNKNOWN_SD"
            sd_mount.mkdir()
            config = build_config(desktop=desktop, sd_volume_uuids=frozenset({SD_TEST_UUID}))
            unknown_card = make_volume(UNKNOWN_SD_UUID, sd_mount, removable=True, label="UNKNOWN_SD")
            prober = fake_prober([unknown_card])

            eligible = detect_sd_sources(prober, config)
            self.assertEqual(eligible, [])

    def test_registered_sd_card_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            sd_mount = Path(tmp) / "MY_SD"
            sd_mount.mkdir()
            config = build_config(desktop=desktop, sd_volume_uuids=frozenset({SD_TEST_UUID}))
            registered_card = make_volume(SD_TEST_UUID, sd_mount, removable=True, label="MY_SD")
            prober = fake_prober([registered_card])

            eligible = detect_sd_sources(prober, config)
            self.assertEqual(len(eligible), 1)
            self.assertEqual(eligible[0].volume.volume_uuid, SD_TEST_UUID)
            self.assertEqual(eligible[0].root, sd_mount)

    def test_non_removable_volume_with_registered_uuid_is_not_treated_as_sd(self):
        """A registered UUID only matters for a genuinely removable volume;
        this guards against a future internal-disk collision being silently
        treated as an SD source."""
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            fixed_mount = Path(tmp) / "InternalPartition"
            fixed_mount.mkdir()
            config = build_config(desktop=desktop, sd_volume_uuids=frozenset({SD_TEST_UUID}))
            fixed_volume = make_volume(SD_TEST_UUID, fixed_mount, removable=False)
            prober = fake_prober([fixed_volume])

            eligible = detect_sd_sources(prober, config)
            self.assertEqual(eligible, [])


if __name__ == "__main__":
    unittest.main()
