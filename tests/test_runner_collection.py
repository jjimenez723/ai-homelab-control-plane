"""Guards against tests that exist but never run.

`tests/test_phase2_foundation.py` spent its whole life uncollected: it was
written with pytest-style module-level functions taking a `tmp_path`
fixture, pytest is not a declared dependency of this repository, and
`python -m unittest discover` -- the documented runner, and the one CI uses
-- silently collects nothing from a file like that. Two Phase-2 assertions
therefore passed review, passed CI, and never executed.

The failure mode is nastier than a missing test, because the file looks
like coverage in a directory listing and in a diff. These guards make it
loud instead of silent, and they also record honestly which parts of the
suite a Linux CI host cannot substantiate at all.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# Fixture names that only mean something to pytest. Their presence in a
# signature is the tell that a file was written for the wrong runner.
PYTEST_ONLY_FIXTURES = {
    "tmp_path", "tmpdir", "monkeypatch", "capsys", "capfd", "caplog",
    "request", "pytestconfig", "recwarn", "tmp_path_factory",
}


def _test_files() -> list[Path]:
    return sorted(
        path for path in TESTS_DIR.rglob("test_*.py")
        if "__pycache__" not in path.parts
    )


class CollectionTests(unittest.TestCase):
    def test_every_test_file_defines_at_least_one_unittest_test_case(self):
        for path in _test_files():
            if path.name == Path(__file__).name:
                continue
            with self.subTest(module=path.relative_to(REPO_ROOT).as_posix()):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                cases = [
                    node for node in tree.body
                    if isinstance(node, ast.ClassDef)
                    and any(
                        (isinstance(base, ast.Attribute) and base.attr.endswith("TestCase"))
                        or (isinstance(base, ast.Name) and base.id.endswith("TestCase"))
                        or (isinstance(base, ast.Name) and base.id.endswith("Tests"))
                        or (isinstance(base, ast.Name) and base.id.endswith("Base"))
                        for base in node.bases
                    )
                ]
                self.assertTrue(
                    cases,
                    f"{path.name} defines no unittest.TestCase subclass, so "
                    "`python -m unittest discover -s tests` collects nothing from it",
                )

    def test_no_test_file_uses_a_pytest_only_fixture(self):
        for path in _test_files():
            if path.name == Path(__file__).name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not node.name.startswith("test_"):
                    continue
                argument_names = {arg.arg for arg in node.args.args} | {arg.arg for arg in node.args.kwonlyargs}
                offenders = argument_names & PYTEST_ONLY_FIXTURES
                with self.subTest(module=path.name, function=node.name):
                    self.assertEqual(
                        offenders, set(),
                        f"{path.name}::{node.name} takes pytest fixture(s) {sorted(offenders)}; "
                        "this repository runs `python -m unittest discover`, which would skip it entirely",
                    )

    def test_module_level_test_functions_are_not_silently_uncollected(self):
        for path in _test_files():
            if path.name == Path(__file__).name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            stray = [
                node.name for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
            ]
            with self.subTest(module=path.name):
                self.assertEqual(
                    stray, [],
                    f"{path.name} defines module-level test function(s) {stray}; "
                    "unittest only collects methods on a TestCase",
                )


class PlatformCoverageTests(unittest.TestCase):
    """Say out loud which guarantees this host's run does not substantiate.

    The fixture runner's isolation and verdict-integrity properties are
    macOS Seatbelt properties. On a Linux CI host those tests skip, and a
    green CI badge therefore proves the fail-closed path and nothing else.
    That is an acceptable state of affairs; silently believing otherwise is
    not.
    """

    def test_the_sandbox_dependent_suite_declares_its_platform_requirement(self):
        source = (TESTS_DIR / "test_fixture_runner.py").read_text(encoding="utf-8")
        self.assertIn("_NEEDS_SANDBOX", source)
        self.assertIn("sandbox_available()", source)

    def test_the_fail_closed_path_is_proven_on_every_platform(self):
        """One test must never be gated on having a sandbox."""
        source = (TESTS_DIR / "test_fixture_runner.py").read_text(encoding="utf-8")
        marker = "def test_sandbox_unavailable_fails_closed_without_running_anything"
        index = source.index(marker)
        preceding = source[:index].rsplit("\n", 3)[-3:]
        self.assertNotIn("_NEEDS_SANDBOX", "\n".join(preceding),
                         "the unconditional fail-closed proof became platform-gated")

    def test_routing_matrix_is_not_platform_gated(self):
        """Autonomous-routing policy is pure logic and must run everywhere."""
        source = (TESTS_DIR / "test_autonomous_routing.py").read_text(encoding="utf-8")
        self.assertNotIn("skipUnless", source,
                         "the autonomous-routing decision matrix must run on every platform, "
                         "including the CI host that gates merges")


if __name__ == "__main__":
    unittest.main()
