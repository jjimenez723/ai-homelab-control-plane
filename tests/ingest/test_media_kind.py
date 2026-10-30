from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tools.ingest.media_kind import classify_media
from tools.ingest.scanner import scan_source
from tests.ingest.helpers import build_config, write_file


class MediaKindTests(unittest.TestCase):
    def test_raw_and_video_use_signatures_without_visual_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "IMG_0001.CR3"
            raw.write_bytes(b"\x00\x00\x00\x18ftypcrx \x00\x00\x00\x01")
            video = root / "MVI_0002.MP4"
            video.write_bytes(b"\x00\x00\x00\x1cftypmp42\x00\x00\x00\x01")
            self.assertEqual(classify_media(raw).kind, "photo/raw")
            self.assertEqual(classify_media(video).kind, "video")
            self.assertEqual(classify_media(raw).route_type, "Pics")
            self.assertEqual(classify_media(video).route_type, "Vids")

    def test_provider_mime_wins_when_it_agrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.jpg"
            path.write_bytes(b"\xff\xd8\xff\xe0")
            result = classify_media(path, provider_mime="image/jpeg; charset=binary")
            self.assertEqual(result.kind, "photo")
            self.assertEqual(result.source, "provider-mime")

    def test_unknown_conflicting_and_temporary_inputs_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unknown = root / "capture.bin"
            unknown.write_bytes(b"not media")
            conflict = root / "capture.mp4"
            conflict.write_bytes(b"\xff\xd8\xff\xe0")
            temporary = root / "capture.mp4.part"
            temporary.write_bytes(b"partial")
            self.assertEqual(classify_media(unknown).kind, "review")
            self.assertEqual(classify_media(conflict).kind, "review")
            self.assertEqual(classify_media(temporary).kind, "review")
            self.assertIsNone(classify_media(unknown).route_type)

    def test_media_only_scan_stages_media_and_reports_non_media_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "SD"
            write_file(root / "DCIM" / "IMG_0001.JPG", b"\xff\xd8\xff\xe0")
            write_file(root / "DCIM" / "notes.txt", b"not media")
            config = build_config(desktop=root)
            config = replace(
                config,
                archive=replace(config.archive, enabled=True, media_only_sd=True),
            )
            scan = scan_source(root, config=config, media_only=True)
            self.assertEqual([item.rel_path for item in scan.eligible], ["DCIM/IMG_0001.JPG"])
            self.assertEqual([item.rel_path for item in scan.media_review], ["DCIM/notes.txt"])


if __name__ == "__main__":
    unittest.main()
