"""Tests for burn-rate fitting and exhaustion projections."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from headroom.burn_rate import (
    MIN_SPAN_TO_HORIZON_RATIO,
    MIN_SAMPLES,
    BurnRateProjection,
    NoProjectionReason,
    ProjectionConfidence,
    project_exhaustion,
)


class BurnRateTests(unittest.TestCase):
    def _project(
        self, records: list[dict[str, object]], malformed: str = ""
    ) -> list[BurnRateProjection]:
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.jsonl"
            lines = [json.dumps(record, separators=(",", ":")) for record in records]
            if malformed:
                lines.append(malformed)
            history_path.write_text("\n".join(lines), encoding="utf-8")
            return project_exhaustion(history_path, now=1_000_000.0)

    @staticmethod
    def _record(
        captured_at: float,
        used_percentage: float,
        resets_at: float = 1_000.0,
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
                self._record(1_890.0, 13.1, resets_at=604_800.0, window="weekly"),
                self._record(3_780.0, 11.55, resets_at=604_800.0, window="weekly"),
                self._record(5_670.0, 14.65, resets_at=604_800.0, window="weekly"),
                self._record(7_560.0, 12.325, resets_at=604_800.0, window="weekly"),
            ]
        )[0]

        self.assertEqual(projection.span_seconds, 2.1 * 60.0 * 60.0)
        self.assertIsNotNone(projection.rate_percent_per_second)
        horizon_seconds = (100.0 - 12.325) / (
            projection.rate_percent_per_second or 1.0
        )
        self.assertAlmostEqual(horizon_seconds / 86_400.0, 2.9, delta=0.1)
        self.assertGreater(horizon_seconds / projection.span_seconds, 30.0)
        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertIsNone(projection.exhaustion_precedes_reset)
        self.assertEqual(projection.confidence, ProjectionConfidence.LOW)
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
        projection = self._project(
            [
                self._record(0.0, 10.0, resets_at=20_000.0),
                self._record(1_800.0, 30.0, resets_at=20_000.0),
                self._record(3_600.0, 20.0, resets_at=20_000.0),
                self._record(5_400.0, 40.0, resets_at=20_000.0),
                self._record(7_200.0, 25.0, resets_at=20_000.0),
            ]
        )[0]

        self.assertEqual(projection.confidence, ProjectionConfidence.LOW)
        self.assertIsNone(projection.projected_exhaustion_at)
        self.assertIsNone(projection.exhaustion_precedes_reset)
        self.assertEqual(projection.reason, NoProjectionReason.LOW_CONFIDENCE)

    def test_usage_that_went_backwards_yields_no_projection(self) -> None:
        projection = self._project(
            [
                self._record(0.0, 40.0),
                self._record(60.0, 39.0),
                self._record(120.0, 38.0),
            ]
        )[0]

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

    def test_one_wild_outlier_does_not_dominate_fit(self) -> None:
        projection = self._project(
            [
                self._record(0.0, 10.0, resets_at=1_000.0),
                self._record(60.0, 20.0, resets_at=1_000.0),
                self._record(120.0, 95.0, resets_at=1_000.0),
                self._record(180.0, 40.0, resets_at=1_000.0),
                self._record(240.0, 50.0, resets_at=1_000.0),
            ]
        )[0]

        self.assertAlmostEqual(
            projection.rate_percent_per_second or 0.0, 1.0 / 6.0
        )
        self.assertAlmostEqual(projection.projected_exhaustion_at or 0.0, 540.0)
        self.assertTrue(projection.exhaustion_precedes_reset)

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


if __name__ == "__main__":
    unittest.main()
