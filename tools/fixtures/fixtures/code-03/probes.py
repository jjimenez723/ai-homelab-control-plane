"""Verifier-owned probes for code-03 (fix the off-by-one list chunker)."""

from __future__ import annotations

import random

from tools.fixtures.probes import Probe, call_probe


def reference(items: list, size: int) -> list[list]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _conservation_probe(items: list, size: int, *, probe_id: str) -> Probe:
    """Observe that no element is dropped and no chunk exceeds ``size``.

    The original bug silently discarded one element per chunk, so a probe
    that only checks shape would miss it. This one reports the flattened
    output and the widest chunk, and the verifier compares both.
    """
    source = (
        "from solution import chunk\n"
        f"_groups = chunk({items!r}, {size!r})\n"
        "OBSERVED = {\n"
        "    'flat': [item for group in _groups for item in group],\n"
        "    'max_len': max((len(group) for group in _groups), default=0),\n"
        "}\n"
    )
    expected = {"flat": list(items), "max_len": max((len(g) for g in reference(items, size)), default=0)}
    return Probe(probe_id=probe_id, source=source, expected=expected,
                 description=f"chunk conserves every element of {len(items)} items at size {size}")


def build(rng: random.Random) -> list[Probe]:
    fixed = [
        ("even-split", ([1, 2, 3, 4], 2)),
        ("trailing-partial", ([1, 2, 3, 4, 5], 2)),
        ("size-equals-length", ([1, 2, 3], 3)),
        ("size-one", ([1, 2, 3], 1)),
        ("empty-input", ([], 3)),
    ]
    randomized = [
        (f"random-{index}", (list(range(rng.randint(1, 30))), rng.randint(1, 7)))
        for index in range(3)
    ]
    probes = [
        call_probe("chunk", args, probe_id=probe_id, expected=reference(*args),
                   description=f"chunk({args[0]!r}, {args[1]})")
        for probe_id, args in fixed + randomized
    ]
    probes.append(_conservation_probe(list(range(17)), 5, probe_id="no-items-dropped"))
    return probes
