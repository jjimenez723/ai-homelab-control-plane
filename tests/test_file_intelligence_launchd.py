"""Portability and opt-in guarantees for the file-intelligence LaunchAgent.

These tests run on Linux CI, where neither `plutil` nor `launchctl` exists, so
nothing here executes launchctl and the native validator is never assumed to be
present. The renderer is exercised for real against a throwaway checkout; the
installer and uninstaller are asserted against their source text.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent

LABEL = "com.ai-homelab.file-intelligence"
PLIST_NAME = f"{LABEL}.plist"
TEMPLATE_NAME = f"{LABEL}.plist.template"
RENDERER_NAME = "render-file-intelligence-launchd"

# Everything the renderer shells out to. `plutil` is deliberately absent: the
# sandbox PATH built from this list is how a macOS developer machine reproduces
# the Linux-CI environment where the native validator does not exist.
RENDERER_BINARIES = ("sh", "bash", "sed", "mkdir", "chmod", "mktemp", "rm", "mv", "dirname", "python3")


def _make_checkout(root: Path) -> Path:
    """A minimal copy of this checkout containing just the renderer + template."""
    checkout = root / "checkout & portable"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "launchd").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / RENDERER_NAME, checkout / "scripts" / RENDERER_NAME)
    shutil.copy2(REPO_ROOT / "launchd" / TEMPLATE_NAME, checkout / "launchd" / TEMPLATE_NAME)
    return checkout


def _plutil_free_path(root: Path) -> Path:
    """A PATH directory holding every renderer dependency except `plutil`."""
    bindir = root / "bin"
    bindir.mkdir()
    for name in RENDERER_BINARIES:
        found = shutil.which(name)
        if found is not None:
            (bindir / name).symlink_to(found)
    return bindir


def _render(checkout: Path, target: Path, *args: object, env: dict | None = None):
    return subprocess.run(
        [checkout / "scripts" / RENDERER_NAME, *args, target],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class FileIntelligenceRendererTests(unittest.TestCase):
    def test_renderer_uses_its_own_checkout_and_escapes_legal_path_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = _make_checkout(root)
            target = root / "agents"

            result = _render(checkout, target)
            self.assertEqual(result.returncode, 0, result.stderr)

            agent = plistlib.loads((target / PLIST_NAME).read_bytes())
            self.assertEqual(agent["Label"], LABEL)
            # The rendered agent points at the checkout it was rendered from,
            # not at REPO_ROOT, even though the path contains `&` and a space.
            self.assertEqual(agent["WorkingDirectory"], str(checkout))
            self.assertEqual(
                agent["ProgramArguments"],
                [str(checkout / "scripts/file-intelligence-scheduled")],
            )
            self.assertEqual(
                agent["StandardOutPath"],
                str(checkout / ".state/file-intelligence/logs/launchd.out.log"),
            )
            self.assertEqual(
                agent["StandardErrorPath"],
                str(checkout / ".state/file-intelligence/logs/launchd.err.log"),
            )

    def test_rendered_agent_is_an_hourly_one_shot_that_yields_to_the_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = _make_checkout(root)
            target = root / "agents"
            self.assertEqual(_render(checkout, target).returncode, 0)

            agent = plistlib.loads((target / PLIST_NAME).read_bytes())
            # No Hour key: launchd fires this every hour at :20. An Hour key
            # would silently demote it to once a day.
            self.assertEqual(agent["StartCalendarInterval"], {"Minute": 20})
            self.assertNotIn("Hour", agent["StartCalendarInterval"])
            # One-shot, not a resident process.
            self.assertNotIn("KeepAlive", agent)
            self.assertNotIn("RunAtLoad", agent)
            self.assertNotIn("WatchPaths", agent)
            self.assertNotIn("StartInterval", agent)
            # Owner-private output (0o077) and never competing with foreground work.
            self.assertEqual(agent["Umask"], 0o077)
            self.assertIs(agent["LowPriorityIO"], True)
            self.assertEqual(agent["Nice"], 5)
            self.assertEqual(agent["ProcessType"], "Background")

    def test_renderer_refuses_to_overwrite_without_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = _make_checkout(root)
            target = root / "agents"
            self.assertEqual(_render(checkout, target).returncode, 0)

            refused = _render(checkout, target)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("Refusing to overwrite", refused.stderr)

            replaced = _render(checkout, target, "--replace")
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertTrue((target / PLIST_NAME).exists())

    def test_renderer_does_not_load_the_agent(self):
        """Rendering is inert; loading is the installer's opt-in-gated job."""
        renderer = (REPO_ROOT / "scripts" / RENDERER_NAME).read_text(encoding="utf-8")
        self.assertNotIn("launchctl", renderer)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = _make_checkout(root)
            target = root / "agents"
            result = _render(checkout, target)
            self.assertIn("Rendered but did not load:", result.stdout)

    def test_renderer_still_validates_when_plutil_is_unavailable(self):
        """The Linux-CI case: no `plutil`, so the plistlib fallback has to run."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = _make_checkout(root)
            bindir = _plutil_free_path(root)
            target = root / "agents"
            self.assertIsNone(shutil.which("plutil", path=str(bindir)))
            self.assertIsNotNone(shutil.which("python3", path=str(bindir)), "fallback needs python3")

            result = _render(
                checkout, target, env={"PATH": str(bindir), "HOME": str(root)}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            agent = plistlib.loads((target / PLIST_NAME).read_bytes())
            self.assertEqual(agent["WorkingDirectory"], str(checkout))
            self.assertEqual(agent["StartCalendarInterval"], {"Minute": 20})

    def test_renderer_refuses_to_install_a_malformed_plist(self):
        """Validation is a real gate, not a decorative step that can be skipped.

        Run once with the ambient PATH (native `plutil` on macOS) and once with
        the plutil-free sandbox PATH, so neither validator can silently degrade
        into accepting a broken LaunchAgent.
        """
        for label, env in (("native", None), ("fallback", "sandbox")):
            with self.subTest(validator=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                checkout = _make_checkout(root)
                target = root / "agents"
                broken = checkout / "launchd" / TEMPLATE_NAME
                broken.write_text(
                    broken.read_text(encoding="utf-8").replace("</dict>", "<key>unclosed"),
                    encoding="utf-8",
                )
                run_env = None
                if env == "sandbox":
                    run_env = {"PATH": str(_plutil_free_path(root)), "HOME": str(root)}

                result = _render(checkout, target, env=run_env)
                self.assertNotEqual(result.returncode, 0, "a malformed plist must fail the render")
                # The staging directory is discarded on failure, so a rejected
                # plist never reaches LaunchAgents.
                rendered = sorted(p.name for p in target.glob("*.plist")) if target.exists() else []
                self.assertEqual(rendered, [])


class FileIntelligenceInstallerTests(unittest.TestCase):
    """Text assertions only: CI has no launchctl, and installing is a real act."""

    def setUp(self):
        self.installer = (REPO_ROOT / "scripts/install-file-intelligence-launchd").read_text(
            encoding="utf-8"
        )
        self.uninstaller = (REPO_ROOT / "scripts/uninstall-file-intelligence-launchd").read_text(
            encoding="utf-8"
        )

    def test_installer_rerenders_generated_plist_from_current_checkout(self):
        self.assertIn(f'{RENDERER_NAME}" --replace "$AGENT_DIR"', self.installer)

    def test_installer_requires_automation_opt_in_before_bootstrapping(self):
        self.assertIn("config/ingest.yaml", self.installer)
        # Parsed as YAML by the repo's interpreter, never grepped.
        self.assertIn("import yaml", self.installer)
        self.assertIn('automation = config.get("automation")', self.installer)
        self.assertIn('automation.get("enabled") is True', self.installer)
        code = "\n".join(
            line for line in self.installer.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("grep", code)

        gate = self.installer.index('automation.get("enabled") is True')
        bootstrap = self.installer.index("launchctl bootstrap")
        render = self.installer.index(f'{RENDERER_NAME}" --replace')
        self.assertLess(gate, bootstrap, "the opt-in gate must run before bootstrapping")
        self.assertLess(gate, render, "the opt-in gate must run before anything is rendered")

    def test_installer_bootstraps_into_the_gui_domain(self):
        self.assertIn('launchctl bootout "gui/$UID"', self.installer)
        self.assertIn('launchctl bootstrap "gui/$UID" "$PLIST"', self.installer)

    def test_uninstaller_unloads_then_removes_the_rendered_plist(self):
        self.assertIn('launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true', self.uninstaller)
        # Opt-in agent: uninstall must not leave an armed plist behind.
        self.assertIn('rm -f "$PLIST"', self.uninstaller)
        bootout = self.uninstaller.index("launchctl bootout")
        removal = self.uninstaller.index('rm -f "$PLIST"')
        self.assertLess(bootout, removal)
        self.assertIn("Removed rendered plist:", self.uninstaller)
        self.assertIn("Unloaded", self.uninstaller)


class FileIntelligenceScheduledWrapperTests(unittest.TestCase):
    def test_scheduled_run_is_automated_and_one_shot(self):
        wrapper = (REPO_ROOT / "scripts/file-intelligence-scheduled").read_text(encoding="utf-8")
        # `--automated` is what keeps an unattended run on the long settle
        # window; without it the scheduled run could fall back to the short
        # manual one and move a file a human is still writing.
        self.assertIn("--automated", wrapper)
        self.assertIn(
            'exec "$PYTHON" -m tools.ingest.cli triage all --apply --automated',
            wrapper,
        )
        self.assertIn("set -euo pipefail", wrapper)
        self.assertIn('PYTHON="$ROOT_DIR/.venv/bin/python3"', wrapper)
        self.assertIn('[[ -x "$PYTHON" ]] || PYTHON="/usr/bin/python3"', wrapper)

    def test_new_scripts_are_executable(self):
        for name in (
            "file-intelligence-scheduled",
            RENDERER_NAME,
            "install-file-intelligence-launchd",
            "uninstall-file-intelligence-launchd",
        ):
            path = REPO_ROOT / "scripts" / name
            with self.subTest(script=name):
                self.assertTrue(path.exists(), path)
                self.assertTrue(
                    os.stat(path).st_mode & stat.S_IXUSR, f"{name} must be executable"
                )
                self.assertTrue(
                    path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")
                )


if __name__ == "__main__":
    unittest.main()
