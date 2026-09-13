# code-03 — fix the off-by-one in a list chunker

In the disposable fixture, `solution.py` defines `chunk(items, size)`, which
should split `items` into consecutive sub-lists of length `size` (the final
chunk may be shorter). It has an off-by-one bug. Fix it.

- File to edit: `solution.py`
- Function signature: `def chunk(items, size):`
- `chunk([1, 2, 3, 4, 5], 2)` must return `[[1, 2], [3, 4], [5]]`.
- `chunk([], size)` must return `[]`.

Return only the full, fixed contents of `solution.py`. A hidden,
verifier-owned test suite determines success — your own claim about whether
tests pass is never evidence.
