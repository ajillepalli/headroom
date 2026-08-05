"""Tests for top-level command-line behavior."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import re
import unittest
from unittest import mock

import headroom
from headroom import cli


class CliTests(unittest.TestCase):
    def test_version_uses_installed_distribution_metadata(self) -> None:
        output = io.StringIO()

        with mock.patch("headroom.cli.metadata.version", return_value="2.3.4"):
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
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                cli.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertRegex(output.getvalue(), r"^headroom \d+\.\d+\.\d+(?:[^\s]*)?\n$")
        self.assertEqual(output.getvalue(), "headroom {}\n".format(headroom.__version__))

    def test_package_and_project_versions_match(self) -> None:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        project_section = pyproject.read_text(encoding="utf-8").split("[project]", 1)[1]
        project_section = project_section.split("\n[", 1)[0]
        match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project_section, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertEqual(headroom.__version__, match.group(1))


if __name__ == "__main__":
    unittest.main()
