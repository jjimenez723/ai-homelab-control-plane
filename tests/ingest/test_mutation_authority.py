"""Who is allowed to change the filesystem, enforced rather than documented.

The design's central claim is that a model may propose a filename, a
classification, or a destination, but never receives filesystem authority
(invariant 3). A docstring saying so is worth nothing a year from now, so this
suite asserts it against the source:

* the modules a proposal flows through contain no mutation primitive at all;
* the evidence a proposer receives carries no `Path` and no file handle, so
  there is nothing there to mutate *with*;
* exactly one function unlinks a source file, and it is the tested transaction
  primitive;
* exactly one function renames a local file, and it cannot overwrite or
  relocate.

If a future change adds a mutation to the wrong module, these fail and name the
file and the line.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import unittest
from pathlib import Path

from tools.ingest.naming.evidence import NamingEvidence

REPO_ROOT = Path(__file__).resolve().parents[2]
INGEST = REPO_ROOT / "tools" / "ingest"

# Anything that can change or destroy a file. `mkdir` is deliberately absent:
# creating a directory destroys nothing, and the staging/sidecar trees need it.
# The complete register of modules permitted to delete something, and why.
# Adding a module here is the deliberate act of granting deletion authority; a
# new one appearing without an entry fails the suite.
DELETION_JUSTIFICATIONS = {
    "transaction.py": (
        "The one source-deleting primitive. Unlinks a user's file only after the "
        "bytes are provably at the destination and the catalog has committed."
    ),
    "naming/policy.py": (
        "Rename internals: `os.link` + `unlink` is the atomic non-clobbering "
        "rename, and the fallback removes only the empty placeholder it just "
        "created. Both operate on the file being renamed, in its own directory."
    ),
    "media/heic.py": (
        "Removes only the `mkstemp` temp file it created in the destination "
        "directory. The verified original is opened read-only."
    ),
    "automation.py": (
        "Removes only its own `mkstemp` temp file when the atomic status.json "
        "write fails. Confined to the state directory."
    ),
}

MUTATION_CALLS = {
    "unlink", "remove", "rmdir", "removedirs", "rmtree", "rename", "renames",
    "replace", "move", "truncate", "chmod", "chown", "link", "symlink",
    "symlink_to", "hardlink_to", "write_text", "write_bytes", "mkstemp", "mkdtemp",
}
WRITE_MODE_RE = re.compile(r"^[rbt+]*[wax]")

# Modules that must contain no filesystem mutation whatsoever. These are the
# ones a model's output passes through, plus the pure policy layers.
PURE_MODULES = (
    "naming/quality.py",
    "naming/evidence.py",
    "naming/proposer.py",
    "triage/policy.py",
    "exclusions.py",
    "scanner.py",
    "hashing.py",
    "identity.py",
    "sources.py",
    "volumes.py",
    "migrations.py",
    "schema_sql.py",
    "status.py",
)


# `"a".replace("a", "b")` and `os.replace(src, dst)` share a method name. The
# string forms always pass two string arguments, which the filesystem forms
# never do, so that is what separates them.
_STRING_METHODS = {"replace", "remove", "truncate"}


def _is_string_method_call(node: ast.Call, name: str) -> bool:
    if name not in _STRING_METHODS:
        return False
    if name == "replace":
        return len(node.args) >= 2 and all(
            isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args[:2]
        )
    return bool(node.args) and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)


def _mutations_in(path: Path) -> list[tuple[int, str]]:
    """Every filesystem-mutating call in a file, as (line, name)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = None
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            name = node.func.id
        # `dataclasses.replace` builds a new object and touches no file. The
        # filesystem form is `os.replace(...)` or `path.replace(...)`.
        bare_dataclass_replace = name == "replace" and (
            isinstance(node.func, ast.Name)
            or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "dataclasses"
            )
        )
        if name in MUTATION_CALLS and not bare_dataclass_replace and not _is_string_method_call(node, name):
            found.append((node.lineno, name))
        elif name == "open":
            mode = _string_arg(node, 1, "mode")
            if mode and WRITE_MODE_RE.match(mode):
                found.append((node.lineno, f"open(mode={mode!r})"))
    return found


def _string_arg(node: ast.Call, index: int, keyword: str) -> str | None:
    if len(node.args) > index and isinstance(node.args[index], ast.Constant):
        value = node.args[index].value
        return value if isinstance(value, str) else None
    for kw in node.keywords:
        if kw.arg == keyword and isinstance(kw.value, ast.Constant):
            value = kw.value.value
            return value if isinstance(value, str) else None
    return None


class PureModuleTests(unittest.TestCase):
    def test_proposal_and_policy_modules_never_touch_the_filesystem(self):
        for relative in PURE_MODULES:
            path = INGEST / relative
            with self.subTest(module=relative):
                self.assertTrue(path.exists(), f"{relative} moved; update this list deliberately")
                mutations = _mutations_in(path)
                self.assertEqual(
                    mutations, [],
                    f"{relative} gained filesystem mutation(s): {mutations}. "
                    "If this is intentional, it needs its own safety review -- these modules "
                    "are on the path a model's output travels.",
                )


class EvidenceBoundaryTests(unittest.TestCase):
    """A proposer cannot reach the filesystem because it is never handed it."""

    def test_evidence_carries_no_path_or_handle(self):
        allowed = {str, int, float, bool, bytes, type(None), dict, tuple}
        for field in dataclasses.fields(NamingEvidence):
            with self.subTest(field=field.name):
                annotation = str(field.type)
                self.assertNotIn("Path", annotation, f"{field.name} exposes a filesystem path to the proposer")
                self.assertNotIn("Callable", annotation, f"{field.name} exposes a callable to the proposer")
                self.assertNotIn("IO", annotation)

    def test_evidence_is_frozen(self):
        """A proposer must not be able to edit the facts it was given."""
        self.assertTrue(NamingEvidence.__dataclass_params__.frozen)


class SourceDeletionAuthorityTests(unittest.TestCase):
    """Exactly one call site may remove a user's source file."""

    def test_only_the_transaction_primitive_unlinks_a_source(self):
        offenders: list[str] = []
        for path in sorted(INGEST.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(INGEST).as_posix()
            for line, name in _mutations_in(path):
                if name not in ("unlink", "remove", "rmtree", "removedirs", "rmdir"):
                    continue
                if relative in DELETION_JUSTIFICATIONS:
                    continue
                offenders.append(f"{relative}:{line} {name}")
        self.assertEqual(
            offenders, [],
            "a new module gained deletion authority: " + ", ".join(offenders),
        )

    def test_the_transaction_module_unlinks_in_exactly_one_place(self):
        source = (INGEST / "transaction.py").read_text(encoding="utf-8")
        unlinks = [
            line for line in source.splitlines()
            if ".unlink()" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(len(unlinks), 1, f"expected one source unlink, found: {unlinks}")
        self.assertIn("source_path.unlink()", unlinks[0])

    def test_the_pipeline_delegates_rather_than_mutating(self):
        """The orchestrator holds no authority of its own."""
        mutations = [
            (line, name) for line, name in _mutations_in(INGEST / "pipeline.py")
            if name not in ("mkdir",)
        ]
        self.assertEqual(mutations, [], f"pipeline.py must delegate every mutation: {mutations}")


class JustificationRegisterTests(unittest.TestCase):
    def test_every_module_granted_deletion_authority_carries_a_reason(self):
        """§16 asks for every destructive path to be documented and justified.

        Keeping the justifications in the test means they cannot drift away
        from the code they describe.
        """
        for module, reason in DELETION_JUSTIFICATIONS.items():
            with self.subTest(module=module):
                self.assertTrue((INGEST / module).exists(), f"{module} no longer exists")
                self.assertGreater(len(reason), 60, f"{module} needs a real justification")


class RenameAuthorityTests(unittest.TestCase):
    def test_only_policy_renames_a_local_file(self):
        offenders: list[str] = []
        for path in sorted(INGEST.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(INGEST).as_posix()
            if relative in ("naming/policy.py", "transaction.py", "media/heic.py", "automation.py"):
                # policy/transaction/heic: covered above and by their own tests.
                # automation: `os.replace` of its own temp onto status.json,
                # which is how that file is written atomically.
                continue
            for line, name in _mutations_in(path):
                if name in ("rename", "renames", "replace", "move"):
                    offenders.append(f"{relative}:{line} {name}")
        self.assertEqual(offenders, [], "unexpected rename authority: " + ", ".join(offenders))

    def test_os_rename_is_only_used_where_it_provably_cannot_clobber(self):
        """`os.rename` overwrites silently on POSIX, so its use is confined.

        One use is allowed, and only one: renaming a file to a name that macOS
        already considers the same file (a case-only or NFD/NFC-only change on
        a case-insensitive volume). There the "target" *is* the source inode,
        so there is nothing it could destroy -- and `os.link` and `O_EXCL`
        cannot express it, because both refuse a target the filesystem reports
        as existing.

        The guard is what makes it safe, so this asserts the guard is there
        rather than merely counting the calls.
        """
        source = (INGEST / "naming" / "policy.py").read_text(encoding="utf-8")
        self.assertIn("os.link(", source)  # the atomic, non-clobbering path
        self.assertIn("O_EXCL", source)  # the fallback reservation

        uses = [line.strip() for line in source.splitlines() if "os.rename(" in line]
        self.assertEqual(len(uses), 1, f"os.rename is confined to one site; found {uses}")

        # It must sit under the same-inode check, not merely somewhere nearby.
        before, _, _ = source.partition("os.rename(")
        guard = before.rsplit("def ", 1)[-1]
        self.assertIn("_is_same_file(", guard, "os.rename must be guarded by a same-inode check")


if __name__ == "__main__":
    unittest.main()
