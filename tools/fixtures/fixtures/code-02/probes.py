"""Verifier-owned probes for code-02 (safe ASCII slugify)."""

from __future__ import annotations

import random
import re

from tools.fixtures.probes import Probe, call_probe

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_WORDS = ("Quarterly", "Report", "Kitchen", "Install", "Invoice", "Draft",
          "Client", "Review", "Photo", "Archive", "Release", "Notes")
_JOINERS = (" ", "   ", "-", "_", "! ", " & ", "?", ", ")


def reference(text: str) -> str:
    """The slug contract from task.md, written once, here, by the verifier."""
    return _NON_ALNUM.sub("-", text.lower()).strip("-")


def build(rng: random.Random) -> list[Probe]:
    fixed = [
        ("basic", "Hello World"),
        ("extra-whitespace", "  Multiple   Spaces  "),
        ("already-slugged", "already-slugged"),
        ("punctuation", "Punctuation! & Symbols?"),
        ("empty", ""),
        ("all-punctuation", "!!!???"),
        ("trailing-hyphens", "Trailing---Hyphens--"),
        ("digits", "Version 2 Release"),
    ]
    randomized = []
    for index in range(3):
        words = rng.sample(_WORDS, rng.randint(2, 4))
        phrase = "".join(
            word + (rng.choice(_JOINERS) if position < len(words) - 1 else "")
            for position, word in enumerate(words)
        )
        randomized.append((f"random-{index}", phrase))
    return [
        call_probe("slugify", (text,), probe_id=probe_id, expected=reference(text),
                   description=f"slugify({text!r})")
        for probe_id, text in fixed + randomized
    ]
