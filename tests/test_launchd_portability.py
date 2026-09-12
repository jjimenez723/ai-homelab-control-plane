from __future__ import annotations

import plistlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent

# Everything the renderer shells out to. `plutil` is deliberately absent: the
# sandbox PATH built from this list is how a macOS developer machine reproduces
# the Linux-CI environment where the native validator does not exist.
RENDERER_BINARIES = ("sh", "bash", "sed", "mkdir", "chmod", "mktemp", "rm", "mv", "dirname", "python3")


def _make_checkout(root: Path) -> Path:
    """A minimal copy of this checkout containing just the renderer + templates."""
    checkout = root / "checkout & portable"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "launchd").mkdir()
    shutil.copy2(REPO_ROOT / "scripts/render-backup-t7-launchd", checkout / "scripts")
    for name in ("backup", "check"):
        shutil.copy2(
            REPO_ROOT / f"launchd/com.ai-homelab.t7-{name}.plist.template",
            checkout / "launchd" / f"com.ai-homelab.t7-{name}.plist.template",
        )
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


class LaunchdPortabilityTests(unittest.TestCase):
    def test_renderer_uses_its_checkout_and_escapes_legal_path_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout & portable"
            scripts = checkout / "scripts"
            launchd = checkout / "launchd"
            target = root / "agents"
            scripts.mkdir(parents=True)
            launchd.mkdir()

            renderer = scripts / "render-backup-t7-launchd"
            shutil.copy2(REPO_ROOT / "scripts/render-backup-t7-launchd", renderer)
            for name in ("backup", "check"):
                shutil.copy2(
                    REPO_ROOT / f"launchd/com.ai-homelab.t7-{name}.plist.template",
                    launchd / f"com.ai-homelab.t7-{name}.plist.template",
                )

            first = subprocess.run([renderer, target], text=True, capture_output=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)

            backup = plistlib.loads((target / "com.ai-homelab.t7-backup.plist").read_bytes())
            check = plistlib.loads((target / "com.ai-homelab.t7-check.plist").read_bytes())
            self.assertEqual(backup["WorkingDirectory"], str(checkout))
            self.assertEqual(
                backup["ProgramArguments"],
                [str(checkout / "scripts/backup-t7-scheduled"), "backup"],
            )
            self.assertEqual(
                check["ProgramArguments"],
                [str(checkout / "scripts/backup-t7-scheduled"), "check"],
            )
            self.assertEqual(backup["StartCalendarInterval"], {"Hour": 2, "Minute": 15})
            self.assertEqual(
                check["StartCalendarInterval"],
                {"Weekday": 7, "Hour": 3, "Minute": 15},
            )

            refused = subprocess.run([renderer, target], text=True, capture_output=True, check=False)
            self.assertEqual(refused.returncode, 1)
            replaced = subprocess.run(
                [renderer, "--replace", target], text=True, capture_output=True, check=False
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)

    def test_renderer_still_validates_when_plutil_is_unavailable(self):
        """The Linux-CI case: no `plutil`, so the plistlib fallback has to run."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = _make_checkout(root)
            bindir = _plutil_free_path(root)
            target = root / "agents"
            self.assertIsNone(shutil.which("plutil", path=str(bindir)))
            self.assertIsNotNone(shutil.which("python3", path=str(bindir)), "fallback needs python3")

            result = subprocess.run(
                [checkout / "scripts/render-backup-t7-launchd", target],
                env={"PATH": str(bindir), "HOME": str(root)},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            backup = plistlib.loads((target / "com.ai-homelab.t7-backup.plist").read_bytes())
            self.assertEqual(backup["WorkingDirectory"], str(checkout))

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
                broken = checkout / "launchd/com.ai-homelab.t7-check.plist.template"
                broken.write_text(
                    broken.read_text(encoding="utf-8").replace("</dict>", "<key>unclosed"),
                    encoding="utf-8",
                )
                run_env = None
                if env == "sandbox":
                    run_env = {"PATH": str(_plutil_free_path(root)), "HOME": str(root)}

                result = subprocess.run(
                    [checkout / "scripts/render-backup-t7-launchd", target],
                    env=run_env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0, "a malformed plist must fail the render")
                # The staging directory is discarded on failure, so a rejected
                # plist never reaches LaunchAgents -- not even the valid sibling.
                rendered = sorted(p.name for p in target.glob("*.plist")) if target.exists() else []
                self.assertEqual(rendered, [])

    def test_installer_rerenders_generated_plists_from_current_checkout(self):
        installer = (REPO_ROOT / "scripts/install-backup-t7-launchd").read_text(encoding="utf-8")
        self.assertIn('render-backup-t7-launchd" --replace "$AGENT_DIR"', installer)


if __name__ == "__main__":
    unittest.main()
