import tempfile
import unittest
from pathlib import Path

from tools.social_video_learn import (
    normalize_input,
    normalize_url,
    output_path,
    process_one,
    safe_component,
    unique_queue_items,
    update_queue_item,
)


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


if __name__ == "__main__":
    unittest.main()
