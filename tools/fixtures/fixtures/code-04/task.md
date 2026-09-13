# code-04 — add JSON output for a pure function

In the disposable fixture, `solution.py` defines a pure function
`summarize(numbers)` that returns a `dict` with `count`, `sum`, and `avg`
keys. Replace the `TODO` with `summarize_json(numbers)`, which returns the
same information serialized as a JSON string.

- File to edit: `solution.py`
- Function signature: `def summarize_json(numbers):`
- Must return a `str` that `json.loads` can parse back into a dict equal to
  `summarize(numbers)`. Do not change `summarize` itself.
- `numbers` may be empty; `summarize([])` defines `avg` as `0`.

Return only the full contents of `solution.py`. A hidden, verifier-owned
test suite determines success — your own claim about whether tests pass is
never evidence.
