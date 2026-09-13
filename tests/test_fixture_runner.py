"""Adversarial tests for the D-038 trusted code-fixture runner.

These tests exist to prove a specific claim: a coding task passes only when
verifier-owned hidden tests actually execute successfully inside a bounded,
isolated environment, never because a model asserted success. Each test
below is named for the attack or failure mode it demonstrates.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tools.fixtures import runner as runner_mod
from tools.fixtures.runner import evaluate_submission, submission_from_model_output
from tools.fixtures.sandbox import sandbox_available
from tools.fixtures.spec import FIXTURES_ROOT, available_fixtures, load_fixture

# This runner's isolation is macOS Seatbelt (sandbox-exec); repository CI
# runs on ubuntu-latest, where sandbox_available() is honestly False. Tests
# that need real sandboxed execution are skipped (not failed) there -- the
# runner's own fail-closed behavior for that case is proven unconditionally
# by test_sandbox_unavailable_fails_closed_without_running_anything below,
# which never depends on the host actually having sandbox-exec.
_SANDBOX = sandbox_available()
_NEEDS_SANDBOX = unittest.skipUnless(_SANDBOX, "sandbox-exec not available on this platform")


def _write_fixture(root: Path, task_id: str, *, starter: str, hidden_test: str,
                    run_module: str = "test_mod", limits: dict | None = None,
                    editable_files=("solution.py",), hidden_test_files=None) -> Path:
    fixture_dir = root / task_id
    (fixture_dir / "starter").mkdir(parents=True)
    (fixture_dir / "hidden").mkdir(parents=True)
    (fixture_dir / "starter" / "solution.py").write_text(starter, encoding="utf-8")
    hidden_name = f"{run_module}.py"
    (fixture_dir / "hidden" / hidden_name).write_text(hidden_test, encoding="utf-8")
    (fixture_dir / "task.md").write_text("test-only fixture\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "task_id": task_id,
        "instructions": "task.md",
        "starter_files": ["solution.py"],
        "editable_files": list(editable_files),
        "hidden_test_files": list(hidden_test_files or [hidden_name]),
        "run_module": run_module,
        "limits": limits or {},
    }
    (fixture_dir / "fixture.json").write_text(json.dumps(manifest), encoding="utf-8")
    return fixture_dir


class FixtureRunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="angel-fixture-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    # 1. correct implementation passes
    @_NEEDS_SANDBOX
    def test_correct_implementation_passes(self):
        _write_fixture(
            self.root, "ok-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        result = evaluate_submission("ok-01", {"solution.py": "def add(a, b):\n    return a + b\n"}, root=self.root)
        self.assertTrue(result.success)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.returncode, 0)

    # 2. incorrect implementation fails
    @_NEEDS_SANDBOX
    def test_incorrect_implementation_fails(self):
        _write_fixture(
            self.root, "bad-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        result = evaluate_submission("bad-01", {"solution.py": "def add(a, b):\n    return a - b\n"}, root=self.root)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertIn("AssertionError", result.stderr)

    # 3. syntax/runtime error fails
    @_NEEDS_SANDBOX
    def test_syntax_error_fails(self):
        _write_fixture(
            self.root, "syntax-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        result = evaluate_submission("syntax-01", {"solution.py": "def add(a, b)\n    return a + b\n"}, root=self.root)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertIn("SyntaxError", result.stderr)

    @_NEEDS_SANDBOX
    def test_runtime_error_fails(self):
        _write_fixture(
            self.root, "runtime-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        result = evaluate_submission("runtime-01", {"solution.py": "def add(a, b):\n    return 1 / 0\n"}, root=self.root)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertIn("ZeroDivisionError", result.stderr)

    # 4. a model's own success claim is never evidence
    def test_claim_of_tests_passed_without_code_fails(self):
        _write_fixture(
            self.root, "claim-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        spec = load_fixture("claim-01", root=self.root)
        # A model can wrap its claim however it likes; none of these shapes
        # contain real code, and none may be treated as passing evidence.
        for claim in (True, {"tests_passed": True}, {"success": True}, 42, None, [], {}):
            submission = submission_from_model_output(claim, spec.editable_files)
            self.assertIsNone(submission, f"claim {claim!r} must not be accepted as a code submission")
            result = evaluate_submission("claim-01", submission, root=self.root)
            self.assertFalse(result.success)
            self.assertEqual(result.status, "invalid-submission")

    @_NEEDS_SANDBOX
    def test_claim_field_is_ignored_even_when_code_is_also_present(self):
        # If a model bundles a tests_passed claim alongside real code, the
        # claim field itself must never be consulted for the verdict -- only
        # whether the code, once run, actually satisfies the hidden tests.
        _write_fixture(
            self.root, "claim-02",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        spec = load_fixture("claim-02", root=self.root)
        output = {"tests_passed": True, "code": "def add(a, b):\n    return a - b\n"}
        submission = submission_from_model_output(output, spec.editable_files)
        result = evaluate_submission("claim-02", submission, root=self.root)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")

    # 5. hidden tests cannot simply be edited by the submission
    def test_submission_cannot_name_the_hidden_test_file(self):
        _write_fixture(
            self.root, "protect-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
            run_module="test_mod",
        )
        result = evaluate_submission(
            "protect-01",
            {"solution.py": "def add(a, b):\n    return a + b\n", "test_mod.py": "# overwritten by submission"},
            root=self.root,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.status, "invalid-submission")

    @_NEEDS_SANDBOX
    def test_hidden_test_edited_at_runtime_is_detected_as_tamper(self):
        _write_fixture(
            self.root, "protect-02",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
            run_module="test_mod",
        )
        malicious = (
            "with open('test_mod.py', 'w') as f:\n"
            "    f.write('import unittest\\nclass T(unittest.TestCase):\\n def test_x(self): pass\\n')\n"
            "def add(a, b):\n    return a + b\n"
        )
        result = evaluate_submission("protect-02", {"solution.py": malicious}, root=self.root)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "tamper-detected")

    # 6. timeout / infinite loop is terminated and fails
    @_NEEDS_SANDBOX
    def test_cpu_bound_infinite_loop_is_terminated(self):
        _write_fixture(
            self.root, "hang-cpu",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
            limits={"timeout_seconds": 6, "cpu_seconds": 1},
        )
        started = time.perf_counter()
        result = evaluate_submission(
            "hang-cpu", {"solution.py": "def add(a, b):\n    while True:\n        pass\n"}, root=self.root,
        )
        elapsed = time.perf_counter() - started
        self.assertFalse(result.success)
        self.assertEqual(result.status, "cpu-limit-exceeded")
        self.assertLess(elapsed, 6.0)

    @_NEEDS_SANDBOX
    def test_io_bound_infinite_loop_hits_wall_clock_timeout(self):
        _write_fixture(
            self.root, "hang-io",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
            limits={"timeout_seconds": 1.5, "cpu_seconds": 30},
        )
        started = time.perf_counter()
        code = "import time\ndef add(a, b):\n    while True:\n        time.sleep(0.05)\n"
        result = evaluate_submission("hang-io", {"solution.py": code}, root=self.root)
        elapsed = time.perf_counter() - started
        self.assertFalse(result.success)
        self.assertEqual(result.status, "timeout")
        self.assertLess(elapsed, 5.0)

    # 7. unexpected/malformed output/submission fails closed
    def test_malformed_submissions_fail_closed_without_raising(self):
        _write_fixture(
            self.root, "malformed-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        cases = [
            None,
            {},
            {"wrong_file.py": "def add(a, b): return a + b"},
            {"solution.py": 12345},
            {"solution.py": "   \n\t  "},
            {"solution.py": "x" * 2_000_000},
            {"solution.py": "def add(a, b): return a + b", "extra.py": "junk"},
        ]
        for submission in cases:
            with self.subTest(submission=str(submission)[:60]):
                result = evaluate_submission("malformed-01", submission, root=self.root)
                self.assertFalse(result.success)
                self.assertEqual(result.status, "invalid-submission")

    def test_missing_fixture_bundle_fails_closed(self):
        result = evaluate_submission("does-not-exist", {"solution.py": "x = 1"}, root=self.root)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "verifier-error")

    # 8. writes outside the permitted fixture area are rejected/contained
    @_NEEDS_SANDBOX
    def test_write_outside_workdir_is_blocked(self):
        _write_fixture(
            self.root, "escape-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        marker_dir = tempfile.mkdtemp(prefix="angel-escape-target-")
        self.addCleanup(shutil.rmtree, marker_dir, ignore_errors=True)
        marker_path = os.path.join(marker_dir, "pwned.txt")
        code = (
            "def add(a, b):\n"
            f"    with open({marker_path!r}, 'w') as f:\n"
            "        f.write('pwned')\n"
            "    return a + b\n"
        )
        result = evaluate_submission("escape-01", {"solution.py": code}, root=self.root)
        self.assertFalse(result.success)
        self.assertIn("Operation not permitted", result.stderr)
        self.assertFalse(os.path.exists(marker_path))

    @_NEEDS_SANDBOX
    def test_subprocess_exec_of_other_binaries_is_blocked(self):
        _write_fixture(
            self.root, "spawn-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        code = (
            "import subprocess\n"
            "def add(a, b):\n"
            "    subprocess.run(['/bin/echo', 'leaked'], check=True)\n"
            "    return a + b\n"
        )
        result = evaluate_submission("spawn-01", {"solution.py": code}, root=self.root)
        self.assertFalse(result.success)
        # Two independent layers can each block this: the Seatbelt profile's
        # literal-exec restriction ("Operation not permitted") or the
        # RLIMIT_NPROC fork ceiling being hit first ("Resource temporarily
        # unavailable"). Either proves the spawn attempt did not succeed.
        self.assertTrue(
            "Operation not permitted" in result.stderr or "Resource temporarily unavailable" in result.stderr,
            result.stderr,
        )
        self.assertNotIn("leaked", result.stdout)

    @_NEEDS_SANDBOX
    def test_network_access_is_blocked(self):
        _write_fixture(
            self.root, "net-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
            limits={"timeout_seconds": 5},
        )
        code = (
            "import socket\n"
            "def add(a, b):\n"
            "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "    s.settimeout(2)\n"
            "    s.connect(('1.1.1.1', 80))\n"
            "    return a + b\n"
        )
        result = evaluate_submission("net-01", {"solution.py": code}, root=self.root)
        self.assertFalse(result.success)
        self.assertIn("Operation not permitted", result.stderr)

    @_NEEDS_SANDBOX
    def test_ambient_secrets_are_not_visible_to_candidate_code(self):
        os.environ["ANGEL_TEST_FAKE_SECRET"] = "should-never-leak"
        self.addCleanup(os.environ.pop, "ANGEL_TEST_FAKE_SECRET", None)
        _write_fixture(
            self.root, "env-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        code = (
            "import os\n"
            "def add(a, b):\n"
            "    assert 'ANGEL_TEST_FAKE_SECRET' not in os.environ, 'secret leaked into sandbox'\n"
            "    return a + b\n"
        )
        result = evaluate_submission("env-01", {"solution.py": code}, root=self.root)
        self.assertTrue(result.success, result.stderr)

    # 9. child processes are cleaned up after timeout/failure
    @_NEEDS_SANDBOX
    def test_child_process_does_not_survive_timeout(self):
        fixture_dir = _write_fixture(
            self.root, "cleanup-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test=(
                "import os, time, unittest\n"
                "from solution import add\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_it(self):\n"
                "        with open('child.pid', 'w') as f:\n"
                "            f.write(str(os.getpid()))\n"
                "        while True:\n"
                "            time.sleep(0.05)\n"
            ),
            limits={"timeout_seconds": 1.0, "cpu_seconds": 30},
        )
        spec = load_fixture("cleanup-01", root=self.root)
        workdir = tempfile.mkdtemp(prefix="angel-cleanup-test-")
        try:
            result = runner_mod._run_in_sandbox(
                spec, {"solution.py": "def add(a, b):\n    return a + b\n"}, workdir, time.perf_counter(),
            )
            self.assertEqual(result.status, "timeout")
            time.sleep(0.3)
            pid_file = Path(workdir) / "child.pid"
            self.assertTrue(pid_file.exists(), "candidate process never started")
            pid = int(pid_file.read_text().strip())
            alive = subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0
            self.assertFalse(alive, f"pid {pid} survived the timeout kill")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # 10. verifier errors are distinguishable from candidate-code failures
    def test_broken_fixture_bundle_reports_verifier_error_not_candidate_failure(self):
        fixture_dir = self.root / "broken-01"
        fixture_dir.mkdir()
        (fixture_dir / "fixture.json").write_text("{not valid json", encoding="utf-8")
        result = evaluate_submission("broken-01", {"solution.py": "x = 1"}, root=self.root)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "verifier-error")

    def test_sandbox_unavailable_fails_closed_without_running_anything(self):
        _write_fixture(
            self.root, "nosandbox-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
        )
        original = runner_mod.sandbox_available
        runner_mod.sandbox_available = lambda: False
        try:
            result = evaluate_submission("nosandbox-01", {"solution.py": "def add(a, b):\n    return a + b\n"}, root=self.root)
        finally:
            runner_mod.sandbox_available = original
        self.assertFalse(result.success)
        self.assertEqual(result.status, "sandbox-unavailable")

    def test_status_vocabulary_never_overlaps_candidate_and_verifier_failures(self):
        candidate_statuses = {"failed", "timeout", "cpu-limit-exceeded", "tamper-detected"}
        verifier_statuses = {"verifier-error", "sandbox-unavailable", "invalid-submission"}
        self.assertEqual(candidate_statuses & verifier_statuses, set())

    # 11. repeated runs start from clean disposable state
    @_NEEDS_SANDBOX
    def test_repeated_runs_do_not_share_state(self):
        _write_fixture(
            self.root, "clean-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test=(
                "import os, unittest\n"
                "from solution import add\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_fresh_workdir(self):\n"
                "        self.assertFalse(os.path.exists('marker.txt'), 'leftover state from a previous run')\n"
                "        with open('marker.txt', 'w') as f:\n"
                "            f.write('seen')\n"
                "        self.assertEqual(add(2, 3), 5)\n"
            ),
        )
        good = "def add(a, b):\n    return a + b\n"
        first = evaluate_submission("clean-01", {"solution.py": good}, root=self.root)
        second = evaluate_submission("clean-01", {"solution.py": good}, root=self.root)
        third = evaluate_submission("clean-01", {"solution.py": good}, root=self.root)
        self.assertTrue(first.success, first.stderr)
        self.assertTrue(second.success, second.stderr)
        self.assertTrue(third.success, third.stderr)

    # 12. historical benchmark evidence is not rewritten
    def test_historical_phase2_evaluation_record_is_unchanged(self):
        doc = Path(__file__).resolve().parents[1] / "docs" / "18-phase-2-evaluation.md"
        text = doc.read_text(encoding="utf-8")
        for verbatim in (
            "**4/20 (20%)**",
            "code-fixture-unavailable",
            "for 0/20",
            "produced 4/20 at 2k",
            "This document is the unedited historical record",
        ):
            self.assertIn(verbatim, text, f"historical record text {verbatim!r} was altered or removed")

    def test_legacy_corpus_file_is_unchanged(self):
        payload = json.loads((Path(__file__).resolve().parents[1] / "data" / "evaluation" / "phase2_tasks.json").read_text())
        self.assertEqual(payload.get("version"), 1)
        self.assertEqual(len(payload.get("tasks", [])), 20)
        code_ids = sorted(t["id"] for t in payload["tasks"] if t["kind"] == "code")
        self.assertEqual(code_ids, ["code-01", "code-02", "code-03", "code-04", "code-05"])

    # Excessive output is capped rather than buffered without bound
    @_NEEDS_SANDBOX
    def test_excessive_output_is_capped(self):
        _write_fixture(
            self.root, "output-01",
            starter="def add(a, b):\n    raise NotImplementedError\n",
            hidden_test="import unittest\nfrom solution import add\n\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_it(self):\n        self.assertEqual(add(2, 3), 5)\n",
            limits={"timeout_seconds": 8, "max_output_bytes": 4096, "max_file_size_bytes": 262144},
        )
        code = (
            "import sys\n"
            "for _ in range(100000):\n"
            "    sys.stdout.write('x' * 80)\n"
            "def add(a, b):\n    return a + b\n"
        )
        result = evaluate_submission("output-01", {"solution.py": code}, root=self.root)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)


class RealFixtureBundleTests(unittest.TestCase):
    """The shipped code-01..05 fixtures actually execute, end to end."""

    REFERENCE_SOLUTIONS = {
        "code-01": ("def add(a, b):\n    return a + b\n",
                    "def add(a, b):\n    return a - b\n"),
        "code-02": (
            "import re\n\ndef slugify(text):\n"
            "    text = text.lower()\n"
            "    text = re.sub(r'[^a-z0-9]+', '-', text)\n"
            "    return text.strip('-')\n",
            "def slugify(text):\n    return text\n",
        ),
        "code-03": (
            "def chunk(items, size):\n"
            "    result = []\n"
            "    for i in range(0, len(items), size):\n"
            "        result.append(items[i:i + size])\n"
            "    return result\n",
            "def chunk(items, size):\n"
            "    result = []\n"
            "    for i in range(0, len(items), size):\n"
            "        result.append(items[i:i + size - 1])\n"
            "    return result\n",
        ),
        "code-04": (
            "def summarize(numbers):\n"
            "    numbers = list(numbers)\n"
            "    total = sum(numbers)\n"
            "    count = len(numbers)\n"
            "    return {'count': count, 'sum': total, 'avg': (total / count) if count else 0}\n\n"
            "import json\n"
            "def summarize_json(numbers):\n"
            "    return json.dumps(summarize(numbers))\n",
            "import json\ndef summarize_json(numbers):\n    return json.dumps({})\n",
        ),
        "code-05": (
            "import time\n"
            "def retry(fn, attempts=3, delay=0.0, exceptions=(Exception,)):\n"
            "    last_exc = None\n"
            "    for i in range(attempts):\n"
            "        try:\n            return fn()\n"
            "        except exceptions as e:\n"
            "            last_exc = e\n"
            "            if i < attempts - 1 and delay:\n                time.sleep(delay)\n"
            "    raise last_exc\n",
            "def retry(fn, attempts=3, delay=0.0, exceptions=(Exception,)):\n    return fn()\n",
        ),
    }

    def test_all_five_code_fixtures_are_registered(self):
        self.assertEqual(available_fixtures(), ("code-01", "code-02", "code-03", "code-04", "code-05"))

    def test_each_fixture_loads_and_is_structurally_valid(self):
        for task_id in available_fixtures():
            with self.subTest(task_id=task_id):
                spec = load_fixture(task_id)
                self.assertEqual(spec.task_id, task_id)
                self.assertTrue(spec.instructions_path.is_file())

    @_NEEDS_SANDBOX
    def test_each_fixture_passes_with_a_correct_solution_and_fails_with_a_wrong_one(self):
        for task_id, (correct, wrong) in self.REFERENCE_SOLUTIONS.items():
            with self.subTest(task_id=task_id):
                good = evaluate_submission(task_id, {"solution.py": correct})
                self.assertTrue(good.success, f"{task_id}: {good.stderr}")
                self.assertEqual(good.status, "passed")

                bad = evaluate_submission(task_id, {"solution.py": wrong})
                self.assertFalse(bad.success, f"{task_id} accepted a wrong solution")
                self.assertEqual(bad.status, "failed")

    def test_prompt_does_not_leak_hidden_test_source(self):
        from tools.bench.corpus import load_corpus
        from tools.bench.harness import benchmark_prompt

        for task in load_corpus():
            if task.kind != "code":
                continue
            spec = load_fixture(task.id)
            prompt = benchmark_prompt(task)
            for hidden_name in spec.hidden_test_files:
                hidden_source = (spec.hidden_dir / hidden_name).read_text(encoding="utf-8")
                self.assertNotIn(hidden_source, prompt)


@unittest.skipUnless(sandbox_available(), "sandbox-exec not available on this platform")
class SandboxAvailabilityTests(unittest.TestCase):
    def test_sandbox_available_reports_true_on_this_host(self):
        self.assertTrue(sandbox_available())


if __name__ == "__main__":
    unittest.main()
