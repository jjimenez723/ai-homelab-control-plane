# CI Policy

The repository's baseline CI validates the Python-based social-video tooling on pull requests and on pushes to `main`.

Current required checks:

- Python source compilation;
- unit tests under `tests/`.

Workflow security requirements:

- default token permissions are read-only;
- checkout credentials are not persisted;
- third-party GitHub Actions are pinned to full commit SHAs;
- jobs have explicit timeouts;
- dependency installation uses the tracked requirements file.

As the repository gains additional executable components, CI should expand only to cover real acceptance gates. Avoid adding expensive or flaky checks that make the default branch difficult to maintain.
