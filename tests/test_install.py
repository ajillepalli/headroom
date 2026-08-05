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
from headroom import settings as settings_module
from headroom.settings import codex_hooks_snippet, settings_snippet


class InstallTests(unittest.TestCase):
    CODEX_DOCUMENT = {
        "description": "headroom",
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "headroom hook",
                            "timeoutSec": 10,
                        }
                    ]
                }
            ]
        },
    }

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

    def test_codex_command_uses_python_fallback_without_headroom(self) -> None:
        with mock.patch("headroom.settings.shutil.which", return_value=None):
            command = codex_hooks_snippet()["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]

        self.assertIn(str(Path(sys.executable).resolve()), command)
        self.assertIn("-m headroom.cli hook", command)
        self.assertIn("PYTHONPATH", command)

    def test_codex_init_writes_verified_schema_to_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex"
            environment = {"CODEX_HOME": str(home)}

            result, _, stderr = self._run_cli(["init", "--codex"], environment)

            self.assertEqual(result, 0, stderr)
            installed_text = (home / "hooks.json").read_text(encoding="utf-8")
            installed = json.loads(installed_text)
            self.assertEqual(installed, self.CODEX_DOCUMENT)
            self.assertEqual(installed_text, json.dumps(self.CODEX_DOCUMENT, indent=2) + "\n")
            with mock.patch("headroom.settings.shutil.which", return_value=os.devnull):
                self.assertEqual(codex_hooks_snippet(), self.CODEX_DOCUMENT)

    def test_codex_home_override_takes_precedence_for_tests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment_home = root / "environment-codex"
            override_home = root / "override-codex"

            result, _, stderr = self._run_cli(
                ["init", "--codex", "--codex-home", str(override_home)],
                {"CODEX_HOME": str(environment_home)},
            )

            self.assertEqual(result, 0, stderr)
            self.assertTrue((override_home / "hooks.json").is_file())
            self.assertFalse((environment_home / "hooks.json").exists())

    def test_codex_init_preserves_other_entries_and_appends_prompt_hook(self) -> None:
        existing_prompt = {"hooks": [{"type": "command", "command": "existing"}]}
        original = {
            "description": "another tool",
            "futureKey": {"preserve": True},
            "hooks": {
                "PreToolUse": [{"hooks": [{"type": "command", "command": "pre"}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": "end"}]}],
                "UserPromptSubmit": [existing_prompt],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex"
            home.mkdir()
            path = home / "hooks.json"
            path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

            result, _, stderr = self._run_cli(
                ["init", "--codex", "--codex-home", str(home)]
            )

            self.assertEqual(result, 0, stderr)
            installed = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(installed["futureKey"], original["futureKey"])
            self.assertEqual(installed["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
            self.assertEqual(installed["hooks"]["SessionEnd"], original["hooks"]["SessionEnd"])
            self.assertEqual(installed["hooks"]["UserPromptSubmit"][0], existing_prompt)
            self.assertEqual(installed["hooks"]["UserPromptSubmit"][1], self.CODEX_DOCUMENT["hooks"]["UserPromptSubmit"][0])
            self.assertEqual(installed["description"], original["description"])

    def test_codex_init_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex"
            arguments = ["init", "--codex", "--codex-home", str(home)]

            first_result, _, first_stderr = self._run_cli(arguments)
            first_content = (home / "hooks.json").read_text(encoding="utf-8")
            second_result, second_stdout, second_stderr = self._run_cli(arguments)

            self.assertEqual(first_result, 0, first_stderr)
            self.assertEqual(second_result, 0, second_stderr)
            self.assertIn("already configured", second_stdout)
            self.assertEqual((home / "hooks.json").read_text(encoding="utf-8"), first_content)
            self.assertEqual(len(json.loads(first_content)["hooks"]["UserPromptSubmit"]), 1)
            self.assertEqual(list(home.glob("hooks.json.*.bak")), [])

    def test_customized_headroom_hook_is_preserved_and_not_duplicated_for_both_targets(self) -> None:
        customized = {
            "hooks": [
                {
                    "type": "command",
                    "command": "  headroom   hook  ",
                    "timeoutSec": 30,
                    "custom": True,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(
                json.dumps({"hooks": {"UserPromptSubmit": [customized]}}),
                encoding="utf-8",
            )
            codex_home = root / "codex"
            codex_home.mkdir()
            hooks_path = codex_home / "hooks.json"
            hooks_path.write_text(
                json.dumps({"hooks": {"UserPromptSubmit": [customized]}}),
                encoding="utf-8",
            )

            result, stdout, stderr = self._run_cli(
                [
                    "init",
                    "--all",
                    "--settings",
                    str(settings),
                    "--codex-home",
                    str(codex_home),
                ]
            )

            self.assertEqual(result, 0, stderr)
            for path in (settings, hooks_path):
                prompt_hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
                self.assertEqual(prompt_hooks, [customized])
            self.assertEqual(stdout.count("customized headroom hook"), 2)

    def test_codex_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex"

            result, stdout, stderr = self._run_cli(
                ["init", "--codex", "--codex-home", str(home), "--dry-run"]
            )

            self.assertEqual(result, 0, stderr)
            self.assertIn("--- {}".format(home / "hooks.json"), stdout)
            self.assertFalse(home.exists())

    def test_codex_print_emits_verified_schema_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex"

            result, stdout, stderr = self._run_cli(
                ["init", "--codex", "--codex-home", str(home), "--print"]
            )

            self.assertEqual(result, 0, stderr)
            self.assertEqual(json.loads(stdout), self.CODEX_DOCUMENT)
            self.assertFalse(home.exists())

    def test_codex_malformed_json_is_refused_and_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex"
            home.mkdir()
            path = home / "hooks.json"
            original = "{not json\n"
            path.write_text(original, encoding="utf-8")

            result, _, stderr = self._run_cli(
                ["init", "--codex", "--codex-home", str(home)]
            )

            self.assertNotEqual(result, 0)
            self.assertIn("refusing to change", stderr)
            self.assertIn("invalid JSON", stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(home.glob("hooks.json.*.bak")), [])

    def test_codex_init_backs_up_original_content_and_prints_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex"
            home.mkdir()
            path = home / "hooks.json"
            original = '{"hooks": {"PreToolUse": []}}\n'
            path.write_text(original, encoding="utf-8")

            result, stdout, stderr = self._run_cli(
                ["init", "--codex", "--codex-home", str(home)]
            )

            self.assertEqual(result, 0, stderr)
            backups = list(home.glob("hooks.json.*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertIn("Backup: {}".format(backups[0]), stdout)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def test_init_all_configures_both_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "claude" / "settings.json"
            codex_home = root / "codex"

            result, _, stderr = self._run_cli(
                [
                    "init",
                    "--all",
                    "--settings",
                    str(settings),
                    "--codex-home",
                    str(codex_home),
                ]
            )

            self.assertEqual(result, 0, stderr)
            self.assertEqual(json.loads(codex_home.joinpath("hooks.json").read_text(encoding="utf-8")), self.CODEX_DOCUMENT)
            claude = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(claude["statusLine"]["command"], "headroom statusline")
            self.assertEqual(claude["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], "headroom hook")

    def test_init_all_preflights_both_before_applying_either(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text('{"theme": "dark"}\n', encoding="utf-8")
            codex_home = root / "codex"
            codex_home.mkdir()
            hooks = codex_home / "hooks.json"
            hooks.write_text("{broken\n", encoding="utf-8")

            result, _, stderr = self._run_cli(
                ["init", "--all", "--settings", str(settings), "--codex-home", str(codex_home)]
            )

            self.assertNotEqual(result, 0)
            self.assertIn("refusing to change {}".format(hooks), stderr)
            self.assertEqual(settings.read_text(encoding="utf-8"), '{"theme": "dark"}\n')
            self.assertEqual(hooks.read_text(encoding="utf-8"), "{broken\n")

    def test_init_all_rejects_aliased_targets_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex"
            aliased_settings = codex_home / "child" / ".." / "hooks.json"

            with mock.patch("headroom.settings._prepare_install") as preflight:
                result, _, stderr = self._run_cli(
                    [
                        "init",
                        "--all",
                        "--settings",
                        str(aliased_settings),
                        "--codex-home",
                        str(codex_home),
                    ]
                )

            self.assertNotEqual(result, 0)
            self.assertIn("same file as both Claude and Codex", stderr)
            preflight.assert_not_called()

    def test_concurrent_creation_after_preflight_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            concurrent = '{"created": "concurrently"}\n'
            real_prepare = settings_module._prepare_install

            def prepare_then_create(target):
                prepared = real_prepare(target)
                path.write_text(concurrent, encoding="utf-8")
                return prepared

            with mock.patch("headroom.settings._prepare_install", side_effect=prepare_then_create):
                result, _, stderr = self._run_init(path)

            self.assertNotEqual(result, 0)
            self.assertIn("appeared after preflight", stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), concurrent)
            self.assertEqual(list(path.parent.glob("settings.json.*.bak")), [])

    def test_concurrent_edit_after_preflight_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"before": true}\n', encoding="utf-8")
            concurrent = '{"edited": "concurrently"}\n'
            real_prepare = settings_module._prepare_install

            def prepare_then_edit(target):
                prepared = real_prepare(target)
                path.write_text(concurrent, encoding="utf-8")
                return prepared

            with mock.patch("headroom.settings._prepare_install", side_effect=prepare_then_edit):
                result, _, stderr = self._run_init(path)

            self.assertNotEqual(result, 0)
            self.assertIn("changed after preflight", stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), concurrent)
            self.assertEqual(list(path.parent.glob("settings.json.*.bak")), [])

    def test_init_all_rolls_back_first_target_if_second_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "claude" / "settings.json"
            codex_home = root / "codex"
            real_write_prepared = settings_module._write_prepared
            calls = 0

            def fail_second_write(item) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated Codex write failure")
                real_write_prepared(item)

            with mock.patch("headroom.settings._write_prepared", side_effect=fail_second_write):
                result, _, stderr = self._run_cli(
                    ["init", "--all", "--settings", str(settings), "--codex-home", str(codex_home)]
                )

            self.assertNotEqual(result, 0)
            self.assertIn("restored the previously updated target", stderr)
            self.assertFalse(settings.exists())
            self.assertFalse((codex_home / "hooks.json").exists())

    @staticmethod
    def _run_init(path: Path, *extra_arguments: str):
        stdout = io.StringIO()
        stderr = io.StringIO()
        arguments = ["init", "--settings", str(path)] + list(extra_arguments)
        with mock.patch("headroom.settings.shutil.which", return_value=os.devnull):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = cli.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def _run_cli(arguments, environment=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        selected_environment = environment or {}
        with mock.patch.dict(os.environ, selected_environment, clear=True):
            with mock.patch("headroom.settings.shutil.which", return_value=os.devnull):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = cli.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
