from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_install_update_and_uninstall_manage_user_skill_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            prefix = home / ".local"
            home.mkdir()
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            install = [str(TOOL_ROOT / "install.sh"), "--prefix", str(prefix)]
            first = subprocess.run(
                install,
                cwd=TOOL_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Codex skill:", first.stdout)
            command = prefix / "bin" / "codex-reconnect"
            app = prefix / "share" / "codex-reconnect"
            skill = home / ".agents" / "skills" / "reconnect"
            self.assertTrue(command.is_symlink())
            self.assertTrue(skill.is_symlink())
            self.assertEqual(os.readlink(skill), str(app / "skills" / "reconnect"))
            version = subprocess.run(
                [str(command), "--version"],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("0.3.6", version.stdout)
            subprocess.run(
                install,
                cwd=TOOL_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(skill.is_symlink())
            state = home / ".local" / "state" / "codex-reconnect" / "kept"
            state.parent.mkdir(parents=True)
            state.write_text("keep", encoding="utf-8")
            subprocess.run(
                [str(app / "uninstall.sh"), "--prefix", str(prefix)],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse(command.exists())
            self.assertFalse(skill.exists())
            self.assertFalse(app.exists())
            self.assertEqual(state.read_text(encoding="utf-8"), "keep")

    def test_installer_refuses_to_replace_user_owned_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            prefix = home / ".local"
            skill = home / ".agents" / "skills" / "reconnect"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("user owned", encoding="utf-8")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            result = subprocess.run(
                [str(TOOL_ROOT / "install.sh"), "--prefix", str(prefix)],
                cwd=TOOL_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("Refusing to replace non-symlink user skill", result.stderr)
            self.assertEqual(
                (skill / "SKILL.md").read_text(encoding="utf-8"), "user owned"
            )

    def test_installer_refuses_to_replace_foreign_command_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            prefix = home / ".local"
            command = prefix / "bin" / "codex-reconnect"
            command.parent.mkdir(parents=True)
            command.symlink_to("/opt/other/codex-reconnect")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            result = subprocess.run(
                [str(TOOL_ROOT / "install.sh"), "--prefix", str(prefix)],
                cwd=TOOL_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("Refusing to replace command linked elsewhere", result.stderr)
            self.assertEqual(os.readlink(command), "/opt/other/codex-reconnect")


if __name__ == "__main__":
    unittest.main()
