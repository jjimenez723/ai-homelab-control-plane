"""Trusted code-fixture runner (D-038, rebuilt by D-047).

Executes a model-produced code submission against verifier-owned probes
inside a bounded, isolated environment, and returns PASS/FAIL evidence that
never trusts a model's own claim about its result -- nor any signal the
candidate's own process controls. Comparison against expected values
happens outside the sandbox; a submission that exits, hangs, or subverts
its process produces no observation, and no observation is a failure.

See ``tools/fixtures/probes.py`` for the trust boundary,
``tools/fixtures/runner.py`` for execution, ``tools/fixtures/sandbox.py``
for isolation, and ``docs/19-model-evaluation.md`` for the architecture
this satisfies.
"""

from .probes import Probe, build_probes, observations_match
from .runner import FixtureResult, ProbeOutcome, evaluate_submission, submission_from_model_output
from .spec import FixtureSpec, available_fixtures, load_fixture

__all__ = [
    "FixtureResult",
    "FixtureSpec",
    "Probe",
    "ProbeOutcome",
    "available_fixtures",
    "build_probes",
    "evaluate_submission",
    "load_fixture",
    "observations_match",
    "submission_from_model_output",
]
