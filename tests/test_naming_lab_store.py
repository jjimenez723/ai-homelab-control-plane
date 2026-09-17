"""Result persistence and human-decision recording."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.naming_lab.grammar import EndToEndResult, RawComplianceResult
from tools.naming_lab.runner import HallucinationFlags, NamingLabResult, SemanticResult
from tools.naming_lab.seeds import SeedStore, new_seed
from tools.naming_lab.store import DecisionStore, ResultStore, record_human_decision

_SHA = "a" * 64


def _result() -> NamingLabResult:
    return NamingLabResult(
        seed_sha256=_SHA, prompt_id="p", prompt_version="1", model_name="m",
        raw_output="25_0615_x.pdf", finish_reason="stop", input_tokens=10, output_tokens=5,
        raw_compliance=RawComplianceResult(
            passed=True, single_line=True, no_prose=True, no_leakage=True,
            extension_matches=True, grammar_matches=True, reasons=(),
        ),
        deterministic_filename="25_0615_x.pdf",
        end_to_end=EndToEndResult(passed=True, filename="25_0615_x.pdf", changed=True, policy_reason="ok", error=None),
        semantic=SemanticResult(
            exact_accepted_match=False, approved_alternate_match=False,
            satisfies_required_semantics=True, violates_forbidden_semantics=False, passed=True,
        ),
        hallucination=HallucinationFlags(unsupported_date=False, unsupported_entities=()),
        error=None, created_at="2026-01-01T00:00:00+00:00",
    )


class ResultStoreTests(unittest.TestCase):
    def test_append_and_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp))
            store.append(_result())
            results = store.for_seed(_SHA)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].deterministic_filename, "25_0615_x.pdf")

    def test_append_all_appends_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp))
            store.append_all([_result(), _result()])
            self.assertEqual(len(store.all()), 2)


class RecordHumanDecisionTests(unittest.TestCase):
    def test_accept_adds_to_accepted_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed_store = SeedStore(Path(tmp) / "seeds")
            decision_store = DecisionStore(Path(tmp) / "decisions")
            seed = seed_store.save(new_seed(sha256=_SHA, original_filename="x.pdf", media_kind="document", extension=".pdf"))

            updated = record_human_decision(
                seed_store, decision_store, seed, decision="accept", filename="25_0615_x.pdf",
            )
            self.assertIn("25_0615_x.pdf", updated.accepted_filenames)
            self.assertIn("25_0615_x.pdf", seed_store.get(_SHA).accepted_filenames)

    def test_reject_never_adds_a_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed_store = SeedStore(Path(tmp) / "seeds")
            decision_store = DecisionStore(Path(tmp) / "decisions")
            seed = seed_store.save(new_seed(sha256=_SHA, original_filename="x.pdf", media_kind="document", extension=".pdf"))

            updated = record_human_decision(
                seed_store, decision_store, seed, decision="reject",
                filename="bad-name.pdf", notes="wrong entity",
            )
            self.assertEqual(updated.accepted_filenames, ())
            self.assertEqual(updated.human_notes, "wrong entity")

    def test_decisions_are_logged_for_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed_store = SeedStore(Path(tmp) / "seeds")
            decision_store = DecisionStore(Path(tmp) / "decisions")
            seed = seed_store.save(new_seed(sha256=_SHA, original_filename="x.pdf", media_kind="document", extension=".pdf"))

            record_human_decision(seed_store, decision_store, seed, decision="accept", filename="a.pdf")
            record_human_decision(seed_store, decision_store, seed_store.get(_SHA), decision="reject", filename=None, notes="later rejected b")

            logged = decision_store.for_seed(_SHA)
            self.assertEqual(len(logged), 2)
            self.assertEqual([d.decision for d in logged], ["accept", "reject"])


if __name__ == "__main__":
    unittest.main()
