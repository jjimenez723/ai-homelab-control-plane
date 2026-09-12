from __future__ import annotations

import plistlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent


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

    def test_installer_rerenders_generated_plists_from_current_checkout(self):
        installer = (REPO_ROOT / "scripts/install-backup-t7-launchd").read_text(encoding="utf-8")
        self.assertIn('render-backup-t7-launchd" --replace "$AGENT_DIR"', installer)


if __name__ == "__main__":
    unittest.main()
