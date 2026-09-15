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
from tools.campaign.store import ResultStore


class DryRunCliTests(unittest.TestCase):
    def test_dry_run_exits_zero_and_prints_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["campaign", "--profile", "gate2", "--dry-run", "--state-root", tmp])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["campaign_id"], "gate2-qualification-001")
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


class CorruptStoreCliTests(unittest.TestCase):
    def test_corrupted_result_store_fails_the_campaign_command_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = result_store_path("gate2-qualification-001", root=Path(tmp))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json\n", encoding="utf-8")

            class Available:
                def available(self):
                    return False

            with mock.patch.object(runner_mod, "build_provider", return_value=Available()):
                code = cli.main(["campaign", "--profile", "gate2", "--state-root", tmp])
            self.assertEqual(code, 4)


class ManifestDriftCliTests(unittest.TestCase):
    def test_changed_manifest_under_the_same_campaign_id_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fp_path = runner_mod.fingerprint_path("gate2-qualification-001", root=Path(tmp))
            fp_path.parent.mkdir(parents=True, exist_ok=True)
            fp_path.write_text("not-the-real-fingerprint", encoding="utf-8")
            code = cli.main(["campaign", "--profile", "gate2", "--dry-run", "--state-root", tmp])
            # dry-run does not check drift (it never mutates state); only a
            # real run does.
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
        # argparse never blocks on its own; this asserts the campaign
        # subcommand requires no positional/interactive-only argument beyond
        # what a script can supply on the command line.
        args = parser.parse_args(["campaign", "--profile", "gate2", "--dry-run"])
        self.assertTrue(args.dry_run)


if __name__ == "__main__":
    unittest.main()
