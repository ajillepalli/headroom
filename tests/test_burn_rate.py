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
    MAX_INTERVAL_RATE_RATIO,
    MIN_INTERVAL_SECONDS,
    MIN_INTERVALS_FOR_HIGH_CONFIDENCE,
    MIN_SPAN_TO_HORIZON_RATIO,
    MIN_SAMPLES,
    BurnRateProjection,
    NoProjectionReason,
    ProjectionConfidence,
    _HistoryRecord,
    _confidence_for_records,
    _pairwise_slopes,
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
        # Four points (three consecutive intervals, all at the same 25%-per-
        # 172800s rate) so this clears MIN_INTERVALS_FOR_HIGH_CONFIDENCE: two
        # agreeing intervals is one coincidence, not a demonstrated pattern.
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

    # -- Regression tests for the second-round Codex P2 confidence review --

    def test_recency_weighting_alone_cannot_launder_a_dominant_interval(
        self,
    ) -> None:
        # (Codex P2) 500 one-second readings: the first interval alone jumps
        # usage from 0 to 50 (half the entire quota in one interval), then
        # 498 more intervals each add a steady +0.1. Linear recency weighting
        # gives that first, dominant interval a weight of 1 out of 124,750,
        # diluting it to a recency-weighted dispersion ratio of ~0.004 --
        # HIGH under the old, purely recency-weighted check. The unweighted
        # ratio cannot be diluted by position and must veto HIGH here.
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        usage = 50.0
        for index in range(1, 500):
            records.append(
                _HistoryRecord(float(index), None, "claude", "short", usage)
            )
            usage += 0.1

        confidence = _confidence_for_records(records)

        self.assertNotEqual(confidence, ProjectionConfidence.HIGH)

    def test_three_intervals_with_one_early_deviation_is_not_high_confidence(
        self,
    ) -> None:
        # (Codex P2) Exactly three intervals with rates [2, 1, 1]: the first
        # interval disagrees with the other two by 100%. Recency weighting
        # alone gave this a weighted dispersion ratio of 1/6 (~0.167, under
        # the 0.25 HIGH cutoff) because the deviant interval was also the
        # oldest and lowest-weighted. The unweighted ratio (~0.33) does not
        # care about position and must veto HIGH.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(1.0, None, "claude", "short", 2.0),
            _HistoryRecord(2.0, None, "claude", "short", 3.0),
            _HistoryRecord(3.0, None, "claude", "short", 4.0),
        ]

        confidence = _confidence_for_records(records)

        self.assertNotEqual(confidence, ProjectionConfidence.HIGH)

    def test_two_intervals_can_never_reach_high_confidence(self) -> None:
        # (Codex P2) Exactly two intervals with rates [1, 1.5] are a perfect
        # dispersion-ratio match (0.2, under the 0.25 cutoff) but two
        # intervals agreeing is one coincidence, not a demonstrated pattern:
        # MIN_INTERVALS_FOR_HIGH_CONFIDENCE must block HIGH regardless of how
        # low the dispersion ratio is.
        records = [
            _HistoryRecord(0.0, None, "claude", "short", 0.0),
            _HistoryRecord(1.0, None, "claude", "short", 1.0),
            _HistoryRecord(2.0, None, "claude", "short", 2.5),
        ]
        self.assertLess(len(records) - 1, MIN_INTERVALS_FOR_HIGH_CONFIDENCE)

        confidence = _confidence_for_records(records)

        self.assertNotEqual(confidence, ProjectionConfidence.HIGH)

    # -- Regression tests for the third-round Codex P2 confidence review ---

    def test_dominant_interval_diluted_across_many_samples_is_not_high_confidence(
        self,
    ) -> None:
        # (Codex P2, round 3) 500 one-second intervals: the first interval
        # alone accounts for 20 of the segment's 99.68 total usage change
        # (about 20%); the other 498 intervals each add a steady +0.16. The
        # dominant interval is now diluted across so many agreeing
        # neighbors that BOTH mean-based dispersion ratios above sit just
        # under their 0.25 HIGH cutoff (the unweighted ratio lands at
        # ~0.2485) -- proving that no mean, however weighted, can be
        # trusted to catch this: growing N is enough to launder any single
        # outlier through a mean. Only comparing the single worst interval
        # (rate 20.0) against the group's median (0.16) -- a 125x ratio --
        # catches it, which is exactly what MAX_INTERVAL_RATE_RATIO checks.
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        usage = 20.0
        records.append(_HistoryRecord(1.0, None, "claude", "short", usage))
        for index in range(2, 500):
            usage += 0.16
            records.append(
                _HistoryRecord(float(index), None, "claude", "short", usage)
            )
        self.assertEqual(len(records), 500)

        confidence = _confidence_for_records(records)

        self.assertNotEqual(confidence, ProjectionConfidence.HIGH)

    def test_genuinely_steady_series_can_still_reach_high_confidence(self) -> None:
        # The max-based veto above must not make HIGH unreachable -- if
        # nothing can ever satisfy it, the confidence metric is useless,
        # which would be a worse outcome than the bug it fixes. 100
        # intervals, each within 3% of a 1.0 %/s baseline (a fixed
        # alternating +/-3% pattern, not real randomness, so this can never
        # flake), must still clear both the mean-based and max-based checks.
        records = [_HistoryRecord(0.0, None, "claude", "short", 0.0)]
        usage = 0.0
        for index in range(1, 101):
            noise = 0.03 if index % 2 == 0 else -0.03
            usage += 1.0 + noise
            records.append(
                _HistoryRecord(float(index), None, "claude", "short", usage)
            )

        confidence = _confidence_for_records(records)

        self.assertEqual(confidence, ProjectionConfidence.HIGH)

    def test_max_interval_rate_ratio_threshold_allows_ordinary_jitter(self) -> None:
        # Sanity check on the constant's value only (mirrors
        # test_degenerate_interval_threshold_is_positive below): it must be
        # greater than 1.0 -- a ratio of exactly 1.0 would require every
        # interval to match the median exactly, which no real sampled data
        # does, making HIGH permanently unreachable. Coverage that the
        # constant is actually USED lives in the two tests above.
        self.assertGreater(MAX_INTERVAL_RATE_RATIO, 1.0)

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


if __name__ == "__main__":
    unittest.main()
