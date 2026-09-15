"""D-041: prompts are versioned first-class configuration, not string literals.

Before this module, the only model-facing prompts in the evaluation path were
three anonymous literal templates in `tools/bench/harness.py:benchmark_prompt()`
-- no identifier, no version, no recorded link between an evaluated prompt and
whatever a production caller would eventually send (`docs/20-prompt-management.md`).

A prompt is addressed by `prompt_id` + `version`, never by an unversioned
string, the same discipline `tools/models/registry.py` already applies to
models (`ModelSpec.version`) and `tools/models/certification.py` already
applies to a certified configuration. A version's content is immutable: the
library module below records a content fingerprint per (prompt_id, version)
at authoring time, and `tests/test_prompt_registry.py` recomputes it from the
live template text on every run. Changing what a version *means* without
bumping the version breaks that test -- the same enforcement style
`tools/models/task_classes.py` uses for the task-class/suite biconditional.

Composition is layered (docs/20 section 2): policy (trusted, never derived
from task input), role, task contract, output contract, tool-schema version,
and an optional example set -- all reasoned about and versioned together as
one semantic unit. The context pack (untrusted data) and the runtime adapter
(provider wire format) are deliberately NOT part of this dataclass -- see
`tools/prompts/renderer.py` and `tools/prompts/adapters.py`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable

PROMPT_REGISTRY_SCHEMA_VERSION = 1

STATUSES = {"draft", "active", "deprecated"}


@dataclass(frozen=True)
class PromptVersion:
    """One immutable, versioned prompt recipe.

    ``capability`` names the ACB-v1 capability or production task-class
    family this prompt serves (docs/19 section 2.1) -- it is what lets a
    caller look up "the prompt for classification" without hard-coding an
    id. ``output_schema`` is a human/machine description of the required
    output shape (a closed label set, a filename grammar, a JSON schema
    reference) -- kept as descriptive text here rather than a second
    validation system, because the actual enforcement is the suite's
    deterministic oracle (`tools/bench/suite.py`), not the prompt.
    """

    prompt_id: str
    version: str
    status: str
    capability: str
    policy_template: str
    role_template: str
    task_template: str
    output_schema: str
    tool_schema_version: str
    renderer_version: str
    example_set_version: str | None = None
    token_or_context_budget: int | None = None
    applicable_constraints: tuple[str, ...] = ()
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"{self.prompt_id}@{self.version}: invalid status {self.status!r}")
        if not self.prompt_id or not self.version:
            raise ValueError("prompt_id and version must be non-empty")
        for name in ("policy_template", "role_template", "task_template", "output_schema"):
            if not getattr(self, name).strip():
                raise ValueError(f"{self.prompt_id}@{self.version}: {name} must be non-empty")

    @property
    def key(self) -> tuple[str, str]:
        return (self.prompt_id, self.version)

    @property
    def content_fingerprint(self) -> str:
        """SHA-256 over every field that defines this version's *meaning*.

        Deliberately excludes ``status`` (draft -> active -> deprecated is a
        lifecycle change, not a semantic one) and ``notes``. Everything that
        could change what a model is told or how its output is judged is
        included, so a silent edit to any of it changes the fingerprint and
        `tests/test_prompt_registry.py` catches it against the recorded
        lock in `tools/prompts/library.py`.
        """
        payload = "\x1f".join([
            self.prompt_id, self.version, self.capability,
            self.policy_template, self.role_template, self.task_template,
            self.output_schema, self.tool_schema_version, self.renderer_version,
            self.example_set_version or "", str(self.token_or_context_budget or ""),
            "|".join(self.applicable_constraints),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class UnknownPrompt(KeyError):
    """No registry entry matches this prompt_id/version (or capability)."""


class PromptRegistry:
    """Looks up prompt versions by identity or by capability.

    At most one ``active`` version may exist per ``prompt_id`` at any time --
    enforced at construction, not at call time, so an ambiguous registry
    never loads at all. ``draft``/``deprecated`` versions may coexist freely;
    only ``active`` is what a production caller or a new suite run should
    default to.
    """

    def __init__(self, versions: Iterable[PromptVersion]):
        by_key: dict[tuple[str, str], PromptVersion] = {}
        active_by_id: dict[str, str] = {}
        for version in versions:
            if version.key in by_key:
                raise ValueError(f"duplicate prompt version: {version.key}")
            by_key[version.key] = version
            if version.status == "active":
                existing = active_by_id.get(version.prompt_id)
                if existing is not None:
                    raise ValueError(
                        f"prompt_id {version.prompt_id!r} has more than one active version "
                        f"({existing!r} and {version.version!r})"
                    )
                active_by_id[version.prompt_id] = version.version
        self._by_key = by_key
        self._active_by_id = active_by_id

    def get(self, prompt_id: str, version: str) -> PromptVersion:
        try:
            return self._by_key[(prompt_id, version)]
        except KeyError as exc:
            raise UnknownPrompt(f"no such prompt version: {prompt_id}@{version}") from exc

    def active(self, prompt_id: str) -> PromptVersion:
        version = self._active_by_id.get(prompt_id)
        if version is None:
            raise UnknownPrompt(f"prompt_id {prompt_id!r} has no active version")
        return self._by_key[(prompt_id, version)]

    def versions_for(self, prompt_id: str) -> tuple[PromptVersion, ...]:
        return tuple(v for v in self._by_key.values() if v.prompt_id == prompt_id)

    def for_capability(self, capability: str) -> tuple[PromptVersion, ...]:
        return tuple(v for v in self._by_key.values() if v.capability == capability)

    def all(self) -> tuple[PromptVersion, ...]:
        return tuple(self._by_key.values())


def load_default_registry() -> PromptRegistry:
    """The one registry evaluation and production both import.

    Deferred import avoids a cycle (`library.py` only needs `PromptVersion`).
    """
    from tools.prompts.library import PROMPT_VERSIONS
    return PromptRegistry(PROMPT_VERSIONS)
