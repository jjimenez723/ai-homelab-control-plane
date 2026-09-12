"""Deterministic media probing (`tools/ingest/media/probe.py`).

Every fixture is synthesised into a `tempfile.TemporaryDirectory`; nothing in
this file reads or writes a real Desktop, Documents, Downloads, or volume.

Two properties get asserted repeatedly rather than once, because they are the
ones whose violation would be silent and expensive: probing never mutates the
file it reads, and probing never raises. The optional-tool paths (ffprobe,
pypdf) are forced with `mock.patch` instead of depending on what happens to be
installed, so the suite behaves identically on a developer Mac with Homebrew
ffmpeg and on a bare CI container.
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

from tools.ingest.hashing import sha256_file
from tools.ingest.media import probe as media_probe
from tools.ingest.media.probe import (
    MEDIA_ARCHIVE,
    MEDIA_AUDIO,
    MEDIA_DOCUMENT,
    MEDIA_IMAGE,
    MEDIA_INSTALLER,
    MEDIA_OTHER,
    MEDIA_TEXT,
    MEDIA_VIDEO,
    PROBE_NAME,
    PROBE_VERSION,
    classify_media_kind,
    probe_file,
    thumbnail_png,
)

from tests.ingest.heic_fixtures import make_corrupt_heic, make_heic

# Canned ffprobe output, so the parsing path is exercised on machines without
# ffmpeg. Field names and the "30000/1001" rational match real ffprobe JSON.
FAKE_FFPROBE_JSON = b"""
{
  "streams": [
    {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
     "avg_frame_rate": "30000/1001"},
    {"codec_type": "audio", "codec_name": "aac", "avg_frame_rate": "0/0"}
  ],
  "format": {
    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
    "duration": "12.500000",
    "bit_rate": "8000000",
    "tags": {"creation_time": "2023-04-05T06:07:08.000000Z"}
  }
}
"""


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

    def make_png(self, name: str = "image.png", *, size=(120, 80), mode="RGB") -> Path:
        path = self.root / name
        Image.new(mode, size, (20, 120, 200) if mode == "RGB" else (20, 120, 200, 255)).save(path)
        return path

    def make_jpeg_with_camera_exif(
        self,
        name: str = "camera.jpg",
        *,
        size=(90, 60),
        capture_time: str | None = "2021:03:04 09:15:30",
        make: str | None = "TestCam",
        model: str | None = "ModelX",
    ) -> Path:
        path = self.root / name
        exif = Image.Exif()
        if make is not None:
            exif[0x010F] = make
        if model is not None:
            exif[0x0110] = model
        exif[0x010E] = "a test frame"
        sub = exif.get_ifd(0x8769)
        if capture_time is not None:
            sub[0x9003] = capture_time
        sub[0xA434] = "50mm f/1.8"
        sub[0x9286] = b"ASCII\x00\x00\x00hello there"
        Image.new("RGB", size, (200, 60, 10)).save(path, exif=exif)
        return path

    def dir_listing(self) -> set[str]:
        return {str(p.relative_to(self.root)) for p in self.root.rglob("*")}


class ClassifyMediaKindTests(unittest.TestCase):
    def _assert_all(self, suffixes: list[str], expected: str) -> None:
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                self.assertEqual(classify_media_kind(Path(f"sample{suffix}")), expected)

    def test_images(self):
        self._assert_all(
            [
                ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".tif", ".tiff",
                ".webp", ".bmp", ".nef", ".cr2", ".cr3", ".arw", ".dng", ".raf",
                ".orf", ".rw2", ".avif",
            ],
            MEDIA_IMAGE,
        )

    def test_video(self):
        self._assert_all(
            [".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg",
             ".mts", ".m2ts", ".wmv", ".flv", ".3gp"],
            MEDIA_VIDEO,
        )

    def test_audio(self):
        self._assert_all(
            [".mp3", ".wav", ".aiff", ".aif", ".m4a", ".flac", ".aac", ".ogg", ".opus"],
            MEDIA_AUDIO,
        )

    def test_documents(self):
        self._assert_all(
            [".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pages",
             ".numbers", ".key", ".rtf", ".odt", ".epub"],
            MEDIA_DOCUMENT,
        )

    def test_archives(self):
        self._assert_all([".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"], MEDIA_ARCHIVE)

    def test_installers(self):
        self._assert_all(
            [".dmg", ".pkg", ".mpkg", ".iso", ".exe", ".msi", ".deb", ".rpm", ".appimage", ".app"],
            MEDIA_INSTALLER,
        )

    def test_text_and_source(self):
        self._assert_all(
            [".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".log",
             ".py", ".js", ".ts", ".go", ".rs", ".sh", ".sql", ".css"],
            MEDIA_TEXT,
        )

    def test_unknown_extension_is_other(self):
        self.assertEqual(classify_media_kind(Path("mystery.qqq")), MEDIA_OTHER)
        self.assertEqual(classify_media_kind(Path("no-extension-at-all")), MEDIA_OTHER)

    def test_uppercase_and_mixed_case_extensions(self):
        for name, expected in [
            ("IMG_0001.JPG", MEDIA_IMAGE),
            ("Clip.MOV", MEDIA_VIDEO),
            ("Track.MP3", MEDIA_AUDIO),
            ("Report.PDF", MEDIA_DOCUMENT),
            ("Bundle.ZIP", MEDIA_ARCHIVE),
            ("Installer.DMG", MEDIA_INSTALLER),
            ("Notes.Md", MEDIA_TEXT),
            ("Photo.HeIc", MEDIA_IMAGE),
        ]:
            with self.subTest(name=name):
                self.assertEqual(classify_media_kind(Path(name)), expected)

    def test_mime_is_only_a_fallback_for_unknown_extensions(self):
        # Extension wins when it is recognised: a stable answer beats a
        # marginally better-informed one (see classify_media_kind's docstring).
        self.assertEqual(classify_media_kind(Path("a.png"), "video/mp4"), MEDIA_IMAGE)
        # ...and MIME is consulted only when the extension says nothing.
        self.assertEqual(classify_media_kind(Path("a.qqq"), "image/png"), MEDIA_IMAGE)
        self.assertEqual(classify_media_kind(Path("a.qqq"), "video/quicktime"), MEDIA_VIDEO)
        self.assertEqual(classify_media_kind(Path("a.qqq"), "audio/mpeg"), MEDIA_AUDIO)
        self.assertEqual(classify_media_kind(Path("a.qqq"), "text/plain; charset=utf-8"), MEDIA_TEXT)
        self.assertEqual(classify_media_kind(Path("a.qqq"), "application/pdf"), MEDIA_DOCUMENT)
        self.assertEqual(classify_media_kind(Path("a.qqq"), "application/zip"), MEDIA_ARCHIVE)
        self.assertEqual(classify_media_kind(Path("a.qqq"), "application/x-nonsense"), MEDIA_OTHER)


class ImageProbeTests(TempDirTestCase):
    def test_png_dimensions_aspect_and_format(self):
        path = self.make_png(size=(120, 80))
        result = probe_file(path)

        self.assertEqual(result.media_kind, MEDIA_IMAGE)
        self.assertEqual(result.probe, PROBE_NAME)
        self.assertEqual(result.probe_version, PROBE_VERSION)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.facts["width"], 120)
        self.assertEqual(result.facts["height"], 80)
        self.assertEqual(result.facts["aspect_ratio"], 1.5)
        self.assertEqual(result.facts["format"], "PNG")
        self.assertEqual(result.facts["mode"], "RGB")
        self.assertEqual(result.facts["extension"], ".png")
        self.assertEqual(result.facts["size_bytes"], path.stat().st_size)

    def test_aspect_ratio_is_rounded_to_four_places(self):
        result = probe_file(self.make_png("odd.png", size=(3, 7)))
        self.assertEqual(result.facts["aspect_ratio"], round(3 / 7, 4))

    def test_alpha_channel_is_reported(self):
        self.assertTrue(probe_file(self.make_png("rgba.png", mode="RGBA")).facts["has_alpha"])
        self.assertFalse(probe_file(self.make_png("rgb.png", mode="RGB")).facts["has_alpha"])

    def test_jpeg_exif_capture_time_and_camera_fields(self):
        path = self.make_jpeg_with_camera_exif(capture_time="2021:03:04 09:15:30")
        facts = probe_file(path).facts

        self.assertTrue(facts["exif_present"])
        self.assertEqual(facts["capture_timestamp"], "2021-03-04T09:15:30")
        self.assertEqual(facts["camera_make"], "TestCam")
        self.assertEqual(facts["camera_model"], "ModelX")
        self.assertEqual(facts["lens_model"], "50mm f/1.8")
        self.assertEqual(facts["image_description"], "a test frame")
        self.assertEqual(facts["exif_user_comment"], "hello there")
        self.assertFalse(facts["gps_present"])

    def test_capture_timestamp_falls_back_to_datetime_digitized(self):
        path = self.root / "digitized.jpg"
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9004] = "2019:12:25 18:45:00"
        Image.new("RGB", (10, 10), (0, 0, 0)).save(path, exif=exif)

        self.assertEqual(probe_file(path).facts["capture_timestamp"], "2019-12-25T18:45:00")

    def test_unparseable_capture_timestamp_is_reported_not_invented(self):
        path = self.root / "badtime.jpg"
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9003] = "not a timestamp"
        Image.new("RGB", (10, 10), (0, 0, 0)).save(path, exif=exif)

        result = probe_file(path)
        self.assertNotIn("capture_timestamp", result.facts)
        self.assertTrue(any("unparseable exif timestamp" in e for e in result.errors))

    def test_orientation_tag_is_read_as_int(self):
        path = self.root / "rotated.jpg"
        exif = Image.Exif()
        exif[0x0112] = 6
        Image.new("RGB", (10, 10), (0, 0, 0)).save(path, exif=exif)

        facts = probe_file(path).facts
        self.assertEqual(facts["orientation"], 6)
        self.assertIsInstance(facts["orientation"], int)

    def test_icc_profile_presence_and_size(self):
        with_profile = self.root / "icc.jpg"
        profile = b"FAKEICC" + b"z" * 120
        Image.new("RGB", (10, 10), (1, 2, 3)).save(with_profile, icc_profile=profile)

        facts = probe_file(with_profile).facts
        self.assertTrue(facts["icc_profile_present"])
        self.assertEqual(facts["icc_profile_bytes"], len(profile))

        plain = probe_file(self.make_png("plain.png")).facts
        self.assertFalse(plain["icc_profile_present"])
        # An absent key means "not measured"; a zero would read as a fact.
        self.assertNotIn("icc_profile_bytes", plain)

    def test_missing_facts_are_omitted_not_placeholdered(self):
        facts = probe_file(self.make_png("bare.png")).facts
        for key in ("camera_make", "camera_model", "lens_model", "capture_timestamp",
                    "orientation", "exif_user_comment", "image_description"):
            self.assertNotIn(key, facts)

    def test_heic_probes_without_error(self):
        path = make_heic(self.root / "photo.heic", size=(64, 48), capture_time="2020:06:15 08:30:00")
        result = probe_file(path)

        self.assertEqual(result.errors, ())
        self.assertEqual(result.media_kind, MEDIA_IMAGE)
        self.assertEqual(result.facts["width"], 64)
        self.assertEqual(result.facts["height"], 48)
        self.assertEqual(result.facts["capture_timestamp"], "2020-06-15T08:30:00")
        self.assertTrue(result.facts["icc_profile_present"])

    def test_facts_are_json_serialisable_and_key_sorted(self):
        import json

        facts = probe_file(self.make_jpeg_with_camera_exif("json.jpg")).facts
        self.assertEqual(list(facts), sorted(facts))
        json.dumps(facts, sort_keys=True)  # must not raise


class ScreenshotHeuristicTests(TempDirTestCase):
    def test_metadata_free_png_is_flagged(self):
        facts = probe_file(self.make_png("Screen Shot.png")).facts
        self.assertTrue(facts["is_probably_screenshot"])

    def test_jpeg_with_camera_exif_is_not_flagged(self):
        facts = probe_file(self.make_jpeg_with_camera_exif()).facts
        self.assertFalse(facts["is_probably_screenshot"])

    def test_png_carrying_camera_exif_is_not_flagged(self):
        # The heuristic keys on absence of camera identification, not on the
        # container, so a PNG that does carry camera EXIF is excluded.
        path = self.root / "camera.png"
        exif = Image.Exif()
        exif[0x010F] = "TestCam"
        exif[0x0110] = "ModelX"
        Image.new("RGB", (30, 20), (5, 5, 5)).save(path, exif=exif)

        self.assertFalse(probe_file(path).facts["is_probably_screenshot"])

    def test_metadata_free_jpeg_is_not_flagged(self):
        path = self.root / "plain.jpg"
        Image.new("RGB", (30, 20), (5, 5, 5)).save(path)
        self.assertFalse(probe_file(path).facts["is_probably_screenshot"])


class MalformedInputTests(TempDirTestCase):
    def test_corrupt_image_reports_errors_without_raising_or_mutating(self):
        path = self.root / "corrupt.jpg"
        path.write_bytes(b"this is definitely not a jpeg" * 20)
        before_digest = sha256_file(path)
        before_mtime = path.stat().st_mtime_ns

        result = probe_file(path)

        self.assertEqual(result.media_kind, MEDIA_IMAGE)
        self.assertTrue(result.errors)
        self.assertEqual(sha256_file(path), before_digest)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_truncated_png_reports_errors_and_keeps_header_facts(self):
        source = self.make_png("full.png", size=(120, 80))
        truncated = self.root / "truncated.png"
        truncated.write_bytes(source.read_bytes()[: len(source.read_bytes()) // 2])
        before_digest = sha256_file(truncated)

        result = probe_file(truncated)

        self.assertTrue(result.errors)
        # The header was intact, so its facts survive alongside the error.
        self.assertEqual(result.facts["width"], 120)
        self.assertEqual(sha256_file(truncated), before_digest)

    def test_corrupt_heic_does_not_raise(self):
        path = make_corrupt_heic(self.root / "broken.heic")
        result = probe_file(path)
        self.assertEqual(result.media_kind, MEDIA_IMAGE)
        self.assertTrue(result.errors)

    def test_empty_file_does_not_raise(self):
        path = self.root / "empty.png"
        path.write_bytes(b"")
        result = probe_file(path)
        self.assertEqual(result.facts["size_bytes"], 0)
        self.assertTrue(result.errors)

    def test_missing_file_does_not_raise(self):
        result = probe_file(self.root / "does-not-exist.png")
        self.assertTrue(result.errors)
        self.assertNotIn("size_bytes", result.facts)

    def test_unknown_binary_extension_is_other_and_does_not_raise(self):
        path = self.root / "blob.qqq"
        path.write_bytes(bytes(range(256)) * 8)

        result = probe_file(path)

        self.assertEqual(result.media_kind, MEDIA_OTHER)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.facts["extension"], ".qqq")
        self.assertEqual(result.facts["size_bytes"], 2048)

    def test_extensionless_file_does_not_raise(self):
        path = self.root / "README"
        path.write_bytes(b"hello")
        result = probe_file(path)
        self.assertEqual(result.media_kind, MEDIA_OTHER)
        self.assertEqual(result.facts["extension"], "")


class ArchiveProbeTests(TempDirTestCase):
    def make_zip(self, name: str = "bundle.zip") -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("project/a.txt", "x" * 10)
            archive.writestr("project/b.txt", "y" * 20)
            archive.writestr("project/nested/c.txt", "z" * 30)
            archive.writestr("notes/d.md", "w" * 5)
        return path

    def test_zip_facts(self):
        path = self.make_zip()
        result = probe_file(path)

        self.assertEqual(result.media_kind, MEDIA_ARCHIVE)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.facts["entry_count"], 4)
        self.assertEqual(result.facts["top_level_entries"], ["notes", "project"])
        self.assertEqual(result.facts["uncompressed_size"], 65)
        self.assertEqual(result.facts["dominant_extension"], ".txt")

    def test_top_level_entries_are_sorted_and_capped(self):
        path = self.root / "many.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for index in range(40):
                archive.writestr(f"dir{index:02d}/file.txt", "q")

        entries = probe_file(path).facts["top_level_entries"]
        self.assertEqual(len(entries), 20)
        self.assertEqual(entries, sorted(entries))

    def test_probing_a_zip_creates_no_files(self):
        # The probe reads the central directory only. If it ever decompressed
        # anything -- the zip-bomb vector this guards against -- extracted
        # members would show up here.
        path = self.make_zip()
        before = self.dir_listing()

        probe_file(path)

        self.assertEqual(self.dir_listing(), before)

    def test_probe_never_calls_read_or_extract(self):
        path = self.make_zip()
        with mock.patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("read() called")), \
             mock.patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError("extract() called")), \
             mock.patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("extractall() called")):
            result = probe_file(path)

        self.assertEqual(result.errors, ())
        self.assertEqual(result.facts["entry_count"], 4)

    def test_corrupt_zip_reports_error_without_raising(self):
        path = self.root / "broken.zip"
        path.write_bytes(b"PK\x03\x04 but not really a zip")

        result = probe_file(path)

        self.assertEqual(result.media_kind, MEDIA_ARCHIVE)
        self.assertTrue(result.errors)
        self.assertNotIn("entry_count", result.facts)

    def test_non_zip_archive_extension_yields_base_facts_only(self):
        path = self.root / "stuff.tar.gz"
        path.write_bytes(b"\x1f\x8b\x08" + b"\x00" * 40)

        result = probe_file(path)

        self.assertEqual(result.media_kind, MEDIA_ARCHIVE)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.facts["extension"], ".gz")


class DocumentProbeTests(TempDirTestCase):
    def test_pdf_records_probe_unavailable_when_pypdf_is_missing(self):
        path = self.root / "doc.pdf"
        path.write_bytes(b"%PDF-1.4\n% not a real pdf\n")

        # Forced rather than observed, so the assertion holds on a machine
        # that happens to have pypdf installed.
        with mock.patch.object(media_probe, "pypdf_available", return_value=False):
            result = probe_file(path)

        self.assertEqual(result.media_kind, MEDIA_DOCUMENT)
        self.assertEqual(result.facts["probe_unavailable"], "pypdf")
        self.assertTrue(any("pypdf" in error for error in result.errors))

    def test_non_pdf_document_yields_base_facts_only(self):
        path = self.root / "letter.docx"
        path.write_bytes(b"not really a docx")

        result = probe_file(path)

        self.assertEqual(result.media_kind, MEDIA_DOCUMENT)
        self.assertEqual(result.errors, ())
        self.assertEqual(set(result.facts), {"extension", "size_bytes"})


class VideoProbeTests(TempDirTestCase):
    def make_clip(self, name: str = "clip.mp4") -> Path:
        path = self.root / name
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        return path

    def test_records_probe_unavailable_when_ffprobe_is_missing(self):
        path = self.make_clip()

        with mock.patch.object(media_probe, "ffprobe_available", return_value=False):
            result = probe_file(path)

        self.assertEqual(result.media_kind, MEDIA_VIDEO)
        self.assertEqual(result.facts["probe_unavailable"], "ffprobe")
        self.assertTrue(any("ffprobe" in error for error in result.errors))

    def test_ffprobe_failure_is_recorded_not_raised(self):
        path = self.make_clip()
        failure = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"moov atom not found\n"
        )

        with mock.patch.object(media_probe, "ffprobe_available", return_value=True), \
             mock.patch.object(subprocess, "run", return_value=failure):
            result = probe_file(path)

        self.assertEqual(result.facts["probe_unavailable"], "ffprobe")
        self.assertTrue(any("moov atom not found" in error for error in result.errors))

    def test_ffprobe_timeout_is_recorded_not_raised(self):
        path = self.make_clip()

        with mock.patch.object(media_probe, "ffprobe_available", return_value=True), \
             mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
            result = probe_file(path)

        self.assertEqual(result.facts["probe_unavailable"], "ffprobe")
        self.assertTrue(any("timed out" in error for error in result.errors))

    def test_ffprobe_output_is_parsed_into_facts(self):
        path = self.make_clip()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=FAKE_FFPROBE_JSON, stderr=b""
        )

        with mock.patch.object(media_probe, "ffprobe_available", return_value=True), \
             mock.patch.object(subprocess, "run", return_value=completed) as run:
            result = probe_file(path)

        command = run.call_args.args[0]
        self.assertEqual(command[:6], ["ffprobe", "-v", "error", "-print_format", "json", "-show_format"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertFalse(run.call_args.kwargs["check"])

        facts = result.facts
        self.assertEqual(result.errors, ())
        self.assertEqual(facts["duration_seconds"], 12.5)
        self.assertEqual(facts["width"], 1920)
        self.assertEqual(facts["height"], 1080)
        self.assertEqual(facts["aspect_ratio"], round(1920 / 1080, 4))
        self.assertEqual(facts["video_codec"], "h264")
        self.assertTrue(facts["audio_present"])
        self.assertEqual(facts["audio_codec"], "aac")
        self.assertEqual(facts["frame_rate"], round(30000 / 1001, 6))
        self.assertEqual(facts["bit_rate"], 8000000)
        self.assertEqual(facts["container_format"], "mov,mp4,m4a,3gp,3g2,mj2")
        self.assertEqual(facts["capture_timestamp"], "2023-04-05T06:07:08.000000Z")

    def test_enable_video_false_skips_the_subprocess_entirely(self):
        path = self.make_clip()

        with mock.patch.object(media_probe, "ffprobe_available", return_value=True), \
             mock.patch.object(subprocess, "run", side_effect=AssertionError("ffprobe invoked")):
            result = probe_file(path, enable_video=False)

        self.assertEqual(result.facts["probe_skipped"], "ffprobe")
        self.assertEqual(result.errors, ())

    def test_audio_files_use_the_same_ffprobe_path(self):
        path = self.root / "track.mp3"
        path.write_bytes(b"ID3" + b"\x00" * 32)

        with mock.patch.object(media_probe, "ffprobe_available", return_value=False):
            result = probe_file(path)

        self.assertEqual(result.media_kind, MEDIA_AUDIO)
        self.assertEqual(result.facts["probe_unavailable"], "ffprobe")


class ThumbnailTests(TempDirTestCase):
    def test_large_image_is_downscaled_to_png_bytes(self):
        path = self.root / "large.jpg"
        Image.new("RGB", (2000, 1000), (10, 20, 30)).save(path)

        data = thumbnail_png(path)

        self.assertIsInstance(data, bytes)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(data)) as thumb:
            self.assertEqual(thumb.format, "PNG")
            self.assertLessEqual(max(thumb.size), 512)
            # Aspect ratio is preserved by the downscale.
            self.assertEqual(thumb.size, (512, 256))

    def test_max_edge_is_honoured(self):
        path = self.root / "wide.png"
        Image.new("RGB", (900, 300), (1, 2, 3)).save(path)

        with Image.open(io.BytesIO(thumbnail_png(path, max_edge=128))) as thumb:
            self.assertLessEqual(max(thumb.size), 128)

    def test_small_image_is_not_upscaled(self):
        path = self.root / "small.png"
        Image.new("RGB", (40, 25), (1, 2, 3)).save(path)

        with Image.open(io.BytesIO(thumbnail_png(path))) as thumb:
            self.assertEqual(thumb.size, (40, 25))

    def test_non_image_returns_none(self):
        blob = self.root / "blob.qqq"
        blob.write_bytes(b"\x00\x01\x02\x03" * 16)
        self.assertIsNone(thumbnail_png(blob))

        corrupt = self.root / "corrupt.jpg"
        corrupt.write_bytes(b"not a jpeg at all" * 10)
        self.assertIsNone(thumbnail_png(corrupt))

        self.assertIsNone(thumbnail_png(self.root / "missing.png"))

    def test_declared_decompression_bomb_is_refused(self):
        # Guarded on the declared pixel count, so the decode never happens;
        # patching Image.open's reported size proves the check runs before any
        # pixels are touched.
        path = self.root / "bomb.png"
        Image.new("RGB", (40, 25), (1, 2, 3)).save(path)

        with mock.patch.object(media_probe, "MAX_DECODE_PIXELS", 10):
            self.assertIsNone(thumbnail_png(path))

    def test_thumbnail_writes_nothing_to_disk(self):
        path = self.root / "source.png"
        Image.new("RGB", (1200, 900), (7, 8, 9)).save(path)
        before = self.dir_listing()
        before_digest = sha256_file(path)

        thumbnail_png(path)

        self.assertEqual(self.dir_listing(), before)
        self.assertEqual(sha256_file(path), before_digest)


class MutationSafetyTests(TempDirTestCase):
    """probe_file must be indistinguishable from not having run, on disk.

    Ingestion probes a user's only copy of a file, often before any backup
    exists. A metadata read that rewrote EXIF, touched mtime, or dropped a
    sidecar would corrupt exactly the originals this pipeline exists to
    preserve, so the guarantee is asserted per file type rather than assumed.
    """

    def _assert_unchanged(self, path: Path, **kwargs) -> None:
        before_digest = sha256_file(path)
        before_stat = path.stat()
        before_listing = self.dir_listing()

        probe_file(path, **kwargs)

        after_stat = path.stat()
        self.assertEqual(sha256_file(path), before_digest, f"content changed: {path.name}")
        self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns, f"mtime changed: {path.name}")
        self.assertEqual(after_stat.st_size, before_stat.st_size, f"size changed: {path.name}")
        self.assertEqual(self.dir_listing(), before_listing, f"directory changed while probing {path.name}")

    def test_originals_are_untouched_across_every_kind(self):
        png = self.make_png("photo.png")
        jpeg = self.make_jpeg_with_camera_exif("camera.jpg")
        heic = make_heic(self.root / "photo.heic")
        corrupt = self.root / "corrupt.jpg"
        corrupt.write_bytes(b"garbage" * 30)
        blob = self.root / "blob.qqq"
        blob.write_bytes(b"\x01\x02\x03" * 30)
        archive = self.root / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a/b.txt", "content")
        document = self.root / "doc.pdf"
        document.write_bytes(b"%PDF-1.4\n%stub\n")
        clip = self.root / "clip.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)

        for path in (png, jpeg, heic, corrupt, blob, archive):
            with self.subTest(name=path.name):
                self._assert_unchanged(path)

        with self.subTest(name=document.name), \
             mock.patch.object(media_probe, "pypdf_available", return_value=False):
            self._assert_unchanged(document)

        with self.subTest(name=clip.name), \
             mock.patch.object(media_probe, "ffprobe_available", return_value=False):
            self._assert_unchanged(clip)

    def test_read_only_source_can_still_be_probed(self):
        # A file on a read-only mount (an SD card with the lock tab set, a
        # write-protected archive volume) must still probe cleanly.
        path = self.make_jpeg_with_camera_exif("locked.jpg")
        os.chmod(path, 0o444)
        self.addCleanup(os.chmod, path, 0o644)

        result = probe_file(path)

        self.assertEqual(result.errors, ())
        self.assertEqual(result.facts["camera_make"], "TestCam")


class OptionalToolDetectionTests(unittest.TestCase):
    def test_detectors_return_booleans_and_are_cached(self):
        self.assertIsInstance(media_probe.ffprobe_available(), bool)
        self.assertIsInstance(media_probe.pypdf_available(), bool)
        # Cached: a second call must not re-run the lookup.
        with mock.patch.object(media_probe.shutil, "which", side_effect=AssertionError("uncached")):
            media_probe.ffprobe_available()


if __name__ == "__main__":
    unittest.main()
