"""Phase 3 semantic catalog primitives.

The package is intentionally split into three seams:

* :mod:`contract` is the model-facing, plain-data contract;
* :mod:`manifests` resolves canonical client/project identities;
* :mod:`index` and :mod:`materializer` are trusted catalog/view operations.

Model output is always a proposal. Only catalog methods and the explicit
materializer can persist or copy anything.
"""

from .contract import (
    Candidate,
    SemanticEvidence,
    SemanticProposal,
    parse_semantic_output,
)

__all__ = [
    "Candidate",
    "SemanticEvidence",
    "SemanticProposal",
    "parse_semantic_output",
]
