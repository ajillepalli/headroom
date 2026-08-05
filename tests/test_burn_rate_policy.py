"""Tests for the burn-rate speaking policy and its rendering surfaces.

burn_rate.py itself reports measurements and takes no position on whether a
projection is trustworthy enough to act on (see BurnRateProjection's
docstring). Whether to SPEAK about a projection is a policy question that
lives in severity.py (``burn_rate_projection_is_trustworthy``) and is
consumed by render.py's doctor/status/hook renderers. These tests exercise
that policy and its three consuming renderers directly, with hand-built
BurnRateProjection objects, so every threshold and every tri-state branch is
pinned down precisely rather than relying on end-to-end history fixtures.
"""

from __future__ import annotations

from typing import Optional
import unittest

from headroom.bounds import Confidence, Reading
from headroom.burn_rate import BurnRateProjection, NoProjectionReason
from headroom.render import (
    render_burn_rate_doctor_lines,
    render_burn_rate_status_lines,
    render_hook,
)
from headroom.severity import (
    MAX_TRUSTED_RATE_DRIFT,
    MAX_TRUSTED_RAW_RATE_RATIO,
    MAX_TRUSTED_RELATIVE_DEVIATION,
    MAX_TRUSTED_USAGE_SHARE,
    MIN_TRUSTED_INTERVALS,
    Severity,
    burn_rate_projection_is_trustworthy,
)


def _projection(
    *,
    reason: Optional[NoProjectionReason] = None,
    max_relative_deviation: float = 0.1,
    max_usage_share: float = 0.2,
    intervals_used: int = 6,
    rate_drift: float = 0.05,
    max_raw_rate_ratio: float = 1.5,
    exhaustion_precedes_reset: Optional[bool] = True,
    projected_exhaustion_at: Optional[float] = 2_000.0,
    source: str = "claude",
    window: str = "short",
    rate_percent_per_second: Optional[float] = 0.01,
    samples_used: int = 7,
    span_seconds: Optional[float] = 600.0,
    effective_intervals: Optional[float] = 5.5,
    zero_delta_fraction: Optional[float] = 0.1,
    longest_above_overall_rate_run: Optional[int] = 1,
) -> BurnRateProjection:
    """Build a projection that clears every trust threshold with margin by
    default, so each test only needs to override the one field it targets.
    """

    declined = reason is not None
    return BurnRateProjection(
        source=source,
        window=window,
        rate_percent_per_second=rate_percent_per_second,
        projected_exhaustion_at=None if declined else projected_exhaustion_at,
        exhaustion_precedes_reset=None if declined else exhaustion_precedes_reset,
        samples_used=samples_used,
        span_seconds=span_seconds,
        reason=reason,
        max_relative_deviation=None if declined else max_relative_deviation,
        max_usage_share=None if declined else max_usage_share,
        intervals_used=None if declined else intervals_used,
        rate_drift=None if declined else rate_drift,
        effective_intervals=None if declined else effective_intervals,
        zero_delta_fraction=None if declined else zero_delta_fraction,
        max_raw_rate_ratio=None if declined else max_raw_rate_ratio,
        longest_above_overall_rate_run=None if declined else longest_above_overall_rate_run,
    )


class TrustworthyPolicyTests(unittest.TestCase):
    def test_default_projection_clears_every_threshold(self) -> None:
        self.assertTrue(burn_rate_projection_is_trustworthy(_projection()))

    def test_declined_projection_is_never_trustworthy(self) -> None:
        self.assertFalse(
            burn_rate_projection_is_trustworthy(
                _projection(reason=NoProjectionReason.FLAT_USAGE)
            )
        )

    def test_reason_guard_overrides_populated_measurement_fields(self) -> None:
        # An unrealistic but adversarial construction: reason is set (so this
        # is, by definition, a declined projection) yet every measurement
        # field is populated at a maximally trustworthy value. This pins
        # down that the policy checks `reason is not None` explicitly,
        # rather than only inferring "declined" from the fields being None.
        adversarial = BurnRateProjection(
            source="claude",
            window="short",
            rate_percent_per_second=0.01,
            projected_exhaustion_at=None,
            exhaustion_precedes_reset=None,
            samples_used=10,
            span_seconds=600.0,
            reason=NoProjectionReason.FLAT_USAGE,
            max_relative_deviation=0.0,
            max_usage_share=0.1,
            intervals_used=10,
            rate_drift=0.0,
            effective_intervals=10.0,
            zero_delta_fraction=0.0,
            max_raw_rate_ratio=1.0,
            longest_above_overall_rate_run=1,
        )
        self.assertFalse(burn_rate_projection_is_trustworthy(adversarial))

    def test_max_relative_deviation_boundary(self) -> None:
        at_boundary = _projection(max_relative_deviation=MAX_TRUSTED_RELATIVE_DEVIATION)
        just_over = _projection(
            max_relative_deviation=MAX_TRUSTED_RELATIVE_DEVIATION + 0.001
        )
        self.assertTrue(burn_rate_projection_is_trustworthy(at_boundary))
        self.assertFalse(burn_rate_projection_is_trustworthy(just_over))

    def test_max_usage_share_boundary(self) -> None:
        at_boundary = _projection(max_usage_share=MAX_TRUSTED_USAGE_SHARE)
        just_over = _projection(max_usage_share=MAX_TRUSTED_USAGE_SHARE + 0.001)
        self.assertTrue(burn_rate_projection_is_trustworthy(at_boundary))
        self.assertFalse(burn_rate_projection_is_trustworthy(just_over))

    def test_min_intervals_used_boundary(self) -> None:
        at_boundary = _projection(intervals_used=MIN_TRUSTED_INTERVALS)
        just_under = _projection(intervals_used=MIN_TRUSTED_INTERVALS - 1)
        self.assertTrue(burn_rate_projection_is_trustworthy(at_boundary))
        self.assertFalse(burn_rate_projection_is_trustworthy(just_under))

    def test_max_rate_drift_boundary(self) -> None:
        at_boundary = _projection(rate_drift=MAX_TRUSTED_RATE_DRIFT)
        just_over = _projection(rate_drift=MAX_TRUSTED_RATE_DRIFT + 0.001)
        self.assertTrue(burn_rate_projection_is_trustworthy(at_boundary))
        self.assertFalse(burn_rate_projection_is_trustworthy(just_over))

    def test_max_raw_rate_ratio_boundary(self) -> None:
        at_boundary = _projection(max_raw_rate_ratio=MAX_TRUSTED_RAW_RATE_RATIO)
        just_over = _projection(max_raw_rate_ratio=MAX_TRUSTED_RAW_RATE_RATIO + 0.001)
        self.assertTrue(burn_rate_projection_is_trustworthy(at_boundary))
        self.assertFalse(burn_rate_projection_is_trustworthy(just_over))


class RenderBurnRateDoctorTests(unittest.TestCase):
    def test_declined_projection_shows_plain_language_not_raw_enum_name(self) -> None:
        projection = _projection(reason=NoProjectionReason.TOO_FEW_SAMPLES)
        lines = render_burn_rate_doctor_lines([projection], now=1_000.0)

        self.assertEqual(len(lines), 1)
        self.assertIn("not enough usage samples recorded yet", lines[0])
        self.assertNotIn("TOO_FEW_SAMPLES", lines[0])
        self.assertNotIn("too_few_samples", lines[0])

    def test_every_decline_reason_has_plain_language_text(self) -> None:
        for reason in NoProjectionReason:
            with self.subTest(reason=reason):
                projection = _projection(reason=reason)
                lines = render_burn_rate_doctor_lines([projection], now=1_000.0)
                self.assertEqual(len(lines), 1)
                self.assertNotIn(reason.name, lines[0])
                self.assertNotIn(reason.value, lines[0])

    def test_successful_projection_reports_measurements_and_tri_state(self) -> None:
        for precedes, phrase in ((True, "before reset"), (False, "after reset"), (None, "reset time unknown")):
            with self.subTest(precedes=precedes):
                projection = _projection(exhaustion_precedes_reset=precedes)
                lines = render_burn_rate_doctor_lines([projection], now=1_000.0)
                self.assertEqual(len(lines), 1)
                self.assertIn(phrase, lines[0])
                self.assertIn("deviation", lines[0])
                self.assertIn("usage share", lines[0])
                self.assertIn("raw rate ratio", lines[0])


class RenderBurnRateStatusTests(unittest.TestCase):
    def test_trustworthy_projection_produces_a_line(self) -> None:
        projection = _projection(source="codex", window="weekly")
        lines = render_burn_rate_status_lines([projection], now=1_000.0)

        self.assertEqual(len(lines), 1)
        self.assertIn("Codex", lines[0])
        self.assertIn("burn rate", lines[0])

    def test_declined_projection_says_nothing(self) -> None:
        projection = _projection(reason=NoProjectionReason.SPAN_TOO_SHORT)
        lines = render_burn_rate_status_lines([projection], now=1_000.0)

        self.assertEqual(lines, [])

    def test_present_but_untrustworthy_projection_says_nothing(self) -> None:
        # reason is None (a projection genuinely exists) but the deviation
        # is far outside the trust bar -- status must stay silent exactly
        # like the declined case, and must not leak the structural reason
        # (there isn't one) or any raw measurement.
        projection = _projection(max_relative_deviation=50.0)
        lines = render_burn_rate_status_lines([projection], now=1_000.0)

        self.assertEqual(lines, [])


class RenderHookCompositionTests(unittest.TestCase):
    def _reading(
        self,
        *,
        lower_bound_percent: Optional[float],
        source: str = "codex",
        window: str = "weekly",
        confidence: Confidence = Confidence.FRESH,
        resets_at: Optional[float] = 100_000.0,
    ) -> Reading:
        return Reading(
            certain=confidence is Confidence.FRESH,
            lower_bound_percent=lower_bound_percent,
            resets_at=resets_at,
            age_seconds=10.0,
            window=window,
            source=source,
            confidence=confidence,
        )

    def test_silent_when_nothing_actionable_and_no_projections(self) -> None:
        readings = [self._reading(lower_bound_percent=3.0)]
        self.assertEqual(render_hook(readings, now=1_000.0), "")

    def test_burn_only_speaks_without_a_usage_headroom_line(self) -> None:
        readings = [self._reading(lower_bound_percent=3.0)]
        projection = _projection()
        text = render_hook(readings, now=1_000.0, projections=[projection])

        self.assertNotEqual(text, "")
        self.assertNotIn("Usage headroom:", text)
        self.assertIn("Burn rate:", text)
        self.assertIn("before its reset", text)

    def test_notice_or_warn_severity_composes_with_burn_warning(self) -> None:
        # 15% headroom -> WARN (see severity.py's ladder).
        readings = [self._reading(lower_bound_percent=85.0)]
        projection = _projection()
        text = render_hook(readings, now=1_000.0, projections=[projection])

        self.assertIn("Usage headroom:", text)
        self.assertIn("Burn rate:", text)

    def test_critical_rate_limit_suppresses_burn_warning(self) -> None:
        # <10% headroom -> CRITICAL.
        readings = [self._reading(lower_bound_percent=95.0)]
        projection = _projection()
        text = render_hook(readings, now=1_000.0, projections=[projection])

        self.assertIn("Usage headroom:", text)
        self.assertNotIn("Burn rate:", text)

    def test_exhaustion_after_reset_is_never_spoken_by_hook(self) -> None:
        readings = [self._reading(lower_bound_percent=3.0)]
        projection = _projection(exhaustion_precedes_reset=False)
        text = render_hook(readings, now=1_000.0, projections=[projection])

        self.assertEqual(text, "")

    def test_exhaustion_with_unknown_reset_is_never_spoken_by_hook(self) -> None:
        readings = [self._reading(lower_bound_percent=3.0)]
        projection = _projection(exhaustion_precedes_reset=None)
        text = render_hook(readings, now=1_000.0, projections=[projection])

        self.assertEqual(text, "")

    def test_untrustworthy_projection_is_not_spoken_even_when_precedes_reset(self) -> None:
        readings = [self._reading(lower_bound_percent=3.0)]
        projection = _projection(max_raw_rate_ratio=1_000.0)
        text = render_hook(readings, now=1_000.0, projections=[projection])

        self.assertEqual(text, "")

    def test_forced_severity_isolates_from_burn_rate_composition(self) -> None:
        readings = [self._reading(lower_bound_percent=3.0)]
        projection = _projection()
        text = render_hook(
            readings, now=1_000.0, projections=[projection], forced_severity=Severity.WARN
        )

        self.assertIn("FORCED TEST (warn)", text)
        self.assertNotIn("Burn rate:", text)

    def test_earliest_of_several_trustworthy_warnings_is_selected(self) -> None:
        readings = [self._reading(lower_bound_percent=3.0)]
        soon = _projection(source="claude", window="short", projected_exhaustion_at=1_100.0)
        later = _projection(source="codex", window="weekly", projected_exhaustion_at=5_000.0)
        text = render_hook(readings, now=1_000.0, projections=[later, soon])

        self.assertIn("Claude", text)
        self.assertNotIn("Codex", text)

    def test_composed_hook_output_stays_within_word_budget(self) -> None:
        readings = [self._reading(lower_bound_percent=85.0)]
        projection = _projection()
        text = render_hook(readings, now=1_000.0, projections=[projection])

        word_count = len(text.split())
        self.assertLessEqual(word_count, 70, text)


if __name__ == "__main__":
    unittest.main()
