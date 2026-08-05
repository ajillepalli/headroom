"""Pure severity calculation for bounded readings."""

from enum import IntEnum
from typing import Optional

from .bounds import Confidence, Reading
from .burn_rate import BurnRateProjection


ESCALATE_BELOW_HEADROOM = 50.0


class Severity(IntEnum):
    """Display and notification severity in ascending order."""

    OK = 0
    NOTICE = 1
    WARN = 2
    CRITICAL = 3

    def __str__(self) -> str:
        """Return the lower-case display name."""

        return self.name.lower()


def reading_severity(reading: Reading) -> Severity:
    """Map a bounded reading to its display severity."""

    if reading.confidence is Confidence.POST_RESET:
        return Severity.OK
    if reading.limit_reached:
        return Severity.CRITICAL
    used = reading.lower_bound_percent
    if used is None:
        return Severity.OK

    headroom = 100.0 - used
    if headroom > 40.0:
        severity = Severity.OK
    elif headroom >= 20.0:
        severity = Severity.NOTICE
    elif headroom >= 10.0:
        severity = Severity.WARN
    else:
        severity = Severity.CRITICAL

    # Escalation exists because the true value can only be worse than the
    # bound, but with most of the quota untouched no plausible burn rate
    # crosses a threshold before the next reading.
    if (
        reading.confidence is Confidence.STALE_BOUNDED
        and headroom < ESCALATE_BELOW_HEADROOM
    ):
        severity = Severity(min(int(severity) + 1, int(Severity.CRITICAL)))
    return severity


# --- Burn-rate projection speaking policy ---
#
# burn_rate.py deliberately reports MEASUREMENTS and takes no position on
# whether a given projection is trustworthy enough to act on. That was the
# outcome of ten adversarial review rounds: every attempt to bake a
# HIGH/MEDIUM/LOW threshold into the library was defeated, because a hard
# cutoff on a continuous quantity can always be approached from just
# underneath (see BurnRateProjection's docstring for the specific defeats).
# The library's job is to report; whether a caller should SPEAK about what
# it reports is a policy question, and the answer legitimately differs by
# caller (a status line and an automated cutoff switch tolerate very
# different risk). So that decision lives here, in the application layer
# next to the existing severity ladder, not in burn_rate.py.
#
# Each constant below is justified by the claim a spoken warning makes to a
# human or a model -- "the recent rate predicts when this window will run
# out" -- not by a target firing rate. A false "you will run out" is worse
# than silence, because it makes the model degrade its own behavior for no
# reason, so every constant is chosen conservatively rather than tuned to
# make the warning fire more often.
#
# Every threshold here was set by scanning this project's own
# ~/.headroom/history.jsonl at each of its historical capture timestamps
# (2,409 distinct timestamps, 58 successful projections total) and reading
# off where the real distribution of each measurement actually falls, not
# by picking a round number and hoping. A first pass through this exercise
# (round 1 of review) picked constants from first principles alone and, when
# checked against the same history, turned out to reject all 58 -- a policy
# that never fires on any real data it will ever see is exactly as broken as
# one that always fires, because either way the trust decision is not doing
# any work. The fix below is not "loosen everything until something
# passes"; it is reading the real distribution once, finding where it is
# and is not naturally bimodal, and setting each constant at the gap.

# max_relative_deviation: how far the single worst interval's rate sat from
# the segment's median rate. A warning claims "the recent rate" is one
# number worth extrapolating; if any one interval ran wildly different from
# the middle of the pack, "the recent rate" is really a wide range. Real
# data is sharply bimodal here: 42 of the 58 successful projections cluster
# tightly between 0.98 and 1.22 (the worst interval running at up to ~2.2x
# the median -- ordinary pacing variation across a human-paced session,
# where a few tool calls happen close together and then there is a pause),
# and the other 16 jump to 8.49-17.39 (a single interval running an order of
# magnitude off the rest -- a categorically different failure, not more of
# the same variation). 1.5 sits in that gap: comfortable margin above the
# ordinary cluster's ceiling (1.22) and nowhere near the anomalous cluster's
# floor (8.49).
MAX_TRUSTED_RELATIVE_DEVIATION = 1.5

# max_usage_share: the largest fraction of total usage attributed to any one
# interval. A projection resting mostly on one interval is a projection
# about that interval, not about a trend. Capping this at a third means no
# single interval can be more than half again as large as an even split
# across three, so at least three intervals meaningfully contributed. Real
# data never approached this cap on its own (observed range 0.14-0.33), so
# it was left at its original, structurally-justified value rather than
# refitted to a distribution it was already comfortably inside of.
MAX_TRUSTED_USAGE_SHARE = 1.0 / 3.0

# intervals_used: how many independent (post-folding) intervals the
# measurements above were computed from. burn_rate.py's own floor
# (MIN_SAMPLES = 3 samples, i.e. 2 intervals) is enough for a projection to
# exist at all, but deviation and drift computed from 2 intervals describe a
# coin flip, not a pattern. Five is the smallest count where "the worst
# interval deviated by more than X" and "the second half drifted from the
# first half" are statements about more than the last two points. Real data
# was always at or above this floor (observed range 5-11), so it too was
# left unchanged.
MIN_TRUSTED_INTERVALS = 5

# rate_drift: the relative difference between the segment's early-half and
# late-half time-weighted rate. A projection extrapolates the CURRENT rate
# forward in a straight line; if the rate already looks very different in
# the second half of the observed window than the first, "the current rate"
# is itself still moving, and a straight-line extrapolation overstates how
# confidently it lands where it says it will. Real data clusters from 0.19
# to 0.41 (54 of 58 values), then jumps to 0.69 (the remaining 4) -- the
# single largest gap in the sorted distribution. 0.45 sits in that gap,
# admitting the ordinary cluster and rejecting the outliers.
MAX_TRUSTED_RATE_DRIFT = 0.45

# max_raw_rate_ratio and zero_delta_fraction are deliberately NOT part of
# this policy, even though burn_rate.py reports them specifically to catch
# what the folded fields above can smooth away (see BurnRateProjection's
# docstring). They were part of an earlier version of this policy and were
# removed after checking that version against real history: max_raw_rate_ratio
# was 69-566 on EVERY ONE of the 58 successful real projections, ~20x to
# ~190x past any threshold that would still mean anything, and the reason is
# structural, not a property of this account's usage being unusually bursty.
# max_raw_rate_ratio compares one RAW (unfolded) inter-capture gap's rate to
# the segment's overall rate; headroom's own captures are quantized to whole
# percentage points (see burn_rate.py's docstring) and, within a session, are
# often only seconds apart. Any time a real percentage-point change happens,
# it lands entirely within one sub-minute raw gap while its neighbors show
# zero change -- so that gap's raw rate is enormous relative to the session
# average BY CONSTRUCTION, whether the underlying usage is bursty or
# perfectly smooth. The metric cannot tell "this account's traffic is
# spiky" from "headroom samples much faster than a whole point of usage
# accrues" for data shaped like this project's own -- it saturates on both.
# That is a different failure from max_relative_deviation's (which measures
# something a real bimodal gap in this same data shows IS discriminating):
# no threshold rescues a measurement that is structurally uninformative for
# the input it is being asked to judge. Both fields remain fully reported in
# `json` and `doctor` (nothing here removes them from the data a caller can
# inspect), and remain available to a caller building a different policy for
# input with a different (coarser, evenly-spaced) capture cadence than
# headroom's own.


def burn_rate_projection_is_trustworthy(projection: BurnRateProjection) -> bool:
    """Whether a projection's own measurements clear the bar to speak about.

    This is the policy question burn_rate.py deliberately does not answer
    (see the module comment above). A declined projection (``reason`` is
    set) is never trustworthy by definition -- there is no projection to
    speak about. Every threshold is a maximum or minimum a caller can meet;
    a projection must clear all four to be shown, matching the "be
    conservative" instruction this policy was built against.
    """

    if (
        projection.reason is not None
        or projection.max_relative_deviation is None
        or projection.max_usage_share is None
        or projection.intervals_used is None
        or projection.rate_drift is None
    ):
        return False
    return (
        projection.max_relative_deviation <= MAX_TRUSTED_RELATIVE_DEVIATION
        and projection.max_usage_share <= MAX_TRUSTED_USAGE_SHARE
        and projection.intervals_used >= MIN_TRUSTED_INTERVALS
        and projection.rate_drift <= MAX_TRUSTED_RATE_DRIFT
    )


def burn_rate_evidence_is_current(
    projection: BurnRateProjection, reading: Optional[Reading]
) -> bool:
    """Whether there is currently fresh evidence backing this projection's
    trend, not just an internally consistent historical fit.

    ``burn_rate_projection_is_trustworthy`` asks whether the projection's own
    measurements are internally consistent; it says nothing about how long
    ago the sample they were built from was captured. A source that stops
    reporting mid-window can leave behind a perfectly steady, low-deviation
    historical trend -- every one of the five thresholds above can pass on
    data nothing has confirmed in hours. Extrapolating that forward and
    telling the model "you are on pace to run out" would be presenting a
    stale extrapolation as a live warning (Codex review, round 1, P2).

    This is a second, independent gate for exactly that reason, rather than
    folded into the five-threshold check above: it answers a different
    question ("is this still happening") using different evidence (the
    CURRENT reading for the same exact source and window, not the
    projection's own history), and reuses the existing freshness machinery
    (``bounds.Confidence``, ``freshness.py``) instead of inventing a second,
    parallel staleness threshold. A missing reading (no current data for
    this source and window at all) or anything less current than
    ``Confidence.FRESH`` fails this gate, regardless of how clean the
    projection's own fit looks.
    """

    return reading is not None and reading.confidence is Confidence.FRESH
