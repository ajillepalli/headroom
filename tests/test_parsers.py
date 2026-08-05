"""Tests for defensive Claude and Codex input parsing."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from headroom.claude import parse_payload, parse_reset_time
from headroom.cli import main
from headroom.codexsrc import parse_rate_limits


class ParserTests(unittest.TestCase):
    def test_null_codex_secondary_yields_only_weekly_window(self) -> None:
        payload = {
            "rate_limits": {
                "limit_id": "codex",
                "primary": {
                    "used_percent": 3.0,
                    "window_minutes": 10_080,
                    "resets_at": 1_786_494_688,
                },
                "secondary": None,
            }
        }

        snapshots = parse_rate_limits(payload, captured_at=100.0)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].window, "weekly")
        self.assertEqual(snapshots[0].used_percentage, 3.0)

    def test_snake_case_and_camel_case_claude_fields_parse(self) -> None:
        payload = {
            "wrapper": {
                "five_hour": {
                    "used_percentage": 12.0,
                    "resets_at": 1_786_494_688,
                },
                "sevenDay": {
                    "usedPercentage": 34.0,
                    "windowMinutes": 10_080,
                    "resetsAt": "2026-08-12T00:00:00Z",
                },
            }
        }

        result = parse_payload(payload, captured_at=100.0)
        snapshots = {snapshot.window: snapshot for snapshot in result.snapshots}

        self.assertEqual(set(snapshots), {"short", "weekly"})
        self.assertEqual(snapshots["short"].used_percentage, 12.0)
        self.assertEqual(snapshots["weekly"].used_percentage, 34.0)
        self.assertIsNotNone(snapshots["weekly"].resets_at)

    def test_camel_case_codex_bucket_fields_parse(self) -> None:
        payload = {
            "rate_limits": {
                "primary": {
                    "usedPercentage": 8,
                    "windowMinutes": 300,
                    "resetsAt": 1_786_494_688_000,
                }
            }
        }

        snapshots = parse_rate_limits(payload, captured_at=100.0)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].window, "short")
        self.assertEqual(snapshots[0].used_percentage, 8.0)
        self.assertEqual(snapshots[0].resets_at, 1_786_494_688.0)

    def test_reset_time_accepts_epoch_seconds_milliseconds_and_iso(self) -> None:
        cases = (
            (1_786_494_688, 1_786_494_688.0),
            (1_786_494_688_000, 1_786_494_688.0),
            ("2026-08-12T00:00:00Z", 1_786_492_800.0),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(parse_reset_time(value), expected)

    def test_malformed_or_empty_stdin_still_prints_and_exits_zero(self) -> None:
        for payload in ("", "{"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                output = StringIO()
                environment = {"HEADROOM_STATE_DIR": directory}
                with mock.patch.dict(os.environ, environment, clear=False):
                    with mock.patch("sys.stdin", StringIO(payload)):
                        with redirect_stdout(output):
                            result = main(["statusline"])

                self.assertEqual(result, 0)
                self.assertTrue(output.getvalue().strip())
                self.assertTrue((Path(directory) / "state.json").is_file())


if __name__ == "__main__":
    unittest.main()
