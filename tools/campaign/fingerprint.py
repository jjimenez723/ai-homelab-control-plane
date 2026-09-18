"""Stable configuration fingerprints, so a comparison cannot silently mix runs.

A fingerprint covers every input the task brief names as relevant to
measured behavior: model, model digest, quantization, runtime, prompt id and
version, suite and version, tool-schema version, sampling configuration, and
an augmentation/skill version (recorded even though no skill applies today --
`docs/19-model-evaluation.md` section 10's D-046 finding is precisely that
"no skill exists yet" is itself a fact worth pinning, not a null field).

Fingerprints are computed from canonical JSON (sorted keys, no
insignificant whitespace) so field order in a YAML manifest never changes
the hash. Changing anything included here changes the fingerprint, which
changes every work-unit id built from it (`tools/campaign/execution.py`) --
so a meaningfully different configuration is mechanically a different set of
work units, never a silent overwrite of a prior one's results.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sha256_of(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptRef:
    prompt_id: str
    prompt_version: str


@dataclass(frozen=True)
class Candidate:
    """One evaluated execution configuration, minus the suite it runs against.

    ``prompts`` maps suite id -> the exact pinned `PromptRef` used for that
    suite, mirroring the certification schema's own pinning rule
    (`tools/models/certification.py::Certification.configuration_matches`):
    an unpinned prompt version is a mismatch, never a wildcard.
    """

    name: str
    model: str
    runtime: str
    endpoint: str
    tool_schema_version: str
    prompts: dict[str, PromptRef]
    model_digest: str | None = None
    quantization: str | None = None
    sampling: dict[str, Any] | None = None
    augmentation_version: str = "none"
    reasoning: bool = False
    """Whether this model's runtime emits a separate reasoning channel that
    shares the output budget (D-059). Part of the identity because it changes
    what ``num_predict`` has to be for the answer to survive at all -- a
    reasoning candidate measured at a non-reasoning budget is a different
    (and, as `gate2-validation-001` showed, meaningless) measurement."""

    def candidate_identity(self) -> dict[str, Any]:
        """The model/runtime-only identity, independent of any suite."""
        return {
            "model": self.model, "model_digest": self.model_digest,
            "quantization": self.quantization, "runtime": self.runtime,
            "tool_schema_version": self.tool_schema_version,
            "sampling": dict(sorted((self.sampling or {}).items())),
            "augmentation_version": self.augmentation_version,
            "reasoning": self.reasoning,
        }


def candidate_fingerprint(candidate: Candidate) -> str:
    """Model/runtime/sampling identity only -- the same across every suite."""
    return sha256_of(candidate.candidate_identity())


def configuration_fingerprint(candidate: Candidate, *, suite_id: str, suite_version: str) -> str:
    """The full evaluated-unit fingerprint: candidate + suite + pinned prompt.

    This is what makes two runs comparable or not: if a suite version, a
    prompt version, or any candidate field differs, this hash differs, and
    the campaign store (`tools/campaign/store.py`) treats it as different
    work entirely rather than contaminating prior results.
    """
    if suite_id not in candidate.prompts:
        raise ValueError(f"candidate {candidate.name!r} has no pinned prompt for suite {suite_id!r}")
    prompt_ref = candidate.prompts[suite_id]
    payload = dict(candidate.candidate_identity())
    payload.update({
        "suite": suite_id, "suite_version": suite_version,
        "prompt_id": prompt_ref.prompt_id, "prompt_version": prompt_ref.prompt_version,
    })
    return sha256_of(payload)
