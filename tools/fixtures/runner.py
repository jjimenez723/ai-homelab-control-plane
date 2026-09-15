"""Execute a candidate code submission and decide the verdict outside its reach.

The verdict is produced by comparing verifier-held expected values against
observations reported by short-lived sandboxed processes. No comparison,
assertion, or scoring logic ever runs in the same process as candidate
code, and the expected values are never written into the disposable work
directory. See ``tools/fixtures/probes.py`` for why (D-047) and
``tools/fixtures/sandbox.py`` for what the isolation does and does not
cover.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .probes import (
    ERROR_MARKER,
    OBSERVATION_MARKER,
    Probe,
    build_probes,
    observations_match,
)
from .sandbox import SANDBOX_EXEC, build_seatbelt_profile, sandbox_available
from .spec import FixtureSpec, load_fixture

DRIVER_SOURCE = Path(__file__).resolve().parent / "_probe_driver.py"
DRIVER_NAME = "_angel_probe.py"
PROBE_INPUT_NAME = "_angel_probe_input.py"

# Candidate-side failures: the submission is what went wrong.
CANDIDATE_FAILURE_STATUSES = frozenset({
    "failed", "no-observation", "probe-error", "timeout", "cpu-limit-exceeded",
})
# Verifier-side or submission-shape problems: not evidence about capability.
VERIFIER_FAILURE_STATUSES = frozenset({
    "verifier-error", "sandbox-unavailable", "invalid-submission", "tamper-detected",
})


@dataclass(frozen=True)
class ProbeOutcome:
    """What one probe observed. Ordered evidence, never a bare boolean."""

    probe_id: str
    passed: bool
    status: str
    description: str
    returncode: int | None
    elapsed_seconds: float
    observed_repr: str | None
    expected_repr: str
    detail: str | None = None


@dataclass(frozen=True)
class FixtureResult:
    """PASS/FAIL evidence for one code-fixture execution.

    ``status`` distinguishes *why* ``success`` is false: a candidate-code
    failure (``failed``, ``no-observation``, ``probe-error``, ``timeout``,
    ``cpu-limit-exceeded``) is a different kind of event from a
    verifier-side problem (``verifier-error``, ``sandbox-unavailable``) or a
    submission-shape problem (``invalid-submission``, ``tamper-detected``).
    Callers must not collapse these into one boolean when reporting
    evidence.

    ``success`` is true only when every probe was observed *and* matched.
    Absence of an observation is a failure, never a pass.
    """

    task_id: str
    success: bool
    status: str
    returncode: int | None
    elapsed_seconds: float
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    detail: str | None = None
    probes_total: int = 0
    probes_passed: int = 0
    probe_seed: int | None = None
    probes: tuple[ProbeOutcome, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fail(task_id: str, status: str, started: float, detail: str, returncode: int | None = None) -> FixtureResult:
    return FixtureResult(task_id, False, status, returncode, time.perf_counter() - started, "", "", False, False, detail)


def _minimal_env(workdir: str) -> dict[str, str]:
    """An explicit allowlist, never the harness's own ``os.environ``.

    No ambient credential, token, or repository path reaches a probe
    process this way. ``HOME`` is redirected for convenience; the Seatbelt
    home-read denial in ``sandbox.py``, not this value, is the boundary.
    """
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": workdir,
        "TMPDIR": workdir,
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cap(text: str, cap: int) -> tuple[str, bool]:
    data = text.encode("utf-8", errors="replace")
    return data[:cap].decode("utf-8", errors="replace"), len(data) > cap


def submission_from_model_output(output: Any, editable_files: tuple[str, ...]) -> dict[str, str] | None:
    """Extract a ``{filename: content}`` submission from raw model output.

    A model's own claim about its result (``tests_passed``, ``success``, and
    similar fields) is never read here and never influences the verdict --
    only actual code text does. Returns ``None`` for any shape that is not
    honestly a code submission (a bare boolean claim, a dict with no usable
    code field, non-string content, ...), which the caller must treat as an
    ``invalid-submission`` failure rather than attempt to run.
    """
    if len(editable_files) != 1:
        # V0 fixtures are single-file; a multi-file submission format is not
        # yet defined, so refuse rather than guess.
        return None
    target = editable_files[0]

    code: Any = None
    if isinstance(output, str):
        code = output
    elif isinstance(output, dict):
        for key in ("code", "solution", "file", "content"):
            value = output.get(key)
            if isinstance(value, str):
                code = value
                break

    if not isinstance(code, str) or not code.strip():
        return None
    return {target: code}


def evaluate_submission(task_id: str, submission: dict[str, str] | None, *,
                        root: Path | None = None, seed: int | None = None) -> FixtureResult:
    """Run ``submission`` against the verifier-owned probes for ``task_id``.

    ``submission`` maps editable filenames to their full replacement
    content. ``seed`` pins the randomised probe inputs so a failing run can
    be replayed exactly; omit it and a fresh seed is drawn and recorded.

    Returns a :class:`FixtureResult` that is always evidence -- every code
    path below returns rather than raises, and the top-level ``except``
    guarantees a ``verifier-error`` result instead of letting an unexpected
    internal exception look like a candidate failure or, worse, escape
    unhandled and be mistaken for something outside this evaluation.
    """
    started = time.perf_counter()

    try:
        spec = load_fixture(task_id) if root is None else load_fixture(task_id, root=root)
    except Exception as exc:
        return _fail(task_id, "verifier-error", started, f"fixture bundle error: {type(exc).__name__}: {exc}")

    # Submission shape is validated before the sandbox-availability check
    # below, on purpose: a malformed submission is "invalid-submission"
    # regardless of what platform the runner happens to be on, and this
    # keeps that verdict identical across hosts. Only once a submission is
    # honestly a code payload do we need to ask whether this host can
    # actually isolate running it.
    if not isinstance(submission, dict) or not submission:
        return _fail(task_id, "invalid-submission", started, "submission must be a non-empty dict of {filename: content}")

    allowed = set(spec.editable_files)
    if set(submission) != allowed:
        return _fail(task_id, "invalid-submission", started, f"submission files must be exactly {sorted(allowed)}, got {sorted(submission)}")

    for name, content in submission.items():
        if not isinstance(content, str) or not content.strip():
            return _fail(task_id, "invalid-submission", started, f"submission file {name!r} must be non-empty text")
        if len(content.encode("utf-8")) > spec.max_file_size_bytes:
            return _fail(task_id, "invalid-submission", started, f"submission file {name!r} exceeds {spec.max_file_size_bytes} byte limit")

    try:
        probes, probe_seed = build_probes(spec.probes_path, task_id, seed=seed)
    except Exception as exc:
        return _fail(task_id, "verifier-error", started, f"probe build error: {type(exc).__name__}: {exc}")

    if not sandbox_available():
        return _fail(task_id, "sandbox-unavailable", started, f"{SANDBOX_EXEC} not found; refusing to execute untrusted code unsandboxed")

    workdir = tempfile.mkdtemp(prefix=f"angel-fixture-{task_id}-")
    try:
        return _run_probes(spec, submission, probes, probe_seed, workdir, started)
    except Exception as exc:
        return _fail(task_id, "verifier-error", started, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _parse_observation(stdout: str) -> tuple[str, Any]:
    """Return ``(kind, payload)`` from a probe process's stdout.

    ``kind`` is ``"observed"``, ``"error"``, or ``"absent"``. The last
    well-formed marker wins, which is what an honest run produces; marker
    ordering is not relied on as a security property (see
    ``_probe_driver.py``).
    """
    kind, payload = "absent", None
    for line in stdout.splitlines():
        if line.startswith(OBSERVATION_MARKER):
            try:
                payload = json.loads(line[len(OBSERVATION_MARKER):])
            except (TypeError, ValueError):
                kind, payload = "error", "unparseable observation payload"
            else:
                kind = "observed"
        elif line.startswith(ERROR_MARKER):
            try:
                payload = json.loads(line[len(ERROR_MARKER):])
            except (TypeError, ValueError):
                payload = "unparseable error payload"
            kind = "error"
    return kind, payload


def _run_probes(spec: FixtureSpec, submission: dict[str, str], probes: list[Probe],
                probe_seed: int, workdir: str, started: float) -> FixtureResult:
    real_workdir = os.path.realpath(workdir)
    work = Path(real_workdir)
    task_id = spec.task_id

    for name in spec.starter_files:
        shutil.copy2(spec.starter_dir / name, work / name)
    for name, content in submission.items():
        (work / name).write_text(content, encoding="utf-8")

    driver_path = work / DRIVER_NAME
    shutil.copy2(DRIVER_SOURCE, driver_path)
    driver_hash_before = _sha256(driver_path)

    python_bin = os.path.realpath(sys.executable)
    profile_path = work / "_sandbox.sb"
    profile_path.write_text(build_seatbelt_profile(real_workdir, python_bin), encoding="utf-8")
    profile_hash_before = _sha256(profile_path)

    env = _minimal_env(real_workdir)
    cpu_seconds = int(spec.cpu_seconds)
    max_file_size_bytes = int(spec.max_file_size_bytes)
    max_processes = int(spec.max_processes)

    def _preexec() -> None:
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_size_bytes, max_file_size_bytes))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))
        except (ValueError, OSError):
            pass  # best-effort defense-in-depth; the exec restriction is the primary guard

    outcomes: list[ProbeOutcome] = []
    transcript: list[str] = []
    stderr_chunks: list[str] = []
    last_returncode: int | None = None
    deadline = time.perf_counter() + spec.total_timeout_seconds
    budget_exhausted = False

    probe_input = work / PROBE_INPUT_NAME

    for probe in probes:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            budget_exhausted = True
            outcomes.append(ProbeOutcome(
                probe.probe_id, False, "timeout", probe.description, None, 0.0, None,
                repr(probe.expected), f"fixture budget of {spec.total_timeout_seconds}s exhausted before this probe ran",
            ))
            continue

        # Only the probe currently running is ever present on disk, so
        # candidate code cannot read ahead to the inputs of probes it has
        # not been given yet.
        probe_input.write_text(probe.source, encoding="utf-8")

        cmd = [SANDBOX_EXEC, "-f", str(profile_path), python_bin, "-B", "-E", "-s",
               DRIVER_NAME, PROBE_INPUT_NAME]
        probe_started = time.perf_counter()
        timed_out = False
        # Pipes, not files in the work directory: a candidate can write to
        # any path it is allowed to write, so an evidence log living inside
        # the sandbox would be candidate-editable.
        proc = subprocess.Popen(cmd, cwd=real_workdir, env=env, preexec_fn=_preexec,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out_bytes, err_bytes = proc.communicate(timeout=min(spec.timeout_seconds, remaining))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out_bytes, err_bytes = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                out_bytes, err_bytes = proc.communicate(timeout=5)
        finally:
            probe_input.unlink(missing_ok=True)

        elapsed = time.perf_counter() - probe_started
        stdout_text = (out_bytes or b"").decode("utf-8", errors="replace")
        stderr_text = (err_bytes or b"").decode("utf-8", errors="replace")
        last_returncode = proc.returncode
        transcript.append(f"--- probe {probe.probe_id} (rc={proc.returncode}, {elapsed:.3f}s) ---\n{stdout_text}")
        if stderr_text.strip():
            stderr_chunks.append(f"--- probe {probe.probe_id} ---\n{stderr_text}")

        kind, payload = _parse_observation(stdout_text)
        if timed_out:
            status, detail, passed = "timeout", f"probe exceeded {spec.timeout_seconds}s", False
        elif proc.returncode == -signal.SIGXCPU:
            status, detail, passed = "cpu-limit-exceeded", f"probe exceeded {spec.cpu_seconds}s CPU", False
        elif kind == "absent":
            # The submission exited, crashed, or suppressed the driver
            # before it could report. Silence is a failure.
            status, detail, passed = "no-observation", "probe produced no observation", False
        elif kind == "error":
            status, detail, passed = "probe-error", str(payload)[:500], False
        elif observations_match(payload, probe.expected):
            status, detail, passed = "passed", None, True
        else:
            status, detail, passed = "failed", "observation did not match the expected value", False

        outcomes.append(ProbeOutcome(
            probe.probe_id, passed, status, probe.description, proc.returncode, elapsed,
            None if kind == "absent" else repr(payload), repr(probe.expected), detail,
        ))

    stdout_text, stdout_trunc = _cap("".join(transcript), spec.max_output_bytes)
    stderr_text, stderr_trunc = _cap("".join(stderr_chunks), spec.max_output_bytes)
    passed_count = sum(outcome.passed for outcome in outcomes)

    # The driver and the profile are verifier-owned and live inside a
    # directory the candidate can write. If either changed, no observation
    # from this run is trustworthy.
    if _sha256(driver_path) != driver_hash_before or _sha256(profile_path) != profile_hash_before:
        return FixtureResult(
            task_id, False, "tamper-detected", last_returncode, time.perf_counter() - started,
            stdout_text, stderr_text, stdout_trunc, stderr_trunc,
            "verifier-owned driver or sandbox profile changed on disk during execution",
            probes_total=len(probes), probes_passed=passed_count,
            probe_seed=probe_seed, probes=tuple(outcomes),
        )

    success = passed_count == len(probes) and len(probes) > 0

    if success:
        status, detail = "passed", None
    else:
        failures = [outcome for outcome in outcomes if not outcome.passed]
        # The whole fixture takes the status of its first failure, so a
        # timeout is never reported as a wrong answer.
        status = failures[0].status if failures else "failed"
        if budget_exhausted:
            status = "timeout"
        detail = (
            f"{passed_count}/{len(probes)} probes passed; "
            f"first failure: {failures[0].probe_id} ({failures[0].status})" if failures else None
        )

    return FixtureResult(
        task_id, success, status, last_returncode, time.perf_counter() - started,
        stdout_text, stderr_text, stdout_trunc, stderr_trunc, detail,
        probes_total=len(probes), probes_passed=passed_count,
        probe_seed=probe_seed, probes=tuple(outcomes),
    )
