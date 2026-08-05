"""Tests for burn-rate fitting and exhaustion projections."""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
import time
import tracemalloc
from typing import Optional
import unittest
from unittest import mock

from headroom.burn_rate import (
    MAX_FIT_SAMPLES,
    MIN_INTERVAL_SECONDS,
    MIN_SPAN_TO_HORIZON_RATIO,
    MIN_SAMPLES,
    BurnRateProjection,
    NoProjectionReason,
    ProjectionConfidence,
    _HistoryRecord,
    _project_group,
    project_exhaustion,
)


class BurnRateTests(unittest.TestCase):
    def _project(
        self,
        records: list[dict[str, object]],
        malformed: str = "",
        now: Optional[float] = None,
    ) -> list[BurnRateProjection]:
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.jsonl"
            lines = [json.dumps(record, separators=(",", ":")) for record in records]
            if malformed:
                lines.append(malformed)
            history_path.write_text("\n".join(lines), encoding="utf-8")
            if now is None:
                # Default "now" to just after the last capture, i.e. "we just
                # captured this". Tests that need to exercise staleness pass
                # an explicit now instead. A fixed, far-future default would
                # make every window look already reset once resets_at started
                # being checked against now.
                captured_times = [record["captured_at"] for record in records]
                now = (max(captured_times) if captured_times else 0.0) + 1.0
            return project_exhaustion(history_path, now=now)

    @staticmethod
    def _record(
        captured_at: float,
        used_percentage: float,
        resets_at: Optional[float] = 1_000.0,
        source: str = "claude",
        window: str = "short",
    ) -> dict[str, object]:
        return {
            "captured_at": captured_at,
            "used_percentage": used_percentage,
            "resets_at": resets_at,
            "source": source,
            "window": window,
        }

    def test_flat_usage_yields_no_projection_and_reason(self) -> None:
        projection = self._project(
            [
                self._record(0.0, 40.0),
                self._record(60.0, 40.0),
                self._record(120.0, 40.0),
            ]
        )[0]

        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertIsNone(projection.exhaustion_precedes_reset)
        self.assertEqual(projection.reason, NoProjectionReason.FLAT_USAGE)

    def test_real_weekly_shape_is_too_short_for_projection_horizon(self) -> None:
        projection = self._project(
            [
                self._record(0.0, 10.0, resets_at=604_800.0, window="weekly"),
                self._record(1_890.0, 11.5, resets_at=604_800.0, window="weekly"),
                self._record(3_780.0, 12.6, resets_at=604_800.0, window="weekly"),
                self._record(5_670.0, 13.9, resets_at=604_800.0, window="weekly"),
                self._record(7_560.0, 15.2, resets_at=604_800.0, window="weekly"),
            ]
        )[0]

        self.assertEqual(projection.span_seconds, 2.1 * 60.0 * 60.0)
        self.assertIsNotNone(projection.rate_percent_per_second)
        horizon_seconds = (100.0 - 15.2) / (
            projection.rate_percent_per_second or 1.0
        )
        self.assertAlmostEqual(horizon_seconds / 86_400.0, 1.43, delta=0.1)
        self.assertGreater(horizon_seconds / projection.span_seconds, 10.0)
        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertIsNone(projection.exhaustion_precedes_reset)
        self.assertEqual(
            projection.reason,
            NoProjectionReason.INSUFFICIENT_SPAN_FOR_HORIZON,
        )

    def test_short_horizon_projects_from_two_hours_of_evidence(self) -> None:
        projection = self._project(
            [
                self._record(0.0, 20.0, resets_at=18_000.0, window="five_hour"),
                self._record(3_600.0, 50.0, resets_at=18_000.0, window="five_hour"),
                self._record(7_200.0, 80.0, resets_at=18_000.0, window="five_hour"),
            ]
        )[0]

        self.assertEqual(MIN_SPAN_TO_HORIZON_RATIO, 0.1)
        self.assertAlmostEqual(projection.projected_exhaustion_at or 0.0, 9_600.0)
        self.assertTrue(projection.exhaustion_precedes_reset)
        self.assertIsNone(projection.reason)

    def test_weekly_projection_with_several_days_of_evidence(self) -> None:
        projection = self._project(
            [
                self._record(0.0, 20.0, resets_at=604_800.0, window="weekly"),
                self._record(172_800.0, 45.0, resets_at=604_800.0, window="weekly"),
                self._record(345_600.0, 70.0, resets_at=604_800.0, window="weekly"),
            ]
        )[0]

        self.assertAlmostEqual(
            projection.projected_exhaustion_at or 0.0,
            552_960.0,
        )
        self.assertTrue(projection.exhaustion_precedes_reset)
        self.assertEqual(projection.confidence, ProjectionConfidence.HIGH)
        self.assertIsNone(projection.reason)

    def test_low_confidence_never_emits_exhaustion_boolean(self) -> None:
        # Monotonic (no decreases, so segmentation leaves all 5 samples in
        # play) but with wildly uneven per-interval rates: +20, +2, +23, +2.
        # That inconsistency, not the old sign-based check, is what must
        # drive LOW confidence here.
        projection = self._project(
            [
                self._record(0.0, 10.0, resets_at=20_000.0),
                self._record(1_800.0, 30.0, resets_at=20_000.0),
                self._record(3_600.0, 32.0, resets_at=20_000.0),
                self._record(5_400.0, 55.0, resets_at=20_000.0),
                self._record(7_200.0, 57.0, resets_at=20_000.0),
            ]
        )[0]

        self.assertEqual(projection.samples_used, 5)
        self.assertEqual(projection.confidence, ProjectionConfidence.LOW)
        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertIsNone(projection.exhaustion_precedes_reset)
        self.assertEqual(projection.reason, NoProjectionReason.LOW_CONFIDENCE)

    def test_usage_went_backwards_is_a_defensive_invariant(self) -> None:
        # project_exhaustion can no longer reach _project_group with a
        # decreasing segment: _records_since_latest_reset treats any
        # decrease as a reset boundary and strips it out first (see the
        # unmarked-reset test below). This calls the private _project_group
        # directly to prove the invariant check inside it still holds if it
        # is ever invoked with unsegmented data.
        records = [
            _HistoryRecord(0.0, 1_000.0, "claude", "short", 40.0),
            _HistoryRecord(60.0, 1_000.0, "claude", "short", 39.0),
            _HistoryRecord(120.0, 1_000.0, "claude", "short", 38.0),
        ]

        projection = _project_group(("claude", "short"), records, now=121.0)

        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertEqual(
            projection.reason, NoProjectionReason.USAGE_WENT_BACKWARDS
        )

    def test_already_exhausted_usage_yields_no_projection(self) -> None:
        projection = self._project(
            [
                self._record(0.0, 80.0),
                self._record(60.0, 90.0),
                self._record(120.0, 100.0),
            ]
        )[0]

        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertEqual(projection.reason, NoProjectionReason.ALREADY_EXHAUSTED)

    def test_steep_ramp_exhausts_before_reset(self) -> None:
        projection = self._project(
            [
                self._record(0.0, 20.0),
                self._record(60.0, 40.0),
                self._record(120.0, 60.0),
            ]
        )[0]

        self.assertAlmostEqual(projection.rate_percent_per_second or 0.0, 1.0 / 3.0)
        self.assertAlmostEqual(projection.projected_exhaustion_at or 0.0, 240.0)
        self.assertTrue(projection.exhaustion_precedes_reset)

    def test_gentle_ramp_exhausts_after_reset(self) -> None:
        projection = self._project(
            [
                self._record(0.0, 20.0, resets_at=4_000.0),
                self._record(1_800.0, 50.0, resets_at=4_000.0),
                self._record(3_600.0, 80.0, resets_at=4_000.0),
            ]
        )[0]

        self.assertAlmostEqual(
            projection.projected_exhaustion_at or 0.0, 4_800.0
        )
        self.assertFalse(projection.exhaustion_precedes_reset)

    def test_only_records_since_latest_reset_are_fitted(self) -> None:
        projection = self._project(
            [
                self._record(0.0, 95.0, resets_at=1_000.0),
                self._record(60.0, 99.0, resets_at=1_000.0),
                self._record(120.0, 1.0, resets_at=5_000.0),
                self._record(1_920.0, 31.0, resets_at=5_000.0),
                self._record(3_720.0, 61.0, resets_at=5_000.0),
            ]
        )[0]

        self.assertEqual(projection.samples_used, 3)
        self.assertEqual(projection.span_seconds, 3_600.0)
        self.assertAlmostEqual(
            projection.rate_percent_per_second or 0.0, 1.0 / 60.0
        )
        self.assertIsNotNone(projection.projected_exhaustion_at)
        self.assertFalse(projection.exhaustion_precedes_reset)

    def test_too_few_samples_has_distinct_reason(self) -> None:
        projection = self._project(
            [self._record(0.0, 10.0), self._record(120.0, 20.0)]
        )[0]

        self.assertEqual(projection.samples_used, MIN_SAMPLES - 1)
        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertEqual(projection.reason, NoProjectionReason.TOO_FEW_SAMPLES)

    def test_too_short_span_has_distinct_reason(self) -> None:
        projection = self._project(
            [self._record(0.0, 10.0), self._record(10.0, 20.0), self._record(20.0, 30.0)]
        )[0]

        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertEqual(projection.reason, NoProjectionReason.SPAN_TOO_SHORT)

    def test_flat_usage_and_insufficient_horizon_have_distinct_reasons(self) -> None:
        flat_projection = self._project(
            [
                self._record(0.0, 40.0),
                self._record(60.0, 40.0),
                self._record(120.0, 40.0),
            ]
        )[0]
        short_projection = self._project(
            [
                self._record(0.0, 10.0),
                self._record(60.0, 11.0),
                self._record(120.0, 12.0),
            ]
        )[0]

        self.assertEqual(flat_projection.reason, NoProjectionReason.FLAT_USAGE)
        self.assertEqual(
            short_projection.reason,
            NoProjectionReason.INSUFFICIENT_SPAN_FOR_HORIZON,
        )
        self.assertNotEqual(flat_projection.reason, short_projection.reason)

    def test_usage_drop_segments_out_the_stale_window(self) -> None:
        # (Codex P1) Reset detection used to rely solely on resets_at
        # changing. A 95% -> 40% drop with resets_at unchanged is itself
        # proof of an unmarked reset: usage is monotonic within a window, so
        # a decrease can only mean the window turned over. Only (180, 40)
        # and (240, 50) are evidence about the CURRENT window; the earlier
        # climb to 95% belongs to a window that is already gone and must not
        # donate span or samples to the current fit.
        projection = self._project(
            [
                self._record(0.0, 10.0, resets_at=1_000.0),
                self._record(60.0, 20.0, resets_at=1_000.0),
                self._record(120.0, 95.0, resets_at=1_000.0),
                self._record(180.0, 40.0, resets_at=1_000.0),
                self._record(240.0, 50.0, resets_at=1_000.0),
            ]
        )[0]

        self.assertEqual(projection.samples_used, 2)
        self.assertEqual(projection.span_seconds, 60.0)
        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertEqual(projection.reason, NoProjectionReason.TOO_FEW_SAMPLES)

    def test_interleaved_sources_and_windows_are_never_mixed(self) -> None:
        records = []
        for captured_at, usage in ((0.0, 10.0), (60.0, 20.0), (120.0, 30.0)):
            records.append(self._record(captured_at, usage, source="claude", window="short"))
            records.append(self._record(captured_at, usage / 10.0, source="codex", window="short"))
            records.append(self._record(captured_at, usage / 5.0, source="claude", window="weekly"))

        projections = self._project(records)
        by_group = {(item.source, item.window): item for item in projections}

        self.assertEqual(
            set(by_group),
            {("claude", "short"), ("claude", "weekly"), ("codex", "short")},
        )
        self.assertAlmostEqual(
            by_group[("claude", "short")].rate_percent_per_second or 0.0,
            1.0 / 6.0,
        )
        self.assertAlmostEqual(
            by_group[("claude", "weekly")].rate_percent_per_second or 0.0,
            1.0 / 30.0,
        )
        self.assertAlmostEqual(
            by_group[("codex", "short")].rate_percent_per_second or 0.0,
            1.0 / 60.0,
        )

    def test_malformed_or_truncated_line_is_skipped(self) -> None:
        projections = self._project(
            [
                self._record(0.0, 10.0),
                self._record(60.0, 20.0),
                self._record(120.0, 30.0),
            ],
            malformed='{"captured_at":180,"used_percentage":',
        )

        self.assertEqual(len(projections), 1)
        self.assertEqual(projections[0].samples_used, 3)
        self.assertIsNotNone(projections[0].projected_exhaustion_at)

    def test_empty_or_missing_history_yields_no_projections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.jsonl"
            history_path.write_text("", encoding="utf-8")
            self.assertEqual(project_exhaustion(history_path, now=10_000.0), [])

            history_path.unlink()
            self.assertEqual(project_exhaustion(history_path, now=10_000.0), [])

    # -- Regression tests for the Codex adversarial review findings --------

    def test_unmarked_reset_without_resets_at_is_still_detected(self) -> None:
        # (Codex P1, burn_rate.py:150) resets_at is optional and here is
        # always None, so the only signal that a reset happened is the
        # 90% -> 2% drop at t=3594. The pre-fix code let the previous
        # window's climb to 90% donate a fake 3600-second span to the
        # current window; the real current-window evidence spans only the
        # 6 seconds from t=3594 to t=3600 and must fail MIN_SPAN_SECONDS.
        records = [
            self._record(0.0, 0.0, resets_at=None),
            self._record(3_000.0, 90.0, resets_at=None),
            self._record(3_594.0, 2.0, resets_at=None),
            self._record(3_595.0, 16.0, resets_at=None),
            self._record(3_596.0, 30.0, resets_at=None),
            self._record(3_597.0, 44.0, resets_at=None),
            self._record(3_598.0, 58.0, resets_at=None),
            self._record(3_599.0, 72.0, resets_at=None),
            self._record(3_600.0, 86.0, resets_at=None),
        ]

        projection = self._project(records, now=3_601.0)[0]

        self.assertEqual(projection.samples_used, 7)
        self.assertEqual(projection.span_seconds, 6.0)
        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertEqual(projection.reason, NoProjectionReason.SPAN_TOO_SHORT)
        self.assertEqual(projection.confidence, ProjectionConfidence.NONE)

    def test_completed_window_relative_to_now_is_rejected(self) -> None:
        # (Codex P1, burn_rate.py:85) The window reset at t=1000 and "now"
        # is 2000: the reset already happened long ago, so the 60% reading
        # says nothing about the window in effect now. bounds.bound_snapshot
        # treats a passed resets_at as POST_RESET; burn_rate must reach the
        # same conclusion instead of confidently projecting through it.
        projection = self._project(
            [
                self._record(0.0, 20.0, resets_at=1_000.0),
                self._record(60.0, 40.0, resets_at=1_000.0),
                self._record(120.0, 60.0, resets_at=1_000.0),
            ],
            now=2_000.0,
        )[0]

        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertIsNone(projection.exhaustion_precedes_reset)
        self.assertEqual(
            projection.reason, NoProjectionReason.WINDOW_ALREADY_RESET
        )

    def test_no_projection_is_ever_in_the_past(self) -> None:
        # Direct check of the general safety net: even outside the
        # resets_at-specific case above, project_exhaustion must never hand
        # back a projected_exhaustion_at that already lies in the past.
        projections = self._project(
            [
                self._record(0.0, 20.0, resets_at=1_000.0),
                self._record(60.0, 40.0, resets_at=1_000.0),
                self._record(120.0, 60.0, resets_at=1_000.0),
            ],
            now=2_000.0,
        )
        for projection in projections:
            if projection.projected_exhaustion_at is not None:
                self.assertGreaterEqual(projection.projected_exhaustion_at, 2_000.0)

    def test_single_terminal_burst_after_quiet_history_is_not_high_confidence(
        self,
    ) -> None:
        # (Codex P1, burn_rate.py:253) All the acceleration comes from one
        # 1-second burst (7200 -> 7201) after two hours of near-flat usage.
        # The old sign-based confidence check saw 6 of 6 positive pairwise
        # slopes and called that HIGH. A single independent interval out of
        # three cannot defend HIGH confidence.
        projection = self._project(
            [
                self._record(0.0, 0.0, resets_at=None),
                self._record(3_600.0, 0.1, resets_at=None),
                self._record(7_200.0, 0.2, resets_at=None),
                self._record(7_201.0, 99.0, resets_at=None),
            ],
            now=7_202.0,
        )[0]

        self.assertNotEqual(projection.confidence, ProjectionConfidence.HIGH)
        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertEqual(projection.reason, NoProjectionReason.LOW_CONFIDENCE)

    def test_ten_thousand_reading_segment_is_bounded_in_time_and_memory(
        self,
    ) -> None:
        # (Codex P1, burn_rate.py:239) Exact Theil-Sen over an unbounded
        # segment is O(n^2) in time and memory: 10,000 readings would build
        # ~50 million float pairs. history.jsonl is append-only and a
        # segment can grow without bound absent a marked reset, so this must
        # stay fast and small regardless of history length.
        records = [
            self._record(float(index) * 10.0, index * 0.005, resets_at=None)
            for index in range(10_000)
        ]

        tracemalloc.start()
        started = time.perf_counter()
        projection = self._project(records, now=100_000.0)[0]
        elapsed = time.perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(projection.samples_used, MAX_FIT_SAMPLES)
        self.assertLess(elapsed, 2.0, "capped fit must stay fast regardless of history length")
        self.assertLess(
            peak_bytes,
            50 * 1024 * 1024,
            "capped fit must stay well under the pre-fix multi-GB peak",
        )

    def test_near_zero_elapsed_intervals_never_produce_non_finite_results(
        self,
    ) -> None:
        # (Codex P2, burn_rate.py:248) Denormal timestamps a few
        # femtoseconds apart made (delta_usage / elapsed) overflow to inf
        # even though the 60-second global span guard was satisfied. Every
        # degenerate interval must be filtered before it can reach a
        # division, and every returned number must be finite.
        projection = self._project(
            [
                self._record(0.0, 0.0, resets_at=None),
                self._record(5e-324, 20.0, resets_at=None),
                self._record(1e-323, 40.0, resets_at=None),
                self._record(60.0, 60.0, resets_at=None),
            ],
            now=61.0,
        )[0]

        if projection.rate_percent_per_second is not None:
            self.assertTrue(math.isfinite(projection.rate_percent_per_second))
        if projection.projected_exhaustion_at is not None:
            self.assertTrue(math.isfinite(projection.projected_exhaustion_at))
        self.assertNotEqual(
            projection.rate_percent_per_second, float("inf")
        )

    def test_unreadable_history_file_yields_no_projections(self) -> None:
        # (Codex P2, burn_rate.py:82) Only FileNotFoundError/IsADirectoryError
        # were handled; a PermissionError (or any other OSError) escaped and
        # crashed the caller. state.read_state treats any read failure as
        # "no state"; burn_rate must match that.
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.jsonl"
            history_path.write_text(
                json.dumps(self._record(0.0, 10.0)), encoding="utf-8"
            )
            with mock.patch(
                "headroom.burn_rate.Path.open",
                side_effect=PermissionError("denied"),
            ):
                self.assertEqual(
                    project_exhaustion(history_path, now=1.0), []
                )

    def test_degenerate_interval_threshold_is_positive(self) -> None:
        # Sanity check that the constant used to gate out near-zero-elapsed
        # divisions is itself a small positive number, not accidentally 0
        # (which would defeat the guard) or negative.
        self.assertGreater(MIN_INTERVAL_SECONDS, 0.0)
        self.assertLess(MIN_INTERVAL_SECONDS, 1.0)


if __name__ == "__main__":
    unittest.main()
