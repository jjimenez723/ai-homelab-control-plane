import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import social_video_learn
from tools.social_video_learn import (
    DEFAULT_CANDIDATES_ROOT,
    normalize_input,
    normalize_url,
    output_path,
    process_one,
    safe_component,
    unique_queue_items,
    update_queue_item,
)
from tools.social_video_queue import parse_args as queue_parse_args
from tools.social_video_queue import process_queue

REPO_ROOT = Path(__file__).resolve().parent.parent


class SocialVideoLearnTests(unittest.TestCase):
    def test_instagram_reel_normalization(self):
        self.assertEqual(normalize_url("https://www.instagram.com/reel/DczW8D7M8ko/?x=1"), {
            "platform": "instagram",
            "content_id": "DczW8D7M8ko",
            "canonical_url": "https://www.instagram.com/reel/DczW8D7M8ko/",
        })

    def test_youtube_id_normalization(self):
        expected = {
            "platform": "youtube",
            "content_id": "abcDEF_123",
            "canonical_url": "https://www.youtube.com/watch?v=abcDEF_123",
        }
        self.assertEqual(normalize_url("https://youtu.be/abcDEF_123?t=10"), expected)
        self.assertEqual(normalize_url("https://www.youtube.com/shorts/abcDEF_123"), expected)

    def test_duplicate_detection(self):
        items = [
            {"platform": "instagram", "content_id": "same", "url": "one"},
            {"platform": "instagram", "content_id": "same", "url": "two"},
            {"platform": "instagram", "content_id": "other", "url": "three"},
        ]
        self.assertEqual([item["url"] for item in unique_queue_items(items)], ["one", "three"])

    def test_queue_status_transition_preserves_item(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "queue.yaml"
            path.write_text("items:\n  - platform: instagram\n    content_id: one\n    url: https://example.test/one\n    status: queued\n", encoding="utf-8")
            update_queue_item(path, "one", "blocked", "access denied")
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: blocked", text)
            self.assertIn("failure_reason: \"access denied\"", text)
            self.assertIn("content_id: one", text)

    def test_safe_paths(self):
        self.assertEqual(safe_component("../../private secret"), "private-secret")
        self.assertEqual(output_path(Path("knowledge"), "instagram", "../../x"), Path("knowledge/instagram/x"))

    def test_local_input_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clip.mp4"
            path.write_bytes(b"fixture")
            normalized = normalize_input(str(path))
            self.assertEqual(normalized["platform"], "local")
            self.assertTrue(normalized["content_id"].startswith("sha256-"))

    def test_already_processed_item_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.mp4"
            video.write_bytes(b"fixture")
            normalized = normalize_input(str(video))
            artifact = output_path(root / "knowledge", normalized["platform"], normalized["content_id"])
            artifact.mkdir(parents=True)
            (artifact / "source.json").write_text('{"classification": "knowledge"}\n', encoding="utf-8")
            result, classification, candidate = process_one(
                str(video),
                knowledge_root=root / "knowledge",
                work_root=root / "work",
                candidates_root=root / "candidates",
                vts_bin="does-not-exist",
            )
            self.assertEqual(result, artifact)
            self.assertEqual(classification, "knowledge")
            self.assertIsNone(candidate)

    def test_completed_youtube_queue_item_uses_youtube_artifact_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queue = root / "queue.yaml"
            queue.write_text(
                "items:\n"
                "  - platform: youtube\n"
                "    content_id: abcDEF_123\n"
                "    url: https://youtu.be/abcDEF_123\n"
                "    status: complete\n",
                encoding="utf-8",
            )
            marker = root / "knowledge" / "youtube" / "abcDEF_123" / "source.json"
            marker.parent.mkdir(parents=True)
            marker.write_text("{}\n", encoding="utf-8")

            with patch("tools.social_video_queue.process_one") as mocked:
                outcomes = process_queue(
                    queue,
                    knowledge_root=root / "knowledge",
                    work_root=root / "work",
                    candidates_root=root / "candidates",
                    vts_bin="does-not-exist",
                )

            mocked.assert_not_called()
            self.assertEqual(outcomes, [("abcDEF_123", "skipped", "already complete")])

    def test_lock_file_matches_pinned_constants(self):
        lock = (REPO_ROOT / "tools" / "video-to-skill.lock").read_text(encoding="utf-8")
        pins = dict(
            (key.strip(), value.strip().strip('"'))
            for key, value in (line.split("=", 1) for line in lock.splitlines() if "=" in line)
        )
        self.assertEqual(pins["commit"], social_video_learn.VTS_COMMIT)
        self.assertEqual(pins["version"], social_video_learn.VTS_VERSION)

    def test_candidate_skills_default_outside_trusted_skills_tree(self):
        # The fallback is gitignored (`.state/`) and, crucially, is not under
        # `skills/` -- an agent harness loads every SKILL.md it finds there.
        self.assertEqual(DEFAULT_CANDIDATES_ROOT.parts[0], ".state")
        self.assertNotIn("skills", DEFAULT_CANDIDATES_ROOT.parts[:1])
        self.assertNotEqual(DEFAULT_CANDIDATES_ROOT.parts[0], "skills")
        # Neither CLI hard-codes a destination any more: both defer to
        # `resolve_candidates_root`, which prefers the UUID-verified volume.
        self.assertIsNone(social_video_learn.parse_args(["x"]).candidates_root)
        self.assertIsNone(queue_parse_args(["--queue", "q.yaml"]).candidates_root)

    def test_angel_candidates_relative_path_is_the_untrusted_tree(self):
        self.assertEqual(
            social_video_learn.ANGEL_CANDIDATES_RELATIVE, Path("40-ai/skills/candidates")
        )

    def test_resolve_candidates_root_honours_an_explicit_path(self):
        explicit = Path("/tmp/somewhere-else")
        self.assertEqual(social_video_learn.resolve_candidates_root(explicit), explicit)

    def test_resolve_candidates_root_falls_back_when_no_volume_is_configured(self):
        # Resolution happens relative to the current working directory's
        # config; with no `config/ingest.yaml` there is no UUID to verify, and
        # the pipeline must still run rather than refuse.
        with tempfile.TemporaryDirectory() as temp:
            cwd = os.getcwd()
            try:
                os.chdir(temp)
                self.assertEqual(social_video_learn.resolve_candidates_root(None), DEFAULT_CANDIDATES_ROOT)
            finally:
                os.chdir(cwd)

    def test_force_flag_reprocesses_completed_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.mp4"
            video.write_bytes(b"fixture")
            normalized = normalize_input(str(video))
            artifact = output_path(root / "knowledge", normalized["platform"], normalized["content_id"])
            artifact.mkdir(parents=True)
            (artifact / "source.json").write_text('{"classification": "knowledge"}\n', encoding="utf-8")
            with patch("tools.social_video_learn.run_command") as mocked:
                mocked.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="vts-extract failed")
                with self.assertRaises(social_video_learn.PipelineError):
                    process_one(
                        str(video),
                        knowledge_root=root / "knowledge",
                        work_root=root / "work",
                        candidates_root=root / "candidates",
                        vts_bin="does-not-exist",
                        force=True,
                    )
            self.assertEqual(mocked.call_args.args[0][:2], ["does-not-exist", "extract"])
            # The extractor receives the resolved local path, not the raw argument string.
            self.assertEqual(mocked.call_args.args[0][2], str(video.resolve()))

    def test_missing_tool_blocks_item_instead_of_aborting_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queue = root / "queue.yaml"
            queue.write_text(
                "items:\n"
                "  - platform: youtube\n"
                "    content_id: abcDEF_123\n"
                "    url: https://youtu.be/abcDEF_123\n"
                "    status: queued\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"PATH": str(root)}):
                outcomes = process_queue(
                    queue,
                    knowledge_root=root / "knowledge",
                    work_root=root / "work",
                    candidates_root=root / "candidates",
                    vts_bin=str(root / "missing-vts"),
                )
            self.assertEqual(outcomes[0][:2], ("abcDEF_123", "blocked"))
            self.assertIn("required tool not available", outcomes[0][2])
            self.assertIn("status: blocked", queue.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
