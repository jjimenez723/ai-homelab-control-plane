"""Trust-boundary tests for human labels, frozen canaries, and the companion API."""

from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from tools.naming_lab.seeds import SeedStore
from tools.operator_dashboard.dashboard_cli import BIND_ADDRESS, build_command
from tools.operator_dashboard.canary import (
    AssetDescriptor, CANARY_SIZE, CanaryError, CorpusStore, FrozenCanaryStore, HumanTruth,
    TruthStore, assert_not_frozen_for_development, blind_review_payload,
    canary_records_from_truth, deterministic_select, export_training_jsonl,
    frozen_canary_report,
)
from tools.operator_dashboard.service import CompanionService, OperatorAuthorizer, ServicePolicy
from tools.semantic.evaluation import evaluate
from tools.semantic.manifests import ManifestRegistry


def _sha(number: int) -> str:
    return hashlib.sha256(f"asset-{number}".encode()).hexdigest()


def _assets(count: int = CANARY_SIZE) -> list[AssetDescriptor]:
    return [AssetDescriptor(
        content_sha256=_sha(index), original_filename=f"asset-{index}.jpg",
        media_type="image" if index % 2 else "video",
        orientation=("portrait", "landscape", "square")[index % 3],
        evidence_strength="strong" if index % 4 else "weak",
        evidence_flags=("ocr",) if index % 5 == 0 else (),
    ) for index in range(count)]


def _truth(asset: AssetDescriptor, **changes) -> HumanTruth:
    value = HumanTruth(
        content_sha256=asset.content_sha256, corpus_id="shoot-2026", split=asset.split,
        naming_action="RENAME", preferred_filename="kitchen-install.jpg",
        accepted_filenames=("kitchen-install.jpg", "countertop-install.jpg"),
        required_semantics=("kitchen",), forbidden_semantics=("guess",),
        semantic_date=None, client_id=None, project_id=None, subject="kitchen install",
        tags=("Kitchen", "installation"), media_type=asset.media_type, review_required=True,
        human_confidence=.75, additional_context_required=False,
        reason_codes=("visual_context_required",), reviewer_notes=None,
        annotator_id="operator-uid", created_at="2026-09-20T00:00:00+00:00",
    )
    return replace(value, **changes)


class CanaryModelTests(unittest.TestCase):
    def test_selection_is_reproducible_and_never_filename_first(self):
        assets = list(reversed(_assets(124)))
        first = deterministic_select(assets, seed="pilot-seed")
        second = deterministic_select(_assets(124), seed="pilot-seed")
        self.assertEqual([item.content_sha256 for item in first], [item.content_sha256 for item in second])
        self.assertEqual(len(first), 100)
        self.assertGreater(len({item.stratum for item in first}), 3)

    def test_selection_fails_cleanly_when_corpus_is_small(self):
        with self.assertRaisesRegex(CanaryError, "need at least 100"):
            deterministic_select(_assets(99), seed="pilot")

    def test_frozen_canary_cannot_become_seed_or_training_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canaries = FrozenCanaryStore(root / "canaries")
            frozen = canaries.freeze(canary_id="gate3-shoot", corpus_id="shoot-2026", assets=_assets(), seed="s", selection_rules="stratified")
            target = frozen.entries[0]
            with self.assertRaises(CanaryError):
                assert_not_frozen_for_development(target.content_sha256, canaries)
            with self.assertRaises(CanaryError):
                SeedStore(root / "seeds", frozen_canary_root=root / "canaries").get_or_create(
                    sha256=target.content_sha256, original_filename="moved.jpg", media_kind="image", extension="jpg", semantic_date=None)
            with self.assertRaises(CanaryError):
                export_training_jsonl(
                    [_truth(target, split="frozen_canary")],
                    root / "training.jsonl",
                    canaries=canaries,
                )

    def test_corpus_types_are_separate_and_gate3_refuses_general_or_overlarge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = CorpusStore(root / "corpora", "general-files")
            general.create(corpus_kind="general_file_intelligence", purpose="ordinary files")
            general.import_descriptors(_assets())
            with self.assertRaisesRegex(CanaryError, "coherent_client"):
                general.freeze_gate3(FrozenCanaryStore(root / "canaries"), canary_id="wrong", seed="s")
            coherent = CorpusStore(root / "corpora", "client-shoot")
            coherent.create(corpus_kind="coherent_client", purpose="real shoot")
            coherent.import_descriptors(_assets())
            frozen = coherent.freeze_gate3(FrozenCanaryStore(root / "canaries"), canary_id="right", seed="s")
            self.assertEqual(len(frozen.entries), 100)

    def test_locks_are_content_addressed_and_auditable(self):
        with tempfile.TemporaryDirectory() as temp:
            asset = _assets(1)[0]
            store = TruthStore(Path(temp))
            truth = _truth(asset)
            store.lock(truth)
            self.assertEqual(store.locked(asset.content_sha256).accepted_filenames, truth.accepted_filenames)
            with self.assertRaisesRegex(CanaryError, "locked"):
                store.lock(replace(truth, preferred_filename="renamed-after-move.jpg"))
            self.assertEqual(store.locked(asset.content_sha256).asset_id, f"sha256:{asset.content_sha256}")

    def test_blind_payload_hides_model_until_lock(self):
        asset = _assets(1)[0]
        proposal = {"client_id": "wrong", "tags": ["wrong"]}
        before = blind_review_payload(asset, truth=None, model_result=proposal, evidence={"text": "safe"})
        after = blind_review_payload(asset, truth=_truth(asset), model_result=proposal, evidence={"text": "safe"})
        self.assertNotIn("model_comparison", before)
        self.assertEqual(after["model_comparison"], proposal)

    def test_versioning_unknowns_and_evaluator_adapter(self):
        asset = _assets(1)[0]
        truth = _truth(asset, client_id=None, project_id=None, tags=("kitchen",))
        self.assertEqual(truth.label_schema_version, "angel-human-label/v1")
        self.assertEqual(truth.taxonomy_version, "angel-taxonomy/v1")
        report = evaluate(canary_records_from_truth([truth], {asset.content_sha256: {"tags": ["kitchen"]}}))
        self.assertEqual(report.unknown_or_review, 1)
        self.assertEqual(report.tag_precision, 1.0)

    def test_manifest_validation_rejects_noncanonical_ids_but_unknown_is_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "clients" / "acme"; root.mkdir(parents=True)
            (root / "manifest.yaml").write_text("client_id: acme\ndisplay_name: Acme\nprojects:\n - project_id: launch\n   display_name: Launch\n", encoding="utf-8")
            registry = ManifestRegistry.load(root.parent)
            store = TruthStore(Path(temp) / "truth")
            asset = _assets(1)[0]
            store.lock(_truth(asset), registry=registry)  # explicit unknown is representable
            with self.assertRaisesRegex(CanaryError, "unknown client"):
                store.lock(_truth(_assets(2)[1], client_id="made-up"), registry=registry)


    def test_freeze_seals_candidate_pool_and_prevents_membership_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus = CorpusStore(root / "corpora", "shoot-2026")
            corpus.create(corpus_kind="coherent_client", purpose="real shoot")
            corpus.import_descriptors(_assets(124))
            frozen = corpus.freeze_gate3(
                FrozenCanaryStore(root / "canaries"),
                canary_id="gate3-shoot",
                seed="s",
            )
            metadata = corpus.metadata()
            self.assertEqual(frozen.candidate_pool_count, 124)
            self.assertEqual(metadata["candidate_pool_sha256"], frozen.candidate_pool_sha256)
            self.assertEqual(metadata["sealed_canary_id"], "gate3-shoot")
            with self.assertRaisesRegex(CanaryError, "sealed"):
                corpus.import_descriptors([_assets(125)[124]])

    def test_actual_dashboard_style_frozen_truth_cannot_export_even_if_declared_development(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canaries = FrozenCanaryStore(root / "canaries")
            corpus = CorpusStore(root / "corpora", "shoot-2026")
            corpus.create(corpus_kind="coherent_client", purpose="real shoot")
            originals = _assets()
            corpus.import_descriptors(originals)
            frozen = corpus.freeze_gate3(canaries, canary_id="gate3-shoot", seed="s")
            target = next(
                asset for asset in originals
                if asset.content_sha256 == frozen.entries[0].content_sha256
            )
            self.assertEqual(target.split, "development")
            with self.assertRaisesRegex(CanaryError, "frozen/holdout"):
                export_training_jsonl(
                    [_truth(target)],
                    root / "training.jsonl",
                    canaries=canaries,
                )

    def test_contains_verifies_manifest_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canaries = FrozenCanaryStore(root / "canaries")
            frozen = canaries.freeze(
                canary_id="gate3-shoot",
                corpus_id="shoot-2026",
                assets=_assets(),
                seed="s",
                selection_rules="stratified",
            )
            manifest = root / "canaries" / "gate3-shoot" / "manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["selection_seed"] = "tampered"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(CanaryError, "digest mismatch"):
                canaries.contains(frozen.entries[0].content_sha256)

    def test_local_path_index_rehashes_source_before_preview_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus = CorpusStore(root / "corpora", "shoot-2026")
            corpus.create(corpus_kind="coherent_client", purpose="paths")
            asset = _assets(1)[0]
            good = root / "asset.jpg"
            good.write_bytes(b"asset-0")
            corpus.import_descriptors([asset], local_paths={asset.content_sha256: good})
            self.assertEqual(corpus.local_path_for(asset.content_sha256), good.resolve())
            bad = root / "wrong.jpg"
            bad.write_bytes(b"wrong")
            with self.assertRaisesRegex(CanaryError, "do not match"):
                corpus.import_descriptors([asset], local_paths={asset.content_sha256: bad})

    def test_holdout_comparison_stays_hidden_after_truth_lock(self):
        asset = _assets(1)[0]
        proposal = {"client_id": "wrong", "tags": ["wrong"]}
        payload = blind_review_payload(
            asset,
            truth=_truth(asset),
            model_result=proposal,
            evidence={},
            reveal_model_comparison=False,
        )
        self.assertNotIn("model_comparison", payload)
        self.assertTrue(payload["model_comparison_withheld"])

    def test_truth_corrections_are_append_only_and_audited(self):
        with tempfile.TemporaryDirectory() as temp:
            asset = _assets(1)[0]
            store = TruthStore(Path(temp))
            original = _truth(asset)
            store.lock(original)
            corrected = replace(
                original,
                preferred_filename="corrected.jpg",
                created_at="2026-09-20T01:00:00+00:00",
            )
            store.correct(
                corrected,
                actor_id="operator-uid",
                reason="reviewed source evidence",
            )
            self.assertEqual(
                store.locked(asset.content_sha256).preferred_filename,
                "corrected.jpg",
            )
            self.assertTrue(store.corrections_path.exists())
            with self.assertRaisesRegex(CanaryError, "actor and reason"):
                store.correct(original, actor_id="", reason="")

    def test_manifest_bound_gate3_report_requires_all_truth_and_rejects_extras(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canaries = FrozenCanaryStore(root / "canaries")
            frozen = canaries.freeze(
                canary_id="gate3-shoot",
                corpus_id="shoot-2026",
                assets=_assets(),
                seed="s",
                selection_rules="stratified",
            )
            truth_store = TruthStore(root / "truth")
            with self.assertRaisesRegex(CanaryError, "100 unlabeled"):
                frozen_canary_report(
                    canaries=canaries,
                    canary_id=frozen.canary_id,
                    truth_store=truth_store,
                    predictions={},
                    run_identity="model@prompt",
                )
            originals = {asset.content_sha256: asset for asset in _assets()}
            for entry in frozen.entries:
                truth_store.lock(_truth(originals[entry.content_sha256]))
            report = frozen_canary_report(
                canaries=canaries,
                canary_id=frozen.canary_id,
                truth_store=truth_store,
                predictions={},
                run_identity="model@prompt",
            )
            self.assertEqual(report["canary_manifest_sha256"], frozen.manifest_sha256)
            self.assertEqual(report["metrics"]["assets"], 100)
            with self.assertRaisesRegex(CanaryError, "outside frozen canary"):
                frozen_canary_report(
                    canaries=canaries,
                    canary_id=frozen.canary_id,
                    truth_store=truth_store,
                    predictions={_sha(999): {"content_sha256": _sha(999)}},
                    run_identity="model@prompt",
                )


class CompanionServiceTests(unittest.TestCase):
    def setUp(self):
        policy = ServicePolicy(allowed_origins=frozenset({"https://angel-508305.web.app"}), operator_subjects=frozenset({"uid-good"}))
        authorizer = OperatorAuthorizer(policy=policy, verify_bearer=lambda token: {"good": "uid-good", "other": "uid-other"}[token])
        self.service = CompanionService("127.0.0.1", 0, authorizer=authorizer, overview=lambda: {"assets": 0})
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.service.shutdown(); self.service.server_close(); self.thread.join()

    def _request(self, headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.service.server_port)
        connection.request("GET", "/v1/overview", headers=headers)
        response = connection.getresponse(); body = response.read(); connection.close()
        return response, body

    def test_rejects_unauthorized_and_cors_is_exact(self):
        response, _ = self._request({"Origin": "https://angel-508305.web.app", "Authorization": "Bearer other"})
        self.assertEqual(response.status, 401)
        response, _ = self._request({"Origin": "https://evil.example", "Authorization": "Bearer good"})
        self.assertEqual(response.status, 403)
        response, _ = self._request({"Origin": "https://angel-508305.web.app", "Authorization": "Bearer good"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "https://angel-508305.web.app")

    def test_loopback_and_wildcard_protections(self):
        with self.assertRaises(ValueError):
            ServicePolicy(allowed_origins=frozenset({"*"}), operator_subjects=frozenset({"uid"}))
        with self.assertRaises(ValueError):
            CompanionService("0.0.0.0", 8765, authorizer=self.service.authorizer, overview=lambda: {})

    def test_local_dashboard_binds_loopback_and_hosted_page_has_no_bridge(self):
        command = build_command(Path("/tmp/operator-state"), "shoot")
        self.assertIn(BIND_ADDRESS, command)
        self.assertIn("--clients-root", command)
        source = Path("tools/operator_dashboard/app.py").read_text(encoding="utf-8")
        self.assertIn("ManifestRegistry.load", source)
        self.assertIn("registry=registry", source)
        page = Path("ops/angel-oauth-site/public/app.html").read_text(encoding="utf-8")
        self.assertIn("Sign in with Google", page)
        self.assertIn("OPERATOR_ALLOWLIST", page)
        self.assertNotIn("127.0.0.1:", page)
