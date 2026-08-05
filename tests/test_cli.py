"""Tests for top-level command-line behavior."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
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
                        "headroom.update_check._open_pypi",
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
                        "headroom.update_check._open_pypi",
                        side_effect=AssertionError("network attempted"),
                    ) as opened:
                        with redirect_stdout(output):
                            result = cli.main(["doctor"])

            self.assertEqual(result, 0)
            opened.assert_not_called()
            self.assertIn("Last outcome: failed: installed version", output.getvalue())

    def test_doctor_ignores_cache_for_another_installed_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "update-check.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "checked_at": 100.0,
                        "installed_version": "1.0",
                        "outcome": "current",
                        "latest_version": None,
                        "reason": None,
                    }
                ),
                encoding="utf-8",
            )
            environment = {
                "CODEX_HOME": str(root / "codex-hooks"),
                "HEADROOM_STATE_DIR": str(state_dir),
                "HEADROOM_CODEX_HOME": str(root / "codex"),
                "HEADROOM_CODEX_RPC": "0",
                "HEADROOM_UPDATE_CHECK": "0",
            }
            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch("headroom.cli._installed_version", return_value="2.0"):
                    with redirect_stdout(output):
                        result = cli.main(["doctor"])

            self.assertEqual(result, 0)
            self.assertIn("Last outcome: not checked", output.getvalue())
            self.assertNotIn("Last outcome: up to date", output.getvalue())

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
            forbidden = AssertionError("update command executed a process")
            with mock.patch("subprocess.run", side_effect=forbidden):
                with mock.patch("subprocess.Popen", side_effect=forbidden):
                    with mock.patch("os.system", side_effect=forbidden):
                        with mock.patch("os.execv", side_effect=forbidden):
                            for mode, expected in cases:
                                with self.subTest(mode=mode):
                                    info = InstallInfo(
                                        path=root / "headroom",
                                        mode=(
                                            "source"
                                            if mode == "source"
                                            else "installed"
                                        ),
                                        version="1.0",
                                        modified_at=None,
                                        commit=None,
                                        update_mode=mode,
                                    )
                                    output = io.StringIO()
                                    with mock.patch(
                                        "headroom.cli.inspect_install",
                                        return_value=info,
                                    ):
                                        with redirect_stdout(output):
                                            result = cli.main(["update"])

                                    self.assertEqual(result, 0)
                                    self.assertIn(expected, output.getvalue())
                                    self.assertIn(
                                        "Nothing was changed.",
                                        output.getvalue(),
                                    )
                                    self.assertEqual(
                                        marker.read_text(encoding="utf-8"),
                                        "original\n",
                                    )

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


def _write_history(state_dir: Path, records) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, separators=(",", ":")) for record in records]
    (state_dir / "history.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _steady_climb_records(now: float, source: str = "claude", window: str = "short"):
    """Six records, 600s apart, climbing 10 points each interval, ending 30s
    ago (within Claude's 300s freshness window -- see freshness.py).

    Every interval carries the same rate, so max_relative_deviation and
    rate_drift are both 0.0, max_raw_rate_ratio is 1.0 (no burst), the
    largest single interval supplies only 1/5 of total usage, and there are
    5 post-folding intervals -- clearing every severity.py trust threshold
    with margin. The projected exhaustion (40 more points at this rate,
    40 minutes past the last capture) lands well before the 2-hour resets_at
    below, so exhaustion_precedes_reset is True. The last record's fields
    are also what ``_matching_state_snapshot`` below turns into a FRESH
    Reading, since severity.burn_rate_evidence_is_current requires one.
    """

    base = now - 30.0 - 3_000.0
    resets_at = now + 7_200.0
    return [
        {
            "captured_at": base + index * 600.0,
            "used_percentage": 10.0 * (index + 1),
            "resets_at": resets_at,
            "source": source,
            "window": window,
        }
        for index in range(6)
    ]


def _matching_state_snapshot(records) -> dict:
    """A state.json snapshot dict matching a history record list's latest
    entry, so the corresponding Reading is FRESH and
    severity.burn_rate_evidence_is_current has current evidence to check.
    Real usage always has this correspondence: the latest history line and
    the stored state snapshot for a source/window come from the same
    capture.
    """

    latest = records[-1]
    return {
        "used_percentage": latest["used_percentage"],
        "captured_at": latest["captured_at"],
        "resets_at": latest["resets_at"],
        "window": latest["window"],
        "source": latest["source"],
        "limit_reached": False,
        "raw": {},
    }


def _write_state(state_dir: Path, snapshots) -> None:
    """Write a state.json whose sources contain each given snapshot dict."""

    state_dir.mkdir(parents=True, exist_ok=True)
    sources: dict = {}
    for snapshot in snapshots:
        sources.setdefault(snapshot["source"], {})[snapshot["window"]] = snapshot
    (state_dir / "state.json").write_text(
        json.dumps({"version": 1, "sources": sources}), encoding="utf-8"
    )


def _too_few_samples_records(now: float, source: str = "codex", window: str = "weekly"):
    """Two records: below burn_rate.MIN_SAMPLES, so the projection declines
    with TOO_FEW_SAMPLES."""

    return [
        {
            "captured_at": now - 120.0,
            "used_percentage": 5.0,
            "resets_at": now + 500_000.0,
            "source": source,
            "window": window,
        },
        {
            "captured_at": now - 60.0,
            "used_percentage": 6.0,
            "resets_at": now + 500_000.0,
            "source": source,
            "window": window,
        },
    ]


def _bursty_but_present_records(now: float, source: str = "claude", window: str = "weekly"):
    """Five monotonic records with wildly uneven per-interval rates.

    A projection exists (reason is None: the fit succeeds) but its
    max_relative_deviation is well above MAX_TRUSTED_RELATIVE_DEVIATION, so
    severity.py's policy declines to call it trustworthy. This is the
    "present but not worth showing" case, distinct from a declined
    projection (see burn_rate.py's own
    test_uneven_intervals_still_project_and_report_the_disagreement, which
    this mirrors).
    """

    base = now - 7_200.0
    resets_at = now + 12_800.0
    offsets_and_usage = ((0.0, 10.0), (1_800.0, 30.0), (3_600.0, 32.0), (5_400.0, 55.0), (7_200.0, 57.0))
    return [
        {
            "captured_at": base + offset,
            "used_percentage": usage,
            "resets_at": resets_at,
            "source": source,
            "window": window,
        }
        for offset, usage in offsets_and_usage
    ]


class BurnRateSurfaceTests(unittest.TestCase):
    """End-to-end coverage of burn-rate wiring across json, doctor, status,
    and hook. Unit-level policy and rendering edge cases live in
    tests/test_burn_rate_policy.py; these tests exist to prove each surface
    actually reads history.jsonl, calls the right renderer, and does not
    crash, using real files end to end.
    """

    def _environment(self, root: Path) -> dict:
        return {
            "CODEX_HOME": str(root / "codex-hooks"),
            "HEADROOM_STATE_DIR": str(root / "state"),
            "HEADROOM_CODEX_HOME": str(root / "codex"),
            "HEADROOM_CODEX_RPC": "0",
        }

    def test_json_burn_rate_projections_is_top_level_not_nested_in_readings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            _write_history(
                root / "state",
                _steady_climb_records(now) + _too_few_samples_records(now),
            )
            output = io.StringIO()
            with mock.patch.dict(os.environ, self._environment(root), clear=True):
                with redirect_stdout(output):
                    result = cli.main(["json"])

            self.assertEqual(result, 0)
            document = json.loads(output.getvalue())
            self.assertIn("burn_rate_projections", document)
            self.assertNotIn("burn_rate_projections", document.get("readings", {}))
            projections = {
                (p["source"], p["window"]): p for p in document["burn_rate_projections"]
            }
            self.assertIn(("claude", "short"), projections)
            self.assertIn(("codex", "weekly"), projections)

            declined = projections[("codex", "weekly")]
            self.assertEqual(declined["reason"], "too_few_samples")
            for field in (
                "max_relative_deviation",
                "max_usage_share",
                "intervals_used",
                "rate_drift",
                "effective_intervals",
                "zero_delta_fraction",
                "max_raw_rate_ratio",
                "longest_above_overall_rate_run",
                "projected_exhaustion_at",
                "exhaustion_precedes_reset",
            ):
                self.assertIsNone(declined[field])

            present = projections[("claude", "short")]
            self.assertIsNone(present["reason"])
            self.assertIs(present["exhaustion_precedes_reset"], True)
            for field in (
                "max_relative_deviation",
                "max_usage_share",
                "intervals_used",
                "rate_drift",
                "effective_intervals",
                "zero_delta_fraction",
                "max_raw_rate_ratio",
                "longest_above_overall_rate_run",
                "projected_exhaustion_at",
                "rate_percent_per_second",
            ):
                self.assertIsNotNone(present[field], field)

    def test_doctor_reports_measurements_and_plain_language_decline_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            _write_history(
                root / "state",
                _steady_climb_records(now) + _too_few_samples_records(now),
            )
            output = io.StringIO()
            with mock.patch.dict(os.environ, self._environment(root), clear=True):
                with redirect_stdout(output):
                    result = cli.main(["doctor"])

            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertIn("Burn rate\n", rendered)
            self.assertIn("not enough usage samples recorded yet", rendered)
            self.assertNotIn("too_few_samples", rendered)
            self.assertIn("exhaustion projected in", rendered)
            self.assertIn("before reset", rendered)
            self.assertIn("deviation", rendered)

    def test_status_shows_trustworthy_line_and_omits_untrustworthy_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            climb = _steady_climb_records(now, window="short")
            _write_history(root / "state", climb + _bursty_but_present_records(now, window="weekly"))
            # A fresh matching reading is required for status to speak about
            # the trustworthy projection (severity.burn_rate_evidence_is_current);
            # the bursty/weekly one stays silent on trust grounds alone, so
            # it needs no matching state entry.
            _write_state(root / "state", [_matching_state_snapshot(climb)])
            output = io.StringIO()
            with mock.patch.dict(os.environ, self._environment(root), clear=True):
                with redirect_stdout(output):
                    result = cli.main(["status"])

            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertIn("Burn rate\n", rendered)
            self.assertIn("Claude 5h burn rate: projected exhaustion", rendered)
            self.assertNotIn("7d burn rate", rendered)
            # The ordinary readings section still prints; burn rate does not
            # crowd it out.
            self.assertIn("Claude\n", rendered)
            self.assertIn("Codex\n", rendered)

    def test_hook_speaks_when_trustworthy_projection_precedes_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            climb = _steady_climb_records(now)
            _write_history(root / "state", climb)
            _write_state(root / "state", [_matching_state_snapshot(climb)])
            output = io.StringIO()
            with mock.patch.dict(os.environ, self._environment(root), clear=True):
                with mock.patch("sys.stdin", io.StringIO("")):
                    with redirect_stdout(output):
                        result = cli.main(["hook", "--plain"])

            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertIn("Burn rate:", rendered)
            self.assertIn("Claude 5h", rendered)

    def test_hook_critical_rate_limit_suppresses_burn_rate_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            state_dir = root / "state"
            _write_history(state_dir, _steady_climb_records(now))
            state_dir.mkdir(parents=True, exist_ok=True)
            state_document = {
                "version": 1,
                "sources": {
                    "codex": {
                        "weekly": {
                            "used_percentage": 95.0,
                            "captured_at": now,
                            "resets_at": now + 500_000.0,
                            "window": "weekly",
                            "source": "codex",
                            "limit_reached": False,
                            "raw": {},
                        }
                    }
                },
            }
            (state_dir / "state.json").write_text(json.dumps(state_document), encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, self._environment(root), clear=True):
                with mock.patch("sys.stdin", io.StringIO("")):
                    with redirect_stdout(output):
                        result = cli.main(["hook", "--plain"])

            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertIn("Usage headroom:", rendered)
            self.assertIn("Codex", rendered)
            self.assertNotIn("Burn rate:", rendered)

    def test_projection_includes_the_sample_captured_during_this_calls_refresh(self) -> None:
        # Regression test for Codex review round 1, P1: `now` must be
        # captured AFTER _refresh_codex runs, not before. A successful
        # refresh appends a new history record timestamped with its OWN
        # time.time() call (inside codexrpc.py), which can land after an
        # earlier `now`. project_exhaustion drops any record whose
        # captured_at exceeds the `now` it is given, so a premature `now`
        # would silently exclude the very snapshot this call just captured.
        #
        # Two pre-existing records are one short of MIN_SAMPLES (3); the
        # refresh mock appends a third, in-range record with a real,
        # currently-captured timestamp. Only a `now` captured after the
        # refresh is guaranteed to be >= that timestamp.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            anchor = time.time() - 10_000.0
            history_path = state_dir / "history.jsonl"
            history_path.write_text(
                "\n".join(
                    json.dumps(record, separators=(",", ":"))
                    for record in (
                        {
                            "captured_at": anchor,
                            "used_percentage": 10.0,
                            "resets_at": anchor + 50_000.0,
                            "source": "claude",
                            "window": "short",
                        },
                        {
                            "captured_at": anchor + 120.0,
                            "used_percentage": 20.0,
                            "resets_at": anchor + 50_000.0,
                            "source": "claude",
                            "window": "short",
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            def _append_third_record_during_refresh(deadline=None):
                with history_path.open("a", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "captured_at": time.time(),
                            "used_percentage": 30.0,
                            "resets_at": anchor + 50_000.0,
                            "source": "claude",
                            "window": "short",
                        },
                        handle,
                        separators=(",", ":"),
                    )
                    handle.write("\n")

            output = io.StringIO()
            with mock.patch.dict(os.environ, self._environment(root), clear=True):
                with mock.patch(
                    "headroom.cli._refresh_codex", side_effect=_append_third_record_during_refresh
                ):
                    with redirect_stdout(output):
                        result = cli.main(["json"])

            self.assertEqual(result, 0)
            document = json.loads(output.getvalue())
            projections = {
                (p["source"], p["window"]): p for p in document["burn_rate_projections"]
            }
            self.assertEqual(projections[("claude", "short")]["samples_used"], 3)

    def test_hook_exits_zero_with_malformed_state_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "state.json").write_text("{not json", encoding="utf-8")
            (state_dir / "history.jsonl").write_text("{not json\nalso not json\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, self._environment(root), clear=True):
                with mock.patch("sys.stdin", io.StringIO("")):
                    with redirect_stdout(output):
                        result = cli.main(["hook"])

            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue(), "")

    def test_statusline_output_is_unaffected_by_burn_rate_projections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            _write_history(root / "state", _steady_climb_records(now))
            output = io.StringIO()
            with mock.patch.dict(os.environ, self._environment(root), clear=True):
                with mock.patch("sys.stdin", io.StringIO("")):
                    with redirect_stdout(output):
                        result = cli.main(["statusline"])

            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertNotIn("Burn rate", rendered)
            self.assertNotIn("burn rate", rendered)
            self.assertTrue(rendered.startswith("headroom:"))


if __name__ == "__main__":
    unittest.main()
