"""Tests for top-level command-line behavior."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import headroom
from headroom import cli
from headroom.install_info import InstallInfo
from headroom.settings import codex_hooks_snippet


class CliTests(unittest.TestCase):
    def test_hook_and_statusline_never_open_a_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "HEADROOM_STATE_DIR": str(root / "state"),
                "HEADROOM_CODEX_HOME": str(root / "codex"),
                "HEADROOM_CODEX_RPC": "0",
                "HEADROOM_UPDATE_CHECK": "1",
            }
            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch(
                    "socket.socket", side_effect=AssertionError("socket opened")
                ):
                    with redirect_stdout(output):
                        with mock.patch("sys.stdin", io.StringIO("")):
                            self.assertEqual(cli.main(["hook"]), 0)
                        with mock.patch("sys.stdin", io.StringIO("{}")):
                            self.assertEqual(cli.main(["statusline"]), 0)

    def test_update_check_is_disabled_by_default_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "HEADROOM_STATE_DIR": directory,
                "HEADROOM_CODEX_HOME": str(Path(directory) / "codex"),
                "HEADROOM_CODEX_RPC": "0",
            }
            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch("headroom.cli._refresh_codex"):
                    with mock.patch(
                        "headroom.update_check.request.urlopen",
                        side_effect=AssertionError("network attempted"),
                    ) as opened:
                        with redirect_stdout(output):
                            result = cli.main(["status"])

            self.assertEqual(result, 0)
            opened.assert_not_called()
            self.assertIn(
                "Updates: checking is off; set HEADROOM_UPDATE_CHECK=1 to enable.",
                output.getvalue(),
            )

    def test_explicit_disable_suppresses_update_discovery_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "HEADROOM_STATE_DIR": directory,
                "HEADROOM_CODEX_HOME": str(Path(directory) / "codex"),
                "HEADROOM_CODEX_RPC": "0",
                "HEADROOM_UPDATE_CHECK": "0",
            }
            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch("headroom.cli._refresh_codex"):
                    with redirect_stdout(output):
                        result = cli.main(["status"])

            self.assertEqual(result, 0)
            self.assertNotIn("Updates:", output.getvalue())

    def test_enabled_stubbed_check_adds_update_line_to_status(self) -> None:
        document = {
            "releases": {
                "0.1.10": [{"filename": "headroom.whl", "yanked": False}]
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "HEADROOM_STATE_DIR": directory,
                "HEADROOM_CODEX_HOME": str(Path(directory) / "codex"),
                "HEADROOM_CODEX_RPC": "0",
                "HEADROOM_UPDATE_CHECK": "1",
            }
            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch("headroom.cli._refresh_codex"):
                    with mock.patch("headroom.cli._installed_version", return_value="0.1.9"):
                        with mock.patch(
                            "headroom.update_check._fetch_pypi_json",
                            return_value=document,
                        ):
                            with redirect_stdout(output):
                                result = cli.main(["status"])

            self.assertEqual(result, 0)
            self.assertIn(
                "Update available: headroom-cli 0.1.10 (installed 0.1.9). Run headroom update.",
                output.getvalue(),
            )

    def test_cached_failure_is_visible_in_doctor_with_next_due_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "CODEX_HOME": str(root / "codex-hooks"),
                "HEADROOM_STATE_DIR": str(root / "state"),
                "HEADROOM_CODEX_HOME": str(root / "codex"),
                "HEADROOM_CODEX_RPC": "0",
                "HEADROOM_UPDATE_CHECK": "1",
            }
            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch("headroom.cli._installed_version", return_value="1.0"):
                    with mock.patch(
                        "headroom.update_check._fetch_pypi_json",
                        side_effect=OSError("endpoint unavailable"),
                    ) as fetched:
                        with redirect_stdout(output):
                            first = cli.main(["doctor"])
                        with redirect_stdout(output):
                            second = cli.main(["doctor"])

            self.assertEqual(first, 0)
            self.assertEqual(second, 0)
            self.assertEqual(fetched.call_count, 1)
            self.assertIn("Last outcome: failed: network check failed", output.getvalue())
            self.assertRegex(output.getvalue(), r"Next eligible check: \d{4}-")

    def test_unparsable_installed_version_is_reported_by_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "CODEX_HOME": str(root / "codex-hooks"),
                "HEADROOM_STATE_DIR": str(root / "state"),
                "HEADROOM_CODEX_HOME": str(root / "codex"),
                "HEADROOM_CODEX_RPC": "0",
                "HEADROOM_UPDATE_CHECK": "1",
            }
            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch(
                    "headroom.cli._installed_version", return_value="development"
                ):
                    with mock.patch(
                        "headroom.update_check.request.urlopen",
                        side_effect=AssertionError("network attempted"),
                    ) as opened:
                        with redirect_stdout(output):
                            result = cli.main(["doctor"])

            self.assertEqual(result, 0)
            opened.assert_not_called()
            self.assertIn("Last outcome: failed: installed version", output.getvalue())

    def test_update_prints_commands_without_executing_or_changing_files(self) -> None:
        cases = (
            ("uv-tool", "uv tool upgrade headroom-cli"),
            ("pip", "pip install -U headroom-cli"),
            ("source", "git pull\n  pip install -e ."),
            ("unknown", "No update command is suggested."),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "unchanged.txt"
            marker.write_text("original\n", encoding="utf-8")
            for mode, expected in cases:
                with self.subTest(mode=mode):
                    info = InstallInfo(
                        path=root / "headroom",
                        mode="source" if mode == "source" else "installed",
                        version="1.0",
                        modified_at=None,
                        commit=None,
                        update_mode=mode,
                    )
                    output = io.StringIO()
                    with mock.patch("headroom.cli.inspect_install", return_value=info):
                        with redirect_stdout(output):
                            result = cli.main(["update"])

                    self.assertEqual(result, 0)
                    self.assertIn(expected, output.getvalue())
                    self.assertIn("Nothing was changed.", output.getvalue())
                    self.assertEqual(marker.read_text(encoding="utf-8"), "original\n")

    def test_version_uses_installed_distribution_metadata(self) -> None:
        output = io.StringIO()

        with mock.patch("headroom.cli.metadata.version", return_value="2.3.4"):
            with mock.patch("headroom.cli.source_commit", return_value="a1b2c3d"):
                with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    cli.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), "headroom 2.3.4 (a1b2c3d)\n")

    def test_version_without_git_metadata_is_plain(self) -> None:
        output = io.StringIO()

        with mock.patch("headroom.cli.metadata.version", return_value="2.3.4"):
            with mock.patch("headroom.cli.source_commit", return_value=None):
                with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    cli.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), "headroom 2.3.4\n")

    def test_version_without_distribution_metadata_uses_package_version(self) -> None:
        output = io.StringIO()

        with mock.patch(
            "headroom.cli.metadata.version",
            side_effect=cli.metadata.PackageNotFoundError,
        ):
            with mock.patch("headroom.cli.source_commit", return_value=None):
                with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    cli.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertRegex(output.getvalue(), r"^headroom \d+\.\d+\.\d+(?:[^\s]*)?\n$")
        self.assertEqual(output.getvalue(), "headroom {}\n".format(headroom.__version__))

    def test_version_does_not_crash_outside_a_git_repository(self) -> None:
        from headroom.install_info import source_commit

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "headroom"
            package.mkdir()
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (root / ".git").mkdir()
            self.assertIsNone(source_commit(package))

        output = io.StringIO()
        with mock.patch("headroom.cli.source_commit", return_value=None):
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                cli.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(output.getvalue().startswith("headroom "))

    def test_source_commit_reads_head_without_running_git(self) -> None:
        from headroom.install_info import inspect_install

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "headroom"
            git = root / ".git"
            package.mkdir()
            git.mkdir()
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (git / "HEAD").write_text("0123456789abcdef" * 2 + "01234567\n", encoding="utf-8")

            info = inspect_install("1.2.3", package)

        self.assertEqual(info.mode, "source")
        self.assertEqual(info.commit, "0123456")

    def test_package_copy_nested_in_checkout_is_installed(self) -> None:
        from headroom.install_info import inspect_install

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / ".venv" / "site-packages" / "headroom"
            package.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (root / ".git").mkdir()

            info = inspect_install("1.2.3", package)

        self.assertEqual(info.mode, "installed")
        self.assertIsNone(info.commit)

    def test_uv_tool_install_is_detected_from_receipt(self) -> None:
        from headroom.install_info import inspect_install

        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / "headroom-cli"
            package = environment / "Lib" / "site-packages" / "headroom"
            package.mkdir(parents=True)
            (environment / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")

            info = inspect_install("1.2.3", package)

        self.assertEqual(info.update_mode, "uv-tool")

    def test_pip_install_is_detected_from_installer_metadata(self) -> None:
        import headroom.install_info as install_info

        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "site-packages" / "headroom"
            package.mkdir(parents=True)
            distribution = mock.Mock()
            distribution.read_text.return_value = "pip\n"
            with mock.patch.object(install_info, "_PACKAGE_PATH", package):
                with mock.patch.object(
                    install_info.metadata,
                    "distribution",
                    return_value=distribution,
                ):
                    info = install_info.inspect_install("1.2.3")

        self.assertEqual(info.update_mode, "pip")

    def test_pipx_install_is_not_misidentified_as_pip(self) -> None:
        from headroom.install_info import inspect_install

        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / "headroom-cli"
            package = environment / "Lib" / "site-packages" / "headroom"
            package.mkdir(parents=True)
            (environment / "pipx_metadata.json").write_text("{}\n", encoding="utf-8")

            info = inspect_install("1.2.3", package)

        self.assertEqual(info.update_mode, "unknown")

    def test_doctor_reports_install_path_mode_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "CODEX_HOME": str(Path(directory) / "codex-hooks"),
                "HEADROOM_STATE_DIR": directory,
                "HEADROOM_CODEX_HOME": str(Path(directory) / "codex"),
                "HEADROOM_CODEX_RPC": "0",
            }
            output = io.StringIO()
            with mock.patch.dict("os.environ", environment, clear=True):
                with redirect_stdout(output):
                    result = cli.main(["doctor"])

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("Install\n", rendered)
        self.assertIn(
            "  Path: {}".format(Path(headroom.__file__).resolve().parent),
            rendered,
        )
        self.assertRegex(rendered, r"(?m)^  Mode: (?:installed|source)$")
        self.assertIn("  Version: {}".format(cli._installed_version()), rendered)
        self.assertRegex(rendered, r"(?m)^  Modified: .+$")
        self.assertIn("Codex hooks file: {}".format(Path(directory) / "codex-hooks" / "hooks.json"), rendered)
        self.assertIn("Codex hook: not registered (file missing)", rendered)

    def test_doctor_reports_registered_codex_hook_and_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex"
            codex_home.mkdir()
            hooks_path = codex_home / "hooks.json"
            hooks_path.write_text(json.dumps(codex_hooks_snippet()), encoding="utf-8")
            environment = {
                "CODEX_HOME": str(codex_home),
                "HEADROOM_STATE_DIR": str(root / "state"),
                "HEADROOM_CODEX_HOME": str(root / "capture"),
                "HEADROOM_CODEX_RPC": "0",
            }
            output = io.StringIO()

            with mock.patch.dict(os.environ, environment, clear=True):
                with redirect_stdout(output):
                    result = cli.main(["doctor"])

            self.assertEqual(result, 0)
            self.assertIn("Codex hooks file: {}".format(hooks_path), output.getvalue())
            self.assertIn("Codex hook: registered", output.getvalue())

    def test_doctor_reports_registration_separately_from_command_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex"
            codex_home.mkdir()
            hooks_path = codex_home / "hooks.json"
            document = {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "headroom hook",
                                    "timeoutSec": 30,
                                }
                            ]
                        }
                    ]
                }
            }
            hooks_path.write_text(json.dumps(document), encoding="utf-8")
            environment = {
                "CODEX_HOME": str(codex_home),
                "HEADROOM_STATE_DIR": str(root / "state"),
                "HEADROOM_CODEX_HOME": str(root / "capture"),
                "HEADROOM_CODEX_RPC": "0",
            }
            output = io.StringIO()

            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch("headroom.settings.shutil.which", return_value=None):
                    with redirect_stdout(output):
                        result = cli.main(["doctor"])

            self.assertEqual(result, 0)
            self.assertIn("Codex hook: registered", output.getvalue())
            self.assertIn("Codex hook command: unavailable", output.getvalue())

    def test_package_and_project_versions_match(self) -> None:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        project_section = pyproject.read_text(encoding="utf-8").split("[project]", 1)[1]
        project_section = project_section.split("\n[", 1)[0]
        match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project_section, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertEqual(headroom.__version__, match.group(1))


if __name__ == "__main__":
    unittest.main()
