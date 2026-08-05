"""Tests for the generated Claude Code settings."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

import install


class InstallTests(unittest.TestCase):
    def test_generated_statusline_runs_outside_repository(self) -> None:
        command = install.settings_snippet()["statusLine"]["command"]
        self.assertIsInstance(command, str)

        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["HEADROOM_STATE_DIR"] = directory
            result = subprocess.run(
                command,
                input=json.dumps({}),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=directory,
                env=environment,
                timeout=10,
                check=False,
                shell=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
