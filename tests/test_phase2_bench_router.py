import tempfile
import unittest
from pathlib import Path

from tools.bench.corpus import load_corpus
from tools.bench.harness import benchmark_prompt, run, verify
from tools.fixtures.sandbox import sandbox_available
from tools.router.router import RouteRequest, Router
from tools.router.report import report
from tools.coordinator.ledger import JobLedger

# The code_fixture verifier executes candidate code inside a macOS Seatbelt
# sandbox (tools/fixtures/); this repo's CI runs on ubuntu-latest, where
# that isolation is honestly unavailable. See tests/test_fixture_runner.py
# for the fail-closed behavior that covers that platform unconditionally.
_NEEDS_SANDBOX = unittest.skipUnless(sandbox_available(), "sandbox-exec not available on this platform")

class FakeProvider:
    name = "fake"; model = "fake-v1"; capabilities = ("text", "structured_output")
    def generate(self, prompt, **kwargs):
        return {"output": "ok", "generation_seconds": 0.001}

class Phase2HarnessTests(unittest.TestCase):
    def test_corpus_is_versioned_and_balanced(self):
        tasks = load_corpus()
        self.assertEqual(len(tasks), 20)
        self.assertEqual({kind: sum(t.kind == kind for t in tasks) for kind in ("classification", "filename", "code")}, {"classification": 10, "filename": 5, "code": 5})

    def test_fake_provider_runs_without_runtime(self):
        results = run(FakeProvider(), load_corpus()[:1], contexts=(2048,))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].model, "unknown")
        self.assertFalse(results[0].success)

    def test_code_claims_alone_are_never_verification_evidence(self):
        # D-038's trusted fixture runner (tools/fixtures/) now exists, so a
        # code_fixture task actually executes hidden tests -- but a model
        # claim with no real code attached must still fail closed, just as a
        # more specific status than the pre-runner "code-fixture-unavailable".
        task = next(task for task in load_corpus() if task.id == "code-02")
        self.assertEqual(verify(task, True), (False, "invalid-submission"))
        self.assertEqual(verify(task, {"tests_passed": True}), (False, "invalid-submission"))

    @_NEEDS_SANDBOX
    def test_code_fixture_runs_real_hidden_tests(self):
        task = next(task for task in load_corpus() if task.id == "code-01")
        correct = "def add(a, b):\n    return a + b\n"
        wrong = "def add(a, b):\n    return a - b\n"
        self.assertEqual(verify(task, correct), (True, "passed"))
        ok, status = verify(task, wrong)
        self.assertFalse(ok)
        self.assertEqual(status, "failed")

    def test_code_fixture_unavailable_for_a_task_with_no_bundle(self):
        from tools.bench.corpus import Task
        task = Task(id="code-99", kind="code", prompt="x", expected=True, intended_tier=0,
                    capabilities=(), risk="medium", privacy="public", verifier="code_fixture")
        self.assertEqual(verify(task, "def f(): pass"), (False, "code-fixture-unavailable"))

    def test_benchmark_contract_does_not_leak_expected_answer(self):
        task = next(task for task in load_corpus() if task.id == "cls-01")
        prompt = benchmark_prompt(task)
        self.assertIn(task.prompt, prompt)
        self.assertNotIn(str(task.expected), prompt.removesuffix(task.prompt))

    def test_router_deterministic_path(self):
        result = Router({1: FakeProvider()}).route(RouteRequest("x", deterministic=lambda: "yes", verifier=lambda x: x == "yes"))
        self.assertEqual(result.disposition, "success")
        self.assertEqual(result.attempts[0].tier, 0)

    def test_frontier_is_blocked_for_private(self):
        result = Router({3: FakeProvider()}, frontier_enabled=True).route(RouteRequest("secret", privacy="confidential"))
        self.assertEqual(result.disposition, "unavailable")
        self.assertIn(3, result.escalation_path)

    def test_empty_report(self):
        with tempfile.TemporaryDirectory() as directory:
            value = report(Path(directory) / "jobs.sqlite")
        self.assertEqual(value["attempts"], 0)
        self.assertIsNone(value["estimated_cost_usd"])

    def test_retry_and_verifier_failure_are_retained(self):
        class Flaky(FakeProvider):
            def __init__(self): self.calls = 0
            def generate(self, prompt, **kwargs):
                self.calls += 1
                if self.calls == 1: raise TimeoutError("fixture timeout")
                return {"output": "yes", "input_tokens": 3, "output_tokens": 1}
        with tempfile.TemporaryDirectory() as directory:
            with JobLedger(Path(directory) / "jobs.sqlite") as ledger:
                value = Router({1: Flaky()}, ledger=ledger).route(RouteRequest("x", verifier=lambda x: x == "yes", max_retries=1))
                self.assertEqual(value.disposition, "success")
                self.assertEqual([attempt.status for attempt in value.attempts], ["failed", "succeeded"])
                self.assertEqual(len(ledger.attempts(value.job_id)), 2)

    def test_populated_report_aggregates_tokens_and_energy_label(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.sqlite"
            with JobLedger(db) as ledger:
                Router({1: FakeProvider()}, ledger=ledger).route(RouteRequest("x", verifier=lambda x: x == "ok", max_retries=0))
            value = report(db)
        self.assertEqual(value["attempts"], 1)
        self.assertGreaterEqual(value["output_tokens"], 1)
        self.assertIn("not direct sensor", value["energy_method"])

if __name__ == "__main__": unittest.main()
