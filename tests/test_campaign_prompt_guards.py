"""D-059: guards that run before any inference does.

Two invariants the campaign runner now enforces itself instead of trusting
whoever wrote the manifest:

* a pinned prompt may not quote frozen-holdout content -- the D-052 guard
  `tools.bench.suite.assert_no_holdout_leak` was previously only ever run by
  tests, and only against whichever prompt version happened to be `active`,
  while a campaign may pin any version including a draft with few-shot text;
* two candidates that share one Ollama model tag are never resident at the
  same time -- a prompt-variant experiment has several candidates over one
  model, and each candidate batch unloads that tag when it finishes, which
  would evict a concurrent sibling's weights mid-batch.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.bench.acb_v1 import semantic_naming as sn
from tools.bench.suite import ContaminationError
from tools.campaign import runner as runner_mod
from tools.campaign.fingerprint import Candidate, PromptRef
from tools.campaign.manifest import CampaignManifest, SuiteSpec, SuiteThreshold
from tools.prompts.library import PROMPT_VERSIONS
from tools.prompts.registry import PromptRegistry, PromptVersion


def _candidate(name: str, model: str, prompt_version: str = "1.0.0") -> Candidate:
    return Candidate(
        name=name, model=model, runtime="ollama", endpoint="http://127.0.0.1:11434",
        tool_schema_version="none",
        prompts={"acb-v1/semantic-naming": PromptRef("acb-v1-semantic-naming", prompt_version)},
        sampling={"temperature": 0, "seed": 42, "num_ctx": 4096, "num_predict": 128},
    )


def _manifest(candidates, *, campaign_id="guard-test", concurrency=4) -> CampaignManifest:
    return CampaignManifest(
        campaign_id=campaign_id, description="d",
        suites=(SuiteSpec("acb-v1/semantic-naming", "dev", SuiteThreshold()),),
        candidates=tuple(candidates), samples_per_item=1, timeout_seconds=30,
        concurrency=concurrency,
    )


class ContaminationGuardTests(unittest.TestCase):
    def test_a_clean_manifest_passes_the_guard(self) -> None:
        runner_mod.check_prompt_contamination(_manifest([_candidate("a", "m1")]))

    def test_every_shipped_prompt_version_is_clean(self) -> None:
        for prompt in PROMPT_VERSIONS:
            if prompt.prompt_id != "acb-v1-semantic-naming":
                continue
            runner_mod.check_prompt_contamination(
                _manifest([_candidate("a", "m1", prompt_version=prompt.version)]))

    def test_a_prompt_quoting_holdout_evidence_is_refused_before_inference(self) -> None:
        holdout = sn.SUITE.frozen_holdout()[0]
        leaked = PromptVersion(
            prompt_id="acb-v1-semantic-naming", version="9.9.9-leaky", status="draft",
            capability="naming.semantic",
            policy_template="policy", role_template="role",
            task_template="Example: " + holdout.input["evidence_text"] + "\n{task} {original_filename}",
            output_schema="{output_schema}", tool_schema_version="none", renderer_version="1",
        )
        registry = PromptRegistry(list(PROMPT_VERSIONS) + [leaked])
        manifest = _manifest([_candidate("a", "m1", prompt_version="9.9.9-leaky")])
        with mock.patch.object(runner_mod, "load_default_registry", return_value=registry):
            with self.assertRaises(ContaminationError):
                runner_mod.check_prompt_contamination(manifest)

    def test_the_guard_runs_on_dry_run_too(self) -> None:
        manifest = _manifest([_candidate("a", "m1")])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(runner_mod, "check_prompt_contamination") as guard:
                runner_mod.dry_run(manifest, root=Path(tmp))
            guard.assert_called_once()

    def test_the_guard_runs_before_a_model_is_staged(self) -> None:
        manifest = _manifest([_candidate("a", "m1")])
        order: list[str] = []

        def note_guard(_manifest):
            order.append("guard")

        def note_stage(_manifest, **_kwargs):
            order.append("stage")
            return [], []

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(runner_mod, "check_prompt_contamination", note_guard), \
                 mock.patch.object(runner_mod, "_stage_candidates", note_stage):
                runner_mod.run_campaign(manifest, root=Path(tmp))
        self.assertEqual(order, ["guard", "stage"])


class SharedModelTagSerializationTests(unittest.TestCase):
    class TrackingProvider:
        """Records concurrent residency per model tag."""

        live: dict[str, int] = {}
        overlaps: list[str] = []

        def __init__(self, model: str):
            self.model = model

        def available(self) -> bool:
            return True

        def preload(self) -> None:
            count = self.live.get(self.model, 0) + 1
            self.live[self.model] = count
            if count > 1:
                self.overlaps.append(self.model)

        def unload(self) -> None:
            self.live[self.model] = max(0, self.live.get(self.model, 0) - 1)

        def is_loaded(self) -> bool:
            return bool(self.live.get(self.model))

        def generate(self, prompt, **kwargs):
            return {"output": "x", "input_tokens": 1, "output_tokens": 1}

    def test_two_arms_of_one_model_never_run_concurrently(self) -> None:
        self.TrackingProvider.live = {}
        self.TrackingProvider.overlaps = []
        candidates = [
            _candidate("arm-a", "same-model", "1.0.0"),
            _candidate("arm-b", "same-model", "1.1.0-a"),
            _candidate("arm-c", "same-model", "1.1.0-b"),
            _candidate("other", "other-model", "1.0.0"),
        ]
        manifest = _manifest(candidates, campaign_id="tag-test", concurrency=4)

        def build(candidate):
            return self.TrackingProvider(candidate.model)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(runner_mod, "build_provider", build), \
                 mock.patch.object(runner_mod, "_physical_memory_bytes", return_value=64 * 1024 ** 3):
                summary = runner_mod.run_campaign(manifest, root=Path(tmp))
        self.assertEqual(self.TrackingProvider.overlaps, [])
        self.assertGreater(summary.executed, 0)
        self.assertEqual(summary.status_counts.get("provider_error", 0), 0)


if __name__ == "__main__":
    unittest.main()
