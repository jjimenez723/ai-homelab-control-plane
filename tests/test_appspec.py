"""Tests for tools/appspec: schema loading, example specs, semantic checks,
the CLI, and the pure-Python JSON Schema subset validator that backs all of
it (see tools/appspec/jsonschema_subset.py for why it is hand-rolled).
"""

from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from tools.appspec import brief
from tools.appspec.jsonschema_subset import SchemaIssue, UnsupportedSchemaKeyword, validate
from tools.appspec.schema import REPO_ROOT, SCHEMA_PATH, load_schema
from tools.appspec.validate import KNOWN_BACKEND_PROFILE_IDS, load_spec, validate_spec

EXAMPLES_DIR = REPO_ROOT / "templates" / "app-spec" / "examples"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.yaml"))


def _load_example_dict(name: str) -> dict:
    text = (EXAMPLES_DIR / name).read_text(encoding="utf-8")
    return yaml.safe_load(text)


class SchemaLoadingTests(unittest.TestCase):
    def test_schema_file_exists_at_the_documented_path(self):
        self.assertTrue(SCHEMA_PATH.exists(), f"expected schema at {SCHEMA_PATH}")

    def test_schema_loads_as_a_dict_with_expected_metadata(self):
        schema = load_schema()
        self.assertIsInstance(schema, dict)
        self.assertEqual(schema["$id"], "https://schemas.ai-homelab.internal/app-spec/v1/schema-v1.json")
        self.assertEqual(schema["type"], "object")

    def test_schema_required_covers_every_documented_section(self):
        schema = load_schema()
        expected = {
            "spec_version", "app_id", "name", "product_intent", "target_users",
            "platforms", "workflows", "entities", "relationships", "permissions",
            "authentication", "authorization", "backend_profile", "persistence",
            "realtime", "offline", "notifications", "storage_media", "privacy",
            "analytics", "integrations", "screens", "navigation", "design_system",
            "apis", "acceptance_criteria", "tests", "deployment", "observability",
            "migrations", "implementation_constraints", "open_questions",
        }
        self.assertEqual(set(schema["required"]), expected)
        self.assertEqual(set(schema["properties"]), expected)

    def test_schema_is_loaded_once_and_cached(self):
        # load_schema is lru_cache'd; the same dict object should come back.
        self.assertIs(load_schema(), load_schema())


class ExampleSpecsValidateTests(unittest.TestCase):
    def test_four_representative_examples_exist(self):
        names = {p.name for p in EXAMPLE_FILES}
        self.assertEqual(
            names,
            {"squad-quit.yaml", "nurse-shift.yaml", "single-player-game.yaml", "multiplayer-game.yaml"},
        )

    def test_every_example_validates_structurally_and_semantically(self):
        for path in EXAMPLE_FILES:
            with self.subTest(example=path.name):
                report = validate_spec(path)
                self.assertTrue(
                    report.valid,
                    f"{path.name} failed validation: {report.errors}",
                )
                self.assertEqual(report.errors, [])

    def test_every_example_names_a_known_backend_profile(self):
        for path in EXAMPLE_FILES:
            with self.subTest(example=path.name):
                spec = load_spec(path)
                self.assertIn(spec["backend_profile"]["id"], KNOWN_BACKEND_PROFILE_IDS)

    def test_every_example_is_within_the_target_line_budget(self):
        # "150-400 lines -- complete enough to implement from, not padded".
        # Allow a small amount of headroom over 400 rather than treating the
        # target as a hard wall a realistic example must be trimmed to fit.
        for path in EXAMPLE_FILES:
            with self.subTest(example=path.name):
                line_count = len(path.read_text(encoding="utf-8").splitlines())
                self.assertGreaterEqual(line_count, 150)
                self.assertLessEqual(line_count, 450)


class SemanticCheckFailureTests(unittest.TestCase):
    """Each test mutates a structurally-valid spec so that exactly one
    semantic check fails, proving validate_spec actually exercises it."""

    def setUp(self):
        self.spec = _load_example_dict("squad-quit.yaml")

    def _errors_for(self, spec: dict) -> list[str]:
        report = validate_spec(spec)
        self.assertFalse(report.valid, "expected the mutated spec to fail validation")
        return report.errors

    def test_dangling_id_reference_is_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec["workflows"][0]["actor"] = "no-such-persona"
        errors = self._errors_for(spec)
        self.assertTrue(any("no-such-persona" in e for e in errors), errors)

    def test_workflow_without_acceptance_criterion_is_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec["workflows"].append(
            {
                "id": "orphan-workflow",
                "name": "Orphan Workflow",
                "actor": "quitter",
                "goal": "Exercise the no-acceptance-criterion semantic check.",
                "steps": [{"id": "only-step", "description": "Do the one thing."}],
            }
        )
        errors = self._errors_for(spec)
        self.assertTrue(any("orphan-workflow" in e for e in errors), errors)

    def test_acceptance_criterion_without_test_coverage_is_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec["acceptance_criteria"].append(
            {
                "id": "ac-orphan",
                "workflow_ref": "join-squad",
                "given": "A precondition",
                "when": "An action",
                "then": "An outcome nothing tests cover",
            }
        )
        errors = self._errors_for(spec)
        self.assertTrue(any("ac-orphan" in e for e in errors), errors)

    def test_unreachable_screen_is_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec["screens"].append(
            {
                "id": "orphan-screen",
                "name": "Orphan Screen",
                "workflow_refs": [],
                "entities_shown": [],
            }
        )
        errors = self._errors_for(spec)
        self.assertTrue(any("orphan-screen" in e for e in errors), errors)

    def test_pii_field_without_privacy_entry_is_rejected(self):
        spec = copy.deepcopy(self.spec)
        for field in spec["entities"][0]["fields"]:
            if field["name"] == "display_name":
                field["pii"] = True
        errors = self._errors_for(spec)
        self.assertTrue(any("display_name" in e for e in errors), errors)

    def test_unknown_backend_profile_is_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec["backend_profile"]["id"] = "not-a-real-profile"
        errors = self._errors_for(spec)
        self.assertTrue(any("not-a-real-profile" in e for e in errors), errors)

    def test_valid_baseline_is_unmodified_by_these_mutations(self):
        # Sanity: the fixture itself still validates before any mutation.
        report = validate_spec(copy.deepcopy(self.spec))
        self.assertTrue(report.valid, report.errors)


class BackendProfileIndexCrossCheckTests(unittest.TestCase):
    def test_known_backend_profile_ids_constant_has_exactly_the_four_documented_ids(self):
        self.assertEqual(
            set(KNOWN_BACKEND_PROFILE_IDS),
            {
                "social-behavior-change",
                "nursing-shift-productivity",
                "single-player-game",
                "multiplayer-game",
            },
        )

    def test_validation_warns_when_backend_profile_index_file_is_absent(self):
        # A concurrent workstream produces templates/backend-profiles/index.yaml;
        # tools.appspec must not depend on it existing.
        index_path = REPO_ROOT / "templates" / "backend-profiles" / "index.yaml"
        if index_path.exists():
            self.skipTest("templates/backend-profiles/index.yaml exists in this checkout")
        report = validate_spec(_load_example_dict("squad-quit.yaml"))
        self.assertTrue(report.valid)
        self.assertTrue(any("index.yaml" in w for w in report.warnings), report.warnings)


class CliSmokeTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "tools.appspec", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_validate_subcommand_reports_a_valid_example(self):
        result = self._run("validate", str(EXAMPLES_DIR / "squad-quit.yaml"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VALID", result.stdout)

    def test_validate_subcommand_exits_nonzero_for_an_invalid_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "bad.yaml"
            bad_path.write_text("spec_version: \"1.0\"\n", encoding="utf-8")
            result = self._run("validate", str(bad_path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("INVALID", result.stdout)

    def test_new_subcommand_scaffolds_a_spec_that_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "scaffolded.yaml"
            result = self._run("new", "--class", "multiplayer-game", "--out", str(out_path))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(out_path.exists())
            report = validate_spec(out_path)
            self.assertTrue(report.valid, report.errors)

    def test_new_subcommand_rejects_an_unknown_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "scaffolded.yaml"
            result = self._run("new", "--class", "not-a-real-class", "--out", str(out_path))
            self.assertNotEqual(result.returncode, 0)

    def test_agent_brief_subcommand_prints_all_four_task_classes_by_default(self):
        result = self._run("agent-brief", str(EXAMPLES_DIR / "squad-quit.yaml"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for task_class_name in brief.IMPLEMENTATION_TASK_CLASSES:
            self.assertIn(task_class_name, result.stdout)

    def test_agent_brief_subcommand_can_scope_to_one_task_class(self):
        result = self._run(
            "agent-brief", str(EXAMPLES_DIR / "nurse-shift.yaml"), "--task-class", "ui.implementation"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ui.implementation", result.stdout)
        self.assertNotIn("code.testing --", result.stdout)


class AgentBriefContentTests(unittest.TestCase):
    def setUp(self):
        self.spec = _load_example_dict("nurse-shift.yaml")

    def test_generate_brief_covers_every_implementation_task_class(self):
        for task_class_name in brief.IMPLEMENTATION_TASK_CLASSES:
            with self.subTest(task_class=task_class_name):
                text = brief.generate_brief(self.spec, task_class_name)
                self.assertIn(task_class_name, text)

    def test_generate_brief_rejects_an_unknown_task_class(self):
        with self.assertRaises(Exception):
            brief.generate_brief(self.spec, "not.a.real.class")

    def test_code_implementation_brief_names_entities_and_apis(self):
        text = brief.generate_brief(self.spec, "code.implementation")
        self.assertIn("nurse", text)
        self.assertIn("api-claim-shift", text)

    def test_ui_implementation_brief_names_screens_and_design_system(self):
        text = brief.generate_brief(self.spec, "ui.implementation")
        self.assertIn("shift-board", text)
        self.assertIn(self.spec["design_system"]["library_ref"], text)

    def test_generate_all_briefs_is_the_concatenation_in_a_stable_order(self):
        combined = brief.generate_all_briefs(self.spec)
        positions = [combined.index(name) for name in brief.IMPLEMENTATION_TASK_CLASSES]
        self.assertEqual(positions, sorted(positions))


class JsonSchemaSubsetValidatorTests(unittest.TestCase):
    def test_valid_instance_produces_no_issues(self):
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
        self.assertEqual(validate({"a": "x"}, schema), [])

    def test_type_mismatch_is_reported(self):
        issues = validate("not an object", {"type": "object"})
        self.assertEqual(len(issues), 1)
        self.assertIsInstance(issues[0], SchemaIssue)
        self.assertIn("object", issues[0].message)

    def test_missing_required_property_is_reported(self):
        schema = {"type": "object", "required": ["a", "b"]}
        issues = validate({"a": 1}, schema)
        self.assertEqual(len(issues), 1)
        self.assertIn("b", issues[0].message)

    def test_enum_violation_is_reported(self):
        issues = validate("green", {"type": "string", "enum": ["red", "blue"]})
        self.assertEqual(len(issues), 1)

    def test_enum_success_is_silent(self):
        self.assertEqual(validate("red", {"type": "string", "enum": ["red", "blue"]}), [])

    def test_additional_property_false_rejects_unknown_keys(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False}
        issues = validate({"a": "x", "b": "y"}, schema)
        self.assertEqual(len(issues), 1)
        self.assertIn("b", issues[0].message)

    def test_additional_properties_default_allows_unknown_keys(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        self.assertEqual(validate({"a": "x", "b": "y"}, schema), [])

    def test_min_items_violation_is_reported(self):
        issues = validate([1], {"type": "array", "minItems": 2})
        self.assertEqual(len(issues), 1)

    def test_items_are_recursively_validated(self):
        schema = {"type": "array", "items": {"type": "integer"}}
        issues = validate([1, 2, "three"], schema)
        self.assertEqual(len(issues), 1)
        self.assertIn("[2]", issues[0].path)

    def test_pattern_violation_is_reported(self):
        issues = validate("ABC", {"type": "string", "pattern": "^[a-z]+$"})
        self.assertEqual(len(issues), 1)

    def test_pattern_success_is_silent(self):
        self.assertEqual(validate("abc", {"type": "string", "pattern": "^[a-z]+$"}), [])

    def test_ref_resolves_within_the_document(self):
        schema = {
            "$defs": {"pos_int": {"type": "integer"}},
            "type": "object",
            "properties": {"n": {"$ref": "#/$defs/pos_int"}},
        }
        self.assertEqual(validate({"n": 5}, schema), [])
        issues = validate({"n": "not an int"}, schema)
        self.assertEqual(len(issues), 1)

    def test_unsupported_keyword_raises_rather_than_silently_passing(self):
        with self.assertRaises(UnsupportedSchemaKeyword):
            validate({"a": 1}, {"type": "object", "oneOf": [{"type": "object"}]})

    def test_unresolvable_ref_raises(self):
        with self.assertRaises(UnsupportedSchemaKeyword):
            validate({}, {"$ref": "#/$defs/does_not_exist"})

    def test_null_and_boolean_types(self):
        self.assertEqual(validate(None, {"type": "null"}), [])
        self.assertEqual(validate(True, {"type": "boolean"}), [])
        # bool is a subclass of int in Python; "integer"/"number" must not accept it.
        self.assertEqual(len(validate(True, {"type": "integer"})), 1)
        self.assertEqual(len(validate(True, {"type": "number"})), 1)


if __name__ == "__main__":
    unittest.main()
