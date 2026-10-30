from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tools.ingest.catalog import Asset, Catalog
from tools.ingest.config import AIConfig
from tools.ingest.modeling import ModelOrchestrator, ModelResponse, OllamaClient, normalize_payload
from tests.ingest.archive_fixtures import example_profile


class FakeClient:
    def prompt_sha(self, *, asset, profiles):
        return "prompt-sha"

    def analyze_image(self, image_path, *, asset, profiles):
        return ModelResponse(
            payload=normalize_payload(
                {
                    "summary": "portrait in a kitchen",
                    "tags": ["portrait", "kitchen"],
                    "people": [
                        {
                            "person_profile_id": "person-alice",
                            "confidence": 0.82,
                            "evidence": "matches the confirmed profile context",
                        }
                    ],
                    "animals": [
                        {
                            "animal_type": "dog",
                            "count": 1,
                            "confidence": 0.91,
                            "evidence": "dog visible beside the person",
                        }
                    ],
                    "filename_proposal": {"subject": "kitchen", "variant": "portrait", "confidence": 0.8},
                }
            ),
            model="fake-vlm",
            prompt_sha="prompt-sha",
        )


class ModelingTests(unittest.TestCase):
    def test_prompt_restricts_model_to_canonical_categories_without_prefix_selection(self):
        profile = example_profile()
        prompt = OllamaClient(AIConfig(), profile)._user_prompt(
            asset=Asset({"asset_id": "preflight:card:photo.jpg", "original_filename": "photo.jpg"}),
            profiles=[],
        )
        self.assertIn("choose exactly one of", prompt)
        for category in profile.canonical_categories:
            self.assertIn(category, prompt)
        self.assertNotIn('"prefix"', prompt)
        self.assertNotIn("folder-weddings", prompt)

    def test_prompt_without_archive_profile_asks_for_null_category(self):
        prompt = OllamaClient(AIConfig())._user_prompt(
            asset=Asset({"asset_id": "preflight:card:photo.jpg", "original_filename": "photo.jpg"}),
            profiles=[],
        )
        self.assertIn("none supplied; archive_category must be null", prompt)
        self.assertNotIn("choose exactly one of", prompt)

    def test_normalize_payload_accepts_canonical_category_alias_and_rejects_unknown(self):
        profile = example_profile()
        self.assertEqual(normalize_payload({"archive_category": "wedding"}, profile)["archive_category"], "Weddings")
        self.assertEqual(normalize_payload({"archive_category": "Portraits"}, profile)["archive_category"], "Portraits")
        self.assertIsNone(normalize_payload({"archive_category": "Miscellaneous"}, profile)["archive_category"])
        self.assertIsNone(normalize_payload({"archive_category": "Weddings"})["archive_category"])

    def test_normalize_payload_keeps_description_and_only_valid_capture_date(self):
        valid = normalize_payload({"summary": "ceremony", "description": "A wedding ceremony.", "capture_date": "2026-10-25"})
        invalid = normalize_payload({"summary": "ceremony", "capture_date": "not-a-date"})
        self.assertEqual(valid["description"], "A wedding ceremony.")
        self.assertEqual(valid["capture_date"], "2026-10-25")
        self.assertIsNone(invalid["capture_date"])

    def test_ollama_retries_a_malformed_json_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "photo.jpg"
            Image.new("RGB", (40, 40), (20, 30, 40)).save(image_path)
            valid_content = json.dumps(
                {
                    "summary": "a dog",
                    "tags": ["dog"],
                    "people": [],
                    "animals": [{"animal_type": "dog", "count": 1, "confidence": 0.9, "evidence": "fur"}],
                    "filename_proposal": {"subject": "dog portrait", "variant": None, "confidence": 0.9},
                }
            )
            responses = iter(
                [
                    json.dumps({"choices": [{"message": {"content": "not json"}}]}).encode("utf-8"),
                    json.dumps({"choices": [{"message": {"content": valid_content}}]}).encode("utf-8"),
                ]
            )

            class FakeResponse:
                def __init__(self, body):
                    self.body = body

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    return False

                def read(self):
                    return self.body

            def fake_urlopen(request, timeout):
                return FakeResponse(next(responses))

            client = OllamaClient(AIConfig())
            with patch("tools.ingest.modeling.urllib.request.urlopen", side_effect=fake_urlopen) as urlopen:
                result = client.analyze_image(
                    image_path,
                    asset=Asset({"asset_id": "preflight:test", "original_filename": image_path.name}),
                    profiles=[],
                )

            self.assertEqual(urlopen.call_count, 2)
            self.assertEqual(result.payload["filename_proposal"]["subject"], "dog portrait")

    def test_video_proxy_is_a_contact_sheet_of_extracted_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "clip.mp4"
            video_path.write_bytes(b"video fixture")
            frame = io.BytesIO()
            Image.new("RGB", (120, 80), (20, 30, 40)).save(frame, format="JPEG")

            client = OllamaClient(AIConfig(video_sample_count=4))
            with patch.object(client, "_video_frame_bytes", return_value=[frame.getvalue()] * 4):
                proxy = client._proxy_bytes(video_path)

            with Image.open(io.BytesIO(proxy)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (1600, 900))

    def test_preflight_result_is_durable_in_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "catalog.sqlite"
            with Catalog(catalog_path) as catalog:
                catalog.record_preflight_result(
                    source_volume_id="card",
                    source_path="/Volumes/card/DCIM/photo.jpg",
                    relative_path="DCIM/photo.jpg",
                    source_size=10,
                    source_mtime_ns=20,
                    model="fake-vlm",
                    model_version="media-analysis-v2",
                    prompt_sha="prompt-sha",
                    status="succeeded",
                    result={"status": "ready_for_review"},
                )
                row = catalog._conn.execute(
                    "SELECT status, result_json FROM preflight_results WHERE source_path=?",
                    ("/Volumes/card/DCIM/photo.jpg",),
                ).fetchone()

            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(json.loads(row["result_json"])["status"], "ready_for_review")

    def test_preflight_analysis_reads_source_without_catalog_or_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "photo.jpg"
            Image.new("RGB", (40, 40), (20, 30, 40)).save(image_path)
            orchestrator = ModelOrchestrator(AIConfig())
            orchestrator.client = FakeClient()

            result = orchestrator.analyze_source_image(
                image_path,
                source_id="preflight:card:DCIM/photo.jpg",
                profiles=[],
            )

            self.assertEqual(result.source_path, str(image_path))
            self.assertEqual(result.payload["animals"][0]["animal_type"], "dog")
            self.assertEqual(result.payload["people"][0]["person_profile_id"], "person-alice")
            self.assertTrue(image_path.exists())
            self.assertFalse((root / "catalog.sqlite").exists())

    def test_proposals_are_idempotent_and_do_not_change_final_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "photo.jpg"
            Image.new("RGB", (40, 40), (20, 30, 40)).save(image_path)
            digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
            with Catalog(root / "catalog.sqlite") as catalog:
                catalog.ensure_pending_asset(
                    "asset-1",
                    source_volume_id="t7",
                    source_class="desktop",
                    original_path="photo.jpg",
                    original_filename="photo.jpg",
                    size=image_path.stat().st_size,
                )
                catalog.upsert_blob(digest, size=image_path.stat().st_size, mime="image/jpeg")
                catalog.update_asset(
                    "asset-1",
                    sha256=digest,
                    final_path=str(image_path),
                    final_filename=image_path.name,
                    transfer_state="source_unlinked",
                )
                catalog.upsert_person_profile("person-alice", display_name="Alice")
                orchestrator = ModelOrchestrator(AIConfig())
                orchestrator.client = FakeClient()

                first = orchestrator.analyze_asset(catalog, catalog.get_asset("asset-1"))
                second = orchestrator.analyze_asset(catalog, catalog.get_asset("asset-1"))

                self.assertEqual(len(first.proposal_ids), 6)
                self.assertEqual(first.people_detected, 1)
                self.assertEqual(first.animals_detected, 1)
                self.assertEqual(second.proposal_ids, ())
                self.assertEqual(catalog.get_asset("asset-1").final_path, str(image_path))
                self.assertEqual(
                    catalog._conn.execute("SELECT COUNT(*) FROM proposals WHERE asset_id='asset-1'").fetchone()[0],
                    6,
                )
                animal = catalog._conn.execute(
                    "SELECT field, value, confidence FROM proposals "
                    "WHERE asset_id='asset-1' AND field='animal_observation'"
                ).fetchone()
                self.assertEqual(animal["field"], "animal_observation")
                self.assertEqual(json.loads(animal["value"])["animal_type"], "dog")
                self.assertEqual(animal["confidence"], 0.91)
                observation = catalog._conn.execute(
                    "SELECT source, confidence FROM person_observations WHERE asset_id='asset-1'"
                ).fetchone()
                self.assertEqual(observation["source"], "model")
                self.assertEqual(observation["confidence"], 0.82)

    def test_human_observation_cannot_be_overwritten_by_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Catalog(Path(tmp) / "catalog.sqlite") as catalog:
                catalog.upsert_person_profile("person-alice", display_name="Alice")
                catalog.record_person_observation(
                    "asset-1", profile_id="person-alice", confidence=1.0, source="human", evidence="confirmed"
                )
                catalog.record_person_observation(
                    "asset-1", profile_id="person-alice", confidence=0.1, source="model", evidence="guess"
                )
                row = catalog._conn.execute(
                    "SELECT source, confidence, evidence FROM person_observations WHERE asset_id='asset-1'"
                ).fetchone()
                self.assertEqual(dict(row), {"source": "human", "confidence": 1.0, "evidence": "confirmed"})

    def test_corrections_are_current_and_historied(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Catalog(Path(tmp) / "catalog.sqlite") as catalog:
                catalog.ensure_pending_asset(
                    "asset-1",
                    source_volume_id="t7",
                    source_class="desktop",
                    original_path="photo.jpg",
                    original_filename="photo.jpg",
                    size=1,
                )
                catalog.record_correction("asset-1", field="person_profile_id", value="person-alice")
                catalog.record_correction("asset-1", field="person_profile_id", value="person-bob")
                current = catalog._conn.execute(
                    "SELECT value FROM corrections WHERE asset_id='asset-1' AND field='person_profile_id'"
                ).fetchone()
                events = catalog._conn.execute(
                    "SELECT COUNT(*) FROM correction_events WHERE asset_id='asset-1'"
                ).fetchone()[0]
                self.assertEqual(current["value"], "person-bob")
                self.assertEqual(events, 2)


if __name__ == "__main__":
    unittest.main()
