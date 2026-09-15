"""Campaign runner: dry-run, resume, drift detection, smoke, availability."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.campaign.fingerprint import Candidate, PromptRef
from tools.campaign.manifest import CampaignManifest, SuiteSpec, SuiteThreshold
from tools.campaign.store import ResultStore
from tools.campaign import runner as runner_mod


def _candidate(name="cand", seed=42) -> Candidate:
    return Candidate(
        name=name, model="fake-model", runtime="ollama", endpoint="http://127.0.0.1:11434",
        tool_schema_version="none",
        prompts={"acb-v1/routing-classification": PromptRef("acb-v1-routing-classification", "1.0.0")},
        sampling={"temperature": 0, "seed": seed, "num_ctx": 4096, "num_predict": 64},
    )


def _manifest(*, campaign_id="test-run", samples=1, candidates=None, concurrency=1) -> CampaignManifest:
    return CampaignManifest(
        campaign_id=campaign_id, description="d",
        suites=(SuiteSpec("acb-v1/routing-classification", "dev", SuiteThreshold(min_pass_rate=0.0)),),
        candidates=candidates or (_candidate(),), samples_per_item=samples,
        timeout_seconds=30, concurrency=concurrency,
    )


class FakeAvailableProvider:
    def __init__(self, *args, **kwargs):
        pass

    def available(self) -> bool:
        return True

    def generate(self, prompt, **kwargs):
        return {"output": "general", "input_tokens": 1, "output_tokens": 1}


class FakeUnavailableProvider(FakeAvailableProvider):
    def available(self) -> bool:
        return False


class DryRunTests(unittest.TestCase):
    def test_dry_run_never_calls_build_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest()
            with mock.patch.object(runner_mod, "build_provider") as build:
                report = runner_mod.dry_run(manifest, root=Path(tmp))
            build.assert_not_called()
            self.assertGreater(report.total_work_units, 0)
            self.assertEqual(report.already_complete, 0)
            self.assertEqual(report.would_run, report.total_work_units)

    def test_dry_run_reflects_already_completed_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest()
            with mock.patch.object(runner_mod, "build_provider", return_value=FakeAvailableProvider()):
                runner_mod.run_campaign(manifest, root=Path(tmp))
            report = runner_mod.dry_run(manifest, root=Path(tmp))
            self.assertEqual(report.already_complete, report.total_work_units)
            self.assertEqual(report.would_run, 0)


class ManifestDriftTests(unittest.TestCase):
    def test_first_run_records_the_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest()
            runner_mod.check_manifest_drift(manifest, root=Path(tmp))
            path = runner_mod.fingerprint_path(manifest.campaign_id, root=Path(tmp))
            self.assertTrue(path.exists())

    def test_a_changed_manifest_under_the_same_campaign_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = _manifest(samples=1)
            runner_mod.check_manifest_drift(first, root=Path(tmp))
            second = _manifest(samples=2)
            with self.assertRaises(runner_mod.ManifestDrift):
                runner_mod.check_manifest_drift(second, root=Path(tmp))

    def test_drift_is_allowed_when_explicitly_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = _manifest(samples=1)
            runner_mod.check_manifest_drift(first, root=Path(tmp))
            second = _manifest(samples=2)
            runner_mod.check_manifest_drift(second, root=Path(tmp), allow_manifest_change=True)  # no raise


class CandidateAvailabilityTests(unittest.TestCase):
    def test_unavailable_candidate_is_skipped_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest()
            with mock.patch.object(runner_mod, "build_provider", return_value=FakeUnavailableProvider()):
                summary = runner_mod.run_campaign(manifest, root=Path(tmp))
            self.assertEqual(summary.executed, 0)
            self.assertIn("cand", summary.skipped_candidates)

    def test_unavailable_candidate_raises_under_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest()
            with mock.patch.object(runner_mod, "build_provider", return_value=FakeUnavailableProvider()):
                with self.assertRaises(RuntimeError):
                    runner_mod.run_campaign(manifest, root=Path(tmp), strict_candidates=True)

    def test_available_candidate_runs_normally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest()
            with mock.patch.object(runner_mod, "build_provider", return_value=FakeAvailableProvider()):
                summary = runner_mod.run_campaign(manifest, root=Path(tmp))
            self.assertGreater(summary.executed, 0)
            self.assertEqual(summary.skipped_candidates, [])


class ResumeTests(unittest.TestCase):
    def test_interrupted_run_resumes_rather_than_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest(samples=2)
            calls = []

            class CountingProvider(FakeAvailableProvider):
                def generate(self, prompt, **kwargs):
                    calls.append(1)
                    return super().generate(prompt, **kwargs)

            with mock.patch.object(runner_mod, "build_provider", return_value=CountingProvider()):
                first = runner_mod.run_campaign(manifest, root=Path(tmp))
                total = first.total_work_units
                self.assertEqual(len(calls), total)
                second = runner_mod.run_campaign(manifest, root=Path(tmp))
                # Second run must not re-execute anything already terminal.
                self.assertEqual(second.executed, 0)
                self.assertEqual(second.skipped_already_done, total)
                self.assertEqual(len(calls), total)  # no new provider calls

    def test_no_resume_flag_recomputes_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest(samples=1)
            with mock.patch.object(runner_mod, "build_provider", return_value=FakeAvailableProvider()):
                runner_mod.run_campaign(manifest, root=Path(tmp))
                second = runner_mod.run_campaign(manifest, root=Path(tmp), resume=False)
            self.assertGreater(second.executed, 0)

    def test_results_are_never_overwritten_only_appended(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest(samples=1)
            with mock.patch.object(runner_mod, "build_provider", return_value=FakeAvailableProvider()):
                runner_mod.run_campaign(manifest, root=Path(tmp))
                store = ResultStore(runner_mod.result_store_path(manifest.campaign_id, root=Path(tmp)))
                n_after_first = len(store.read_all())
                runner_mod.run_campaign(manifest, root=Path(tmp), resume=False)
                n_after_second = len(store.read_all())
            self.assertEqual(n_after_second, n_after_first * 2)


class SmokeTests(unittest.TestCase):
    def test_smoke_runs_only_one_item_per_candidate_suite_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest(samples=5)
            with mock.patch.object(runner_mod, "build_provider", return_value=FakeAvailableProvider()):
                summary = runner_mod.run_campaign(manifest, root=Path(tmp), smoke=True)
            self.assertEqual(summary.executed, 1)


class ConcurrencyTests(unittest.TestCase):
    def test_higher_concurrency_still_executes_every_work_unit_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest(samples=3, concurrency=4)
            with mock.patch.object(runner_mod, "build_provider", return_value=FakeAvailableProvider()):
                summary = runner_mod.run_campaign(manifest, root=Path(tmp), max_workers=4)
            store = ResultStore(runner_mod.result_store_path(manifest.campaign_id, root=Path(tmp)))
            records = store.read_all()
            self.assertEqual(len(records), summary.total_work_units)
            self.assertEqual(len({r.work_unit_id for r in records}), len(records))


if __name__ == "__main__":
    unittest.main()
