"""Phase 2 foundations: the model registry and the coordinator ledger.

Written as `unittest.TestCase` because `python -m unittest discover -s tests`
is this repository's documented and CI-enforced runner. These assertions
previously used pytest-style module-level functions with a `tmp_path`
fixture; pytest is not a declared dependency, so `unittest` collected zero
tests from this file and both checks silently never ran -- locally or in CI.
A test that cannot fail is not coverage.

`tests/test_runner_collection.py` now fails loudly if any test file
reintroduces that shape.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.coordinator.ledger import JobLedger
from tools.models.registry import load_registry

REPO_ROOT = Path(__file__).resolve().parents[1]


class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry(REPO_ROOT / "config" / "models.example.yaml")

    def test_registry_is_data_driven_and_validates_capabilities(self):
        self.assertEqual(self.registry.get("small-local").tier, "small_local")

    def test_a_disabled_entry_is_not_available(self):
        self.assertEqual(self.registry.available(tier="frontier"), [],
                         "the example frontier entry is disabled and must not be offered")

    def test_declared_capability_is_a_shape_claim_not_an_eligibility_claim(self):
        """Every example entry declares capabilities; none of them is certified.

        The registry answers "could this model take this shape of work?".
        `tools/models/certification.py` answers "has it been measured doing
        so?". Keeping the two apart is D-042.
        """
        for spec in self.registry.all():
            with self.subTest(model=spec.name):
                self.assertTrue(spec.capabilities, f"{spec.name} declares no capabilities")


class JobLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "jobs.sqlite"

    def test_job_ledger_keeps_multiple_attempts_and_is_idempotent(self):
        with JobLedger(self.path) as ledger:
            job_id = ledger.create_job("classification", {"value": "x"}, idempotency_key="fixture-1")
            self.assertEqual(ledger.create_job("classification", {}, idempotency_key="fixture-1"), job_id)
            first = ledger.start_attempt(job_id, tier="small_local", provider="fake", model="fake-1")
            ledger.finish_attempt(first, status="failed", error_reason="verifier")
            second = ledger.start_attempt(job_id, tier="capable_local", provider="fake", model="fake-2")
            ledger.finish_attempt(second, status="succeeded", input_tokens=2, output_tokens=4)
            ledger.finish_job(job_id, disposition="success", escalated=True, escalation_reason="verifier")
            self.assertEqual(len(ledger.attempts(job_id)), 2)
            self.assertEqual(ledger.get_job(job_id)["retry_count"], 1)

    def test_an_unverified_attempt_stores_a_null_verifier_result(self):
        """The column has three states, and the third one is not "passed"."""
        with JobLedger(self.path) as ledger:
            job_id = ledger.create_job("general", {})
            attempt = ledger.start_attempt(job_id, tier="small_local", provider="fake", model="fake-1")
            ledger.finish_attempt(attempt, status="completed_unverified", verifier_result=None)
            self.assertIsNone(ledger.attempts(job_id)[0]["verifier_result"])


if __name__ == "__main__":
    unittest.main()
