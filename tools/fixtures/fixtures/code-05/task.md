# code-05 — timeout-safe retry helper

In the disposable fixture, replace the `TODO` in `solution.py` with a retry
helper that does not touch the filesystem.

- File to edit: `solution.py`
- Function signature: `def retry(fn, attempts=3, delay=0.0, exceptions=(Exception,)):`
- Call `fn()` up to `attempts` times. If it raises one of `exceptions`,
  wait `delay` seconds and try again. Return the first successful result.
- If every attempt raises, re-raise the last exception after the final
  attempt (do not swallow it).
- `fn` takes no arguments. Do not read or write any file.

Return only the full contents of `solution.py`. A hidden, verifier-owned
test suite determines success — your own claim about whether tests pass is
never evidence.
