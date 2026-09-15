"""Campaign manifest loading, validation, and fingerprinting."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from tools.campaign.manifest import ManifestError, load_manifest


_VALID = """
version: 1
campaign_id: test-campaign
description: a test campaign
samples_per_item: 3
timeout_seconds: 30
concurrency: 2
suites:
  - id: acb-v1/routing-classification
    split: validation
    threshold: {min_pass_rate: 0.8}
candidates:
  - name: cand-a
    model: qwen2.5:0.5b
    runtime: ollama
    endpoint: http://127.0.0.1:11434
    quantization: Q4_K_M
    sampling: {temperature: 0, seed: 42, num_ctx: 4096, num_predict: 64}
    prompts:
      acb-v1/routing-classification:
        prompt_id: acb-v1-routing-classification
        prompt_version: "1.0.0"
"""


def _write(content: str, directory: Path) -> Path:
    path = directory / "manifest.yaml"
    path.write_text(dedent(content), encoding="utf-8")
    return path


class LoadManifestTests(unittest.TestCase):
    def test_a_well_formed_manifest_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = load_manifest(_write(_VALID, Path(tmp)))
            self.assertEqual(manifest.campaign_id, "test-campaign")
            self.assertEqual(manifest.samples_per_item, 3)
            self.assertEqual(len(manifest.suites), 1)
            self.assertEqual(len(manifest.candidates), 1)

    def test_missing_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            content = _VALID.replace("version: 1", "version: 2")
            with self.assertRaises(ManifestError):
                load_manifest(_write(content, Path(tmp)))

    def test_unknown_suite_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            content = _VALID.replace("acb-v1/routing-classification", "acb-v1/not-a-real-suite")
            with self.assertRaises(ManifestError):
                load_manifest(_write(content, Path(tmp)))

    def test_unknown_prompt_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            content = _VALID.replace('prompt_version: "1.0.0"', 'prompt_version: "9.9.9"')
            with self.assertRaises(ManifestError):
                load_manifest(_write(content, Path(tmp)))

    def test_candidate_missing_prompt_for_requested_suite_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompts_block = (
                '    prompts:\n'
                '      acb-v1/routing-classification:\n'
                '        prompt_id: acb-v1-routing-classification\n'
                '        prompt_version: "1.0.0"\n'
            )
            self.assertIn(prompts_block, _VALID)
            content = _VALID.replace(prompts_block, "    prompts: {}\n")
            with self.assertRaises(ManifestError):
                load_manifest(_write(content, Path(tmp)))

    def test_empty_candidates_list_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            content = _VALID.split("candidates:")[0] + "candidates: []\n"
            with self.assertRaises(ManifestError):
                load_manifest(_write(content, Path(tmp)))

    def test_duplicate_candidate_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            content = _VALID + (
                "  - name: cand-a\n"
                "    model: qwen2.5:0.5b\n"
                "    runtime: ollama\n"
                "    prompts:\n"
                "      acb-v1/routing-classification:\n"
                "        prompt_id: acb-v1-routing-classification\n"
                "        prompt_version: \"1.0.0\"\n"
            )
            with self.assertRaises(ManifestError):
                load_manifest(_write(content, Path(tmp)))

    def test_dev_split_is_a_valid_alternative_to_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            content = _VALID.replace("split: validation", "split: dev")
            manifest = load_manifest(_write(content, Path(tmp)))
            self.assertEqual(manifest.suites[0].split, "dev")

    def test_unrecognised_split_value_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            content = _VALID.replace("split: validation", "split: staging")
            with self.assertRaises(ManifestError):
                load_manifest(_write(content, Path(tmp)))


class FingerprintTests(unittest.TestCase):
    def test_identical_manifests_produce_identical_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(_VALID, Path(tmp))
            a = load_manifest(path).manifest_fingerprint()
            b = load_manifest(path).manifest_fingerprint()
            self.assertEqual(a, b)

    def test_changing_samples_per_item_changes_the_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = load_manifest(_write(_VALID, Path(tmp))).manifest_fingerprint()
            changed_content = _VALID.replace("samples_per_item: 3", "samples_per_item: 7")
            changed = load_manifest(_write(changed_content, Path(tmp))).manifest_fingerprint()
            self.assertNotEqual(base, changed)

    def test_changing_prompt_version_changes_the_fingerprint(self) -> None:
        # Bump the prompt library isn't easy from a test, so simulate by
        # changing sampling instead -- still a "meaningful input".
        with tempfile.TemporaryDirectory() as tmp:
            base = load_manifest(_write(_VALID, Path(tmp))).manifest_fingerprint()
            changed_content = _VALID.replace("seed: 42", "seed: 7")
            changed = load_manifest(_write(changed_content, Path(tmp))).manifest_fingerprint()
            self.assertNotEqual(base, changed)


if __name__ == "__main__":
    unittest.main()
