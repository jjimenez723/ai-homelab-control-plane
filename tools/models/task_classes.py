"""The join between production task classes, capabilities, and ACB-v1 suites.

`docs/19-model-evaluation.md` §8 names the production task classes, §2.1
names the capability suites, and `tools/models/registry.py` names the
capabilities a model may declare. Until this module existed those were
three disconnected vocabularies: `RouteRequest.task_class` was recorded in
the ledger and consulted by nothing, and a request for `code` capability
was satisfied by any text-generation model because the registry aliased the
two. A router cannot select on measured capability while the words for
"what this job is" and "what this model was measured at" do not meet.

This is deliberately a small, versioned, inspectable table rather than
configuration. Adding a task class is a design decision with an evaluation
consequence -- something has to certify it -- so it belongs in reviewed
code next to the taxonomy it mirrors, not in a YAML file a deployment can
edit.

Nothing here asserts a model is *good* at anything. A declared capability
is a claim about shape; a certification (`tools/models/certification.py`)
is a claim about measured behaviour. Routing consults both.
"""

from __future__ import annotations

from dataclasses import dataclass

TASK_CLASS_VERSION = "1"


@dataclass(frozen=True)
class TaskClass:
    """One production task class and what it requires of a configuration.

    ``capabilities`` are the registry capabilities a candidate must declare.
    ``suite`` is the ACB-v1 capability suite whose certification makes a
    configuration eligible; ``None`` means no suite gates this class yet, in
    which case ``requires_certification`` must be False and the class is
    usable only in attended, human-reviewed work.
    """

    name: str
    capabilities: tuple[str, ...]
    suite: str | None
    requires_certification: bool
    default_risk: str
    description: str


# Mirrors docs/19 §8. `suite` values match ACB-v1 suite ids; the two Gate-2
# suites (routing/classification and semantic naming) are the only ones
# that gate today, which is exactly what D-044 says Gate 2's minimum is.
TASK_CLASSES: dict[str, TaskClass] = {
    "general": TaskClass(
        "general", (), None, False, "low",
        "Unclassified work. Attended use only -- nothing certifies it.",
    ),
    "classification": TaskClass(
        "classification", ("text_generation", "structured_output"),
        "acb-v1/routing-classification", True, "low",
        "Assign one label from a closed, versioned taxonomy.",
    ),
    "classification.semantic_catalog": TaskClass(
        "classification.semantic_catalog", ("text_generation", "structured_output"),
        None, False, "low",
        "Infer canonical client/project identities and bounded semantic tags. "
        "Phase 3 is attended until its additive capability suite is certified.",
    ),
    "routing": TaskClass(
        "routing", ("text_generation", "structured_output"),
        "acb-v1/routing-classification", True, "medium",
        "Emit a structured routing decision for another job.",
    ),
    "naming.semantic": TaskClass(
        "naming.semantic", ("text_generation", "structured_output"),
        "acb-v1/semantic-naming", True, "low",
        "Propose a canonical filename from evidence. Proposal only (D-022).",
    ),
    "extraction.structured": TaskClass(
        "extraction.structured", ("text_generation", "structured_output"),
        None, False, "low",
        "Bounded facts into a known schema. Suite not built yet.",
    ),
    "code.implementation": TaskClass(
        "code.implementation", ("text_generation", "code_generation"),
        None, False, "medium",
        "Bounded implementation against a spec. Phase 4/5 gate, not Gate 2.",
    ),
    "code.refactor": TaskClass(
        "code.refactor", ("text_generation", "code_generation"), None, False, "medium",
        "Behaviour-preserving change under test. Phase 4/5 gate.",
    ),
    "code.debug": TaskClass(
        "code.debug", ("text_generation", "code_generation"), None, False, "medium",
        "Diagnose and repair from failing tests. Phase 4/5 gate.",
    ),
    "code.testing": TaskClass(
        "code.testing", ("text_generation", "code_generation"), None, False, "medium",
        "Write tests for existing behaviour. Phase 4/5 gate.",
    ),
    "code.orchestration": TaskClass(
        "code.orchestration", ("text_generation", "structured_output", "code_generation"),
        None, False, "high",
        "Decompose and delegate. High risk: it decides what else runs.",
    ),
    "repo.documentation": TaskClass(
        "repo.documentation", ("text_generation",), None, False, "low",
        "Summarise or document a repository. Phase 4/5 gate.",
    ),
    "ui.implementation": TaskClass(
        "ui.implementation", ("text_generation", "code_generation"), None, False, "medium",
        "Frontend work against docs/14's design-system contract. Phase 4/5 gate.",
    ),
    "vision.describe": TaskClass(
        "vision.describe", ("vision",), None, False, "low",
        "Describe an image. Non-authoritative perception (D-029, D-036).",
    ),
}


class UnknownTaskClass(KeyError):
    """A task class no production taxonomy entry covers.

    Raised rather than defaulted: silently treating an unknown class as
    "general" would let a typo downgrade a certification requirement.
    """


def task_class(name: str) -> TaskClass:
    try:
        return TASK_CLASSES[name]
    except KeyError as exc:
        raise UnknownTaskClass(
            f"unknown task_class {name!r}; known classes: {sorted(TASK_CLASSES)}"
        ) from exc


def capabilities_for(name: str) -> tuple[str, ...]:
    return task_class(name).capabilities


def suite_for(name: str) -> str | None:
    return task_class(name).suite


def requires_certification(name: str) -> bool:
    return task_class(name).requires_certification


def certifiable_task_classes() -> tuple[str, ...]:
    """Task classes a certification can currently be recorded against."""
    return tuple(sorted(name for name, spec in TASK_CLASSES.items() if spec.suite))
