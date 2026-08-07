"""Tests for conservative usage bounds and their presentation."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from quotagauge.bounds import Confidence, Snapshot, bound_snapshot
from quotagauge.freshness import freshness_seconds
from quotagauge.render import render_hook, render_report, render_statusline
from quotagauge.severity import Severity, reading_severity


class BoundsTests(unittest.TestCase):
    def test_legacy_implausible_reset_is_not_used(self) -> None:
        captured = 1_800_000_000.0
        snapshot = Snapshot(
            used_percentage=12.0,
            captured_at=captured,
            resets_at=9_999_999_999.0,
            window="short",
            source="codex",
            raw={"window_minutes": 300},
        )

        reading = bound_snapshot(
            snapshot,
            now=captured + 301,
            source="codex",
            window="short",
            fresh_for_seconds=300.0,
        )
        report = render_report([reading], now=captured + 301)

        self.assertEqual(reading.lower_bound_percent, 12.0)
        self.assertIsNone(reading.resets_at)
        self.assertNotEqual(reading.confidence, Confidence.POST_RESET)
        self.assertIn("reset time unknown", report)
        self.assertNotIn("resets in", report)

    def test_stale_usage_is_rendered_only_as_a_lower_bound(self) -> None:
        snapshot = Snapshot(
            used_percentage=65.0,
            captured_at=1_000.0,
            resets_at=10_000.0,
            window="short",
            source="claude",
        )

        reading = bound_snapshot(
            snapshot,
            now=1_301.0,
            source="claude",
            window="short",
            fresh_for_seconds=300.0,
        )

        self.assertFalse(reading.certain)
        self.assertEqual(reading.lower_bound_percent, 65.0)
        self.assertEqual(reading.confidence, Confidence.STALE_BOUNDED)
        statusline = render_statusline([reading], now=1_301.0)
        self.assertIn(">=65% used", statusline)
        self.assertNotIn("C 5h 65% used", statusline)

    def test_reading_after_reset_is_known_good(self) -> None:
        snapshot = Snapshot(
            used_percentage=99.0,
            captured_at=1_000.0,
            resets_at=1_100.0,
            window="weekly",
            source="codex",
            limit_reached=True,
        )

        reading = bound_snapshot(snapshot, now=1_101.0, source="codex", window="weekly")

        self.assertTrue(reading.certain)
        self.assertEqual(reading.lower_bound_percent, 0.0)
        self.assertEqual(reading.confidence, Confidence.POST_RESET)
        self.assertEqual(reading_severity(reading), Severity.OK)

    def test_stale_pre_reset_reading_escalates_one_level(self) -> None:
        snapshot = Snapshot(
            used_percentage=65.0,
            captured_at=1_000.0,
            resets_at=10_000.0,
            window="weekly",
            source="claude",
        )

        fresh = bound_snapshot(
            snapshot,
            now=1_030.0,
            source="claude",
            window="weekly",
            fresh_for_seconds=60.0,
        )
        stale = bound_snapshot(
            snapshot,
            now=1_061.0,
            source="claude",
            window="weekly",
            fresh_for_seconds=60.0,
        )

        self.assertEqual(reading_severity(fresh), Severity.NOTICE)
        self.assertEqual(reading_severity(stale), Severity.WARN)

    def test_high_headroom_stale_reading_stays_ok_and_hook_is_silent(self) -> None:
        snapshot = Snapshot(
            used_percentage=3.0,
            captured_at=1_000.0,
            resets_at=10_000.0,
            window="short",
            source="claude",
        )

        reading = bound_snapshot(
            snapshot,
            now=1_301.0,
            source="claude",
            window="short",
            fresh_for_seconds=300.0,
        )

        self.assertEqual(reading.confidence, Confidence.STALE_BOUNDED)
        self.assertEqual(reading_severity(reading), Severity.OK)
        self.assertEqual(render_hook([reading], now=1_301.0), "")

    def test_low_headroom_stale_reading_still_escalates(self) -> None:
        snapshot = Snapshot(
            used_percentage=65.0,
            captured_at=1_000.0,
            resets_at=10_000.0,
            window="weekly",
            source="claude",
        )

        reading = bound_snapshot(
            snapshot,
            now=1_301.0,
            source="claude",
            window="weekly",
            fresh_for_seconds=300.0,
        )

        self.assertEqual(reading_severity(reading), Severity.WARN)

    def test_reading_inside_source_freshness_window_is_fresh(self) -> None:
        snapshot = Snapshot(
            used_percentage=20.0,
            captured_at=1_000.0,
            resets_at=10_000.0,
            window="short",
            source="claude",
        )

        with patch.dict(os.environ, {}, clear=True):
            reading = bound_snapshot(
                snapshot, now=1_300.0, source="claude", window="short"
            )

        self.assertEqual(reading.confidence, Confidence.FRESH)
        self.assertTrue(reading.certain)

    def test_reading_outside_source_freshness_window_is_stale(self) -> None:
        snapshot = Snapshot(
            used_percentage=20.0,
            captured_at=1_000.0,
            resets_at=10_000.0,
            window="short",
            source="claude",
        )

        with patch.dict(os.environ, {}, clear=True):
            reading = bound_snapshot(
                snapshot, now=1_301.0, source="claude", window="short"
            )

        self.assertEqual(reading.confidence, Confidence.STALE_BOUNDED)
        self.assertFalse(reading.certain)

    def test_freshness_environment_overrides_are_honoured(self) -> None:
        overrides = {
            "QUOTAGAUGE_FRESH_CLAUDE_SECONDS": "42",
            "QUOTAGAUGE_FRESH_CODEX_SECONDS": "84",
        }

        with patch.dict(os.environ, overrides, clear=True):
            self.assertEqual(freshness_seconds("claude"), 42.0)
            self.assertEqual(freshness_seconds("codex"), 84.0)


if __name__ == "__main__":
    unittest.main()
