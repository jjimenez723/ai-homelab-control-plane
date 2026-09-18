"""File naming: deciding whether a name is useful, proposing a better one, and
performing the rename.

The split between these modules is the safety boundary, not tidiness:

* `quality.py`  -- is the existing name already useful? Fully deterministic.
* `evidence.py` -- what do we actually know about this file? Deterministic
                   probing that produces **plain data only**: no `Path`, no
                   file handle. A proposer therefore cannot reach the
                   filesystem even by accident. Also home to
                   `PerceivedEvidence`, the bounded plain-data form perceived
                   content takes when it crosses into naming (D-057).
* `proposer.py` -- turns evidence into a *suggestion*. This is the seam a model
                   plugs into. It returns a string and a confidence. It never
                   renames, moves, or deletes anything.
* `semantic.py` -- the opt-in, model-backed proposer (`naming.proposer:
                   "semantic"`). Talks to a loopback-only local model through
                   the same versioned prompt the Gate-2 naming suite
                   evaluates, refuses anything path-shaped or
                   extension-changing before `policy.py` even sees it, and
                   falls back to the deterministic proposer on any failure.
* `policy.py`   -- validates a suggestion against filename rules and performs
                   the rename, collision-safely and without overwriting.
* `rename.py`   -- the gate: the order of its checks is the order of the
                   guarantees, and D-057 did not change it.

A model may propose a filename. It does not receive filesystem authority
(invariant 3). `policy.safe_rename` is the only function in this package that
mutates anything, and `tests/ingest/test_mutation_authority.py` asserts no
other module here imports a mutation primitive.

**No module in this package may import `tools.ingest.evidence`**, and an AST
guard in `tests/ingest/test_evidence_security.py` enforces it. Perception
reaches naming as `PerceivedEvidence`, converted outside this package by
`tools/ingest/perception_bridge.py` -- one reviewed module, one import pair,
zero filesystem mutation. That is what makes "naming may read perception"
narrower than "naming may reach the perception layer".
"""
