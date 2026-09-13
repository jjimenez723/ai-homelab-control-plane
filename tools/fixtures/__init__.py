"""D-038 trusted code-fixture runner.

Executes a model-produced code submission against verifier-owned hidden
tests inside a bounded, isolated environment, and returns PASS/FAIL evidence
that never trusts a model's own claim about its result. See
``tools/fixtures/runner.py`` for the execution/isolation implementation and
``docs/19-model-evaluation.md`` for the architecture this satisfies.
"""

from .runner import FixtureResult, evaluate_submission
from .spec import FixtureSpec, load_fixture

__all__ = ["FixtureResult", "evaluate_submission", "FixtureSpec", "load_fixture"]
