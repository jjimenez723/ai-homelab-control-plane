"""Append-only result store: durability, corruption handling, resumability."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.campaign.store import CorruptResultRecord, ResultRecord, ResultStore


def _record(work_unit_id: str, status: str, *, passed: bool | None = None,
            raw_output: str | None = None, finish_reason: str | None = None) -> ResultRecord:
    return ResultRecord(
        schema_version=2, work_unit_id=work_unit_id, campaign_id="c", manifest_fingerprint="fp",
        candidate_name="cand", configuration_fingerprint="cfg", suite_id="s", suite_version="1",
        split="dev", item_id="item-1", scenario="sc", sample_index=0, prompt_id="p",
        prompt_version="1.0.0", prompt_sha256="x" * 64, tool_schema_version="none", status=status,
        passed=passed, detail={}, seed=None, wall_seconds=0.1, input_tokens=1, output_tokens=1,
        started_at="2026-01-01T00:00:00+00:00", host="test-host",
        raw_output=raw_output, finish_reason=finish_reason,
    )


class ResultRecordTests(unittest.TestCase):
    def test_invalid_status_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _record("u1", "not-a-real-status")

    def test_terminal_statuses_are_marked_terminal(self) -> None:
        for status in ("verified_pass", "verified_fail", "truncated", "invalid_output", "unverified"):
            self.assertTrue(_record("u", status).is_terminal, status)

    def test_retryable_statuses_are_not_terminal(self) -> None:
        for status in ("timeout", "provider_unavailable", "provider_error",
                       "sandbox_error", "verifier_error", "cancelled"):
            self.assertFalse(_record("u", status).is_terminal, status)


class ResultStoreTests(unittest.TestCase):
    def test_reading_a_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp) / "sub" / "results.jsonl")
            self.assertEqual(store.read_all(), [])
            self.assertEqual(store.completed_work_unit_ids(), set())

    def test_append_then_read_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp) / "results.jsonl")
            store.append(_record("u1", "verified_pass", passed=True))
            store.append(_record("u2", "verified_fail", passed=False))
            records = store.read_all()
            self.assertEqual([r.work_unit_id for r in records], ["u1", "u2"])

    def test_raw_output_and_finish_reason_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp) / "results.jsonl")
            store.append(_record(
                "u1", "verified_fail", passed=False,
                raw_output="Northwind invoice.pdf", finish_reason="stop",
            ))
            record = store.read_all()[0]
            self.assertEqual(record.raw_output, "Northwind invoice.pdf")
            self.assertEqual(record.finish_reason, "stop")

    def test_schema_v1_row_remains_readable_with_new_optional_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            payload = _record("u1", "verified_pass", passed=True).as_dict()
            payload["schema_version"] = 1
            payload.pop("raw_output")
            payload.pop("finish_reason")
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            record = ResultStore(path).read_all()[0]
            self.assertEqual(record.schema_version, 1)
            self.assertIsNone(record.raw_output)
            self.assertIsNone(record.finish_reason)

    def test_appending_never_rewrites_prior_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            store = ResultStore(path)
            store.append(_record("u1", "verified_pass", passed=True))
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
            store.append(_record("u2", "verified_fail", passed=False))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], first_line)
            self.assertEqual(len(lines), 2)

    def test_duplicate_sample_records_for_the_same_item_are_both_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp) / "results.jsonl")
            store.append(_record("campaign:cfg:s:item-1:sample0", "verified_pass", passed=True))
            store.append(_record("campaign:cfg:s:item-1:sample1", "verified_fail", passed=False))
            records = store.read_all()
            self.assertEqual(len(records), 2)

    def test_completed_work_unit_ids_includes_only_terminal_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp) / "results.jsonl")
            store.append(_record("done", "verified_pass", passed=True))
            store.append(_record("retry-me", "timeout"))
            self.assertEqual(store.completed_work_unit_ids(), {"done"})

    def test_corrupted_line_fails_clearly_with_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            store = ResultStore(path)
            store.append(_record("u1", "verified_pass", passed=True))
            with path.open("a", encoding="utf-8") as stream:
                stream.write("{not valid json\n")
            with self.assertRaises(CorruptResultRecord) as ctx:
                store.read_all()
            self.assertEqual(ctx.exception.line_number, 2)

    def test_a_json_line_missing_required_fields_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            store = ResultStore(path)
            with path.open("w", encoding="utf-8") as stream:
                stream.write('{"work_unit_id": "u1"}\n')
            with self.assertRaises(CorruptResultRecord):
                store.read_all()

    def test_blank_lines_are_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            store = ResultStore(path)
            store.append(_record("u1", "verified_pass", passed=True))
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n")
            records = store.read_all()
            self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
