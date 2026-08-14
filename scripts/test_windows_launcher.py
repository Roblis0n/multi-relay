#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "configure-deepseek-subagents.cmd"


@unittest.skipUnless(os.name == "nt", "Windows launcher test")
class WindowsLauncherTests(unittest.TestCase):
    def test_launcher_uses_working_python_command_and_reaches_setup_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_manager = root / "fake manager.py"
            codex_home = root / "Codex Home"
            fake_manager.write_text(
                "import json, sys\n"
                "print('FAKE_MANAGER_ARGS=' + json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            codex_home.mkdir()

            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join(
                [
                    str(Path(sys.executable).parent),
                    str(Path(os.environ["SystemRoot"]) / "System32"),
                ]
            )
            environment["DEEPSEEK_MANAGER"] = str(fake_manager)
            environment["CODEX_HOME"] = str(codex_home)
            environment["DEEPSEEK_NO_PAUSE"] = "1"
            environment.pop("DEEPSEEK_PYTHON", None)

            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", str(LAUNCHER)],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn(
            "FAKE_MANAGER_ARGS="
            + json.dumps(["setup", "--codex-home", str(codex_home)]),
            completed.stdout,
        )


if __name__ == "__main__":
    unittest.main()
