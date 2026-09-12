"""File naming: deciding whether a name is useful, proposing a better one, and
performing the rename.

The split between these four modules is the safety boundary, not tidiness:

* `quality.py`  -- is the existing name already useful? Fully deterministic.
* `evidence.py` -- what do we actually know about this file? Deterministic
                   probing that produces **plain data only**: no `Path`, no
                   file handle. A proposer therefore cannot reach the
                   filesystem even by accident.
* `proposer.py` -- turns evidence into a *suggestion*. This is the seam a model
                   plugs into. It returns a string and a confidence. It never
                   renames, moves, or deletes anything.
* `policy.py`   -- validates a suggestion against filename rules and performs
                   the rename, collision-safely and without overwriting.

A model may propose a filename. It does not receive filesystem authority
(invariant 3). `policy.safe_rename` is the only function in this package that
mutates anything, and `tests/ingest/test_mutation_authority.py` asserts no
other module here imports a mutation primitive.
"""
