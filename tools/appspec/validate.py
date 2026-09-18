"""Structural + semantic validation for a canonical application spec.

`validate_spec` is the one entry point implementation agents, the CLI
(`tools/appspec/cli.py`), and `tests/test_appspec.py` all go through.
Structural validation (does the document have the shape
`templates/app-spec/schema-v1.json` requires) is necessary but not
sufficient -- a spec can be structurally perfect and still name a workflow
with no acceptance criterion, an acceptance criterion no test covers, a
screen navigation never reaches, a PII field with no recorded privacy
classification, or a backend profile id nothing recognizes. Those are
semantic checks over cross-references by id, run only once structural
validation passes (a structurally invalid document cannot be trusted to
have the lists/ids semantic checks assume are present).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tools.appspec.jsonschema_subset import validate as validate_structure
from tools.appspec.schema import REPO_ROOT, load_schema

# The four backend-profile ids a concurrent workstream is expected to
# publish in templates/backend-profiles/index.yaml. This constant is the
# authoritative fallback: validate_spec never depends on that file
# existing. When it does exist, its ids are unioned in (see
# known_backend_profile_ids) so a newly added profile is not wrongly
# rejected -- but this tuple is never removed or narrowed by that file.
KNOWN_BACKEND_PROFILE_IDS = (
    "social-behavior-change",
    "nursing-shift-productivity",
    "single-player-game",
    "multiplayer-game",
)

BACKEND_PROFILE_INDEX_PATH = REPO_ROOT / "templates" / "backend-profiles" / "index.yaml"


@dataclass
class ValidationReport:
    """The result of validating one spec."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.valid

    def raise_if_invalid(self) -> None:
        if not self.valid:
            raise SpecInvalid(self)


class SpecInvalid(ValueError):
    """Raised by `ValidationReport.raise_if_invalid` for an invalid spec."""

    def __init__(self, report: ValidationReport) -> None:
        super().__init__("; ".join(report.errors) or "spec is invalid")
        self.report = report


def load_spec(path_or_dict: "str | Path | dict") -> dict:
    """Load a spec from a YAML/JSON file path, or pass a dict through."""
    if isinstance(path_or_dict, dict):
        return path_or_dict
    path = Path(path_or_dict)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        loaded = yaml.safe_load(text)
    else:
        loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: top-level document must be a mapping, got {type(loaded).__name__}")
    return loaded


def known_backend_profile_ids() -> tuple[str, ...]:
    """`KNOWN_BACKEND_PROFILE_IDS`, unioned with `templates/backend-profiles/index.yaml`
    when that file is present. See the module docstring: this never depends
    on the file existing, only optionally extends what it already knows."""
    ids = set(KNOWN_BACKEND_PROFILE_IDS)
    if BACKEND_PROFILE_INDEX_PATH.exists():
        try:
            data = yaml.safe_load(BACKEND_PROFILE_INDEX_PATH.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            data = {}
        for entry in data.get("profiles", []) if isinstance(data, dict) else []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                ids.add(entry["id"])
    return tuple(sorted(ids))


def _ids(items: list[dict], key: str = "id") -> set[str]:
    return {item[key] for item in items if isinstance(item, dict) and key in item}


def _check_dangling_refs(spec: dict, errors: list[str]) -> None:
    personas = _ids(spec.get("target_users", []))
    workflows = _ids(spec.get("workflows", []))
    entities = _ids(spec.get("entities", []))
    roles = _ids(spec.get("authorization", {}).get("roles", []))
    screens = _ids(spec.get("screens", []))
    acs = _ids(spec.get("acceptance_criteria", []))

    for w in spec.get("workflows", []):
        if w.get("actor") not in personas:
            errors.append(f"workflows[{w.get('id')}].actor references unknown persona '{w.get('actor')}'")
        for step in w.get("steps", []):
            ref = step.get("screen_ref")
            if ref is not None and ref not in screens:
                errors.append(
                    f"workflows[{w.get('id')}].steps[{step.get('id')}].screen_ref "
                    f"references unknown screen '{ref}'"
                )

    for r in spec.get("relationships", []):
        for endpoint in ("from_entity", "to_entity"):
            if r.get(endpoint) not in entities:
                errors.append(
                    f"relationships[{r.get('id')}].{endpoint} references unknown entity '{r.get(endpoint)}'"
                )

    for p in spec.get("permissions", []):
        if p.get("role") not in roles:
            errors.append(f"permissions[{p.get('id')}].role references unknown role '{p.get('role')}'")
        if p.get("entity") not in entities:
            errors.append(f"permissions[{p.get('id')}].entity references unknown entity '{p.get('entity')}'")

    for eid in spec.get("persistence", {}).get("entities", []):
        if eid not in entities:
            errors.append(f"persistence.entities references unknown entity '{eid}'")

    for dc in spec.get("privacy", {}).get("data_classes", []):
        if dc.get("entity") not in entities:
            errors.append(f"privacy.data_classes[{dc.get('id')}].entity references unknown entity '{dc.get('entity')}'")

    for s in spec.get("screens", []):
        for wref in s.get("workflow_refs", []):
            if wref not in workflows:
                errors.append(f"screens[{s.get('id')}].workflow_refs references unknown workflow '{wref}'")
        for eref in s.get("entities_shown", []):
            if eref not in entities:
                errors.append(f"screens[{s.get('id')}].entities_shown references unknown entity '{eref}'")

    nav = spec.get("navigation", {})
    entry = nav.get("entry_screen")
    if entry not in screens:
        errors.append(f"navigation.entry_screen references unknown screen '{entry}'")
    for edge in nav.get("edges", []):
        if edge.get("from") not in screens:
            errors.append(f"navigation.edges references unknown screen '{edge.get('from')}' (from)")
        if edge.get("to") not in screens:
            errors.append(f"navigation.edges references unknown screen '{edge.get('to')}' (to)")

    for ac in spec.get("acceptance_criteria", []):
        if ac.get("workflow_ref") not in workflows:
            errors.append(
                f"acceptance_criteria[{ac.get('id')}].workflow_ref references unknown workflow "
                f"'{ac.get('workflow_ref')}'"
            )

    for t in spec.get("tests", []):
        for aref in t.get("acceptance_criteria_refs", []):
            if aref not in acs:
                errors.append(
                    f"tests[{t.get('id')}].acceptance_criteria_refs references unknown "
                    f"acceptance criterion '{aref}'"
                )


def _check_workflow_acceptance_coverage(spec: dict, errors: list[str]) -> None:
    workflow_ids = _ids(spec.get("workflows", []))
    covered = {ac.get("workflow_ref") for ac in spec.get("acceptance_criteria", [])}
    for wid in sorted(workflow_ids - covered):
        errors.append(f"workflow '{wid}' has no acceptance criterion")


def _check_acceptance_test_coverage(spec: dict, errors: list[str]) -> None:
    ac_ids = _ids(spec.get("acceptance_criteria", []))
    covered: set[str] = set()
    for t in spec.get("tests", []):
        covered.update(t.get("acceptance_criteria_refs", []))
    for aid in sorted(ac_ids - covered):
        errors.append(f"acceptance criterion '{aid}' is not covered by any test")


def _check_screen_reachability(spec: dict, errors: list[str]) -> None:
    screens = _ids(spec.get("screens", []))
    nav = spec.get("navigation", {})
    entry = nav.get("entry_screen")
    graph: dict[str, set[str]] = {}
    for edge in nav.get("edges", []):
        graph.setdefault(edge.get("from"), set()).add(edge.get("to"))

    reachable: set[str] = set()
    if entry in screens:
        frontier = [entry]
        reachable.add(entry)
        while frontier:
            current = frontier.pop()
            for nxt in graph.get(current, ()):
                if nxt not in reachable:
                    reachable.add(nxt)
                    frontier.append(nxt)

    for sid in sorted(screens - reachable):
        errors.append(f"screen '{sid}' is not reachable from navigation.entry_screen ('{entry}')")


def _check_privacy_coverage(spec: dict, errors: list[str]) -> None:
    data_class_keys = {
        (dc.get("entity"), dc.get("field")) for dc in spec.get("privacy", {}).get("data_classes", [])
    }
    for e in spec.get("entities", []):
        for f in e.get("fields", []):
            if f.get("pii") or f.get("phi"):
                key = (e.get("id"), f.get("name"))
                if key not in data_class_keys:
                    errors.append(
                        f"entity '{e.get('id')}' field '{f.get('name')}' is flagged pii/phi "
                        "but has no matching privacy.data_classes entry"
                    )


def _check_backend_profile(spec: dict, errors: list[str], warnings: list[str]) -> None:
    known = known_backend_profile_ids()
    bp_id = spec.get("backend_profile", {}).get("id")
    if bp_id not in known:
        errors.append(f"backend_profile.id '{bp_id}' is not in the known backend profile list {known}")
    if not BACKEND_PROFILE_INDEX_PATH.exists():
        warnings.append(
            f"{BACKEND_PROFILE_INDEX_PATH.relative_to(REPO_ROOT)} not found; backend_profile.id was "
            "checked only against the fallback constant KNOWN_BACKEND_PROFILE_IDS"
        )


def _semantic_checks(spec: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    _check_dangling_refs(spec, errors)
    _check_workflow_acceptance_coverage(spec, errors)
    _check_acceptance_test_coverage(spec, errors)
    _check_screen_reachability(spec, errors)
    _check_privacy_coverage(spec, errors)
    _check_backend_profile(spec, errors, warnings)
    return errors, warnings


def validate_spec(path_or_dict: "str | Path | dict") -> ValidationReport:
    """Validate a spec structurally, then semantically. Never raises for an
    invalid spec -- call `.raise_if_invalid()` on the result if that is
    what the caller wants."""
    try:
        spec = load_spec(path_or_dict)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return ValidationReport(valid=False, errors=[f"could not load spec: {exc}"])

    structural_issues = validate_structure(spec, load_schema())
    if structural_issues:
        return ValidationReport(valid=False, errors=[str(issue) for issue in structural_issues])

    errors, warnings = _semantic_checks(spec)
    return ValidationReport(valid=not errors, errors=errors, warnings=warnings)
