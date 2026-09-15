"""Report generation: from partial campaigns, and without model access."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.campaign.report import generate_report, to_markdown, write_report
from tools.campaign.runner import result_store_path
from tools.campaign.statistics import wilson_interval
from tools.campaign.store import ResultRecord, ResultStore


def _record(item_id, sample_index, status, passed, *, suite="acb-v1/routing-classification",
            candidate="cand", wall=0.5) -> ResultRecord:
    return ResultRecord(
        schema_version=1, work_unit_id=f"c:{candidate}:{suite}:{item_id}:sample{sample_index}",
        campaign_id="c", manifest_fingerprint="fp", candidate_name=candidate,
        configuration_fingerprint="cfg", suite_id=suite, suite_version="1", split="validation",
        item_id=item_id, scenario="sc", sample_index=sample_index, prompt_id="p",
        prompt_version="1.0.0", prompt_sha256="x" * 64, tool_schema_version="none",
        status=status, passed=passed, detail={}, seed=42, wall_seconds=wall,
        input_tokens=10, output_tokens=5, started_at="2026-01-01T00:00:00+00:00", host="h",
    )


class ReportFromPartialCampaignTests(unittest.TestCase):
    def test_report_generates_from_a_mix_of_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp-1", root=Path(tmp)))
            store.append(_record("i1", 0, "verified_pass", True))
            store.append(_record("i1", 1, "verified_fail", False))
            store.append(_record("i2", 0, "timeout", None))
            store.append(_record("i3", 0, "truncated", False))

            report = generate_report("camp-1", root=Path(tmp))
            self.assertEqual(report.total_records, 4)
            cell = report.cells[0]
            self.assertEqual(cell["measured_samples"], 2)
            self.assertEqual(cell["verified_pass"], 1)
            self.assertEqual(cell["verified_fail"], 1)
            self.assertEqual(cell["infrastructure_failures"], 1)
            self.assertEqual(cell["truncated"], 1)

    def test_report_on_an_empty_campaign_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = generate_report("no-such-campaign", root=Path(tmp))
            self.assertEqual(report.total_records, 0)
            self.assertEqual(report.cells, [])

    def test_wilson_ci_appears_on_the_cell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp-2", root=Path(tmp)))
            for i in range(8):
                store.append(_record(f"i{i}", 0, "verified_pass", True))
            for i in range(2):
                store.append(_record(f"j{i}", 0, "verified_fail", False))
            report = generate_report("camp-2", root=Path(tmp))
            cell = report.cells[0]
            expected = wilson_interval(8, 10)
            self.assertAlmostEqual(cell["verified_pass_rate"], expected.point_estimate)
            self.assertAlmostEqual(cell["verified_pass_rate_wilson_95ci"][0], expected.lower)

    def test_item_consistency_flags_a_flip_flopping_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp-3", root=Path(tmp)))
            store.append(_record("flaky", 0, "verified_pass", True))
            store.append(_record("flaky", 1, "verified_fail", False))
            store.append(_record("stable", 0, "verified_pass", True))
            store.append(_record("stable", 1, "verified_pass", True))
            report = generate_report("camp-3", root=Path(tmp))
            consistency = report.item_consistency["acb-v1/routing-classification"]
            self.assertEqual(consistency["items_with_inconsistent_outcomes"], 1)
            self.assertIn("flaky", consistency["inconsistent_item_ids"])


class ReportWithoutModelAccessTests(unittest.TestCase):
    """A report must regenerate from disk alone -- no provider, no network."""

    def test_generate_report_never_imports_a_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp-4", root=Path(tmp)))
            store.append(_record("i1", 0, "verified_pass", True))
            # If this touched a provider it would raise on a bogus endpoint;
            # it does not even construct one.
            report = generate_report("camp-4", root=Path(tmp))
            self.assertEqual(report.total_records, 1)

    def test_write_report_produces_valid_json_and_nonempty_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp-5", root=Path(tmp)))
            store.append(_record("i1", 0, "verified_pass", True))
            report = generate_report("camp-5", root=Path(tmp))
            out_dir = Path(tmp) / "out"
            json_path, md_path = write_report(report, out_dir=out_dir)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["campaign_id"], "camp-5")
            markdown = md_path.read_text(encoding="utf-8")
            self.assertIn("# Campaign report: camp-5", markdown)

    def test_markdown_report_reflects_qualification_when_manifest_supplied(self) -> None:
        from tools.campaign.fingerprint import Candidate, PromptRef
        from tools.campaign.manifest import CampaignManifest, SuiteSpec, SuiteThreshold

        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp-6", root=Path(tmp)))
            for i in range(10):
                store.append(_record(f"i{i}", 0, "verified_pass", True))
            manifest = CampaignManifest(
                campaign_id="camp-6", description="d",
                suites=(SuiteSpec("acb-v1/routing-classification", "validation",
                                  SuiteThreshold(min_pass_rate=0.5, min_confidence=0.5)),),
                candidates=(Candidate(
                    name="cand", model="m", runtime="ollama", endpoint="http://127.0.0.1:11434",
                    tool_schema_version="none",
                    prompts={"acb-v1/routing-classification": PromptRef("acb-v1-routing-classification", "1.0.0")},
                ),), samples_per_item=1, timeout_seconds=30, concurrency=1,
            )
            report = generate_report("camp-6", root=Path(tmp), manifest=manifest)
            self.assertEqual(len(report.qualification), 1)
            self.assertTrue(report.qualification[0]["satisfies_threshold"])
            markdown = to_markdown(report)
            self.assertIn("Qualification thresholds", markdown)


if __name__ == "__main__":
    unittest.main()
