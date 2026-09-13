# code-02 — safe ASCII slugify

In the disposable fixture, replace the `TODO` in `solution.py` with a safe
slugify function for ASCII words.

- File to edit: `solution.py`
- Function signature: `def slugify(text):`
- Lower-case the text, replace runs of whitespace/punctuation with a single
  `-`, strip any leading/trailing `-`, and keep only `[a-z0-9-]` characters.
- An empty or all-punctuation input slugifies to `""`.

Return only the full contents of `solution.py`. A hidden, verifier-owned
test suite determines success — your own claim about whether tests pass is
never evidence.
