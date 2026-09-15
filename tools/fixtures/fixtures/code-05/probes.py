"""Verifier-owned probes for code-05 (timeout-safe retry helper).

`retry` takes a callable, so these probes build the callable inside the
sandbox and report what was observed about it -- the result, how many times
it was invoked, and which exception escaped. The verifier holds the
expected triples.
"""

from __future__ import annotations

import random

from tools.fixtures.probes import Probe

_PREAMBLE = "from solution import retry\n_calls = []\n"


def _flaky(succeed_on: int, token: str) -> str:
    return (
        "def _fn():\n"
        "    _calls.append(1)\n"
        f"    if len(_calls) < {succeed_on}:\n"
        "        raise ValueError('not yet')\n"
        f"    return {token!r}\n"
    )


def build(rng: random.Random) -> list[Probe]:
    probes: list[Probe] = [
        Probe(
            probe_id="succeeds-after-failures",
            source=_PREAMBLE + _flaky(3, "ok") + (
                "OBSERVED = {'result': retry(_fn, attempts=5, delay=0), 'calls': len(_calls)}\n"
            ),
            expected={"result": "ok", "calls": 3},
            description="returns the first success and stops calling after it",
        ),
        Probe(
            probe_id="reraises-after-exhausting-attempts",
            source=_PREAMBLE + (
                "def _fn():\n"
                "    _calls.append(1)\n"
                "    raise RuntimeError('nope')\n"
                "try:\n"
                "    retry(_fn, attempts=3, delay=0)\n"
                "    _raised = '<no-exception>'\n"
                "except BaseException as exc:\n"
                "    _raised = type(exc).__name__\n"
                "OBSERVED = {'raised': _raised, 'calls': len(_calls)}\n"
            ),
            expected={"raised": "RuntimeError", "calls": 3},
            description="the last exception escapes rather than being swallowed",
        ),
        Probe(
            probe_id="respects-attempt-count",
            source=_PREAMBLE + (
                "def _fn():\n"
                "    _calls.append(1)\n"
                "    raise ValueError('boom')\n"
                "try:\n"
                "    retry(_fn, attempts=4, delay=0)\n"
                "    _raised = '<no-exception>'\n"
                "except BaseException as exc:\n"
                "    _raised = type(exc).__name__\n"
                "OBSERVED = {'raised': _raised, 'calls': len(_calls)}\n"
            ),
            expected={"raised": "ValueError", "calls": 4},
            description="attempts is an exact budget, not a suggestion",
        ),
        Probe(
            probe_id="first-attempt-success-calls-once",
            source=_PREAMBLE + _flaky(1, "first-try") + (
                "OBSERVED = {'result': retry(_fn, attempts=5, delay=0), 'calls': len(_calls)}\n"
            ),
            expected={"result": "first-try", "calls": 1},
            description="a success on the first attempt costs exactly one call",
        ),
        Probe(
            probe_id="completes-quickly-without-delay",
            source=_PREAMBLE + (
                "import time\n"
                "def _fn():\n"
                "    _calls.append(1)\n"
                "    raise ValueError('x')\n"
                "_started = time.time()\n"
                "try:\n"
                "    retry(_fn, attempts=3, delay=0)\n"
                "except BaseException:\n"
                "    pass\n"
                "OBSERVED = (time.time() - _started) < 2.0\n"
            ),
            expected=True,
            description="delay=0 means no sleeping",
        ),
    ]

    for index in range(2):
        attempts = rng.randint(3, 6)
        succeed_on = rng.randint(1, attempts)
        token = f"done-{rng.randint(1000, 9999)}"
        probes.append(Probe(
            probe_id=f"random-{index}",
            source=_PREAMBLE + _flaky(succeed_on, token) + (
                f"OBSERVED = {{'result': retry(_fn, attempts={attempts}, delay=0), 'calls': len(_calls)}}\n"
            ),
            expected={"result": token, "calls": succeed_on},
            description=f"succeeds on attempt {succeed_on} of {attempts}",
        ))
    return probes
