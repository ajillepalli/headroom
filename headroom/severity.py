"""Pure severity calculation for bounded readings."""

from enum import IntEnum

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
# make the warning fire more often. Verified against this project's own
# ~/.headroom/history.jsonl: real captures round used_percentage to whole
# points, so every real segment that manages to produce a projection at all
# is bursty by construction (max_relative_deviation was never below ~0.98
# and max_raw_rate_ratio was never below ~69 across the full history), and
# the policy below declines to speak about any of them. That is the correct,
# conservative outcome, not a bug: this machine's data has never actually
# supported a steady-rate claim.

# max_relative_deviation: how far the single worst interval's rate sat from
# the segment's median rate. A warning claims "the recent rate" is one
# number worth extrapolating; if any one interval ran more than 50% off the
# middle of the pack, "the recent rate" is really a wide range, and the
# extrapolation could just as easily be describing a burst.
MAX_TRUSTED_RELATIVE_DEVIATION = 0.5

# max_usage_share: the largest fraction of total usage attributed to any one
# interval. A projection resting mostly on one interval is a projection
# about that interval, not about a trend. Capping this at a third means no
# single interval can be more than half again as large as an even split
# across three, so at least three intervals meaningfully contributed.
MAX_TRUSTED_USAGE_SHARE = 1.0 / 3.0

# intervals_used: how many independent (post-folding) intervals the
# measurements above were computed from. burn_rate.py's own floor
# (MIN_SAMPLES = 3 samples, i.e. 2 intervals) is enough for a projection to
# exist at all, but deviation and drift computed from 2 intervals describe a
# coin flip, not a pattern. Five is the smallest count where "the worst
# interval deviated by more than X" and "the second half drifted from the
# first half" are statements about more than the last two points.
MIN_TRUSTED_INTERVALS = 5

# rate_drift: the relative difference between the segment's early-half and
# late-half time-weighted rate. A projection extrapolates the CURRENT rate
# forward in a straight line; if the rate already looks 25%+ different in
# the second half of the observed window than the first, "the current rate"
# is itself still moving, and a straight-line extrapolation overstates how
# confidently it lands where it says it will.
MAX_TRUSTED_RATE_DRIFT = 0.25

# max_raw_rate_ratio: the fastest single RAW interval's rate divided by the
# segment's overall (start-to-finish) rate -- the one measurement folding
# cannot smooth away (see BurnRateProjection's docstring on why a folded
# view alone can hide a burst diluted into its flat neighbors). A warning
# claims the recent PACE will continue; if some single raw gap ran at more
# than 3x the segment's average pace, part of the evidence is a burst, not a
# pace, no matter how steady the folded view looks.
MAX_TRUSTED_RAW_RATE_RATIO = 3.0


def burn_rate_projection_is_trustworthy(projection: BurnRateProjection) -> bool:
    """Whether a projection's own measurements clear the bar to speak about.

    This is the policy question burn_rate.py deliberately does not answer
    (see the module comment above). A declined projection (``reason`` is
    set) is never trustworthy by definition -- there is no projection to
    speak about. Every threshold is a maximum or minimum a caller can meet;
    a projection must clear all five to be shown, matching the "be
    conservative" instruction this policy was built against.
    """

    if (
        projection.reason is not None
        or projection.max_relative_deviation is None
        or projection.max_usage_share is None
        or projection.intervals_used is None
        or projection.rate_drift is None
        or projection.max_raw_rate_ratio is None
    ):
        return False
    return (
        projection.max_relative_deviation <= MAX_TRUSTED_RELATIVE_DEVIATION
        and projection.max_usage_share <= MAX_TRUSTED_USAGE_SHARE
        and projection.intervals_used >= MIN_TRUSTED_INTERVALS
        and projection.rate_drift <= MAX_TRUSTED_RATE_DRIFT
        and projection.max_raw_rate_ratio <= MAX_TRUSTED_RAW_RATE_RATIO
    )
