"""Adversarial tests for the trusted code-fixture runner (D-038, rebuilt by D-047).

These tests exist to prove one claim: a coding task passes only when
verifier-owned probes were actually observed to produce the right answers,
never because a model asserted success and never because the candidate's
own process was able to say so.

The D-045 runner failed that claim. It ran verifier-owned ``unittest`` files
in the same process that imported the candidate, and read the process exit
code as the verdict, so ``os._exit(0)`` and a rebound ``assertEqual``
both produced ``status="passed"`` on wrong code. The class of test that
would have caught it is grouped under "verdict integrity" below; those
cases are the reason this file exists in its current shape.

Each test is named for the attack or failure mode it demonstrates.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tools.fixtures import runner as runner_mod
from tools.fixtures.probes import OBSERVATION_MARKER, observations_match
from tools.fixtures.runner import evaluate_submission, submission_from_model_output
from tools.fixtures.sandbox import interpreter_read_roots, real_home, sandbox_available
from tools.fixtures.spec import FIXTURES_ROOT, available_fixtures, load_fixture

REPO_ROOT = Path(__file__).resolve().parents[1]

# This runner's isolation is macOS Seatbelt (sandbox-exec); repository CI
# runs on ubuntu-latest, where sandbox_available() is honestly False. Tests
# that need real sandboxed execution are skipped (not failed) there -- the
# runner's own fail-closed behavior for that case is proven unconditionally
# by test_sandbox_unavailable_fails_closed_without_running_anything below,
# which never depends on the host actually having sandbox-exec.
#
# A CI run on Linux therefore does NOT substantiate any isolation or
# verdict-integrity claim in this file. `tests/test_platform_coverage.py`
# fails loudly when that gap stops being reported honestly.
_SANDBOX = sandbox_available()
_NEEDS_SANDBOX = unittest.skipUnless(_SANDBOX, "sandbox-exec not available on this platform")

ADD_GOOD = "def add(a, b):\n    return a + b\n"
ADD_WRONG = "def add(a, b):\n    return a - b\n"

# A probes module for the synthetic fixtures below. Written as a string so
# each test fixture is self-contained in its own temporary directory.
ADD_PROBES = '''
from tools.fixtures.probes import Probe, call_probe

def build(rng):
    return [
        call_probe("add", (2, 3), probe_id="p1", expected=5),
        call_probe("add", (-1, 1), probe_id="p2", expected=0),
    ]
'''


def _write_fixture(root: Path, task_id: str, *, starter: str, probes: str = ADD_PROBES,
                   limits: dict | None = None, editable_files=("solution.py",)) -> Path:
    fixture_dir = root / task_id
    (fixture_dir / "starter").mkdir(parents=True)
    (fixture_dir / "starter" / "solution.py").write_text(starter, encoding="utf-8")
    (fixture_dir / "probes.py").write_text(probes, encoding="utf-8")
    (fixture_dir / "task.md").write_text("test-only fixture\n", encoding="utf-8")
    manifest = {
        "version": 2,
        "task_id": task_id,
        "instructions": "task.md",
        "starter_files": ["solution.py"],
        "editable_files": list(editable_files),
        "probes": "probes.py",
        "limits": limits or {},
    }
    (fixture_dir / "fixture.json").write_text(json.dumps(manifest), encoding="utf-8")
    return fixture_dir


class FixtureRunnerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="angel-fixture-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def evaluate(self, task_id, code, **kwargs):
        return evaluate_submission(task_id, {"solution.py": code}, root=self.root, **kwargs)


class VerdictIntegrityTests(FixtureRunnerBase):
    """A candidate must not be able to declare its own verdict.

    Every case here produced ``status="passed"`` under the D-045 runner on
    code that does not solve the task. None of them may pass again.
    """

    @_NEEDS_SANDBOX
    def test_os_exit_zero_before_the_probe_runs_is_not_a_pass(self):
        _write_fixture(self.root, "exit-01", starter=ADD_GOOD)
        # The D-045 verdict channel: exit 0 before a single assertion ran.
        code = "import os\nos._exit(0)\n\ndef add(a, b):\n    return 0\n"
        result = self.evaluate("exit-01", code)
        self.assertFalse(result.success, "a submission exited 0 and was scored as a pass")
        self.assertEqual(result.status, "no-observation")
        self.assertEqual(result.probes_passed, 0)

    @_NEEDS_SANDBOX
    def test_sys_exit_zero_at_import_is_not_a_pass(self):
        _write_fixture(self.root, "exit-02", starter=ADD_GOOD)
        code = "import sys\nsys.exit(0)\n\ndef add(a, b):\n    return 0\n"
        result = self.evaluate("exit-02", code)
        self.assertFalse(result.success)
        self.assertIn(result.status, {"no-observation", "probe-error"})

    @_NEEDS_SANDBOX
    def test_rebinding_assertion_machinery_does_not_help(self):
        """The old runner's assertions lived in the candidate's process.

        Nothing this submission can rebind exists in its process any more:
        the comparison happens in the parent, against a value that was
        never sent into the sandbox.
        """
        _write_fixture(self.root, "patch-01", starter=ADD_GOOD)
        code = (
            "import unittest\n"
            "unittest.TestCase.assertEqual = lambda self, a, b, msg=None: None\n"
            "def add(a, b):\n    return 'nope'\n"
        )
        result = self.evaluate("patch-01", code)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")

    @_NEEDS_SANDBOX
    def test_a_forged_observation_with_the_wrong_value_is_not_a_pass(self):
        """Emitting the marker is not enough; the value still has to be right."""
        _write_fixture(self.root, "forge-01", starter=ADD_GOOD)
        code = (
            "import os, sys\n"
            f"sys.stdout.write({OBSERVATION_MARKER!r} + '999\\n')\n"
            "sys.stdout.flush()\n"
            "os._exit(0)\n"
            "def add(a, b):\n    return 0\n"
        )
        result = self.evaluate("forge-01", code)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")

    @_NEEDS_SANDBOX
    def test_the_expected_answer_is_never_present_inside_the_sandbox(self):
        """The answer key does not cross the boundary, so it cannot be read.

        The probe's inputs sum to a distinctive sentinel, 999999. This
        submission hunts for that literal in every readable file, in argv,
        and in the environment, and reports what it found: the inputs are
        visible (they must be), the answer is not.
        """
        probes = '''
from tools.fixtures.probes import call_probe

def build(rng):
    return [call_probe("add", (123456, 876543), probe_id="sentinel", expected=999999)]
'''
        _write_fixture(self.root, "leak-01", starter=ADD_GOOD, probes=probes)
        code = (
            "import os, sys, json\n"
            "SENTINEL = '9' * 6\n"
            "hits = []\n"
            "for name in sorted(os.listdir('.')):\n"
            "    try:\n"
            "        text = open(name, 'r', errors='replace').read()\n"
            "    except OSError:\n"
            "        continue\n"
            "    if SENTINEL in text:\n"
            "        hits.append(name)\n"
            "haystack = json.dumps(sys.argv) + json.dumps(dict(os.environ))\n"
            "if SENTINEL in haystack:\n"
            "    hits.append('<argv-or-environ>')\n"
            "sys.stderr.write('ANSWER_HITS=' + json.dumps(hits) + '\\n')\n"
            + ADD_GOOD
        )
        result = self.evaluate("leak-01", code)
        self.assertTrue(result.success, result.stderr)
        self.assertIn("ANSWER_HITS=[]", result.stderr,
                      "the verifier's expected answer was reachable from inside the sandbox")

    @_NEEDS_SANDBOX
    def test_only_the_current_probe_input_exists_on_disk(self):
        """A candidate cannot read ahead to probes it has not been given."""
        _write_fixture(self.root, "peek-01", starter=ADD_GOOD)
        code = (
            "import os, sys, json\n"
            "sys.stderr.write('CWD=' + json.dumps(sorted(os.listdir('.'))) + '\\n')\n"
            + ADD_GOOD
        )
        result = self.evaluate("peek-01", code)
        self.assertTrue(result.success, result.stderr)
        for line in result.stderr.splitlines():
            if line.startswith("CWD="):
                names = json.loads(line[len("CWD="):])
                inputs = [n for n in names if n.startswith("_angel_probe_input")]
                self.assertEqual(len(inputs), 1, f"more than one probe input was on disk: {names}")

    @_NEEDS_SANDBOX
    def test_every_probe_must_pass_for_the_fixture_to_pass(self):
        probes = '''
from tools.fixtures.probes import call_probe

def build(rng):
    return [
        call_probe("add", (2, 3), probe_id="easy", expected=5),
        call_probe("add", (10, 10), probe_id="hard", expected=20),
    ]
'''
        _write_fixture(self.root, "partial-01", starter=ADD_GOOD, probes=probes)
        # Correct for the first probe only.
        code = "def add(a, b):\n    return 5 if (a, b) == (2, 3) else 0\n"
        result = self.evaluate("partial-01", code)
        self.assertFalse(result.success)
        self.assertEqual(result.probes_passed, 1)
        self.assertEqual(result.probes_total, 2)

    @_NEEDS_SANDBOX
    def test_per_probe_outcomes_are_recorded_as_ordered_evidence(self):
        _write_fixture(self.root, "evidence-01", starter=ADD_GOOD)
        result = self.evaluate("evidence-01", ADD_WRONG)
        self.assertEqual(len(result.probes), result.probes_total)
        self.assertTrue(all(outcome.probe_id for outcome in result.probes))
        self.assertTrue(all(outcome.expected_repr for outcome in result.probes))
        self.assertIsNotNone(result.probe_seed)


class ProbeDeterminismTests(FixtureRunnerBase):
    """Randomised inputs defeat answer tables but stay replayable."""

    @_NEEDS_SANDBOX
    def test_the_same_seed_reproduces_the_same_probe_inputs(self):
        probes = '''
from tools.fixtures.probes import call_probe

def build(rng):
    a, b = rng.randint(0, 10_000), rng.randint(0, 10_000)
    return [call_probe("add", (a, b), probe_id="random", expected=a + b)]
'''
        _write_fixture(self.root, "seed-01", starter=ADD_GOOD, probes=probes)
        first = self.evaluate("seed-01", ADD_GOOD, seed=4242)
        second = self.evaluate("seed-01", ADD_GOOD, seed=4242)
        self.assertEqual(first.probe_seed, 4242)
        self.assertEqual([o.expected_repr for o in first.probes], [o.expected_repr for o in second.probes])

    @_NEEDS_SANDBOX
    def test_a_hardcoded_answer_table_does_not_survive_a_new_seed(self):
        probes = '''
from tools.fixtures.probes import call_probe

def build(rng):
    a, b = rng.randint(1000, 9999), rng.randint(1000, 9999)
    return [call_probe("add", (a, b), probe_id="random", expected=a + b)]
'''
        _write_fixture(self.root, "seed-02", starter=ADD_GOOD, probes=probes)
        pinned = self.evaluate("seed-02", ADD_GOOD, seed=1)
        answer = json.loads(pinned.probes[0].expected_repr)
        # A submission that memorised seed 1's answer and returns it always.
        cheater = f"def add(a, b):\n    return {answer}\n"
        self.assertTrue(self.evaluate("seed-02", cheater, seed=1).success, "control: memorised answer fits its own seed")
        self.assertFalse(self.evaluate("seed-02", cheater, seed=2).success,
                         "a memorised answer survived a different seed")

    def test_observations_match_tolerates_json_transport_but_not_wrong_values(self):
        self.assertTrue(observations_match([1, 2], (1, 2)))
        self.assertTrue(observations_match(2, 2.0))
        self.assertTrue(observations_match({"avg": 0}, {"avg": 0.0}))
        self.assertFalse(observations_match({"avg": 1}, {"avg": 0}))
        self.assertFalse(observations_match([1, 2], [1, 2, 3]))
        self.assertFalse(observations_match(1, True))


class SandboxIsolationTests(FixtureRunnerBase):
    """What candidate code can and cannot reach."""

    @_NEEDS_SANDBOX
    def test_the_real_home_directory_is_not_readable(self):
        """`HOME` is redirected, but `pwd` reveals the real one -- so deny it.

        Relying on the `HOME` environment variable alone was the documented
        mitigation before D-047; a candidate defeats it with one stdlib
        call, which is why the Seatbelt profile denies the subtree.
        """
        _write_fixture(self.root, "home-01", starter=ADD_GOOD)
        code = (
            "import os, pwd, sys\n"
            "home = pwd.getpwuid(os.getuid()).pw_dir\n"
            "try:\n"
            "    os.listdir(home)\n"
            "    sys.stderr.write('HOME_READABLE\\n')\n"
            "except OSError:\n"
            "    sys.stderr.write('HOME_BLOCKED\\n')\n"
            "try:\n"
            "    os.listdir(os.path.join(home, '.ssh'))\n"
            "    sys.stderr.write('SSH_READABLE\\n')\n"
            "except OSError:\n"
            "    sys.stderr.write('SSH_BLOCKED\\n')\n"
            + ADD_GOOD
        )
        result = self.evaluate("home-01", code)
        self.assertTrue(result.success, result.stderr)
        self.assertIn("HOME_BLOCKED", result.stderr)
        self.assertIn("SSH_BLOCKED", result.stderr)
        self.assertNotIn("HOME_READABLE", result.stderr)
        self.assertNotIn("SSH_READABLE", result.stderr)

    @_NEEDS_SANDBOX
    def test_repository_source_is_not_readable(self):
        _write_fixture(self.root, "repo-01", starter=ADD_GOOD)
        target = REPO_ROOT / "PRINCIPLES.md"
        code = (
            "import sys\n"
            "try:\n"
            f"    open({str(target)!r}).read()\n"
            "    sys.stderr.write('REPO_READABLE\\n')\n"
            "except OSError:\n"
            "    sys.stderr.write('REPO_BLOCKED\\n')\n"
            + ADD_GOOD
        )
        result = self.evaluate("repo-01", code)
        self.assertIn("REPO_BLOCKED", result.stderr)

    def test_the_interpreter_read_allowance_stays_inside_the_home_directory(self):
        """The re-allow is narrow by construction, not by inspection."""
        home = real_home()
        for root in interpreter_read_roots():
            self.assertTrue(root.startswith(home + os.sep), f"{root} is not a home-relative interpreter prefix")
        self.assertNotIn(home, interpreter_read_roots(), "the whole home directory was re-allowed")

    @_NEEDS_SANDBOX
    def test_write_outside_workdir_is_blocked(self):
        _write_fixture(self.root, "write-01", starter=ADD_GOOD)
        target = self.root / "escape.txt"
        code = (
            "import sys\n"
            "try:\n"
            f"    open({str(target)!r}, 'w').write('escaped')\n"
            "    sys.stderr.write('WROTE_OUTSIDE\\n')\n"
            "except OSError:\n"
            "    sys.stderr.write('WRITE_BLOCKED\\n')\n"
            + ADD_GOOD
        )
        result = self.evaluate("write-01", code)
        self.assertIn("WRITE_BLOCKED", result.stderr)
        self.assertFalse(target.exists())

    @_NEEDS_SANDBOX
    def test_subprocess_exec_of_other_binaries_is_blocked(self):
        _write_fixture(self.root, "exec-01", starter=ADD_GOOD)
        code = (
            "import subprocess, sys\n"
            "try:\n"
            "    subprocess.run(['/bin/ls'], capture_output=True)\n"
            "    sys.stderr.write('EXEC_OK\\n')\n"
            "except Exception:\n"
            "    sys.stderr.write('EXEC_BLOCKED\\n')\n"
            + ADD_GOOD
        )
        result = self.evaluate("exec-01", code)
        self.assertIn("EXEC_BLOCKED", result.stderr)

    @_NEEDS_SANDBOX
    def test_network_access_is_blocked(self):
        _write_fixture(self.root, "net-01", starter=ADD_GOOD)
        code = (
            "import socket, sys\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
            "    sys.stderr.write('NET_OK\\n')\n"
            "except Exception:\n"
            "    sys.stderr.write('NET_BLOCKED\\n')\n"
            + ADD_GOOD
        )
        result = self.evaluate("net-01", code)
        self.assertIn("NET_BLOCKED", result.stderr)

    @_NEEDS_SANDBOX
    def test_ambient_environment_variables_are_not_visible_to_candidate_code(self):
        """Scoped honestly: this is about the environment, not the filesystem.

        Filesystem-reachable secrets are a separate property, covered by
        test_the_real_home_directory_is_not_readable above. The previous
        name of this test ("ambient secrets are not visible") promised more
        than it checked.
        """
        os.environ["ANGEL_TEST_FAKE_SECRET"] = "should-never-leak"
        self.addCleanup(os.environ.pop, "ANGEL_TEST_FAKE_SECRET", None)
        _write_fixture(self.root, "env-01", starter=ADD_GOOD)
        code = (
            "import os\n"
            "assert 'ANGEL_TEST_FAKE_SECRET' not in os.environ, 'secret leaked into sandbox'\n"
            + ADD_GOOD
        )
        result = self.evaluate("env-01", code)
        self.assertTrue(result.success, result.stderr)


class ResourceBoundTests(FixtureRunnerBase):
    @_NEEDS_SANDBOX
    def test_cpu_bound_infinite_loop_is_terminated(self):
        _write_fixture(self.root, "cpu-01", starter=ADD_GOOD,
                       limits={"timeout_seconds": 20, "cpu_seconds": 2, "total_timeout_seconds": 60})
        code = "def add(a, b):\n    return a + b\n\nwhile True:\n    pass\n"
        result = self.evaluate("cpu-01", code)
        self.assertFalse(result.success)
        self.assertIn(result.status, {"cpu-limit-exceeded", "timeout"})

    @_NEEDS_SANDBOX
    def test_io_bound_sleep_hits_the_wall_clock_timeout(self):
        _write_fixture(self.root, "hang-01", starter=ADD_GOOD,
                       limits={"timeout_seconds": 1.0, "cpu_seconds": 30, "total_timeout_seconds": 20})
        code = "import time\ntime.sleep(600)\n\ndef add(a, b):\n    return a + b\n"
        result = self.evaluate("hang-01", code)
        self.assertEqual(result.status, "timeout")

    @_NEEDS_SANDBOX
    def test_total_budget_bounds_the_whole_fixture_not_just_one_probe(self):
        """N probes must not multiply the advertised time budget."""
        probes = '''
from tools.fixtures.probes import call_probe

def build(rng):
    return [call_probe("add", (i, i), probe_id=f"p{i}", expected=i + i) for i in range(10)]
'''
        _write_fixture(self.root, "budget-01", starter=ADD_GOOD, probes=probes,
                       limits={"timeout_seconds": 1.0, "total_timeout_seconds": 3.0, "cpu_seconds": 30})
        code = "import time\ntime.sleep(600)\n\ndef add(a, b):\n    return a + b\n"
        started = time.perf_counter()
        result = self.evaluate("budget-01", code)
        elapsed = time.perf_counter() - started
        self.assertEqual(result.status, "timeout")
        self.assertLess(elapsed, 10.0, "the fixture ran past its total budget")

    @_NEEDS_SANDBOX
    def test_child_process_does_not_survive_timeout(self):
        probes = '''
from tools.fixtures.probes import Probe

def build(rng):
    return [Probe(probe_id="hang", source=(
        "import os, time\\n"
        "import solution\\n"
        "open('child.pid', 'w').write(str(os.getpid()))\\n"
        "time.sleep(600)\\n"
        "OBSERVED = 1\\n"
    ), expected=1)]
'''
        fixture_dir = _write_fixture(self.root, "cleanup-01", starter=ADD_GOOD, probes=probes,
                                     limits={"timeout_seconds": 1.0, "cpu_seconds": 30, "total_timeout_seconds": 20})
        self.assertTrue(fixture_dir.exists())
        seen = {}
        original = runner_mod.shutil.rmtree

        def _capture(path, *args, **kwargs):
            pid_file = Path(path) / "child.pid"
            if pid_file.is_file():
                seen["pid"] = int(pid_file.read_text().strip())
            return original(path, *args, **kwargs)

        runner_mod.shutil.rmtree = _capture
        self.addCleanup(setattr, runner_mod.shutil, "rmtree", original)
        try:
            result = self.evaluate("cleanup-01", ADD_GOOD)
        finally:
            runner_mod.shutil.rmtree = original
        self.assertEqual(result.status, "timeout")
        self.assertIn("pid", seen, "candidate process never started")
        time.sleep(0.3)
        alive = subprocess.run(["ps", "-p", str(seen["pid"])], capture_output=True).returncode == 0
        self.assertFalse(alive, f"pid {seen['pid']} survived the timeout kill")

    @_NEEDS_SANDBOX
    def test_excessive_output_is_capped(self):
        _write_fixture(self.root, "output-01", starter=ADD_GOOD,
                       limits={"timeout_seconds": 8, "max_output_bytes": 4096, "max_file_size_bytes": 262144})
        code = (
            "import sys\n"
            "for _ in range(100000):\n"
            "    sys.stdout.write('x' * 80)\n"
            + ADD_GOOD
        )
        result = self.evaluate("output-01", code)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)


class SubmissionShapeTests(FixtureRunnerBase):
    @_NEEDS_SANDBOX
    def test_correct_implementation_passes(self):
        _write_fixture(self.root, "ok-01", starter=ADD_GOOD)
        result = self.evaluate("ok-01", ADD_GOOD)
        self.assertTrue(result.success, result.stderr)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.probes_passed, result.probes_total)

    @_NEEDS_SANDBOX
    def test_incorrect_implementation_fails(self):
        _write_fixture(self.root, "bad-01", starter=ADD_GOOD)
        result = self.evaluate("bad-01", ADD_WRONG)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")

    @_NEEDS_SANDBOX
    def test_syntax_error_fails_without_raising(self):
        _write_fixture(self.root, "syntax-01", starter=ADD_GOOD)
        result = self.evaluate("syntax-01", "def add(a, b)\n    return a + b\n")
        self.assertFalse(result.success)
        self.assertEqual(result.status, "probe-error")

    @_NEEDS_SANDBOX
    def test_runtime_error_fails_without_raising(self):
        _write_fixture(self.root, "runtime-01", starter=ADD_GOOD)
        result = self.evaluate("runtime-01", "def add(a, b):\n    raise RuntimeError('boom')\n")
        self.assertFalse(result.success)
        self.assertEqual(result.status, "probe-error")

    def test_claim_of_tests_passed_without_code_is_not_a_submission(self):
        self.assertIsNone(submission_from_model_output({"tests_passed": True}, ("solution.py",)))
        self.assertIsNone(submission_from_model_output(True, ("solution.py",)))
        self.assertIsNone(submission_from_model_output({"success": True}, ("solution.py",)))

    def test_claim_field_is_ignored_even_when_code_is_also_present(self):
        extracted = submission_from_model_output(
            {"tests_passed": True, "code": ADD_WRONG}, ("solution.py",))
        self.assertEqual(extracted, {"solution.py": ADD_WRONG})

    @_NEEDS_SANDBOX
    def test_a_claim_only_submission_fails_closed(self):
        _write_fixture(self.root, "claim-01", starter=ADD_GOOD)
        result = evaluate_submission("claim-01", None, root=self.root)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "invalid-submission")

    @_NEEDS_SANDBOX
    def test_submission_cannot_name_a_file_it_may_not_edit(self):
        _write_fixture(self.root, "shadow-01", starter=ADD_GOOD)
        result = evaluate_submission(
            "shadow-01", {"solution.py": ADD_GOOD, "_angel_probe.py": "print('hi')"}, root=self.root)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "invalid-submission")

    def test_malformed_submissions_fail_closed_without_raising(self):
        _write_fixture(self.root, "malformed-01", starter=ADD_GOOD)
        for payload in ({}, {"solution.py": ""}, {"solution.py": "   "}, {"solution.py": 42}, "not-a-dict", []):
            with self.subTest(payload=payload):
                result = evaluate_submission("malformed-01", payload, root=self.root)
                self.assertFalse(result.success)
                self.assertEqual(result.status, "invalid-submission")

    def test_oversized_submission_is_refused_before_execution(self):
        _write_fixture(self.root, "big-01", starter=ADD_GOOD, limits={"max_file_size_bytes": 128})
        result = self.evaluate("big-01", "x = '" + "a" * 500 + "'\n" + ADD_GOOD)
        self.assertEqual(result.status, "invalid-submission")


class VerifierSideFailureTests(FixtureRunnerBase):
    """A verifier problem is never reported as a candidate failure."""

    def test_missing_fixture_bundle_fails_closed(self):
        result = evaluate_submission("nonexistent-99", {"solution.py": ADD_GOOD}, root=self.root)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "verifier-error")

    def test_broken_fixture_manifest_reports_verifier_error(self):
        fixture_dir = self.root / "broken-01"
        fixture_dir.mkdir()
        (fixture_dir / "fixture.json").write_text("{not json", encoding="utf-8")
        result = evaluate_submission("broken-01", {"solution.py": ADD_GOOD}, root=self.root)
        self.assertEqual(result.status, "verifier-error")

    def test_version_1_manifests_are_refused_rather_than_guessed(self):
        """The D-045 bundle format is not silently reinterpreted."""
        fixture_dir = self.root / "legacy-01"
        (fixture_dir / "starter").mkdir(parents=True)
        (fixture_dir / "starter" / "solution.py").write_text(ADD_GOOD, encoding="utf-8")
        (fixture_dir / "task.md").write_text("x\n", encoding="utf-8")
        (fixture_dir / "fixture.json").write_text(json.dumps({
            "version": 1, "task_id": "legacy-01", "starter_files": ["solution.py"],
            "editable_files": ["solution.py"], "hidden_test_files": ["t.py"], "run_module": "t",
        }), encoding="utf-8")
        result = evaluate_submission("legacy-01", {"solution.py": ADD_GOOD}, root=self.root)
        self.assertEqual(result.status, "verifier-error")

    def test_a_probes_module_with_no_build_is_a_verifier_error(self):
        _write_fixture(self.root, "noprobe-01", starter=ADD_GOOD, probes="X = 1\n")
        result = self.evaluate("noprobe-01", ADD_GOOD)
        self.assertEqual(result.status, "verifier-error")

    def test_an_empty_probe_set_is_a_verifier_error_not_a_vacuous_pass(self):
        _write_fixture(self.root, "empty-01", starter=ADD_GOOD, probes="def build(rng):\n    return []\n")
        result = self.evaluate("empty-01", ADD_GOOD)
        self.assertFalse(result.success, "a fixture with no probes must never pass")
        self.assertEqual(result.status, "verifier-error")

    def test_sandbox_unavailable_fails_closed_without_running_anything(self):
        """Proven on every platform, including CI hosts with no sandbox-exec."""
        _write_fixture(self.root, "nosandbox-01", starter=ADD_GOOD)
        original = runner_mod.sandbox_available
        runner_mod.sandbox_available = lambda: False
        self.addCleanup(setattr, runner_mod, "sandbox_available", original)
        try:
            result = self.evaluate("nosandbox-01", ADD_GOOD)
        finally:
            runner_mod.sandbox_available = original
        self.assertFalse(result.success)
        self.assertEqual(result.status, "sandbox-unavailable")

    def test_status_vocabulary_never_overlaps_candidate_and_verifier_failures(self):
        self.assertEqual(
            runner_mod.CANDIDATE_FAILURE_STATUSES & runner_mod.VERIFIER_FAILURE_STATUSES, set(),
            "a status that means 'the model was wrong' must not also mean 'the verifier broke'")

    @_NEEDS_SANDBOX
    def test_tampering_with_the_verifier_owned_driver_is_detected(self):
        probes = '''
from tools.fixtures.probes import call_probe

def build(rng):
    return [call_probe("add", (2, 3), probe_id="p1", expected=5)]
'''
        _write_fixture(self.root, "tamper-01", starter=ADD_GOOD, probes=probes)
        code = (
            "open('_angel_probe.py', 'a').write('\\n# candidate was here\\n')\n"
            + ADD_GOOD
        )
        result = self.evaluate("tamper-01", code)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "tamper-detected")

    @_NEEDS_SANDBOX
    def test_repeated_runs_do_not_share_state(self):
        probes = '''
from tools.fixtures.probes import Probe

def build(rng):
    return [Probe(probe_id="fresh", source=(
        "import os\\n"
        "import solution\\n"
        "OBSERVED = os.path.exists('marker.txt')\\n"
        "open('marker.txt', 'w').write('seen')\\n"
    ), expected=False)]
'''
        _write_fixture(self.root, "clean-01", starter=ADD_GOOD, probes=probes)
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                self.assertTrue(self.evaluate("clean-01", ADD_GOOD).success, "leftover state from a previous run")


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
                self.assertTrue(spec.probes_path.is_file())

    def test_no_fixture_ships_a_version_1_hidden_test_directory(self):
        """The in-process test bundles D-047 replaced are gone, not orphaned."""
        for task_id in available_fixtures():
            with self.subTest(task_id=task_id):
                self.assertFalse((FIXTURES_ROOT / task_id / "hidden").exists())

    @_NEEDS_SANDBOX
    def test_each_fixture_passes_with_a_correct_solution_and_fails_with_a_wrong_one(self):
        for task_id, (correct, wrong) in self.REFERENCE_SOLUTIONS.items():
            with self.subTest(task_id=task_id):
                good = evaluate_submission(task_id, {"solution.py": correct})
                self.assertTrue(good.success, f"{task_id}: {good.detail} / {good.stderr}")
                self.assertEqual(good.status, "passed")
                self.assertEqual(good.probes_passed, good.probes_total)

                bad = evaluate_submission(task_id, {"solution.py": wrong})
                self.assertFalse(bad.success, f"{task_id} accepted a wrong solution")
                self.assertIn(bad.status, runner_mod.CANDIDATE_FAILURE_STATUSES)

    @_NEEDS_SANDBOX
    def test_code_04_rejects_a_submission_that_deletes_the_function_it_must_preserve(self):
        """task.md says "Do not change summarize itself"; that is now verified.

        D-046 observed this exact failure (a model replaced the whole file
        and dropped `summarize`) but the fixture could not see it, because
        no test exercised the pre-existing function.
        """
        dropped = "import json\ndef summarize_json(numbers):\n    return json.dumps({'count': 0, 'sum': 0, 'avg': 0})\n"
        result = evaluate_submission("code-04", {"solution.py": dropped})
        self.assertFalse(result.success)
        self.assertIn("summarize-preserved", [o.probe_id for o in result.probes if not o.passed])

    def test_prompt_does_not_leak_probe_source_or_expected_values(self):
        from tools.bench.corpus import load_corpus
        from tools.bench.harness import benchmark_prompt

        for task in load_corpus():
            if task.kind != "code":
                continue
            with self.subTest(task_id=task.id):
                spec = load_fixture(task.id)
                prompt = benchmark_prompt(task)
                probe_source = spec.probes_path.read_text(encoding="utf-8")
                self.assertNotIn(probe_source, prompt)
                for line in probe_source.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("expected=") and len(stripped) > 12:
                        self.assertNotIn(stripped, prompt)


class HistoricalRecordTests(unittest.TestCase):
    """Measured history is preserved verbatim, never re-scored in place."""

    def test_historical_phase2_evaluation_record_is_unchanged(self):
        doc = REPO_ROOT / "docs" / "18-phase-2-evaluation.md"
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
        payload = json.loads((REPO_ROOT / "data" / "evaluation" / "phase2_tasks.json").read_text())
        self.assertEqual(payload.get("version"), 1)
        self.assertEqual(len(payload.get("tasks", [])), 20)
        code_ids = sorted(t["id"] for t in payload["tasks"] if t["kind"] == "code")
        self.assertEqual(code_ids, ["code-01", "code-02", "code-03", "code-04", "code-05"])


@unittest.skipUnless(sandbox_available(), "sandbox-exec not available on this platform")
class SandboxAvailabilityTests(unittest.TestCase):
    def test_sandbox_available_reports_true_on_this_host(self):
        self.assertTrue(sandbox_available())

    def test_real_home_ignores_the_home_environment_variable(self):
        original = os.environ.get("HOME")
        os.environ["HOME"] = "/tmp/definitely-not-home"
        self.addCleanup(lambda: os.environ.__setitem__("HOME", original) if original else None)
        try:
            self.assertEqual(real_home(), os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
        finally:
            if original:
                os.environ["HOME"] = original


if __name__ == "__main__":
    unittest.main()
