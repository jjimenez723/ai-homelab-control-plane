"""Memory-aware candidate scheduling and approved auto-staging."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.campaign import runner as runner_mod
from tools.campaign.fingerprint import Candidate, PromptRef
from tools.campaign.manifest import CampaignManifest, SuiteSpec, SuiteThreshold


_GIB = 1024 ** 3
_MIB = 1024 ** 2


def _candidate(name: str) -> Candidate:
    return Candidate(
        name=name, model=f"{name}:latest", runtime="ollama",
        endpoint="http://127.0.0.1:11434", tool_schema_version="none",
        prompts={"acb-v1/routing-classification":
                 PromptRef("acb-v1-routing-classification", "1.0.0")},
        sampling={"temperature": 0, "seed": 42, "num_ctx": 4096, "num_predict": 64},
    )


def _manifest(*, split: str = "dev", concurrency: int = 2) -> CampaignManifest:
    return CampaignManifest(
        campaign_id=f"resource-{split}", description="resource scheduler test",
        suites=(SuiteSpec("acb-v1/routing-classification", split, SuiteThreshold()),),
        candidates=(_candidate("a"), _candidate("b")),
        samples_per_item=1, timeout_seconds=30, concurrency=concurrency,
    )


class SharedActivity:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.events: list[str] = []

    def enter(self, name: str) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.events.append(f"load:{name}")

    def leave(self, name: str) -> None:
        with self.lock:
            self.events.append(f"unload:{name}")
            self.active -= 1


class ManagedProvider:
    def __init__(self, name: str, shared: SharedActivity, *, size_bytes: int,
                 pulled: bool = False) -> None:
        self.name = name
        self.shared = shared
        self.size_bytes = size_bytes
        self.pulled = pulled
        self.loaded = False
        self.auto_pull_seen: bool | None = None

    def available(self) -> bool:
        return True

    def ensure_model(self, *, auto_pull: bool):
        self.auto_pull_seen = auto_pull
        self.shared.events.append(f"stage:{self.name}")
        return SimpleNamespace(
            digest=f"sha256:{self.name}", quantization="Q4_K_M",
            size_bytes=self.size_bytes,
        ), self.pulled

    def is_loaded(self) -> bool:
        return self.loaded

    def running_models(self):
        return ()

    def preload(self) -> None:
        self.loaded = True
        self.shared.enter(self.name)

    def unload(self) -> None:
        if self.loaded:
            self.shared.leave(self.name)
            self.loaded = False

    def generate(self, prompt, **kwargs):
        time.sleep(0.002)
        return {"output": "general", "input_tokens": 1, "output_tokens": 1}


class ResourceAwareSchedulingTests(unittest.TestCase):
    def _run(self, manifest, providers, *, memory_bytes):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                runner_mod, "build_provider",
                side_effect=lambda candidate: providers[candidate.name],
            ), mock.patch.object(
                runner_mod, "_physical_memory_bytes", return_value=memory_bytes,
            ):
                summary = runner_mod.run_campaign(manifest, root=Path(tmp))
                resolved = json.loads(
                    runner_mod.resolved_candidates_path(
                        manifest.campaign_id, root=Path(tmp)
                    ).read_text(encoding="utf-8")
                )
            return summary, resolved

    def test_large_candidates_serialize_when_two_do_not_fit_budget(self) -> None:
        shared = SharedActivity()
        providers = {
            "a": ManagedProvider("a", shared, size_bytes=3 * _GIB),
            "b": ManagedProvider("b", shared, size_bytes=3 * _GIB),
        }
        summary, _ = self._run(_manifest(concurrency=2), providers, memory_bytes=8 * _GIB)
        self.assertGreater(summary.executed, 0)
        self.assertEqual(shared.max_active, 1)
        lifecycle = [e for e in shared.events if e.startswith(("load:", "unload:"))]
        self.assertEqual(lifecycle, ["load:a", "unload:a", "load:b", "unload:b"])

    def test_small_candidates_can_run_concurrently_when_they_fit(self) -> None:
        shared = SharedActivity()
        providers = {
            "a": ManagedProvider("a", shared, size_bytes=256 * _MIB),
            "b": ManagedProvider("b", shared, size_bytes=256 * _MIB),
        }
        self._run(_manifest(concurrency=2), providers, memory_bytes=16 * _GIB)
        self.assertEqual(shared.max_active, 2)

    def test_frozen_holdout_forces_serial_candidates_even_when_memory_allows_parallel(self) -> None:
        shared = SharedActivity()
        providers = {
            "a": ManagedProvider("a", shared, size_bytes=128 * _MIB),
            "b": ManagedProvider("b", shared, size_bytes=128 * _MIB),
        }
        summary, _ = self._run(
            _manifest(split="frozen_holdout", concurrency=4),
            providers, memory_bytes=32 * _GIB,
        )
        self.assertEqual(summary.max_parallel_candidates, 1)
        self.assertEqual(shared.max_active, 1)

    def test_real_run_enables_auto_pull_and_records_resolved_identity(self) -> None:
        shared = SharedActivity()
        providers = {
            "a": ManagedProvider("a", shared, size_bytes=128 * _MIB, pulled=True),
            "b": ManagedProvider("b", shared, size_bytes=128 * _MIB),
        }
        summary, resolved = self._run(_manifest(), providers, memory_bytes=16 * _GIB)
        self.assertTrue(providers["a"].auto_pull_seen)
        self.assertTrue(providers["b"].auto_pull_seen)
        self.assertIn("a", summary.pulled_candidates)
        self.assertEqual(resolved["a"]["model_digest"], "sha256:a")
        self.assertEqual(resolved["a"]["quantization"], "Q4_K_M")


if __name__ == "__main__":
    unittest.main()
