"""Gate-2 failure analysis: categories, filters, and output provenance."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.bench.gate2_failure_analysis import generate_failure_analysis, to_markdown
from tools.campaign.runner import result_store_path
from tools.campaign.store import ResultRecord, ResultStore


def _record(*, candidate="qwen", suite="acb-v1/routing-classification", item="i1",
            sample=0, status="verified_fail", passed=False, detail=None,
            raw_output=None, finish_reason=None, schema_version=2):
    return ResultRecord(
        schema_version=schema_version,
        work_unit_id=f"c:{candidate}:{suite}:{item}:sample{sample}",
        campaign_id="camp", manifest_fingerprint="fp", candidate_name=candidate,
        configuration_fingerprint="cfg", suite_id=suite, suite_version="1",
        split="validation", item_id=item, scenario="scenario", sample_index=sample,
        prompt_id="prompt", prompt_version="1.0.0", prompt_sha256="x" * 64,
        tool_schema_version="none", status=status, passed=passed, detail=detail or {},
        seed=42, wall_seconds=0.1, input_tokens=10, output_tokens=4,
        started_at="2026-01-01T00:00:00+00:00", host="h",
        raw_output=raw_output, finish_reason=finish_reason,
    )


class FailureAnalysisTests(unittest.TestCase):
    def test_schema_v2_prefers_real_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp", root=Path(tmp)))
            store.append(_record(
                detail={"expected": "code.debug", "got": "general"},
                raw_output="GENERAL\n", finish_reason="stop",
            ))
            analysis = generate_failure_analysis("camp", root=Path(tmp))
            row = analysis["records"][0]
            self.assertEqual(row["output"], "GENERAL\n")
            self.assertEqual(row["output_provenance"], "raw_output")
            self.assertEqual(row["failure_reason"], "wrong_label")
            self.assertEqual(row["finish_reason"], "stop")

    def test_old_routing_row_recovers_normalized_got_but_labels_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp", root=Path(tmp)))
            store.append(_record(
                schema_version=1,
                detail={"expected": "code.debug", "got": "general"},
                raw_output=None,
            ))
            analysis = generate_failure_analysis("camp", root=Path(tmp))
            row = analysis["records"][0]
            self.assertEqual(row["output"], "general")
            self.assertEqual(row["output_provenance"], "verifier_detail.got")
            self.assertFalse(row["raw_output_available"])

    def test_old_naming_filename_is_not_mislabeled_as_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp", root=Path(tmp)))
            store.append(_record(
                suite="acb-v1/semantic-naming",
                detail={"failure": "missing_required_date", "filename": "Northwind-invoice.pdf"},
                raw_output=None,
            ))
            analysis = generate_failure_analysis("camp", root=Path(tmp))
            row = analysis["records"][0]
            self.assertEqual(row["failure_reason"], "missing_required_date")
            self.assertEqual(row["output"], "Northwind-invoice.pdf")
            self.assertEqual(row["output_provenance"], "verifier_detail.filename_normalized_not_raw")

    def test_filters_limit_candidates_and_suites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(result_store_path("camp", root=Path(tmp)))
            store.append(_record(candidate="a"))
            store.append(_record(candidate="b"))
            store.append(_record(candidate="a", suite="acb-v1/semantic-naming", item="n1"))
            analysis = generate_failure_analysis(
                "camp", root=Path(tmp), candidates=["a"], suites=["acb-v1/semantic-naming"],
            )
            self.assertEqual(analysis["records_analyzed"], 1)
            self.assertEqual(analysis["records"][0]["candidate"], "a")
            self.assertEqual(analysis["records"][0]["suite_id"], "acb-v1/semantic-naming")

    def test_markdown_states_schema_v1_limitation(self) -> None:
        analysis = {
            "campaign_id": "camp",
            "records_analyzed": 0,
            "total_records_in_store": 0,
            "limitations": ["Schema-v1 campaign rows did not persist raw model output."],
            "cells": [],
            "records": [],
        }
        markdown = to_markdown(analysis)
        self.assertIn("did not persist raw model output", markdown)


if __name__ == "__main__":
    unittest.main()
