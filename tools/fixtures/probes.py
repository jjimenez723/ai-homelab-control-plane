"""Verifier-owned probes: the only thing that decides a code fixture's verdict.

Why this exists at all
----------------------

The first fixture runner (D-045) executed verifier-owned ``unittest`` files
*in the same process that imported the candidate's code*, and read the
process exit code as the verdict. That is not a verdict a candidate cannot
forge: ``os._exit(0)`` at import time exits 0 before a single assertion
runs, and rebinding ``unittest.TestCase.assertEqual`` to a no-op makes every
assertion vacuous. Both produced ``status="passed"`` on objectively wrong
code (D-047).

The fix is structural, not a patch: **no verifier-owned comparison logic
ever runs inside the sandbox.** A probe is split in two halves that live on
opposite sides of the trust boundary:

* ``source`` runs *inside* the sandbox. It imports the candidate, performs
  exactly one bounded interaction, and assigns the result to ``OBSERVED``.
  It contains inputs, never answers.
* ``expected`` stays *outside* the sandbox, in the parent process, and is
  compared against whatever the sandboxed process reported.

What that buys, precisely:

* a candidate that exits, hangs, crashes, or subverts its own process
  produces no observation, and **absence of an observation is a failure**,
  never a pass -- the runner fails closed by construction rather than by a
  check someone remembered to write;
* a candidate cannot read the expected values, because they are never
  written into the disposable directory or the probe source;
* a candidate cannot see the other probes' inputs, because each probe runs
  in its own short-lived process;
* randomised inputs (``build(rng)`` receives a freshly seeded ``Random``)
  mean a lookup table keyed on fixed inputs does not survive a second run.

The residual, stated honestly: within its own process a candidate can print
a forged observation for the one input it was given. To be *accepted* that
forgery must equal the correct answer for that input, which is the same
thing the task asks for. This is the ordinary limit of black-box testing,
not a defect specific to this runner, and randomised inputs are what keep
"compute the answer" from collapsing into "recall the answer".
"""

from __future__ import annotations

import importlib.util
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Emitted by the in-sandbox driver on its own line. The parent reads the
# last well-formed marker; anything else on stdout is candidate noise.
OBSERVATION_MARKER = "@@ANGEL-OBSERVED@@ "
ERROR_MARKER = "@@ANGEL-ERROR@@ "


@dataclass(frozen=True)
class Probe:
    """One bounded interaction with a candidate submission.

    ``source`` is executed inside the sandbox and must assign ``OBSERVED``.
    ``expected`` is held by the verifier and never crosses the boundary.
    """

    probe_id: str
    source: str
    expected: Any
    description: str = ""


def call_probe(entry: str, args: tuple[Any, ...], *, probe_id: str, expected: Any,
               description: str = "", transform: str | None = None) -> Probe:
    """A probe that imports ``entry`` and calls it once with ``args``.

    ``transform`` wraps the call expression when the observable is a
    property of the result rather than the result itself (for example
    ``type({}).__name__`` to check a return type without trusting the
    candidate to describe its own output).
    """
    call = f"{entry}({', '.join(repr(arg) for arg in args)})"
    expression = transform.format(call=call) if transform else call
    source = f"from solution import {entry}\nOBSERVED = {expression}\n"
    return Probe(probe_id=probe_id, source=source, expected=expected, description=description)


def raises_probe(entry: str, setup: str, call: str, *, probe_id: str, expected: Any,
                 description: str = "") -> Probe:
    """A probe whose observable is *which* exception a call raised.

    Recorded as the exception class name, or ``"<no-exception>"`` when the
    call returned normally -- so "swallowed the error" and "raised the
    wrong error" are different observations rather than both being absent.
    """
    source = (
        f"from solution import {entry}\n"
        f"{setup}\n"
        "try:\n"
        f"    {call}\n"
        "    OBSERVED = '<no-exception>'\n"
        "except BaseException as exc:\n"
        "    OBSERVED = type(exc).__name__\n"
    )
    return Probe(probe_id=probe_id, source=source, expected=expected, description=description)


def load_probe_builder(probes_path: Path, task_id: str) -> Callable[[random.Random], list[Probe]]:
    """Import a fixture's verifier-owned ``probes.py`` and return ``build``.

    Loaded by file path rather than package name so a fixture directory
    stays a self-contained bundle and two fixtures cannot collide on a
    module name.
    """
    spec = importlib.util.spec_from_file_location(f"angel_probes_{task_id}", probes_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"fixture {task_id!r} has an unloadable probes module at {probes_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    builder = getattr(module, "build", None)
    if not callable(builder):
        raise ValueError(f"fixture {task_id!r} probes module defines no callable build(rng)")
    return builder


def build_probes(probes_path: Path, task_id: str, *, seed: int | None = None) -> tuple[list[Probe], int]:
    """Build this run's probe set, returning it with the seed that made it.

    The seed is recorded in the result so a failing run can be replayed
    exactly; randomisation exists to defeat answer tables, not to make
    failures irreproducible.
    """
    if seed is None:
        seed = random.SystemRandom().randrange(2**31)
    probes = list(load_probe_builder(probes_path, task_id)(random.Random(seed)))
    if not probes:
        raise ValueError(f"fixture {task_id!r} built an empty probe set")
    seen = [probe.probe_id for probe in probes]
    if len(set(seen)) != len(seen):
        raise ValueError(f"fixture {task_id!r} has duplicate probe ids: {seen}")
    return probes, seed


def observations_match(observed: Any, expected: Any, *, tolerance: float = 1e-9) -> bool:
    """Compare an observation with the expected value the verifier holds.

    JSON transport turns tuples into lists and can turn an integral float
    into an int, so this compares by structure and value rather than by
    serialized text. Floats compare within a relative tolerance -- the
    ``unittest`` suite this replaced used ``assertAlmostEqual`` for exactly
    that reason, and tightening it here would fail correct submissions.
    """
    if isinstance(expected, bool) or isinstance(observed, bool):
        return observed is expected
    if isinstance(expected, float) or isinstance(observed, float):
        if not isinstance(observed, (int, float)) or not isinstance(expected, (int, float)):
            return False
        return abs(float(observed) - float(expected)) <= tolerance * max(1.0, abs(float(expected)))
    if isinstance(expected, (list, tuple)):
        if not isinstance(observed, (list, tuple)) or len(observed) != len(expected):
            return False
        return all(observations_match(o, e, tolerance=tolerance) for o, e in zip(observed, expected))
    if isinstance(expected, dict):
        if not isinstance(observed, dict) or set(observed) != set(expected):
            return False
        return all(observations_match(observed[key], expected[key], tolerance=tolerance) for key in expected)
    return observed == expected
