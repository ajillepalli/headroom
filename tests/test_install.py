"""Tests for installing the Claude Code settings."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from headroom import cli
from headroom.settings import settings_snippet


class InstallTests(unittest.TestCase):
    def test_init_preserves_unrelated_settings_and_hook_events(self) -> None:
        original = {
            "theme": "dark",
            "permissions": {"allow": ["Read"]},
            "hooks": {
                "PreToolUse": [{"hooks": [{"type": "command", "command": "pre"}]}],
                "PostToolUse": [{"hooks": [{"type": "command", "command": "post"}]}],
                "SessionStart": [{"hooks": [{"type": "command", "command": "session"}]}],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original_text = json.dumps(original, indent=2) + "\n"
            path.write_text(original_text, encoding="utf-8")

            result, stdout, stderr = self._run_init(path)

            self.assertEqual(result, 0, stderr)
            self.assertIn("Backup:", stdout)
            installed = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(installed["theme"], original["theme"])
            self.assertEqual(installed["permissions"], original["permissions"])
            for event in ("PreToolUse", "PostToolUse", "SessionStart"):
                self.assertEqual(installed["hooks"][event], original["hooks"][event])
            self.assertEqual(installed["statusLine"]["command"], "headroom statusline")
            self.assertEqual(
                installed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
                "headroom hook",
            )
            backups = list(path.parent.glob("settings.json.*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), original_text)

    def test_init_appends_to_existing_user_prompt_submit(self) -> None:
        existing_hook = {"hooks": [{"type": "command", "command": "existing-hook"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [existing_hook]}}), encoding="utf-8")

            result, _, stderr = self._run_init(path)

            self.assertEqual(result, 0, stderr)
            prompt_hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
            self.assertEqual(prompt_hooks[0], existing_hook)
            self.assertEqual(len(prompt_hooks), 2)

    def test_running_init_twice_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}\n", encoding="utf-8")

            first_result, _, first_stderr = self._run_init(path)
            first_content = path.read_text(encoding="utf-8")
            first_backups = list(path.parent.glob("settings.json.*.bak"))
            second_result, second_stdout, second_stderr = self._run_init(path)

            self.assertEqual(first_result, 0, first_stderr)
            self.assertEqual(second_result, 0, second_stderr)
            self.assertIn("already configured", second_stdout)
            self.assertEqual(path.read_text(encoding="utf-8"), first_content)
            self.assertEqual(list(path.parent.glob("settings.json.*.bak")), first_backups)
            installed = json.loads(first_content)
            self.assertEqual(len(installed["hooks"]["UserPromptSubmit"]), 1)

    def test_dry_run_prints_diff_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = '{"theme": "dark"}\n'
            path.write_text(original, encoding="utf-8")

            result, stdout, stderr = self._run_init(path, "--dry-run")

            self.assertEqual(result, 0, stderr)
            self.assertIn("--- {}".format(path), stdout)
            self.assertIn("+  \"statusLine\"", stdout)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(path.parent.glob("settings.json.*.bak")), [])

    def test_malformed_json_is_refused_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = "{not json\n"
            path.write_text(original, encoding="utf-8")

            result, _, stderr = self._run_init(path)

            self.assertNotEqual(result, 0)
            self.assertIn("refusing to change", stderr)
            self.assertIn("invalid JSON", stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(path.parent.glob("settings.json.*.bak")), [])

    def test_print_emits_snippet_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"

            result, stdout, stderr = self._run_init(path, "--print")

            self.assertEqual(result, 0, stderr)
            snippet = json.loads(stdout)
            self.assertEqual(snippet["statusLine"]["command"], "headroom statusline")
            self.assertEqual(
                snippet["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
                "headroom hook",
            )
            self.assertFalse(path.exists())

    def test_commands_use_bare_headroom_when_it_is_on_path(self) -> None:
        with mock.patch("headroom.settings.shutil.which", return_value=os.devnull):
            snippet = settings_snippet()

        self.assertEqual(snippet["statusLine"]["command"], "headroom statusline")
        self.assertEqual(
            snippet["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
            "headroom hook",
        )

    def test_commands_use_absolute_python_module_fallback_without_headroom(self) -> None:
        with mock.patch("headroom.settings.shutil.which", return_value=None):
            snippet = settings_snippet()

        commands = (
            snippet["statusLine"]["command"],
            snippet["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
        )
        for command, subcommand in zip(commands, ("statusline", "hook")):
            self.assertIn(str(Path(sys.executable).resolve()), command)
            self.assertIn("-m headroom.cli {}".format(subcommand), command)
            self.assertIn("PYTHONPATH", command)

    @staticmethod
    def _run_init(path: Path, *extra_arguments: str):
        stdout = io.StringIO()
        stderr = io.StringIO()
        arguments = ["init", "--settings", str(path)] + list(extra_arguments)
        with mock.patch("headroom.settings.shutil.which", return_value=os.devnull):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = cli.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
