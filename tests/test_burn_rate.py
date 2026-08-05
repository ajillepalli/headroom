"""Tests for burn-rate fitting and exhaustion projections."""

from __future__ import annotations

import json
import math
from pathlib import Path
import statistics
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
    _HistoryRecord,
    _RateUnderflowedToZero,
    _folded_intervals,
    _interval_measurements,
    _pairwise_slopes,
    _project_group,
    _raw_intervals,
    _relative_difference,
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

    def _assertClose(
        self,
        actual: Optional[float],
        expected: float,
        rel_tol: float = 1e-9,
    ) -> None:
        # A tight relative tolerance (not a loose ">"/"<" assertion) so a
        # mutation to the underlying computation is caught rather than
        # absorbed -- mutation audits on this file's old tier-based tests
        # found that loose assertions ("large enough", "not HIGH") survived
        # mutated cutoffs across three separate review rounds.
        self.assertIsNotNone(actual)
        assert actual is not None  # narrows for mypy/readers
        self.assertTrue(
            math.isclose(actual, expected, rel_tol=rel_tol, abs_tol=1e-9),
            f"{actual!r} not close to {expected!r}",
        )

    def _assertNoMeasurements(self, projection: BurnRateProjection) -> None:
        self.assertIsNone(projection.max_relative_deviation)
        self.assertIsNone(projection.max_usage_share)
        self.assertIsNone(projection.intervals_used)
        self.assertIsNone(projection.rate_drift)
        self.assertIsNone(projection.effective_intervals)
        self.assertIsNone(projection.zero_delta_fraction)
        self.assertIsNone(projection.max_raw_rate_ratio)

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
        self._assertNoMeasurements(projection)

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
        # INSUFFICIENT_SPAN_FOR_HORIZON declines the projection, so it
        # carries no measurements either -- there is nothing for a caller
        # to judge the steadiness of when there is no projected exhaustion.
        self._assertNoMeasurements(projection)

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
        # Two equal 3,600s intervals, each climbing 30 points: a perfectly
        # steady rate with no dilution.
        self._assertClose(projection.max_relative_deviation, 0.0)
        self._assertClose(projection.max_usage_share, 0.5)
        self.assertEqual(projection.intervals_used, 2)
        self._assertClose(projection.rate_drift, 0.0)
        self._assertClose(projection.effective_intervals, 2.0)

    def test_weekly_projection_with_several_days_of_evidence(self) -> None:
        # Four points (three consecutive intervals, all at the same 25%-per-
        # 172800s rate): a clean, evenly-spread steady series.
        projection = self._project(
            [
                self._record(0.0, 20.0, resets_at=604_800.0, window="weekly"),
                self._record(172_800.0, 45.0, resets_at=604_800.0, window="weekly"),
                self._record(345_600.0, 70.0, resets_at=604_800.0, window="weekly"),
                self._record(518_400.0, 95.0, resets_at=604_800.0, window="weekly"),
            ]
        )[0]

        self.assertAlmostEqual(
            projection.projected_exhaustion_at or 0.0,
            552_960.0,
        )
        self.assertTrue(projection.exhaustion_precedes_reset)
        self.assertIsNone(projection.reason)
        self._assertClose(projection.max_relative_deviation, 0.0)
        self._assertClose(projection.max_usage_share, 1.0 / 3.0)
        self.assertEqual(projection.intervals_used, 3)
        self._assertClose(projection.rate_drift, 0.0)
        self._assertClose(projection.effective_intervals, 3.0)

    def test_uneven_intervals_still_project_and_report_the_disagreement(
        self,
    ) -> None:
        # Monotonic (no decreases, so segmentation leaves all 5 samples in
        # play) but with wildly uneven per-interval rates: +20, +2, +23, +2
        # over 1,800s intervals. Confidence tiering used to veto this
        # outright (LOW_CONFIDENCE); the continuous library has no veto, so
        # it must still project, while reporting numbers that make the
        # disagreement plain to a caller.
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
        self.assertIsNone(projection.reason)
        self.assertIsNotNone(projection.projected_exhaustion_at)
        # No raw delta here is zero, so folding is a no-op: intervals_used is
        # the raw consecutive-interval count.
        self.assertEqual(projection.intervals_used, 4)
        self._assertClose(projection.max_relative_deviation, 1.0909090909090908)
        self._assertClose(projection.max_usage_share, 0.48936170212765956)
        self._assertClose(projection.rate_drift, 0.13636363636363624)
        self._assertClose(projection.effective_intervals, 2.3575240128068304)

    # -- Regression tests for the second-round Codex P2 confidence review --
    # (kept as the historical record of what this metric must expose, now
    # asserting the measured values rather than a since-deleted tier)

    def test_first_interval_carrying_half_the_quota(self) -> None:
        # (Codex P2, round 2) 500 one-second readings: the first interval
        # alone jumps usage from 0 to 50 (half the entire quota in one
        # interval), then 498 more intervals each add a steady +0.1.
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        usage = 50.0
        for index in range(1, 500):
            records.append(
                _HistoryRecord(float(index), None, "claude", "short", usage)
            )
            usage += 0.1

        measurements = _interval_measurements(records)

        assert measurements is not None
        # The dominant interval runs at 500x the 0.1 median rate...
        self._assertClose(measurements.max_relative_deviation, 499.0000000000284)
        # ...and alone supplies just over half the segment's total usage.
        self._assertClose(measurements.max_usage_share, 0.5010020040080253)
        self.assertEqual(measurements.intervals_used, 499)
        # Effective intervals collapses from 499 raw intervals to ~4: nearly
        # all the "evidence" is really just this one interval.
        self._assertClose(measurements.effective_intervals, 3.976095617529735)

    def test_three_intervals_with_one_early_deviation(self) -> None:
        # (Codex P2, round 2) Exactly three intervals with rates [2, 1, 1]:
        # the first interval disagrees with the other two by 100%.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(1.0, None, "claude", "short", 2.0),
            _HistoryRecord(2.0, None, "claude", "short", 3.0),
            _HistoryRecord(3.0, None, "claude", "short", 4.0),
        ]

        measurements = _interval_measurements(records)

        assert measurements is not None
        self._assertClose(measurements.max_relative_deviation, 1.0)
        self._assertClose(measurements.max_usage_share, 0.5)
        self.assertEqual(measurements.intervals_used, 3)

    def test_two_intervals_disagreeing_by_fifty_percent(self) -> None:
        # (Codex P2, round 2) The original fixture: two intervals at rates
        # [1.0, 1.5]. With confidence tiering removed there is no longer a
        # "too few intervals" veto to isolate -- intervals_used is reported
        # plainly instead, and the deviation between the two rates (20% off
        # their 1.25 mean... but 20% off the 1.0 MEDIAN, since Theil-Sen-
        # style measures here use the median of just two values, which is
        # their average) is what a caller sees.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(1.0, None, "claude", "short", 1.0),
            _HistoryRecord(2.0, None, "claude", "short", 2.5),
        ]

        measurements = _interval_measurements(records)

        assert measurements is not None
        self.assertEqual(measurements.intervals_used, 2)
        self._assertClose(measurements.max_relative_deviation, 0.2)
        self._assertClose(measurements.max_usage_share, 0.6)
        self._assertClose(measurements.rate_drift, 0.5)
        self._assertClose(measurements.effective_intervals, 1.9230769230769231)

    # -- Regression tests for the third-round Codex P2 confidence review --

    def test_dominant_interval_diluted_across_many_samples(self) -> None:
        # (Codex P2, round 3) 500 one-second intervals: the first interval
        # alone accounts for 20 of the segment's ~99.68 total usage change;
        # the other 498 intervals each add a steady +0.16. Any MEAN-based
        # dispersion statistic -- however weighted -- can be diluted toward
        # zero by growing the sample count around one outlier; a MAXIMUM
        # cannot. This is the fixture that proved it: the single worst
        # interval (rate 20.0) against the 0.16 median is what the old means
        # could not see, and what max_relative_deviation catches directly.
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        usage = 20.0
        records.append(_HistoryRecord(1.0, None, "claude", "short", usage))
        for index in range(2, 500):
            usage += 0.16
            records.append(
                _HistoryRecord(float(index), None, "claude", "short", usage)
            )
        self.assertEqual(len(records), 500)

        measurements = _interval_measurements(records)

        assert measurements is not None
        # Over 100x the median rate -- about 12,400% relative deviation.
        self._assertClose(measurements.max_relative_deviation, 124.00000000000267)
        # But its usage SHARE stays modest (~20%): proof the deviation cap
        # catches something the share cap does not, and vice versa (see the
        # round-4 fixture below).
        self._assertClose(measurements.max_usage_share, 0.2006420545746417)
        self._assertClose(measurements.effective_intervals, 24.073001302486468)

    def test_genuinely_steady_series_measures_small(self) -> None:
        # The measurements above must not saturate on ALL data -- if nothing
        # can ever measure small, the metric is useless, which would be a
        # worse outcome than the instability it exists to expose. 100
        # intervals, each within 3% of a 1.0 %/s baseline (a fixed
        # alternating +/-3% pattern, not real randomness, so this can never
        # flake).
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        usage = 0.0
        for index in range(1, 101):
            noise = 0.03 if index % 2 == 0 else -0.03
            usage += 1.0 + noise
            records.append(
                _HistoryRecord(float(index), None, "claude", "short", usage)
            )

        measurements = _interval_measurements(records)

        assert measurements is not None
        self._assertClose(measurements.max_relative_deviation, 0.030000000000001137)
        self._assertClose(measurements.max_usage_share, 0.01030000000000001)
        self.assertEqual(measurements.intervals_used, 100)
        self._assertClose(measurements.rate_drift, 0.0)
        self._assertClose(measurements.effective_intervals, 99.91008092716555)

    # -- Regression tests for the fourth-round Codex P2 confidence review --

    def test_long_interval_dominant_by_share_not_by_ratio(self) -> None:
        # (Codex P2, round 4) The concrete fixture from that review: a
        # 1000-second interval running at 39.9/1000 = 0.0399 %/s, followed
        # by twelve 60-second intervals each adding 0.6 (a 0.01 %/s rate).
        # The first interval's rate is 3.99x the 0.01 median -- comfortably
        # inside what used to be a 4.0x ratio cap -- but because it is
        # 1000 seconds long against 60-second neighbors, it alone supplies
        # 39.9 of the segment's 47.1 total usage change (84.7%). Rate ratio
        # and usage share are different quantities, which is exactly why
        # both are reported rather than collapsed into one pass/fail cap.
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        records.append(_HistoryRecord(1000.0, None, "claude", "short", 39.9))
        for step in range(1, 13):
            records.append(
                _HistoryRecord(
                    1000.0 + 60.0 * step,
                    None,
                    "claude",
                    "short",
                    39.9 + 0.6 * step,
                )
            )

        measurements = _interval_measurements(records)

        assert measurements is not None
        self._assertClose(measurements.max_relative_deviation, 2.98999999999999)
        self._assertClose(measurements.max_usage_share, 0.8471337579617835)
        self.assertEqual(measurements.intervals_used, 13)
        # Effective intervals near 1: nearly all the evidence is really one
        # interval's worth, despite 13 raw intervals existing.
        self._assertClose(measurements.effective_intervals, 1.3896938602920446)

    def test_share_is_independent_of_deviation(self) -> None:
        # Isolates max_usage_share from max_relative_deviation: three
        # intervals with rates [1.2, 1.0, 1.0]. The first interval's
        # deviation from the 1.0 median is only 20%, but that same interval
        # is 1000 seconds long against 1-second neighbors, so it alone
        # supplies 1200 of the segment's 1202 total usage change (99.8%).
        # Deviation alone would call this unremarkable; share alone flags
        # it -- proof the two fields carry different information.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(1000.0, None, "claude", "short", 1200.0),
            _HistoryRecord(1001.0, None, "claude", "short", 1201.0),
            _HistoryRecord(1002.0, None, "claude", "short", 1202.0),
        ]

        measurements = _interval_measurements(records)

        assert measurements is not None
        self._assertClose(measurements.max_relative_deviation, 0.19999999999999996)
        self._assertClose(measurements.max_usage_share, 0.9983361064891847)
        self._assertClose(measurements.effective_intervals, 1.00333471759067)

    def test_steady_rate_sampled_at_uneven_intervals_measures_small(self) -> None:
        # Realistic shape #1: captures do not arrive on a fixed clock (the
        # gap between two readings depends on when headroom happened to
        # run), so a genuinely steady 1.0 %/s process can still be sampled
        # at durations spread over a 5x range (30s to 150s, a fixed
        # sequence, not real randomness). Every interval reports EXACTLY
        # the same rate, so max_relative_deviation is 0.0. No single
        # interval's duration dominates the segment's total elapsed time,
        # so max_usage_share stays well under the interval count's inverse.
        durations = [30, 150, 60, 40, 100, 55, 45, 130, 35, 70, 90, 50, 60, 80, 45]
        rate = 1.0
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        elapsed = 0.0
        usage = 0.0
        for duration in durations:
            elapsed += duration
            usage += rate * duration
            records.append(
                _HistoryRecord(elapsed, None, "claude", "short", usage)
            )

        measurements = _interval_measurements(records)

        assert measurements is not None
        self._assertClose(measurements.max_relative_deviation, 0.0)
        self._assertClose(measurements.max_usage_share, 0.14423076923076922)
        self.assertEqual(measurements.intervals_used, 15)
        self._assertClose(measurements.rate_drift, 0.0)
        self._assertClose(measurements.effective_intervals, 12.111982082866742)

    def test_steady_rate_with_rounding_jitter_measures_small(self) -> None:
        # Realistic shape #2: this project's own captured history
        # (~/.headroom/history.jsonl) reports used_percentage as a whole
        # number, so a continuously climbing true rate is quantized on
        # capture -- the "57 -> 56 -> 57" jitter described in
        # _records_since_latest_reset's docstring. Modeled here as a steady
        # 5-points-per-60s rate with a fixed +/-1 point rounding pattern
        # (never real randomness, so this can never flake). No interval
        # ever reports a zero delta, so folding is a no-op here.
        jitter = [1, -1, 0, 1, -1, 0, 1, -1, 0, 0, 1, -1]
        base = 5.0
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        elapsed = 0.0
        usage = 0.0
        for step in range(60):
            elapsed += 60.0
            usage += base + jitter[step % len(jitter)]
            records.append(
                _HistoryRecord(elapsed, None, "claude", "short", usage)
            )

        measurements = _interval_measurements(records)

        assert measurements is not None
        self.assertEqual(measurements.intervals_used, 60)
        self._assertClose(measurements.max_relative_deviation, 0.20000000000000012)
        self._assertClose(measurements.max_usage_share, 0.02)
        self._assertClose(measurements.effective_intervals, 58.44155844155844)

    def test_low_resolution_quantization_jitter_is_a_plain_measurement_now(
        self,
    ) -> None:
        # FINDING (round 4 verification, now expressed as a number rather
        # than an accepted tier failure): the same +/-1 point rounding
        # jitter as the test above, but sampled at a lower true rate (2
        # points per 60s instead of 5), produces a real 50% deviation --
        # a +/-1 swing off a base of 2 really is half the signal, not a
        # metric flaw. This is not a "gap" any more: there is no threshold
        # this number is being compared against, so there is nothing for it
        # to fail. It is simply a moderate deviation, and max_usage_share /
        # effective_intervals stay low, correctly telling a caller this is
        # spread-out resolution noise, not one bad interval.
        jitter = [1, -1, 0, 1, -1, 0]
        base = 2.0
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        elapsed = 0.0
        usage = 0.0
        for step in range(60):
            elapsed += 60.0
            usage += max(base + jitter[step % len(jitter)], 0.0)
            records.append(
                _HistoryRecord(elapsed, None, "claude", "short", usage)
            )

        measurements = _interval_measurements(records)

        assert measurements is not None
        self._assertClose(measurements.max_relative_deviation, 0.5000000000000001)
        self._assertClose(measurements.max_usage_share, 0.025)
        self._assertClose(measurements.effective_intervals, 51.42857142857143)

    def test_long_idle_gap_is_a_plain_measurement_now(self) -> None:
        # FINDING (round 4 design trade-off, now expressed as a number): a
        # perfectly steady 0.02 %/s rate, sampled every 120 seconds except
        # for one 8-hour idle gap in the middle (a laptop asleep overnight).
        # Every interval, including the long one, reports EXACTLY the same
        # rate, so max_relative_deviation is ~0. But the 8-hour interval
        # alone accounts for over 92% of the segment's total usage change
        # purely because of its duration (max_usage_share), and
        # effective_intervals collapses to ~1.2 out of 21 raw intervals.
        # This is the intended shape: a caller who only looks at deviation
        # would (correctly) see a steady rate; a caller who also looks at
        # share or effective count would (also correctly) see that the
        # evidence is really resting on one long interval. Both are true at
        # once, which is exactly why they are reported separately instead
        # of collapsed into one verdict.
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        rate = 0.02
        elapsed = 0.0
        usage = 0.0
        for _ in range(10):
            elapsed += 120.0
            usage += rate * 120.0
            records.append(
                _HistoryRecord(elapsed, None, "claude", "short", usage)
            )
        elapsed += 8 * 3600.0
        usage += rate * 8 * 3600.0
        records.append(_HistoryRecord(elapsed, None, "claude", "short", usage))
        for _ in range(10):
            elapsed += 120.0
            usage += rate * 120.0
            records.append(
                _HistoryRecord(elapsed, None, "claude", "short", usage)
            )

        measurements = _interval_measurements(records)

        assert measurements is not None
        self._assertClose(measurements.max_relative_deviation, 0.0, rel_tol=1e-6)
        self._assertClose(measurements.max_usage_share, 0.9230769230769235)
        self.assertEqual(measurements.intervals_used, 21)
        self._assertClose(measurements.rate_drift, 0.0, rel_tol=1e-6)
        self._assertClose(measurements.effective_intervals, 1.1732037486983677)

    # -- Round 5: quantized real data and whole-series drift -----------------

    def test_quantized_real_data_produces_actionable_numbers(self) -> None:
        # (Round 5 review, P1) Input straight from the finding: 60-second
        # captures with usage [90,90,90,91,91,92,92,93]. Under the old
        # tiering every zero-delta raw interval (5 of the 7 raw gaps here
        # report no change at all) vetoed HIGH outright, and a production
        # history audit found ALL 54 eligible claude-short segments scored
        # LOW for exactly this reason -- the metric was unusable on the data
        # it exists to describe.
        #
        # Folding zero-delta intervals forward into the next reading that
        # actually changed (see _folded_intervals) turns the 7 raw gaps
        # into 3 real intervals (deltas of 1 point each, over 180s/120s/
        # 120s), producing a moderate, actionable 33% deviation instead of
        # a saturated one. max_usage_share (33%) and effective_intervals
        # (3.0, equal to intervals_used) confirm this is evenly spread
        # quantization noise, not one dominant interval.
        projection = self._project(
            [
                self._record(0.0, 90.0, resets_at=604_800.0, window="weekly"),
                self._record(60.0, 90.0, resets_at=604_800.0, window="weekly"),
                self._record(120.0, 90.0, resets_at=604_800.0, window="weekly"),
                self._record(180.0, 91.0, resets_at=604_800.0, window="weekly"),
                self._record(240.0, 91.0, resets_at=604_800.0, window="weekly"),
                self._record(300.0, 92.0, resets_at=604_800.0, window="weekly"),
                self._record(360.0, 92.0, resets_at=604_800.0, window="weekly"),
                self._record(420.0, 93.0, resets_at=604_800.0, window="weekly"),
            ],
            now=421.0,
        )[0]

        self.assertIsNone(projection.reason)
        self.assertEqual(projection.samples_used, 8)
        # 7 raw gaps folded down to 3 real intervals.
        self.assertEqual(projection.intervals_used, 3)
        self._assertClose(projection.max_relative_deviation, 1.0 / 3.0)
        self._assertClose(projection.max_usage_share, 1.0 / 3.0)
        self._assertClose(projection.effective_intervals, 3.0)
        self._assertClose(projection.rate_drift, 0.5)

        # (round 7) The raw structural fields must be visible alongside the
        # folded ones. 4 of the 7 raw gaps are zero-delta (the two leading
        # 90->90 gaps, the 91->91 gap, and the 92->92 gap), so
        # zero_delta_fraction is 4/7 -- a plain, non-saturating measure of
        # how quantized this data is. max_raw_rate_ratio compares the
        # fastest single raw interval (1 point per 60s, wherever a real
        # jump lands) against the segment's overall rate (3 points over
        # 420s total): a modest 2.33x, nowhere near the 601x a genuine
        # burst produces (see test_zero_delta_fraction_and_max_raw_rate_
        # ratio_separate_quantized_data_from_a_burst below) -- correctly
        # telling a caller this is quantization, not instability.
        self._assertClose(projection.zero_delta_fraction, 4.0 / 7.0)
        self._assertClose(projection.max_raw_rate_ratio, 2.3333333333333335)

    def test_unfolded_zero_delta_intervals_would_saturate_the_deviation(
        self,
    ) -> None:
        # Direct unit-level companion to the test above: proves the folding
        # in _folded_intervals is what keeps max_relative_deviation
        # actionable. This asserts _folded_intervals' OWN output on the
        # same 8-point fixture, not a hand-written literal list -- a prior
        # version of this test built its "would-be-raw" rates by hand,
        # which stayed green even if _folded_intervals stopped folding
        # entirely (deleting the fold_zero_delta branch is not caught by
        # checking a literal that was never produced by the function). This
        # version fails immediately if folding is deleted, because deleting
        # it changes _folded_intervals' actual return value.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 90.0),
            _HistoryRecord(60.0, None, "claude", "short", 90.0),
            _HistoryRecord(120.0, None, "claude", "short", 90.0),
            _HistoryRecord(180.0, None, "claude", "short", 91.0),
            _HistoryRecord(240.0, None, "claude", "short", 91.0),
            _HistoryRecord(300.0, None, "claude", "short", 92.0),
            _HistoryRecord(360.0, None, "claude", "short", 92.0),
            _HistoryRecord(420.0, None, "claude", "short", 93.0),
        ]

        rates, deltas, elapsed_list = _folded_intervals(records)

        # 7 raw gaps fold down to 3: the two leading zero-delta gaps merge
        # forward into the first real jump, and each later zero-delta gap
        # merges into the jump right after it.
        self.assertEqual(len(rates), 3)
        self._assertClose(rates[0], 1.0 / 180.0)
        self._assertClose(rates[1], 1.0 / 120.0)
        self._assertClose(rates[2], 1.0 / 120.0)
        self.assertEqual(deltas, [1.0, 1.0, 1.0])
        self.assertEqual(elapsed_list, [180.0, 120.0, 120.0])

        # And, as before: the RAW (unfolded) view of the same fixture is
        # what saturates -- this is the direct proof that folding is not
        # merely cosmetic but is what keeps max_relative_deviation
        # actionable on quantized data. It is also why a median-based
        # deviation is the wrong tool for a raw diagnostic (see the
        # deleted five-field raw mirror discussed in BurnRateProjection's
        # docstring): applying the SAME median-and-deviation math this test
        # applies here is exactly what saturated every raw field at 1.0 on
        # real, non-pathological data. zero_delta_fraction and
        # max_raw_rate_ratio replace it precisely because neither takes a
        # median of the raw rates.
        raw_rates, _, _ = _raw_intervals(records)
        self.assertEqual(len(raw_rates), 7)
        center = statistics.median(raw_rates)
        self.assertEqual(center, 0.0)
        self._assertClose(_relative_difference(1.0 / 60.0, center), 1.0)

    def test_conspiring_intervals_evade_per_interval_maxima_but_not_drift(
        self,
    ) -> None:
        # (Round 5 review, P2) Input straight from the finding: intervals
        # with (rate, usage_delta) of [(0.751, 44), (1, 0.4) x5, (1.249,
        # 44)]. Every interval individually stays under what used to be a
        # 25% deviation cap and a 50% share cap (max_relative_deviation
        # lands at 0.249, max_usage_share at 0.489, both just inside the old
        # thresholds), because per-interval maxima can only ever see ONE
        # interval at a time. But the endpoint intervals jointly carry
        # 97.8% of the usage while the rate climbs from 0.751 to 1.249 --
        # a whole-series drift no per-interval check can see.
        #
        # rate_drift (comparing the time-weighted rate of the first half of
        # intervals to the second half) and effective_intervals (the
        # usage-weighted effective count) both expose it: effective_intervals
        # lands at about 2.1 despite 7 raw intervals existing, meaning the
        # segment really carries only about two intervals' worth of
        # independent evidence.
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        elapsed = 0.0
        usage = 0.0
        for rate, delta in [(0.751, 44.0)] + [(1.0, 0.4)] * 5 + [(1.249, 44.0)]:
            elapsed += delta / rate
            usage += delta
            records.append(
                _HistoryRecord(elapsed, None, "claude", "short", usage)
            )

        measurements = _interval_measurements(records)

        assert measurements is not None
        self.assertEqual(measurements.intervals_used, 7)
        # Both per-interval maxima stay inside what used to be "safe":
        self._assertClose(measurements.max_relative_deviation, 0.2490000000000001)
        self._assertClose(measurements.max_usage_share, 0.488888888888889)
        # But the whole-series measures expose the conspiracy plainly:
        self._assertClose(measurements.rate_drift, 0.6448474590894441)
        self._assertClose(measurements.effective_intervals, 2.0915100185911997)
        self.assertLess(measurements.effective_intervals, 3.0)

    # -- Round 6: raw/folded divergence, usage conservation, underflow ------

    def test_folding_smooths_a_genuine_burst_but_raw_reveals_it(self) -> None:
        # (HIGH finding, round 6) The complementary case to the quantized
        # fixture above: ten repetitions of "hold flat for 600s, then burst
        # 6.01 points in the next single second." Folding merges each flat
        # run forward into the burst that follows it, so every FOLDED
        # interval is (delta=6.01, elapsed=601s) -- a uniform ~0.01 %/s rate
        # that looks perfectly steady, even though the data is a real
        # 6.01-point-per-SECOND burst, not a steady trickle. This is the
        # other side of the same coin as the quantized-data case: folding
        # merging elapsed time across zero-delta gaps is what makes
        # quantized data readable, and it is the exact same operation that
        # smooths a burst into a flat rate. Both are true; a caller cannot
        # tell them apart from the folded fields alone.
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        elapsed = 0.0
        usage = 0.0
        for _ in range(10):
            elapsed += 600.0
            records.append(_HistoryRecord(elapsed, None, "claude", "short", usage))
            elapsed += 1.0
            usage += 6.01
            records.append(_HistoryRecord(elapsed, None, "claude", "short", usage))

        measurements = _interval_measurements(records)

        assert measurements is not None
        # Folded: ten equal (delta, elapsed) intervals -- looks flawless.
        self.assertEqual(measurements.intervals_used, 10)
        self._assertClose(measurements.max_relative_deviation, 0.0, rel_tol=1e-9)
        self._assertClose(measurements.max_usage_share, 0.1)
        self._assertClose(measurements.rate_drift, 0.0, rel_tol=1e-9)
        self._assertClose(measurements.effective_intervals, 10.0)

        # Raw: half of the 20 raw gaps are the genuine 1-second bursts,
        # running at 6.01 %/s against a 601-second-weighted overall rate of
        # 0.01 %/s -- a 601x max_raw_rate_ratio, the unmistakable signature
        # of a burst that the folded fields above cannot show (they measure
        # a flawless ~0.0 deviation on this exact same data). Half the raw
        # gaps are also zero-delta (the flat runs), matching the quantized
        # fixture's zero_delta_fraction exactly -- see the comparison test
        # below for why zero_delta_fraction alone cannot tell these two
        # series apart, and max_raw_rate_ratio is what does.
        self._assertClose(measurements.zero_delta_fraction, 0.5)
        self._assertClose(measurements.max_raw_rate_ratio, 601.0000000000002)

    def test_zero_delta_fraction_and_max_raw_rate_ratio_separate_quantized_data_from_a_burst(
        self,
    ) -> None:
        # (round 7, replacing the deleted five-field raw mirror) Direct
        # proof that the two new raw structural fields achieve what the
        # deleted mirror did not: telling a genuine burst apart from
        # ordinary quantized-but-steady data, even though the two series
        # below are indistinguishable on every one of the five FOLDED
        # fields (folding smooths both the same way -- see the assertion
        # below that proves it).
        #
        # Series A, quantized-steady: ten reps of "flat for 60s, then step
        # +1 over the next 60s" -- an ordinary whole-point-quantized 1-point-
        # per-120s rate, the same shape real captures produce.
        records_a = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        elapsed = 0.0
        usage = 0.0
        for _ in range(10):
            elapsed += 60.0
            records_a.append(_HistoryRecord(elapsed, None, "claude", "short", usage))
            elapsed += 60.0
            usage += 1.0
            records_a.append(_HistoryRecord(elapsed, None, "claude", "short", usage))

        # Series B, burst: ten reps of "flat for 600s, then burst +6.01 over
        # the next 1s" -- the same fixture as the test above, repeated here
        # so both series are built the same way, side by side.
        records_b = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        elapsed = 0.0
        usage = 0.0
        for _ in range(10):
            elapsed += 600.0
            records_b.append(_HistoryRecord(elapsed, None, "claude", "short", usage))
            elapsed += 1.0
            usage += 6.01
            records_b.append(_HistoryRecord(elapsed, None, "claude", "short", usage))

        # Series C, genuinely smooth: ten reps of a uniform +1 over 60s,
        # with no flat runs at all -- the control case neither quantized
        # nor bursty.
        records_c = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        elapsed = 0.0
        usage = 0.0
        for _ in range(10):
            elapsed += 60.0
            usage += 1.0
            records_c.append(_HistoryRecord(elapsed, None, "claude", "short", usage))

        measurements_a = _interval_measurements(records_a)
        measurements_b = _interval_measurements(records_b)
        measurements_c = _interval_measurements(records_c)
        assert measurements_a is not None
        assert measurements_b is not None
        assert measurements_c is not None

        # The deleted five-field raw mirror's exact failure: A and B were
        # PROVEN identical on every folded field. Confirmed still true here
        # (folding is unchanged) so the point of comparison is fair -- the
        # folded fields genuinely cannot tell these apart, which is exactly
        # why the raw structural fields exist.
        self._assertClose(measurements_a.max_relative_deviation, 0.0, rel_tol=1e-9)
        self._assertClose(measurements_b.max_relative_deviation, 0.0, rel_tol=1e-9)
        self._assertClose(measurements_a.max_usage_share, 0.1)
        self._assertClose(measurements_b.max_usage_share, 0.1, rel_tol=1e-9)
        self.assertEqual(measurements_a.intervals_used, measurements_b.intervals_used)
        self._assertClose(measurements_a.rate_drift, 0.0, rel_tol=1e-9)
        self._assertClose(measurements_b.rate_drift, 0.0, rel_tol=1e-9)
        self._assertClose(measurements_a.effective_intervals, 10.0)
        self._assertClose(measurements_b.effective_intervals, 10.0, rel_tol=1e-9)

        # zero_delta_fraction alone does NOT separate A from B: both series
        # spend exactly half their raw gaps flat, so both report 0.5. This
        # is expected, not a gap -- zero_delta_fraction answers "how
        # quantized is this", a question A and B genuinely share the same
        # answer to. It is reported for what it is, not as a burst
        # detector on its own.
        self._assertClose(measurements_a.zero_delta_fraction, 0.5)
        self._assertClose(measurements_b.zero_delta_fraction, 0.5)

        # max_raw_rate_ratio is where A and B stop looking alike. A's single
        # fastest raw interval runs at exactly 2x the segment's overall
        # rate -- an ordinary quantization step, nothing alarming. B's
        # single fastest raw interval (the 1-second burst) runs at over
        # 601x the segment's overall rate: the burst dominates so
        # thoroughly that no amount of folding-driven averaging can call it
        # unremarkable once the raw view is inspected directly. This is the
        # concrete proof that the replacement fields discriminate what the
        # deleted mirror could not.
        self._assertClose(measurements_a.max_raw_rate_ratio, 2.0)
        self._assertClose(measurements_b.max_raw_rate_ratio, 601.0000000000002)
        self.assertGreater(
            measurements_b.max_raw_rate_ratio, measurements_a.max_raw_rate_ratio * 100
        )

        # Series C (genuinely smooth, no flat runs, no burst) is the
        # control: zero_delta_fraction is 0.0 (nothing here is quantized)
        # and max_raw_rate_ratio is exactly 1.0 (every raw interval runs at
        # precisely the overall rate, the theoretical floor for this
        # ratio). C is distinguishable from A by zero_delta_fraction and
        # from B by both fields, so all three of A, B, and C are pairwise
        # distinguishable on at least one reported field.
        self._assertClose(measurements_c.zero_delta_fraction, 0.0)
        self._assertClose(measurements_c.max_raw_rate_ratio, 1.0)

    def test_zero_delta_fraction_and_max_raw_rate_ratio_diverge_from_folded(
        self,
    ) -> None:
        # (MEDIUM test finding, round 6/7 method requirement) A mutation
        # that sourced the raw structural fields from the FOLDED triples
        # instead of the raw ones (or hard-coded a value that happened to
        # match the folded numbers) must be caught, not pass silently. This
        # fixture -- (0,96), (60,96), (60.0000005,97), (120,98) -- is built
        # so the folded and raw views of the SAME segment disagree sharply
        # on both new fields, exactly the shape the round-6 review demanded
        # for the fields it replaced.
        #
        # Folded: the 60.0000005s sub-microsecond gap merges into its
        # neighbor (see _consecutive_intervals), leaving two ordinary
        # ~60s intervals with a 1-point delta each -- no zero-delta gap
        # anywhere in the folded view, and both intervals run at
        # essentially the same rate.
        # Raw: the unfolded view keeps the 96->96 gap as its own zero-delta
        # interval and the 96->97 sub-microsecond jump as its own
        # near-instantaneous one, so half the raw gaps are zero-delta and
        # the fastest raw interval runs at 2x the overall rate.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 96.0),
            _HistoryRecord(60.0, None, "claude", "short", 96.0),
            _HistoryRecord(60.0000005, None, "claude", "short", 97.0),
            _HistoryRecord(120.0, None, "claude", "short", 98.0),
        ]

        folded_rates, folded_deltas, _ = _folded_intervals(records)
        raw_rates, raw_deltas, _ = _raw_intervals(records)

        # The folded view has no zero-delta interval at all -- sourcing
        # zero_delta_fraction from folded_deltas would report 0.0, not the
        # true 0.5.
        self.assertNotIn(0.0, folded_deltas)
        self.assertEqual(raw_deltas.count(0.0), 1)
        self.assertEqual(len(raw_deltas), 2)

        measurements = _interval_measurements(records)
        assert measurements is not None
        # The folded fields, included so a reader can see just how close
        # they are to "nothing is happening here" -- and yet the raw
        # structural fields below tell a different, correct story.
        self._assertClose(measurements.max_relative_deviation, 8.333333398335972e-09)
        self._assertClose(measurements.max_usage_share, 0.5)
        self.assertEqual(measurements.intervals_used, 2)
        self._assertClose(measurements.effective_intervals, 2.0)

        self._assertClose(measurements.zero_delta_fraction, 0.5)
        self._assertClose(measurements.max_raw_rate_ratio, 2.0)

    def test_usage_is_conserved_across_folded_and_raw_intervals(self) -> None:
        # (HIGH finding, round 6, method requirement) Whatever folding or
        # skipping happens internally, the sum of every measured interval's
        # usage delta must equal the segment's overall usage delta (latest
        # minus first) exactly -- for BOTH the folded and the raw view.
        # This is checked across several different fixture shapes: a clean
        # steady series, the quantized-real-data shape, a sub-microsecond
        # skipped interval, and a long run of zero-delta folding.
        fixtures = []

        clean = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        for index in range(1, 5):
            clean.append(
                _HistoryRecord(float(index) * 60.0, None, "claude", "short", float(index) * 10.0)
            )
        fixtures.append(clean)

        quantized = [
            _HistoryRecord(0.0, None, "claude", "short", 90.0),
            _HistoryRecord(60.0, None, "claude", "short", 90.0),
            _HistoryRecord(120.0, None, "claude", "short", 90.0),
            _HistoryRecord(180.0, None, "claude", "short", 91.0),
            _HistoryRecord(240.0, None, "claude", "short", 91.0),
            _HistoryRecord(300.0, None, "claude", "short", 92.0),
            _HistoryRecord(360.0, None, "claude", "short", 92.0),
            _HistoryRecord(420.0, None, "claude", "short", 93.0),
        ]
        fixtures.append(quantized)

        sub_microsecond = [
            _HistoryRecord(0.0, None, "claude", "short", 90.0),
            _HistoryRecord(60.0, None, "claude", "short", 91.0),
            _HistoryRecord(60.0000005, None, "claude", "short", 98.9),
            _HistoryRecord(120.0, None, "claude", "short", 99.9),
        ]
        fixtures.append(sub_microsecond)

        long_zero_run = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        long_zero_run.append(_HistoryRecord(60.0, None, "claude", "short", 1.0))
        elapsed = 60.0
        for _ in range(99):
            elapsed += 60.0
            long_zero_run.append(_HistoryRecord(elapsed, None, "claude", "short", 1.0))
        elapsed += 60.0
        long_zero_run.append(_HistoryRecord(elapsed, None, "claude", "short", 100.0))
        fixtures.append(long_zero_run)

        # (HIGH finding, round 7) The terminal case the round-6 conservation
        # test missed: the segment's very LAST gap is sub-microsecond, so
        # there is no following gap to carry its delta forward into. An
        # earlier version of this fix dropped it outright, reporting
        # deltas summing to 10 while the segment's true total is 11.
        terminal_sub_microsecond = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(60.0, None, "claude", "short", 10.0),
            _HistoryRecord(60.0000005, None, "claude", "short", 11.0),
        ]
        fixtures.append(terminal_sub_microsecond)

        for records in fixtures:
            expected_total = records[-1].used_percentage - records[0].used_percentage
            _, folded_deltas, _ = _folded_intervals(records)
            _, raw_deltas, _ = _raw_intervals(records)
            self.assertAlmostEqual(
                sum(folded_deltas),
                expected_total,
                msg=f"folded deltas do not conserve usage for {records!r}",
            )
            self.assertAlmostEqual(
                sum(raw_deltas),
                expected_total,
                msg=f"raw deltas do not conserve usage for {records!r}",
            )

    def test_sub_microsecond_interval_carries_its_usage_delta_forward(
        self,
    ) -> None:
        # (HIGH finding, round 6) A sub-microsecond gap sits between two
        # ordinary readings: (60, 91) -> (60.0000005, 98.9) is a 7.9-point
        # jump over ~5e-7s, well below MIN_INTERVAL_SECONDS. That gap cannot
        # stand as its own interval (dividing by ~5e-7s is not evidence),
        # but its 7.9-point usage delta is real and must survive by being
        # carried forward into the next measurable interval -- the same
        # carry-forward folding already does for zero-delta gaps. The old
        # code discarded it outright: the elapsed AND delta of a
        # below-threshold gap were dropped before ever reaching the
        # accumulator, so the reported deltas were the degenerate [1.0, 1.0]
        # instead of the true [1.0, 8.9].
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 90.0),
            _HistoryRecord(60.0, None, "claude", "short", 91.0),
            _HistoryRecord(60.0000005, None, "claude", "short", 98.9),
            _HistoryRecord(120.0, None, "claude", "short", 99.9),
        ]

        rates, deltas, elapsed_list = _folded_intervals(records)

        self.assertEqual(len(deltas), 2)
        self._assertClose(deltas[0], 1.0)
        # The 7.9-point jump is preserved inside the second interval's
        # delta (1.0 carried over from the 99.9-98.9 gap, plus the 7.9 from
        # the sub-microsecond gap) rather than vanishing.
        self._assertClose(deltas[1], 8.900000000000006)
        self.assertAlmostEqual(sum(deltas), 99.9 - 90.0)

        measurements = _interval_measurements(records)
        assert measurements is not None
        self.assertEqual(measurements.intervals_used, 2)
        self._assertClose(measurements.max_relative_deviation, 0.7979797979797981)
        self._assertClose(measurements.max_usage_share, 0.898989898989899)

    def test_terminal_sub_microsecond_interval_merges_backward_not_dropped(
        self,
    ) -> None:
        # (HIGH finding, round 7) The input straight from the finding: the
        # segment's LAST gap, not an interior one, is sub-microsecond --
        # (60, 10) -> (60.0000005, 11) is a 1-point jump over ~5e-7s. Unlike
        # the interior case above, there is no following gap to carry this
        # delta forward into. The pre-fix code simply dropped it: the
        # accumulator held a nonzero delta when the loop ended and was
        # discarded unflushed, so both the folded and raw views reported
        # deltas summing to 10 while the segment's true total usage change
        # is 11 -- silently broken conservation on a successful projection.
        #
        # The fix folds the leftover BACKWARD into the last already-
        # measured interval (the only interval that exists here, from the
        # very first gap) instead of dropping it: one merged interval
        # covering the full 60.0000005s span with the full 11.0-point
        # delta, not two.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(60.0, None, "claude", "short", 10.0),
            _HistoryRecord(60.0000005, None, "claude", "short", 11.0),
        ]

        folded_rates, folded_deltas, folded_elapsed = _folded_intervals(records)
        raw_rates, raw_deltas, raw_elapsed = _raw_intervals(records)

        # One interval, not two: the terminal remainder had nowhere to
        # stand on its own, so it was absorbed into the interval before it
        # rather than reported (or dropped) separately.
        self.assertEqual(len(folded_deltas), 1)
        self.assertEqual(len(raw_deltas), 1)
        self._assertClose(folded_deltas[0], 11.0)
        self._assertClose(raw_deltas[0], 11.0)
        self._assertClose(folded_elapsed[0], 60.0000005)
        self._assertClose(raw_elapsed[0], 60.0000005)
        self.assertAlmostEqual(sum(folded_deltas), 11.0)
        self.assertAlmostEqual(sum(raw_deltas), 11.0)

        # The whole projection must still succeed end to end (this is not a
        # decline case -- the data is perfectly good evidence, just
        # unevenly split across a too-short final gap), with the
        # conservation-correct measurements reaching the caller.
        projection = self._project(
            [
                self._record(0.0, 0.0),
                self._record(60.0, 10.0),
                self._record(60.0000005, 11.0),
            ],
            now=61.0,
        )[0]
        self.assertIsNone(projection.reason)
        self.assertIsNotNone(projection.projected_exhaustion_at)
        self.assertEqual(projection.intervals_used, 1)
        self._assertClose(projection.max_usage_share, 1.0)

    def test_positive_delta_underflowing_to_zero_rate_is_declined(self) -> None:
        # (MEDIUM finding, round 6) Disproves the old docstring claim that a
        # zero median is structurally unreachable because every folded
        # interval's delta is strictly positive. It is: 5e-324 and 1e-323
        # are the two smallest representable positive doubles, and dividing
        # either by an ordinary 60s elapsed time underflows to an exact
        # 0.0 -- a positive delta with no representable rate. Folding that
        # interval in as "rate 0.0" would silently misrepresent a
        # measurement failure as trivial zero-usage agreement, and corrupt
        # the median every other interval is compared against. Instead,
        # _RateUnderflowedToZero is raised so the caller can decline the
        # whole projection instead of reporting numbers built on it.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(60.0, None, "claude", "short", 5e-324),
            _HistoryRecord(120.0, None, "claude", "short", 1e-323),
            _HistoryRecord(121.0, None, "claude", "short", 99.0),
        ]

        with self.assertRaises(_RateUnderflowedToZero):
            _interval_measurements(records)

    def test_underflowed_rate_declines_the_whole_projection(self) -> None:
        # End-to-end companion to the direct test above: project_exhaustion
        # must turn the raised _RateUnderflowedToZero into a declined
        # projection with a distinct reason, not let the exception escape
        # or silently fall back to NON_FINITE_RESULT (0.0 is finite; this
        # is a different failure mode and callers should be able to tell
        # them apart).
        projection = self._project(
            [
                self._record(0.0, 0.0, resets_at=None),
                self._record(60.0, 5e-324, resets_at=None),
                self._record(120.0, 1e-323, resets_at=None),
                self._record(121.0, 99.0, resets_at=None),
            ],
            now=121.5,
        )[0]

        self.assertEqual(
            projection.reason, NoProjectionReason.RATE_UNDERFLOWED_TO_ZERO
        )
        self.assertIsNone(projection.projected_exhaustion_at)
        self._assertNoMeasurements(projection)
        # The Theil-Sen rate itself is fitted independently of the interval
        # diagnostics and does not underflow the same way, so it is still
        # reported even though the diagnostics declined.
        self.assertIsNotNone(projection.rate_percent_per_second)

    def test_exact_zero_delta_folding_is_not_a_tolerance(self) -> None:
        # (MEDIUM test finding, round 6) A mutation that replaces the exact
        # `accumulated_delta == 0.0` fold check with a near-zero tolerance
        # (e.g. `< 1e-9`) previously survived the whole suite. This fixture
        # is built to fail under such a mutant: the first gap's delta is
        # 1e-12 -- nonzero, but small enough that a 1e-9 tolerance would
        # treat it as "close enough to zero" and fold it forward anyway.
        # Under the correct exact check it must survive as its own
        # (tiny-rate) interval, giving THREE folded intervals with an
        # enormous rate_drift; the tolerant mutant folds it away, giving
        # only TWO intervals with zero drift.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 98.0),
            _HistoryRecord(60.0, None, "claude", "short", 98.000000000001),
            _HistoryRecord(120.0, None, "claude", "short", 99.0),
            _HistoryRecord(180.0, None, "claude", "short", 99.5),
        ]

        rates, deltas, elapsed_list = _folded_intervals(records)
        self.assertEqual(len(rates), 3)

        measurements = _interval_measurements(records)
        assert measurements is not None
        self.assertEqual(measurements.intervals_used, 3)
        self._assertClose(measurements.rate_drift, 753950830473.4714, rel_tol=1e-6)

    def test_single_surviving_folded_interval(self) -> None:
        # (MEDIUM test finding, round 6) No existing test covered the case
        # where folding collapses a segment down to exactly ONE surviving
        # interval: (0,10) and (60,10) fold together (zero delta), then
        # merge into the (120,20) jump, leaving one interval covering the
        # whole 120s span. All five folded measurements are asserted so a
        # mutation to any one of them (including the single-interval
        # rate_drift=0.0 special case) is caught here.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 10.0),
            _HistoryRecord(60.0, None, "claude", "short", 10.0),
            _HistoryRecord(120.0, None, "claude", "short", 20.0),
        ]

        measurements = _interval_measurements(records)

        assert measurements is not None
        self.assertEqual(measurements.intervals_used, 1)
        self._assertClose(measurements.max_relative_deviation, 0.0)
        self._assertClose(measurements.max_usage_share, 1.0)
        self._assertClose(measurements.rate_drift, 0.0)
        self._assertClose(measurements.effective_intervals, 1.0)

    def test_long_zero_run_accumulates_full_elapsed_time(self) -> None:
        # (MEDIUM test finding, round 6) The longest zero-delta run any
        # prior fixture folded was two consecutive readings. Here, usage
        # sits flat at 1.0 for 99 consecutive one-minute readings (5,940
        # seconds of folded-together zero-delta gaps) before the next real
        # jump to 100.0. A mutant that truncates the accumulated-elapsed
        # carry after just a couple of zero-delta folds (instead of
        # continuing to accumulate across the whole run) would report a
        # short elapsed time for the final interval instead of the true
        # 6,000 seconds (60s for the initial 0->1 jump's neighbor gap, plus
        # 5,940s of zero runs, plus the final 60s gap into the 100.0 jump).
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        records.append(_HistoryRecord(60.0, None, "claude", "short", 1.0))
        elapsed = 60.0
        for _ in range(99):
            elapsed += 60.0
            records.append(_HistoryRecord(elapsed, None, "claude", "short", 1.0))
        elapsed += 60.0
        records.append(_HistoryRecord(elapsed, None, "claude", "short", 100.0))
        self.assertEqual(len(records), 102)
        self.assertEqual(elapsed, 6060.0)

        rates, deltas, elapsed_list = _folded_intervals(records)

        self.assertEqual(len(elapsed_list), 2)
        self._assertClose(elapsed_list[0], 60.0)
        # The full 6,000-second accumulated elapsed time of the long zero
        # run plus its bracketing gaps must survive intact.
        self._assertClose(elapsed_list[1], 6000.0)
        self._assertClose(deltas[1], 99.0)

    def test_single_terminal_burst_after_quiet_history(self) -> None:
        # (Codex P1, round 1) All the acceleration comes from one 1-second
        # burst (7200 -> 7201) after two hours of near-flat usage. The old
        # sign-based confidence check saw 6 of 6 positive pairwise slopes
        # and called that HIGH; the old LOW_CONFIDENCE veto later blocked
        # this outright. The continuous library has no veto -- it still
        # projects -- but the measurements it reports make the single-burst
        # shape unmistakable: an interval running millions of times the
        # median rate, supplying essentially all the usage, with an
        # effective interval count of about 1.
        projection = self._project(
            [
                self._record(0.0, 0.0, resets_at=None),
                self._record(3_600.0, 0.1, resets_at=None),
                self._record(7_200.0, 0.2, resets_at=None),
                self._record(7_201.0, 99.0, resets_at=None),
            ],
            now=7_202.0,
        )[0]

        self.assertIsNone(projection.reason)
        self.assertIsNotNone(projection.projected_exhaustion_at)
        self.assertEqual(projection.intervals_used, 3)
        self._assertClose(projection.max_relative_deviation, 3556799.0)
        self._assertClose(projection.max_usage_share, 0.997979797979798)
        self._assertClose(projection.rate_drift, 987.7253540683142)
        self._assertClose(projection.effective_intervals, 1.0040506235747522)

    def test_project_group_hard_invariant_rejects_decrease_if_called_directly(
        self,
    ) -> None:
        # Scope warning: this does NOT cover the segmentation fix (a decrease
        # starting a new segment). project_exhaustion can no longer reach
        # _project_group with a decreasing segment at all --
        # _records_since_latest_reset treats any decrease as a reset boundary
        # and strips it out first, so this test still passes even if that
        # segmentation fix is reverted (proven: reverting
        # _records_since_latest_reset to the old resets_at-only check does
        # not fail this test, because it calls the private _project_group
        # directly, bypassing segmentation entirely). Real behavioural
        # coverage for the segmentation fix itself lives in
        # test_usage_drop_segments_out_the_stale_window and
        # test_unmarked_reset_without_resets_at_is_still_detected below,
        # which go through project_exhaustion and do fail if segmentation is
        # reverted. This test exists only to prove the defensive invariant
        # inside _project_group holds if it is ever invoked with unsegmented
        # data (e.g. a future caller of the private function).
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
        self._assertNoMeasurements(projection)

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
        self._assertNoMeasurements(projection)

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
        # Usage is nondecreasing across the whole history (10 -> 20 -> 21 ->
        # 31 -> 41): the decrease-based segmentation rule never fires here,
        # so this is a clean isolation of the resets_at-change rule alone.
        # (An earlier version of this test used a 99 -> 1 usage drop
        # alongside the resets_at change; reverting the resets_at rule still
        # passed, because the decrease itself tripped the OTHER
        # segmentation rule and segmented the history anyway -- proving
        # nothing about resets_at specifically. See
        # test_usage_drop_segments_out_the_stale_window for the decrease
        # rule's own isolated coverage.)
        projection = self._project(
            [
                self._record(0.0, 10.0, resets_at=1_000.0),
                self._record(60.0, 20.0, resets_at=1_000.0),
                self._record(120.0, 21.0, resets_at=5_000.0),
                self._record(1_920.0, 31.0, resets_at=5_000.0),
                self._record(3_720.0, 41.0, resets_at=5_000.0),
            ]
        )[0]

        self.assertEqual(projection.samples_used, 3)
        self.assertEqual(projection.span_seconds, 3_600.0)
        self.assertAlmostEqual(
            projection.rate_percent_per_second or 0.0, 1.0 / 180.0
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
        self._assertNoMeasurements(projection)

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
        self._assertNoMeasurements(projection)

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
        # Direct check of the general safety net (the PROJECTED_EXHAUSTION_IN_PAST
        # guard), not the resets_at-specific WINDOW_ALREADY_RESET guard above.
        # resets_at=None is essential: any resets_at <= now would be caught by
        # the earlier WINDOW_ALREADY_RESET check first, so the past-projection
        # guard this test claims to cover would never actually run (a prior
        # version of this test used resets_at=1_000.0 with now=2_000.0 and
        # passed even with the past-projection guard deleted entirely, because
        # WINDOW_ALREADY_RESET fired first and short-circuited the case).
        #
        # The rate here (1/3 %/s from 0->120s) projects exhaustion at t=240,
        # but "now" is far past that, so a live projection would already be
        # stale by the time it is reported.
        projection = self._project(
            [
                self._record(0.0, 20.0, resets_at=None),
                self._record(60.0, 40.0, resets_at=None),
                self._record(120.0, 60.0, resets_at=None),
            ],
            now=2_000.0,
        )[0]

        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertEqual(
            projection.reason, NoProjectionReason.PROJECTED_EXHAUSTION_IN_PAST
        )
        self._assertNoMeasurements(projection)

    def test_ten_thousand_reading_segment_is_bounded_in_time_and_memory(
        self,
    ) -> None:
        # (Codex P1, burn_rate.py:239) Exact Theil-Sen over an unbounded
        # segment is O(n^2) in time and memory: 10,000 readings would build
        # ~50 million float pairs. history.jsonl is append-only and a
        # segment can grow without bound absent a marked reset, so this must
        # stay fast and small regardless of history length. The interval
        # measurements added alongside the fitted rate are only O(n) over
        # the same capped segment, so they do not change this bound.
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
        # Memory and samples_used are the primary guarantees: they are exact
        # and cannot flake. Wall-clock is kept only as a coarse regression
        # tripwire against reintroducing the pre-fix O(n^2)-over-unbounded-N
        # behavior (which builds tens of millions of pairs from 10,000
        # readings and would take far longer than this). The bound is wide
        # on purpose so a loaded CI machine cannot flake it; it is not meant
        # to assert anything about steady-state performance.
        self.assertLess(
            elapsed,
            15.0,
            "capped fit must stay bounded regardless of history length "
            "(this is a regression tripwire against unbounded O(n^2) growth, "
            "not a steady-state performance budget)",
        )
        self.assertLess(
            peak_bytes,
            50 * 1024 * 1024,
            "capped fit must stay well under the pre-fix multi-GB peak",
        )

    # -- Split coverage for each finite-result defense individually --------
    #
    # A single combined test here previously claimed to cover three distinct
    # defenses (the MIN_INTERVAL_SECONDS threshold, the isfinite(slope) gate
    # in _pairwise_slopes, and the aggregate isfinite check in
    # _project_group) using denormal timestamps a few femtoseconds apart.
    # Mutation testing showed that input only ever exercised the threshold
    # gate: removing the isfinite(slope) gate or the aggregate gate
    # individually left the test passing, because the threshold gate alone
    # already filtered those denormal intervals before a division could ever
    # happen. Each defense now has its own test, constructed so ONLY that
    # defense can catch the failure mode being tested.

    def test_pairwise_slopes_filters_intervals_below_the_threshold(
        self,
    ) -> None:
        # Isolates the MIN_INTERVAL_SECONDS threshold specifically: the
        # (r1, r2) pair's elapsed time (~5e-7s) is below the threshold, but
        # its slope (~600) is an ordinary finite float, not inf/nan. The
        # isfinite(slope) gate cannot catch this case, so only the threshold
        # itself can exclude this pair -- a mutation that deletes the
        # threshold check leaves this slope in the result.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(60.0, None, "claude", "short", 6.0),
            _HistoryRecord(60.0 + 5e-7, None, "claude", "short", 6.0003),
        ]

        slopes = _pairwise_slopes(records)

        self.assertEqual(len(slopes), 2)
        self.assertTrue(
            all(slope < 1.0 for slope in slopes),
            f"the sub-threshold pair's ~600 slope leaked into {slopes}",
        )

    def test_pairwise_slopes_filters_non_finite_slopes(self) -> None:
        # Isolates the isfinite(slope) gate specifically: every elapsed time
        # here (0.5s and 1.0s) is comfortably above MIN_INTERVAL_SECONDS, so
        # the threshold gate never fires. The huge used_percentage delta
        # against r1 still overflows two of the three pairwise slopes to
        # +/-inf; only the isfinite(slope) check can exclude them.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(0.5, None, "claude", "short", 1e308),
            _HistoryRecord(1.0, None, "claude", "short", 2.0),
        ]

        slopes = _pairwise_slopes(records)

        self.assertEqual(slopes, [2.0])

    def test_project_group_rejects_non_finite_projection_from_finite_inputs(
        self,
    ) -> None:
        # Isolates the aggregate isfinite() check in _project_group
        # specifically: every raw input (captured_at, used_percentage) and
        # even the fitted rate are individually finite here (elapsed times
        # of 1e308 and 5e307 keep every pairwise slope a tiny but finite
        # subnormal, so neither the threshold gate nor the slope-finiteness
        # gate has anything to catch). Only when that tiny rate is used to
        # extrapolate forward -- (100 - usage) / rate -- does the division
        # overflow to inf. Only the aggregate gate can catch that.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(1e308, None, "claude", "short", 1e-8),
            _HistoryRecord(1.5e308, None, "claude", "short", 2e-8),
        ]

        projection = _project_group(("claude", "short"), records, now=2e308)

        self.assertEqual(projection.reason, NoProjectionReason.NON_FINITE_RESULT)
        self.assertIsNone(projection.rate_percent_per_second)
        self.assertIsNone(projection.projected_exhaustion_at)
        self._assertNoMeasurements(projection)
        # The raw inputs and their span are finite (only the extrapolated
        # projection overflows), which is exactly what distinguishes this
        # gate from the burn_rate.py:241 span fix: that fix is for when the
        # SPAN itself is unrepresentable; this is for when finite inputs
        # still produce a non-finite projection further downstream. Neither
        # may ever put a non-finite number in the returned object.
        self.assertIsNotNone(projection.span_seconds)
        assert projection.span_seconds is not None  # narrows for mypy/readers
        self.assertTrue(math.isfinite(projection.span_seconds))
        self.assertAlmostEqual(projection.span_seconds, 1.5e308)

    def test_span_seconds_is_none_not_infinite_when_unrepresentable(
        self,
    ) -> None:
        # (Codex P2, burn_rate.py:241) Captures at -1e308, 0, and 1e308 are
        # each individually finite, but the earliest-to-latest subtraction
        # that produces span_seconds overflows: 1e308 - (-1e308) exceeds the
        # float range and is literally inf. The old code validated span
        # finiteness only as part of a LATER combined check (alongside rate/
        # projected_at/horizon_seconds) and every early return before that
        # point -- including this one, since resets_at=None and 3 samples
        # clears TOO_FEW_SAMPLES -- handed back the raw infinite span
        # unexamined. span_seconds must be None here, never inf.
        records = [
            _HistoryRecord(-1e308, None, "claude", "short", 0.0),
            _HistoryRecord(0.0, None, "claude", "short", 1.0),
            _HistoryRecord(1e308, None, "claude", "short", 2.0),
        ]

        projection = _project_group(("claude", "short"), records, now=1e308)

        self.assertEqual(projection.reason, NoProjectionReason.NON_FINITE_RESULT)
        self.assertIsNone(projection.span_seconds)
        self._assertNoMeasurements(projection)

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
        # (which would defeat the guard) or negative. This only checks the
        # constant's VALUE: it would still pass even if production code
        # stopped using the constant entirely. That gap is why
        # test_pairwise_slopes_filters_intervals_below_the_threshold exists
        # above -- it fails if _pairwise_slopes stops applying this constant,
        # regardless of what the constant's value is.
        self.assertGreater(MIN_INTERVAL_SECONDS, 0.0)
        self.assertLess(MIN_INTERVAL_SECONDS, 1.0)

    # -- Direct coverage of the shared zero-baseline convention -------------

    def test_relative_difference_zero_baseline_convention(self) -> None:
        # _folded_intervals guarantees every real rate this module computes
        # is strictly positive (see _relative_difference's docstring), so
        # the zero-baseline branch below can never actually run through
        # project_exhaustion. It is tested directly here, independent of
        # that guarantee, so the convention itself -- 0.0 for trivial
        # agreement, 1.0 (not inf/nan) otherwise -- stays covered even
        # though no real segment can reach it.
        self.assertEqual(_relative_difference(0.0, 0.0), 0.0)
        self.assertEqual(_relative_difference(5.0, 0.0), 1.0)
        self.assertEqual(_relative_difference(-5.0, 0.0), 1.0)
        self.assertEqual(_relative_difference(1.25, 1.0), 0.25)
        self.assertEqual(_relative_difference(0.75, 1.0), 0.25)


if __name__ == "__main__":
    unittest.main()
