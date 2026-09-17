"""Draft prompt versioning for the Naming Lab.

Reuses `tools.prompts.registry.PromptVersion`/`PromptRegistry` and
`tools.prompts.renderer` exactly as production does -- a Lab draft is a
first-class `PromptVersion`, addressed the same `(prompt_id, version)` way,
with the same content-fingerprint machinery. The only new rule is where
drafts live and how they are named:

* A saved draft is always `status="draft"` and its `prompt_id` is namespaced
  `naming-lab-draft-<slug>`, so it can never collide with, or be mistaken
  for, a production `prompt_id` such as `acb-v1-semantic-naming`.
* A draft is never overwritten in place (spec section 10): saving always
  allocates the next `vnext-<letter>` version under its `prompt_id`.
* Nothing here can write into `tools/prompts/library.py`. Graduating a
  draft into production is a separate, reviewed code change -- see
  `docs/23-naming-lab.md`.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from tools.prompts.registry import PromptRegistry, PromptVersion, load_default_registry

DRAFT_PROMPT_ID_PREFIX = "naming-lab-draft-"

_VNEXT_STARTER_OUTPUT_SCHEMA = (
    "Output ONLY the proposed filename, including its extension, with no "
    "path separators, no quotation marks, no markdown, and no explanation. "
    "Never include a date yourself, in any form: trusted code adds a "
    "`YY_MMDD_` prefix in front of your proposed stem when, and only when, "
    "a reliable semantic date is available. Propose the descriptive stem "
    "(and extension) only."
)


def _draft_prompt_id(base: PromptVersion) -> str:
    if base.prompt_id.startswith(DRAFT_PROMPT_ID_PREFIX):
        return base.prompt_id
    slug = base.prompt_id.replace("acb-v1-", "").replace("_", "-")
    return f"{DRAFT_PROMPT_ID_PREFIX}{slug}"


def _next_draft_version(existing_versions: tuple[str, ...]) -> str:
    used = set(existing_versions)
    for letter in "abcdefghijklmnopqrstuvwxyz":
        candidate = f"vnext-{letter}"
        if candidate not in used:
            return candidate
    index = 1
    while True:
        candidate = f"vnext-{index}"
        if candidate not in used:
            return candidate
        index += 1


def prompt_version_to_json(prompt: PromptVersion) -> dict[str, Any]:
    return {
        "prompt_id": prompt.prompt_id,
        "version": prompt.version,
        "status": prompt.status,
        "capability": prompt.capability,
        "policy_template": prompt.policy_template,
        "role_template": prompt.role_template,
        "task_template": prompt.task_template,
        "output_schema": prompt.output_schema,
        "tool_schema_version": prompt.tool_schema_version,
        "renderer_version": prompt.renderer_version,
        "example_set_version": prompt.example_set_version,
        "token_or_context_budget": prompt.token_or_context_budget,
        "applicable_constraints": list(prompt.applicable_constraints),
        "notes": prompt.notes,
    }


def prompt_version_from_json(data: dict[str, Any]) -> PromptVersion:
    return PromptVersion(
        prompt_id=str(data["prompt_id"]),
        version=str(data["version"]),
        status=str(data["status"]),
        capability=str(data["capability"]),
        policy_template=str(data["policy_template"]),
        role_template=str(data["role_template"]),
        task_template=str(data["task_template"]),
        output_schema=str(data["output_schema"]),
        tool_schema_version=str(data["tool_schema_version"]),
        renderer_version=str(data["renderer_version"]),
        example_set_version=data.get("example_set_version"),
        token_or_context_budget=data.get("token_or_context_budget"),
        applicable_constraints=tuple(data.get("applicable_constraints") or ()),
        notes=data.get("notes"),
    )


class PromptDraftStore:
    """Draft `PromptVersion`s on disk, under `.state/naming-lab/prompts/`."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def versions_for(self, prompt_id: str) -> tuple[PromptVersion, ...]:
        directory = self.root / prompt_id
        if not directory.exists():
            return ()
        return tuple(
            prompt_version_from_json(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(directory.glob("*.json"))
        )

    def save(self, prompt: PromptVersion) -> PromptVersion:
        if prompt.status != "draft":
            raise ValueError("PromptDraftStore only stores draft prompt versions")
        directory = self.root / prompt.prompt_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{prompt.version}.json"
        path.write_text(
            json.dumps(prompt_version_to_json(prompt), indent=2, sort_keys=True), encoding="utf-8"
        )
        return prompt

    def all(self) -> tuple[PromptVersion, ...]:
        if not self.root.exists():
            return ()
        versions: list[PromptVersion] = []
        for directory in sorted(self.root.iterdir()):
            if directory.is_dir():
                versions.extend(self.versions_for(directory.name))
        return tuple(versions)


def save_draft(
    store: PromptDraftStore,
    base: PromptVersion,
    *,
    role_template: str | None = None,
    task_template: str | None = None,
    output_schema: str | None = None,
    notes: str | None = None,
) -> PromptVersion:
    """Save an edited copy of `base` as a new, never-overwritten draft version.

    `base` may be a production prompt (to start a new draft lineage) or an
    existing draft (to iterate on one) -- either way this always allocates a
    fresh `vnext-<letter>` version rather than mutating anything.
    """
    prompt_id = _draft_prompt_id(base)
    existing_versions = tuple(v.version for v in store.versions_for(prompt_id))
    version = _next_draft_version(existing_versions)
    draft = PromptVersion(
        prompt_id=prompt_id,
        version=version,
        status="draft",
        capability=base.capability,
        policy_template=base.policy_template,
        role_template=role_template if role_template is not None else base.role_template,
        task_template=task_template if task_template is not None else base.task_template,
        output_schema=output_schema if output_schema is not None else base.output_schema,
        tool_schema_version=base.tool_schema_version,
        renderer_version=base.renderer_version,
        example_set_version=base.example_set_version,
        token_or_context_budget=base.token_or_context_budget,
        applicable_constraints=base.applicable_constraints,
        notes=notes if notes is not None else base.notes,
    )
    return store.save(draft)


def starter_vnext_draft(base: PromptVersion) -> PromptVersion:
    """An *unsaved* vNext starting point cloned from `base`.

    Fixes spec section 14's "date prompt mismatch" defect with one clear
    rule -- the model never emits a date, trusted code always adds it --
    instead of production's "optional leading date" wording. Not persisted
    until the UI calls `save_draft` on the (possibly further-edited) result;
    the placeholder version `vnext-unsaved` is never a valid store key.
    """
    return dataclasses.replace(
        base,
        prompt_id=_draft_prompt_id(base),
        version="vnext-unsaved",
        status="draft",
        output_schema=_VNEXT_STARTER_OUTPUT_SCHEMA,
    )


def load_lab_registry(store: PromptDraftStore) -> PromptRegistry:
    """Production prompts (read-only, for comparison) plus this corpus's drafts."""
    return PromptRegistry(load_default_registry().all() + store.all())
