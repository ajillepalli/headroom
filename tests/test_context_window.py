"""Tests for ContextReading, its severity ladder, and hook arbitration.

End-to-end CLI surface tests (statusline write/read, cross-session hook
silence, json/doctor/status wiring) live in tests/test_cli.py's
ContextSurfaceTests, matching how burn-rate policy and surface tests are
split between test_burn_rate_policy.py and test_cli.py.
"""

from __future__ import annotations

import unittest

from headroom.bounds import Confidence, Reading
from headroom.burn_rate import BurnRateProjection
from headroom.context_window import ContextReading
from headroom.render import (
    render_context_doctor_line,
    render_context_status_lines,
    render_hook,
    render_statusline,
)
from headroom.severity import Severity, context_reading_severity, severity_for_headroom


def _reading(
    used_percent: float = 50.0,
    age_seconds: float = 0.0,
    fresh: bool = True,
    session_id: str = "session-a",
    size=None,
) -> ContextReading:
    return ContextReading(
        used_percent=used_percent,
        size=size,
        captured_at=1_000.0 - age_seconds,
        age_seconds=age_seconds,
        fresh=fresh,
        session_id=session_id,
    )


def _burn_rate_projection(
    now: float, source: str = "codex", window: str = "weekly"
) -> BurnRateProjection:
    """A projection built to clear every burn_rate_projection_is_trustworthy
    and burn_rate_evidence_is_current threshold, for a Reading built by
    _rate_reading(...) (confidence FRESH, age_seconds=0.0) at the same
    ``now`` and the same (source, window)."""

    return BurnRateProjection(
        source=source,
        window=window,
        rate_percent_per_second=0.01,
        projected_exhaustion_at=now + 100.0,
        exhaustion_precedes_reset=True,
        samples_used=10,
        span_seconds=1_000.0,
        max_relative_deviation=1.0,
        max_usage_share=0.2,
        intervals_used=6,
        rate_drift=0.1,
        effective_intervals=5.0,
        zero_delta_fraction=0.1,
        max_raw_rate_ratio=1.0,
        longest_above_overall_rate_run=1,
        latest_change_at=now,
        latest_change_delta=1.0,
        latest_captured_at=now,
    )


def _rate_reading(
    used_percent: float,
    confidence: Confidence = Confidence.FRESH,
    limit_reached: bool = False,
    source: str = "codex",
    window: str = "weekly",
) -> Reading:
    return Reading(
        certain=confidence is Confidence.FRESH,
        lower_bound_percent=used_percent,
        resets_at=None,
        age_seconds=0.0,
        window=window,
        source=source,
        confidence=confidence,
        limit_reached=limit_reached,
    )


class ContextReadingCodecTests(unittest.TestCase):
    def test_round_trips_through_to_dict_and_from_dict(self) -> None:
        stored = {
            "used_percentage": 42.0,
            "size": 1_000_000,
            "session_id": "s-1",
            "captured_at": 900.0,
            "source": "claude",
        }

        reading = ContextReading.from_dict(stored, now=1_000.0, fresh_for_seconds=300.0)

        self.assertEqual(reading.used_percent, 42.0)
        self.assertEqual(reading.size, 1_000_000)
        self.assertEqual(reading.session_id, "s-1")
        self.assertEqual(reading.age_seconds, 100.0)
        self.assertTrue(reading.fresh)
        as_dict = reading.to_dict()
        self.assertEqual(as_dict["used_percent"], 42.0)
        self.assertEqual(as_dict["fresh"], True)

    def test_stale_boundary_is_exclusive_of_the_window(self) -> None:
        stored = {"used_percentage": 1.0, "session_id": "s-1", "captured_at": 700.0}

        exactly_fresh = ContextReading.from_dict(stored, now=1_000.0, fresh_for_seconds=300.0)
        one_second_stale = ContextReading.from_dict(stored, now=1_000.1, fresh_for_seconds=300.0)

        self.assertTrue(exactly_fresh.fresh)
        self.assertFalse(one_second_stale.fresh)

    def test_clock_rollback_does_not_keep_a_stale_reading_fresh(self) -> None:
        # finding #3 (context-window adversarial review): a stored
        # captured_at=1000 read back against a rolled-back now=0 used to
        # clamp age to max(0.0, 0 - 1000) == 0.0 -- "just captured" -- so a
        # 92%-used reading stayed reported as fresh forever, instead of
        # the future-dated captured_at being recognized as unsound.
        stored = {
            "used_percentage": 92.0,
            "session_id": "s-1",
            "captured_at": 1_000.0,
        }

        with self.assertRaises((KeyError, TypeError, ValueError, OverflowError)):
            ContextReading.from_dict(stored, now=0.0, fresh_for_seconds=300.0)

    def test_ordinary_clock_skew_within_the_allowance_is_still_accepted(self) -> None:
        # The fix for finding #3 must not reject an ordinary, tiny amount
        # of clock skew between two nearby time.time() calls in the same
        # pipeline -- only a captured_at implausibly far in the future.
        stored = {
            "used_percentage": 10.0,
            "session_id": "s-1",
            "captured_at": 1_000.0,
        }

        reading = ContextReading.from_dict(stored, now=999.0, fresh_for_seconds=300.0)

        self.assertEqual(reading.age_seconds, 0.0)
        self.assertTrue(reading.fresh)

    def test_from_dict_raises_defensively_on_bad_data(self) -> None:
        cases = (
            {},  # missing everything
            {"used_percentage": "nope", "session_id": "s-1", "captured_at": 1.0},
            {"used_percentage": 1.0, "session_id": "s-1", "captured_at": "nope"},
            {"used_percentage": 1.0, "session_id": "", "captured_at": 1.0},
            {"used_percentage": 1.0, "session_id": "s-1", "captured_at": 1.0, "size": "nope"},
        )
        for stored in cases:
            with self.subTest(stored=stored):
                with self.assertRaises((KeyError, TypeError, ValueError, OverflowError)):
                    ContextReading.from_dict(stored, now=1_000.0, fresh_for_seconds=300.0)

    def test_corrupt_state_percentages_are_rejected_not_rendered(self) -> None:
        # finding #4 (context-window adversarial review): the PARSE path
        # (claude.py) already validates used_percentage into [0, 100], but
        # the DECODE path (this method) previously passed a stored value
        # straight through with no bound at all, so a hand-edited or
        # corrupted state.json could reach hook/status/json output as
        # "150% used", "nan% used", or "inf% used".
        for used_percentage in (150.0, float("nan"), float("inf"), float("-inf"), -5.0):
            with self.subTest(used_percentage=used_percentage):
                stored = {
                    "used_percentage": used_percentage,
                    "session_id": "s-1",
                    "captured_at": 900.0,
                }
                with self.assertRaises((KeyError, TypeError, ValueError, OverflowError)):
                    ContextReading.from_dict(stored, now=1_000.0, fresh_for_seconds=300.0)

    def test_lone_surrogate_session_id_is_rejected_not_decoded(self) -> None:
        # finding #9 (context-window adversarial review): a lone UTF-16
        # surrogate (reachable via JSON's \uXXXX escapes, paired or not)
        # decodes as an ordinary Python str here, then fails much later at
        # json.dumps(..., ensure_ascii=False) time, turning `headroom
        # json`'s exit code into 1. Rejecting it at decode keeps the rest
        # of the pipeline's "corrupt state collapses to silence" behavior.
        stored = {
            "used_percentage": 10.0,
            "session_id": "\ud800",
            "captured_at": 900.0,
        }
        with self.assertRaises((KeyError, TypeError, ValueError, OverflowError)):
            ContextReading.from_dict(stored, now=1_000.0, fresh_for_seconds=300.0)


class SeverityLadderTests(unittest.TestCase):
    def test_context_ladder_matches_the_rate_ladder_exactly(self) -> None:
        # CEO review: the ladder must be reused, not a differentiated copy.
        # Same headroom value, same severity, regardless of which signal --
        # checked against an ACTUAL ContextReading run through
        # context_reading_severity, not an algebraic identity. (finding
        # #12, context-window adversarial review: the original version of
        # this test compared severity_for_headroom(headroom) against
        # severity_for_headroom(100.0 - (100.0 - headroom)), which is true
        # by arithmetic alone and exercises context_reading_severity not at
        # all -- it would still pass if that function stopped calling
        # severity_for_headroom entirely.)
        for headroom in (100.0, 60.1, 60.0, 40.1, 40.0, 20.1, 20.0, 10.1, 10.0, 0.0):
            with self.subTest(headroom=headroom):
                reading = _reading(used_percent=100.0 - headroom)
                self.assertEqual(
                    context_reading_severity(reading),
                    severity_for_headroom(headroom),
                )

    def test_context_severity_thresholds(self) -> None:
        # severity_for_headroom's own boundaries (headroom = 100 - used):
        # headroom > 40 -> OK, >= 20 -> NOTICE, >= 10 -> WARN, else CRITICAL.
        # In used-percentage terms that is OK < 60, NOTICE [60, 80], WARN
        # (80, 90], CRITICAL > 90.
        cases = (
            (10.0, Severity.OK),
            (59.9, Severity.OK),
            (60.0, Severity.NOTICE),
            (80.0, Severity.NOTICE),
            (80.1, Severity.WARN),
            (90.0, Severity.WARN),
            (90.1, Severity.CRITICAL),
            (99.0, Severity.CRITICAL),
        )
        for used, expected in cases:
            with self.subTest(used=used):
                self.assertEqual(context_reading_severity(_reading(used_percent=used)), expected)

    def test_absent_or_stale_reading_is_ok(self) -> None:
        self.assertEqual(context_reading_severity(None), Severity.OK)
        self.assertEqual(
            context_reading_severity(_reading(used_percent=99.0, fresh=False)),
            Severity.OK,
        )


class HookWordBanTests(unittest.TestCase):
    """Assert the plan's ban: never instruct the model to compact.

    "may compact without warning" is descriptive (explains why to act) and
    is explicitly allowed; the ban is on an imperative telling the model to
    invoke compaction, which it cannot do.
    """

    def test_no_advice_instructs_compaction(self) -> None:
        from headroom.render import _CONTEXT_ADVICE

        for severity, template in _CONTEXT_ADVICE.items():
            with self.subTest(severity=severity):
                text = template.format(80)
                lowered = text.lower()
                if "compact" in lowered:
                    self.assertIn("may compact without warning", lowered)
                self.assertNotIn("run /compact", lowered)
                self.assertNotIn("invoke compact", lowered)
                self.assertNotIn("please compact", lowered)

    def test_rendered_hook_text_never_instructs_compaction(self) -> None:
        for severity, used in ((Severity.NOTICE, 65.0), (Severity.WARN, 85.0), (Severity.CRITICAL, 95.0)):
            with self.subTest(severity=severity):
                text = render_hook([], now=1_000.0, context=_reading(used_percent=used))
                lowered = text.lower()
                # Every occurrence of "compact" must be the one allowed,
                # purely descriptive phrase; anything else is an imperative
                # this test exists to catch (e.g. "please compact now").
                remainder = lowered
                while "compact" in remainder:
                    index = remainder.index("compact")
                    window = remainder[max(0, index - 5) : index + 30]
                    self.assertIn("may compact without warning", window)
                    remainder = remainder[index + len("compact") :]


class HookArbitrationTests(unittest.TestCase):
    """The four combinations from the plan's arbitration section, plus the
    tie-break and trailing-clause rules layered on top of them."""

    def test_both_ok_or_absent_is_silent(self) -> None:
        readings = [_rate_reading(10.0)]  # headroom 90 -> OK
        self.assertEqual(render_hook(readings, now=1_000.0, context=None), "")
        self.assertEqual(
            render_hook(readings, now=1_000.0, context=_reading(used_percent=10.0)), ""
        )

    def test_only_rate_above_ok_renders_unchanged(self) -> None:
        readings = [_rate_reading(85.0)]  # headroom 15 -> WARN
        text = render_hook(readings, now=1_000.0, context=None)
        self.assertIn("Usage headroom:", text)
        self.assertNotIn("Context", text)

    def test_only_context_above_ok_renders_as_primary_block(self) -> None:
        text = render_hook([], now=1_000.0, context=_reading(used_percent=85.0))  # WARN
        self.assertIn("Context is high", text)
        self.assertNotIn("Usage headroom:", text)

    def test_both_above_ok_rate_critical_wins_over_context_warn(self) -> None:
        readings = [_rate_reading(95.0)]  # CRITICAL
        text = render_hook(readings, now=1_000.0, context=_reading(used_percent=85.0))  # WARN
        self.assertIn("Usage headroom:", text)
        self.assertIn("Context is also at warn", text)

    def test_tie_goes_to_rate(self) -> None:
        # Both WARN (rate headroom 15 -> WARN; context 85% used -> WARN).
        readings = [_rate_reading(85.0)]
        text = render_hook(readings, now=1_000.0, context=_reading(used_percent=85.0))
        self.assertTrue(text.startswith("Usage headroom:"))
        self.assertIn("Context is also at warn", text)

    def test_context_strictly_outranks_lower_rate_severity(self) -> None:
        # rate NOTICE, context CRITICAL -> context wins outright.
        readings = [_rate_reading(65.0)]  # headroom 35 -> NOTICE
        text = render_hook(readings, now=1_000.0, context=_reading(used_percent=95.0))
        self.assertTrue(text.startswith("Context is nearly full"))
        # NOTICE loser is dropped per rule 5, not appended as a trailing clause.
        self.assertNotIn("also at", text.lower())

    def test_notice_loser_is_dropped_not_appended(self) -> None:
        # rate WARN wins, context NOTICE loses -> no trailing clause at all.
        readings = [_rate_reading(85.0)]  # WARN
        text = render_hook(readings, now=1_000.0, context=_reading(used_percent=65.0))  # NOTICE
        self.assertIn("Usage headroom:", text)
        self.assertNotIn("Context", text)

    def test_critical_rate_line_never_omitted_for_context(self) -> None:
        readings = [_rate_reading(99.0)]  # CRITICAL
        text = render_hook(readings, now=1_000.0, context=_reading(used_percent=95.0))  # CRITICAL
        self.assertTrue(text.startswith("Usage headroom:"))

    def test_critical_rate_alone_still_suppresses_burn_rate_not_context(self) -> None:
        # Burn-rate stays suppressed on a CRITICAL rate line (existing
        # rule); the trailing context clause is still allowed alongside it.
        # finding #13 (context-window adversarial review): the original
        # version of this test passed no projection at all, so "Burn rate:"
        # not in text was true regardless of whether the suppression rule
        # existed -- it exercised nothing. This supplies a projection that
        # clears every burn_rate_projection_is_trustworthy and
        # burn_rate_evidence_is_current threshold, first confirming (on an
        # OK rate reading) that it WOULD render on its own, then checking
        # that a CRITICAL rate line suppresses it anyway.
        now = 1_000.0
        projection = _burn_rate_projection(now)

        sanity_text = render_hook([_rate_reading(10.0)], now=now, projections=[projection])
        self.assertIn("Burn rate:", sanity_text)

        readings = [_rate_reading(99.0)]
        text = render_hook(
            readings, now=now, projections=[projection], context=_reading(used_percent=85.0)
        )
        self.assertNotIn("Burn rate:", text)
        self.assertIn("Context is also at warn", text)


class StatuslineContextSegmentTests(unittest.TestCase):
    def test_ok_context_adds_no_segment(self) -> None:
        text = render_statusline([], now=1_000.0, context=_reading(used_percent=10.0))
        self.assertEqual(text, "headroom: usage unavailable")

    def test_non_ok_context_appends_a_segment(self) -> None:
        text = render_statusline([], now=1_000.0, context=_reading(used_percent=85.0))
        self.assertIn("ctx 85% [warn]", text)

    def test_stale_context_never_appends_a_segment(self) -> None:
        text = render_statusline(
            [], now=1_000.0, context=_reading(used_percent=95.0, fresh=False)
        )
        self.assertEqual(text, "headroom: usage unavailable")


class StatusAndDoctorRenderTests(unittest.TestCase):
    def test_status_lines_report_every_fresh_session(self) -> None:
        lines = render_context_status_lines(
            [_reading(used_percent=85.0, session_id="aaaaaaaa-1"), _reading(used_percent=10.0, session_id="bbbbbbbb-2")],
            now=1_000.0,
        )
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("warn" in line for line in lines))
        self.assertTrue(any("ok" in line for line in lines))

    def test_status_lines_unavailable_when_nothing_fresh(self) -> None:
        self.assertEqual(render_context_status_lines([], now=1_000.0), ["  unavailable"])

    def test_doctor_line_three_states(self) -> None:
        self.assertEqual(
            render_context_doctor_line(None, fresh_for_seconds=300.0),
            "Claude context: not available (no session_id in last statusline payload)",
        )
        stale = _reading(used_percent=50.0, age_seconds=400.0, fresh=False)
        self.assertIn("stale (last capture", render_context_doctor_line(stale, fresh_for_seconds=300.0))
        ok = _reading(used_percent=42.0)
        self.assertEqual(
            render_context_doctor_line(ok, fresh_for_seconds=300.0),
            "Claude context: ok (42% used, below notice threshold)",
        )


if __name__ == "__main__":
    unittest.main()
