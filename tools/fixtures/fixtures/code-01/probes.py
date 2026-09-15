"""Verifier-owned probes for code-01 (add two integers).

`build(rng)` returns bounded interactions plus the answers the verifier
keeps. Nothing here is copied into the sandbox except each probe's
`source`, which carries inputs only.
"""

from __future__ import annotations

import random

from tools.fixtures.probes import Probe, call_probe


def reference(a: int, b: int) -> int:
    return a + b


def build(rng: random.Random) -> list[Probe]:
    fixed = [
        ("positive", (2, 3)),
        ("negative", (-2, -3)),
        ("zero", (0, 0)),
        ("mixed-sign", (-5, 10)),
    ]
    randomized = [
        (f"random-{index}", (rng.randint(-10_000, 10_000), rng.randint(-10_000, 10_000)))
        for index in range(3)
    ]
    return [
        call_probe("add", args, probe_id=probe_id, expected=reference(*args),
                   description=f"add{args}")
        for probe_id, args in fixed + randomized
    ]
