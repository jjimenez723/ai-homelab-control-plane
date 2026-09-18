"""Bounded, per-task-class implementation briefs generated from a spec.

An agent doing `code.implementation` does not need the UX copy a
`ui.implementation` agent needs, and neither needs the other's slice of the
spec restated in full. `generate_brief` extracts only what each of the four
Phase 4/5 implementation task classes in `tools/models/task_classes.py`
actually consumes, so `python -m tools.appspec agent-brief` stays a brief
rather than a re-serialization of the whole document (see
`docs/28-application-spec.md` "how agents consume agent-brief").
"""

from __future__ import annotations

from tools.models.task_classes import UnknownTaskClass, task_class

IMPLEMENTATION_TASK_CLASSES = (
    "code.implementation",
    "ui.implementation",
    "code.testing",
    "repo.documentation",
)


def _code_implementation(spec: dict) -> list[str]:
    lines: list[str] = []
    bp = spec.get("backend_profile", {})
    lines.append(f"Backend profile: {bp.get('id')}  overrides={bp.get('overrides')}")
    persistence = spec.get("persistence", {})
    lines.append(f"Persistence: {persistence.get('datastore')} -> entities {persistence.get('entities')}")
    lines.append("Entities:")
    for e in spec.get("entities", []):
        fields = ", ".join(
            f"{f['name']}:{f['type']}{'*' if f.get('required') else ''}"
            for f in e.get("fields", [])
        )
        lines.append(f"  - {e['id']} ({e['name']}): {fields}")
    lines.append("Relationships:")
    for r in spec.get("relationships", []):
        lines.append(f"  - {r['from_entity']} --{r['type']}--> {r['to_entity']}: {r['description']}")
    lines.append("APIs:")
    for a in spec.get("apis", []):
        locator = f"{a.get('method', '')} {a.get('path', '')}".strip() or a.get("kind", "")
        lines.append(f"  - {a['id']} [{a['kind']}] {locator}")
    lines.append("Permissions:")
    for p in spec.get("permissions", []):
        lines.append(f"  - {p['role']} on {p['entity']}: {', '.join(p['actions'])}")
    ic = spec.get("implementation_constraints", {})
    lines.append(f"Stack pins: {ic.get('stack_pins')}")
    lines.append(f"Forbidden dependencies: {ic.get('forbidden_dependencies')}")
    lines.append(f"Budgets: {ic.get('budgets')}")
    lines.append(f"Migrations: {[m['id'] for m in spec.get('migrations', [])]}")
    return lines


def _ui_implementation(spec: dict) -> list[str]:
    lines: list[str] = []
    ds = spec.get("design_system", {})
    lines.append(f"Platforms: {spec.get('platforms')}")
    lines.append(f"Design system: stack={ds.get('stack')} library_ref={ds.get('library_ref')}")
    lines.append(f"Tokens: {ds.get('tokens')}")
    lines.append("Screens:")
    for s in spec.get("screens", []):
        lines.append(
            f"  - {s['id']} ({s['name']}): workflows={s.get('workflow_refs')} "
            f"entities_shown={s.get('entities_shown')}"
        )
    nav = spec.get("navigation", {})
    lines.append(f"Navigation entry: {nav.get('entry_screen')}")
    lines.append("Navigation edges:")
    for edge in nav.get("edges", []):
        lines.append(f"  - {edge['from']} -> {edge['to']}  (on: {edge['trigger']})")
    lines.append("Workflows (for step order and copy context):")
    for w in spec.get("workflows", []):
        lines.append(f"  - {w['id']} ({w['name']}): {w['goal']}")
    return lines


def _code_testing(spec: dict) -> list[str]:
    lines: list[str] = []
    lines.append("Acceptance criteria (Given/When/Then):")
    for ac in spec.get("acceptance_criteria", []):
        lines.append(
            f"  - {ac['id']} (workflow: {ac['workflow_ref']})\n"
            f"      given {ac['given']}\n      when {ac['when']}\n      then {ac['then']}"
        )
    lines.append("Test plan:")
    for t in spec.get("tests", []):
        lines.append(
            f"  - {t['id']} [{t['type']}] covers {t.get('acceptance_criteria_refs')}: {t['description']}"
        )
    lines.append("APIs available to exercise in integration/e2e tests:")
    for a in spec.get("apis", []):
        lines.append(f"  - {a['id']} [{a['kind']}]")
    return lines


def _repo_documentation(spec: dict) -> list[str]:
    lines: list[str] = []
    intent = spec.get("product_intent", {})
    lines.append(f"Problem: {intent.get('problem')}")
    lines.append(f"Value proposition: {intent.get('value_proposition')}")
    lines.append(f"Non-goals: {intent.get('non_goals')}")
    lines.append("Target users:")
    for p in spec.get("target_users", []):
        lines.append(f"  - {p['id']} ({p['name']}): {p['description']}")
    lines.append("Workflows:")
    for w in spec.get("workflows", []):
        lines.append(f"  - {w['id']} ({w['name']}, actor={w['actor']}): {w['goal']}")
    lines.append("Open questions:")
    for q in spec.get("open_questions", []):
        lines.append(f"  - {q['id']} (owner: {q['owner']}): {q['question']}")
    return lines


_BODY_BUILDERS = {
    "code.implementation": _code_implementation,
    "ui.implementation": _ui_implementation,
    "code.testing": _code_testing,
    "repo.documentation": _repo_documentation,
}


def generate_brief(spec: dict, task_class_name: str) -> str:
    """One bounded brief for a single implementation task class."""
    if task_class_name not in IMPLEMENTATION_TASK_CLASSES:
        raise UnknownTaskClass(
            f"agent-brief supports {IMPLEMENTATION_TASK_CLASSES}, got {task_class_name!r}"
        )
    tc = task_class(task_class_name)  # validated against the production taxonomy too
    header = f"=== Agent brief: {task_class_name} -- {tc.description} ==="
    body = _BODY_BUILDERS[task_class_name](spec)
    return "\n".join([header, *body])


def generate_all_briefs(spec: dict) -> str:
    """One brief per implementation task class, in a fixed, stable order."""
    return "\n\n".join(generate_brief(spec, name) for name in IMPLEMENTATION_TASK_CLASSES)
