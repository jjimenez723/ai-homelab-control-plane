"""Verifier-owned probes for code-04 (JSON output for a pure function)."""

from __future__ import annotations

import random

from tools.fixtures.probes import Probe, call_probe

# Observe the parsed JSON rather than the raw string: a candidate that
# returns a dict instead of a str fails the type probe below, and one that
# returns valid JSON with the wrong contents fails these.
_PARSE = "__import__('json').loads({call})"
_TYPE_NAME = "type({call}).__name__"


def reference(numbers) -> dict:
    """`summarize`'s contract, restated by the verifier rather than imported.

    D-046 recorded that this fixture's characteristic failure was a model
    replacing the whole file and dropping `summarize`, which it was told to
    preserve. Keeping the verifier's own copy here means the probes still
    know the right answer when the candidate has deleted the function.
    """
    numbers = list(numbers)
    total = sum(numbers)
    count = len(numbers)
    return {"count": count, "sum": total, "avg": (total / count) if count else 0}


def build(rng: random.Random) -> list[Probe]:
    inputs = [
        ("parses-to-expected-dict", [1, 2, 3]),
        ("empty-input", []),
        ("negative-numbers", [-1, -2, -3]),
        ("single-value", [10]),
    ]
    inputs += [
        (f"random-{index}", [rng.randint(-500, 500) for _ in range(rng.randint(1, 8))])
        for index in range(3)
    ]

    probes = [
        call_probe("summarize_json", (numbers,), probe_id=probe_id,
                   expected=reference(numbers), transform=_PARSE,
                   description=f"json.loads(summarize_json({numbers!r}))")
        for probe_id, numbers in inputs
    ]
    probes.append(
        call_probe("summarize_json", ([1, 2, 3],), probe_id="returns-a-string",
                   expected="str", transform=_TYPE_NAME,
                   description="summarize_json returns str, not a dict")
    )
    # The task says not to change `summarize`. That instruction was
    # previously unverified, so a submission could satisfy every test while
    # deleting it. Now it cannot.
    probes.append(
        call_probe("summarize", ([1, 2, 3],), probe_id="summarize-preserved",
                   expected=reference([1, 2, 3]),
                   description="the pre-existing summarize() still behaves as before")
    )
    return probes
