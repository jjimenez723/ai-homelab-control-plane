from __future__ import annotations

import hashlib
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .sandbox import SANDBOX_EXEC, build_seatbelt_profile, sandbox_available
from .spec import FixtureSpec, load_fixture


@dataclass(frozen=True)
class FixtureResult:
    """PASS/FAIL evidence for one code-fixture execution.

    ``status`` distinguishes *why* ``success`` is false: a candidate-code
    failure (``failed``, ``timeout``, ``cpu-limit-exceeded``) is a different
    kind of event from a verifier-side problem (``verifier-error``,
    ``sandbox-unavailable``) or a submission-shape problem
    (``invalid-submission``, ``tamper-detected``). Callers must not collapse
    these into one boolean when reporting evidence.
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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fail(task_id: str, status: str, started: float, detail: str, returncode: int | None = None) -> FixtureResult:
    return FixtureResult(task_id, False, status, returncode, time.perf_counter() - started, "", "", False, False, detail)


def _minimal_env(workdir: str) -> dict[str, str]:
    """An explicit allowlist, never the harness's own ``os.environ``.

    No ambient credential, token, or repository path reaches the candidate
    process this way. See ``sandbox.py`` for the full rationale.
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


def _read_capped(path: Path, cap: int) -> tuple[str, bool]:
    if not path.is_file():
        return "", False
    data = path.read_bytes()
    truncated = len(data) > cap
    return data[:cap].decode("utf-8", errors="replace"), truncated


def submission_from_model_output(output: Any, editable_files: tuple[str, ...]) -> dict[str, str] | None:
    """Extract a ``{filename: content}`` submission from raw model output.

    A model's own claim about its result (``tests_passed``, ``success``, and
    similar fields) is never read here and never influences the verdict —
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


def evaluate_submission(task_id: str, submission: dict[str, str] | None, *, root: Path | None = None) -> FixtureResult:
    """Run ``submission`` against the hidden tests for ``task_id``.

    ``submission`` maps editable filenames to their full replacement
    content. Returns a :class:`FixtureResult` that is always evidence —
    every code path below returns rather than raises, and the top-level
    ``except`` guarantees a ``verifier-error`` result instead of letting an
    unexpected internal exception look like a candidate failure or, worse,
    escape unhandled and be mistaken for something outside this evaluation.
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
    if set(submission) - allowed or set(submission) != allowed:
        return _fail(task_id, "invalid-submission", started, f"submission files must be exactly {sorted(allowed)}, got {sorted(submission)}")

    for name, content in submission.items():
        if not isinstance(content, str) or not content.strip():
            return _fail(task_id, "invalid-submission", started, f"submission file {name!r} must be non-empty text")
        if len(content.encode("utf-8")) > spec.max_file_size_bytes:
            return _fail(task_id, "invalid-submission", started, f"submission file {name!r} exceeds {spec.max_file_size_bytes} byte limit")

    if not sandbox_available():
        return _fail(task_id, "sandbox-unavailable", started, f"{SANDBOX_EXEC} not found; refusing to execute untrusted code unsandboxed")

    workdir = tempfile.mkdtemp(prefix=f"angel-fixture-{task_id}-")
    try:
        return _run_in_sandbox(spec, submission, workdir, started)
    except Exception as exc:
        return _fail(task_id, "verifier-error", started, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_in_sandbox(spec: FixtureSpec, submission: dict[str, str], workdir: str, started: float) -> FixtureResult:
    real_workdir = os.path.realpath(workdir)
    task_id = spec.task_id

    for name in spec.starter_files:
        shutil.copy2(spec.starter_dir / name, Path(real_workdir) / name)
    for name, content in submission.items():
        (Path(real_workdir) / name).write_text(content, encoding="utf-8")

    # Hidden tests are copied in last, so they always win over any
    # same-named file a submission tried to place — even though submission
    # keys are already restricted to editable_files above, this ordering is
    # a second, structural layer against a hidden test ever being shadowed.
    hidden_sources = [spec.hidden_dir / name for name in spec.hidden_test_files]
    hidden_targets = [Path(real_workdir) / name for name in spec.hidden_test_files]
    before_hashes = [_sha256(p) for p in hidden_sources]
    for src, dst in zip(hidden_sources, hidden_targets):
        shutil.copy2(src, dst)

    python_bin = os.path.realpath(sys.executable)
    profile_path = Path(real_workdir) / "_sandbox.sb"
    profile_path.write_text(build_seatbelt_profile(real_workdir, python_bin), encoding="utf-8")

    stdout_path = Path(real_workdir) / "_stdout.log"
    stderr_path = Path(real_workdir) / "_stderr.log"

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
            pass  # best-effort defense-in-depth; exec restriction below is the primary guard

    cmd = [SANDBOX_EXEC, "-f", str(profile_path), python_bin, "-B", "-E", "-s", "-m", "unittest", spec.run_module, "-v"]
    env = _minimal_env(real_workdir)

    timed_out = False
    with open(stdout_path, "wb") as out_f, open(stderr_path, "wb") as err_f:
        proc = subprocess.Popen(
            cmd, cwd=real_workdir, env=env,
            stdout=out_f, stderr=err_f,
            preexec_fn=_preexec,
        )
        try:
            returncode = proc.wait(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                returncode = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait(timeout=5)

    elapsed = time.perf_counter() - started
    stdout_text, stdout_trunc = _read_capped(stdout_path, spec.max_output_bytes)
    stderr_text, stderr_trunc = _read_capped(stderr_path, spec.max_output_bytes)

    after_hashes = [_sha256(p) if p.is_file() else None for p in hidden_targets]
    if after_hashes != before_hashes:
        return FixtureResult(task_id, False, "tamper-detected", returncode, elapsed, stdout_text, stderr_text, stdout_trunc, stderr_trunc,
                              "hidden test file content changed on disk during execution")

    if timed_out:
        return FixtureResult(task_id, False, "timeout", returncode, elapsed, stdout_text, stderr_text, stdout_trunc, stderr_trunc,
                              f"execution exceeded {spec.timeout_seconds}s wall-clock limit")

    if returncode == -signal.SIGXCPU:
        return FixtureResult(task_id, False, "cpu-limit-exceeded", returncode, elapsed, stdout_text, stderr_text, stdout_trunc, stderr_trunc,
                              f"execution exceeded {spec.cpu_seconds}s CPU limit")

    if returncode == 0:
        return FixtureResult(task_id, True, "passed", returncode, elapsed, stdout_text, stderr_text, stdout_trunc, stderr_trunc)

    return FixtureResult(task_id, False, "failed", returncode, elapsed, stdout_text, stderr_text, stdout_trunc, stderr_trunc,
                          "hidden tests did not pass")
