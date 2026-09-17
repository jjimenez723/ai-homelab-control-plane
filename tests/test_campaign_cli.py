"""angel-eval CLI: exit codes, dry-run purity, no interactive prompts."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools.campaign import cli
from tools.campaign import runner as runner_mod
from tools.campaign.runner import result_store_path
from tools.campaign.store import ResultRecord, ResultStore


class DryRunCliTests(unittest.TestCase):
    def test_dry_run_exits_zero_and_prints_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["campaign", "--profile", "gate2", "--dry-run", "--state-root", tmp])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["campaign_id"], "gate2-validation-001")
            self.assertGreater(payload["total_work_units"], 0)

    def test_unknown_profile_exits_nonzero_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(["campaign", "--profile", "no-such-profile", "--state-root", tmp])
            self.assertEqual(code, 2)


class ReportCliTests(unittest.TestCase):
    def test_report_command_writes_files_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(["report", "empty-campaign", "--state-root", tmp])
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "empty-campaign" / "report" / "report.json").exists())
            self.assertTrue((Path(tmp) / "empty-campaign" / "report" / "report.md").exists())


class AuditCliTests(unittest.TestCase):
    def test_audit_command_writes_human_and_machine_readable_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["audit", "--out", tmp])
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "gate2-contract-audit.json").exists())
            self.assertTrue((Path(tmp) / "gate2-contract-audit.md").exists())
            payload = json.loads((Path(tmp) / "gate2-contract-audit.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["frozen_holdout_content_included"])
            self.assertGreater(payload["summary"]["errors"], 0)

    def test_audit_strict_exits_nonzero_while_current_contract_has_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(["audit", "--out", tmp, "--strict"])
            self.assertEqual(code, 1)

    def test_frozen_holdout_requires_explicit_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(["audit", "--out", tmp, "--split", "frozen_holdout"])
            self.assertEqual(code, 2)
            self.assertFalse((Path(tmp) / "gate2-contract-audit.json").exists())


class AnalyzeCliTests(unittest.TestCase):
    def test_analyze_command_writes_failure_analysis_without_model_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp", root=Path(tmp)))
            store.append(ResultRecord(
                schema_version=2, work_unit_id="u", campaign_id="camp", manifest_fingerprint="fp",
                candidate_name="qwen", configuration_fingerprint="cfg",
                suite_id="acb-v1/routing-classification", suite_version="1", split="validation",
                item_id="item", scenario="sc", sample_index=0, prompt_id="p",
                prompt_version="1.0.0", prompt_sha256="x" * 64, tool_schema_version="none",
                status="verified_fail", passed=False,
                detail={"expected": "code.debug", "got": "general"}, seed=42,
                wall_seconds=0.1, input_tokens=10, output_tokens=1,
                started_at="2026-01-01T00:00:00+00:00", host="h",
                raw_output="general", finish_reason="stop",
            ))
            code = cli.main(["analyze", "camp", "--state-root", tmp])
            self.assertEqual(code, 0)
            out = Path(tmp) / "camp" / "failure-analysis"
            self.assertTrue((out / "failure-analysis.json").exists())
            self.assertTrue((out / "failure-analysis.md").exists())
            payload = json.loads((out / "failure-analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["records_analyzed"], 1)
            self.assertEqual(payload["records"][0]["failure_reason"], "wrong_label")


class CorruptStoreCliTests(unittest.TestCase):
    def test_corrupted_result_store_fails_the_campaign_command_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = result_store_path("gate2-validation-001", root=Path(tmp))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json\n", encoding="utf-8")

            class Available:
                def available(self):
                    return False

            with mock.patch.object(runner_mod, "build_provider", return_value=Available()):
                code = cli.main(["campaign", "--profile", "gate2", "--state-root", tmp])
            self.assertEqual(code, 4)

    def test_corrupted_result_store_fails_analyze_command_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = result_store_path("camp", root=Path(tmp))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json\n", encoding="utf-8")
            code = cli.main(["analyze", "camp", "--state-root", tmp])
            self.assertEqual(code, 4)


class ManifestDriftCliTests(unittest.TestCase):
    def test_changed_manifest_under_the_same_campaign_id_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fp_path = runner_mod.fingerprint_path("gate2-validation-001", root=Path(tmp))
            fp_path.parent.mkdir(parents=True, exist_ok=True)
            fp_path.write_text("not-the-real-fingerprint", encoding="utf-8")
            code = cli.main(["campaign", "--profile", "gate2", "--dry-run", "--state-root", tmp])
            self.assertEqual(code, 0)

            class Available:
                def available(self):
                    return False

            with mock.patch.object(runner_mod, "build_provider", return_value=Available()):
                code = cli.main(["campaign", "--profile", "gate2", "--state-root", tmp])
            self.assertEqual(code, 3)


class NoInteractivePromptsTests(unittest.TestCase):
    """The CLI must never block on stdin during an ordinary run."""

    def test_parser_has_no_required_interactive_confirmation_flags(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["campaign", "--profile", "gate2", "--dry-run"])
        self.assertTrue(args.dry_run)


if __name__ == "__main__":
    unittest.main()
