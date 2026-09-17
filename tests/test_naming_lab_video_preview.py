"""The Lab's video-frame preview reuses `tools.ingest.evidence.backends.ffmpeg_video`
directly -- no second video-decoding implementation."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.ingest.evidence.backends import ffmpeg_video
from tools.naming_lab.preview import video_frame_preview_png

_FFMPEG_AVAILABLE = ffmpeg_video.available()


def _make_test_video(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64:rate=5",
            str(path),
        ],
        check=True, timeout=30, capture_output=True,
    )
    return path


@unittest.skipUnless(_FFMPEG_AVAILABLE, "ffmpeg not installed on this machine")
class VideoFramePreviewTests(unittest.TestCase):
    def test_returns_png_bytes_for_a_real_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = _make_test_video(Path(tmp) / "clip.mp4")
            png = video_frame_preview_png(video_path, sha256="a" * 64, duration_seconds=2.0)
            self.assertIsNotNone(png)
            self.assertTrue(png.startswith(b"\x89PNG"))

    def test_missing_file_returns_none_not_an_exception(self):
        png = video_frame_preview_png(Path("/no/such/video.mp4"), sha256="b" * 64, duration_seconds=None)
        self.assertIsNone(png)


if __name__ == "__main__":
    unittest.main()
