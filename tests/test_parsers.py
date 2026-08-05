"""Tests for defensive Claude and Codex input parsing."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from headroom.bounds import Confidence, Snapshot, bound_snapshot
from headroom.claude import classify_window, parse_payload, parse_reset_time
from headroom.cli import main
from headroom.codexsrc import parse_rate_limits
from headroom.render import render_report
from headroom.state import save_snapshots


CLAUDE_STATUSLINE_PAYLOAD = {
    "rate_limits": {
        "five_hour": {"resets_at": 1785929400, "used_percentage": 20},
        "seven_day": {"resets_at": 1786266000, "used_percentage": 4},
    },
    "context_window": {
        "context_window_size": 1000000,
        "current_usage": {
            "cache_creation_input_tokens": 1100,
            "cache_read_input_tokens": 812517,
            "input_tokens": 2,
            "output_tokens": 1050,
        },
        "remaining_percentage": 19,
        "total_input_tokens": 813619,
        "total_output_tokens": 1050,
        "used_percentage": 81,
    },
}


class ParserTests(unittest.TestCase):
    def test_real_claude_statusline_payload_yields_only_rate_limit_windows(self) -> None:
        result = parse_payload(CLAUDE_STATUSLINE_PAYLOAD, captured_at=1_785_920_000.0)

        self.assertEqual(
            [(snapshot.window, snapshot.used_percentage) for snapshot in result.snapshots],
            [("short", 20.0), ("weekly", 4.0)],
        )
        self.assertNotIn(81.0, [snapshot.used_percentage for snapshot in result.snapshots])
        self.assertNotIn(19.0, [snapshot.used_percentage for snapshot in result.snapshots])
        self.assertFalse(
            any("context_window" in note.get("path", ()) for note in result.unparsed)
        )

    def test_context_window_is_excluded_even_if_it_has_a_duration(self) -> None:
        payload = {
            "context_window": {
                "used_percentage": 81,
                "remaining_percentage": 19,
                "window_minutes": 300,
            }
        }

        result = parse_payload(payload, captured_at=1_785_920_000.0)

        self.assertEqual(result.snapshots, ())
        self.assertNotIn("context_window", repr(result.unparsed))

    def test_existing_name_variants_still_classify(self) -> None:
        cases = (
            (("wrapper", "weekly"), "weekly"),
            (("wrapper", "7d"), "weekly"),
            (("wrapper", "fiveHour"), "short"),
            (("wrapper", "5h"), "short"),
        )
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(classify_window(None, path), expected)

    def test_duration_classification_wins_over_real_window_name(self) -> None:
        self.assertEqual(classify_window(10_080, ("five_hour",)), "weekly")
        self.assertEqual(classify_window(300, ("seven_day",)), "short")

    def test_genuinely_unknown_rate_limit_shape_stays_in_diagnostics(self) -> None:
        payload = {"rate_limits": {"mystery": {"used_percentage": 42}}}

        result = parse_payload(payload, captured_at=1_785_920_000.0)

        self.assertTrue(
            any(
                note.get("path") == ["rate_limits", "mystery"]
                and note.get("reason") == "unknown window"
                for note in result.unparsed
            )
        )

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

        result = parse_payload(payload, captured_at=1_786_400_000.0)
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

        snapshots = parse_rate_limits(payload, captured_at=1_786_490_000.0)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].window, "short")
        self.assertEqual(snapshots[0].used_percentage, 8.0)
        self.assertEqual(snapshots[0].resets_at, 1_786_494_688.0)

    def test_app_server_window_duration_mins_parses(self) -> None:
        payload = {
            "limitId": "codex",
            "primary": {
                "usedPercent": 4,
                "windowDurationMins": 10_080,
                "resetsAt": 1_786_494_688,
            },
            "secondary": None,
        }

        snapshots = parse_rate_limits(payload, captured_at=100.0)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].window, "weekly")
        self.assertEqual(snapshots[0].used_percentage, 4.0)

    def test_reset_time_accepts_epoch_seconds_milliseconds_and_iso(self) -> None:
        cases = (
            (1_786_494_688, 1_786_494_688.0),
            (1_786_494_688_000, 1_786_494_688.0),
            ("2026-08-12T00:00:00Z", 1_786_492_800.0),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(parse_reset_time(value), expected)

    def test_implausible_future_reset_keeps_usage_and_records_note(self) -> None:
        captured = 1_800_000_000.0
        notes: list[str] = []
        payload = {
            "rate_limits": {
                "primary": {
                    "used_percent": 12,
                    "window_minutes": 300,
                    "resets_at": 9_999_999_999,
                }
            }
        }

        snapshots = parse_rate_limits(payload, captured_at=captured, notes=notes)
        reading = bound_snapshot(
            snapshots[0], captured + 301, "codex", "short", fresh_for_seconds=300
        )
        report = render_report([reading], captured + 301)

        self.assertEqual(snapshots[0].used_percentage, 12.0)
        self.assertIsNone(snapshots[0].resets_at)
        self.assertEqual(reading.lower_bound_percent, 12.0)
        self.assertNotEqual(reading.confidence, Confidence.POST_RESET)
        self.assertIn("reset time unknown", report)
        self.assertNotIn("resets in", report)
        self.assertTrue(any("9999999999" in note for note in notes))

    def test_implausible_past_reset_is_not_treated_as_completed(self) -> None:
        captured = 1_800_000_000.0
        notes: list[str] = []
        payload = {
            "rate_limits": {
                "primary": {
                    "used_percent": 88,
                    "window_minutes": 300,
                    "resets_at": captured - 3_600,
                }
            }
        }

        snapshot = parse_rate_limits(payload, captured_at=captured, notes=notes)[0]
        reading = bound_snapshot(
            snapshot, captured + 301, "codex", "short", fresh_for_seconds=300
        )

        self.assertIsNone(snapshot.resets_at)
        self.assertEqual(reading.lower_bound_percent, 88.0)
        self.assertEqual(reading.confidence, Confidence.STALE_BOUNDED)
        self.assertTrue(any(str(int(captured - 3_600)) in note for note in notes))

    def test_normal_short_and_weekly_resets_remain_plausible(self) -> None:
        captured = 1_800_000_000.0
        payload = {
            "rate_limits": {
                "primary": {
                    "used_percent": 8,
                    "window_minutes": 300,
                    "resets_at": captured + 300 * 60,
                },
                "secondary": {
                    "used_percent": 18,
                    "window_minutes": 10_080,
                    "resets_at": captured + 10_080 * 60,
                },
            }
        }

        snapshots = {
            snapshot.window: snapshot
            for snapshot in parse_rate_limits(payload, captured)
        }

        self.assertEqual(snapshots["short"].resets_at, captured + 300 * 60)
        self.assertEqual(snapshots["weekly"].resets_at, captured + 10_080 * 60)

    def test_doctor_surfaces_stored_rejected_reset_note(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            save_snapshots(
                (
                    Snapshot(
                        used_percentage=12.0,
                        captured_at=1_800_000_000.0,
                        resets_at=9_999_999_999.0,
                        window="short",
                        source="claude",
                        raw={"window_minutes": 300},
                    ),
                ),
                state_dir,
            )
            environment = {
                "CODEX_HOME": str(Path(directory) / "codex-hooks"),
                "HEADROOM_STATE_DIR": directory,
                "HEADROOM_CODEX_HOME": str(Path(directory) / "codex"),
                "HEADROOM_CODEX_RPC": "0",
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["doctor"]), 0)

        self.assertIn(
            "rejected implausible resets_at 9999999999.0", output.getvalue()
        )

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
