"""Project quota exhaustion from persisted usage history."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
from pathlib import Path
from statistics import median
import time
from typing import Any, Dict, List, Optional, Tuple, Union


MIN_SAMPLES = 3
MIN_SPAN_SECONDS = 60.0
# Requiring evidence for at least 10% of the forward horizon limits projections
# to 10 times the observed span while still allowing useful short-term warnings.
MIN_SPAN_TO_HORIZON_RATIO = 0.1

# An interval this close to zero is not a second independent observation, it is
# float noise. Denormal or near-duplicate timestamps (e.g. two captures a few
# nanoseconds apart from clock jitter) divide a normal-sized usage delta by an
# almost-zero elapsed time and can overflow to inf even though "elapsed > 0.0"
# is technically true. Anything below a microsecond carries no rate evidence.
MIN_INTERVAL_SECONDS = 1e-6

# Theil-Sen's pairwise-slope step is O(n^2) in both time and memory: a segment
# with N readings builds N*(N-1)/2 float pairs. history.jsonl is append-only
# and, absent a marked reset, a segment can grow without bound, so N must be
# capped rather than trusted to stay small. 500 readings caps the pair count
# at 124,750 (well under a second, a few MB) while still covering a long
# capture history. Recency is what matters for a burn rate, not depth, so the
# cap keeps the most RECENT readings in the segment rather than the oldest.
MAX_FIT_SAMPLES = 500


class NoProjectionReason(str, Enum):
    """Why an exhaustion time could not be defended."""

    TOO_FEW_SAMPLES = "too_few_samples"
    SPAN_TOO_SHORT = "span_too_short"
    INSUFFICIENT_SPAN_FOR_HORIZON = "insufficient_span_for_horizon"
    ALREADY_EXHAUSTED = "already_exhausted"
    FLAT_USAGE = "flat_usage"
    USAGE_WENT_BACKWARDS = "usage_went_backwards"
    NON_POSITIVE_RATE = "non_positive_rate"
    WINDOW_ALREADY_RESET = "window_already_reset"
    NON_FINITE_RESULT = "non_finite_result"
    PROJECTED_EXHAUSTION_IN_PAST = "projected_exhaustion_in_past"
    RATE_UNDERFLOWED_TO_ZERO = "rate_underflowed_to_zero"
    TERMINAL_REMAINDER_UNMERGEABLE = "terminal_remainder_unmergeable"


@dataclass(frozen=True)
class BurnRateProjection:
    """The fitted burn rate and exhaustion decision for one usage window.

    ``exhaustion_precedes_reset`` is a tri-state field and callers MUST branch
    on it with ``is True`` / ``is False`` / ``is None`` rather than truthiness
    (``if not projection.exhaustion_precedes_reset`` conflates all three):

    * ``True`` / ``False`` -- a projection exists (``projected_exhaustion_at``
      is set, ``reason`` is None) and it is known whether exhaustion falls
      before or after the window's reset.
    * ``None`` with ``reason`` set to None -- a projection exists, but the
      window's reset time is unknown (``resets_at`` was absent), so no
      before/after comparison can be made. ``projected_exhaustion_at`` is
      still set in this case.
    * ``None`` with ``reason`` set to a ``NoProjectionReason`` -- no
      projection could be defended at all. ``projected_exhaustion_at`` is
      None in this case too, so the two None cases are distinguished by
      checking ``reason``, not by re-inspecting ``exhaustion_precedes_reset``.

    ``span_seconds`` is ``Optional`` because the subtraction that produces it
    (latest capture minus earliest capture) can itself overflow to infinity
    for pathological epoch timestamps near the float range limit. Rather than
    let a non-finite span escape to a caller, it is None whenever the true
    span cannot be represented as a finite float; ``reason`` is then
    ``NON_FINITE_RESULT``. A non-None ``span_seconds`` is always finite.

    ``max_relative_deviation``, ``max_usage_share``, ``intervals_used``,
    ``rate_drift``, and ``effective_intervals`` report how consistent the
    fitted rate was with the segment's own recent history. This module used
    to collapse those numbers into a HIGH/MEDIUM/LOW confidence tier, but
    four consecutive rounds of adversarial review moved the same fight to a
    different threshold every time (a slope sign, then a dispersion mean,
    then a rate-ratio cap, then a usage-share cap -- each one gameable from
    just underneath). The abstraction was the defect: whether a given
    deviation or share is "acceptable" depends on what the caller does with
    the projection (a status line and an automated cutoff switch tolerate
    very different risk), and that is a policy question this module cannot
    answer once for every caller. So it reports the measurements instead and
    takes no position on what threshold, if any, a caller should apply. A
    projection being returned at all (``reason`` is None) is NOT itself a
    claim that the underlying rate is steady -- these five fields are how a
    caller judges that for itself. All five are None exactly when no
    projection was made (``reason`` is set).

    Each is built from the segment's consecutive, non-overlapping intervals
    (independent evidence, unlike the combinatorial pairwise slopes used to
    fit ``rate_percent_per_second`` itself), after folding any interval whose
    usage delta is EXACTLY zero forward into the next interval that actually
    changed. That folding matters on real data: this project's own captures
    report ``used_percentage`` as a whole number (see
    ``_records_since_latest_reset``'s "57 -> 56 -> 57" note), so a genuinely
    sub-point-per-interval rate reports as a long run of zero-delta readings
    interrupted by occasional single-point jumps. Treating each zero-delta
    reading as its own independent interval manufactures a median interval
    rate of 0 out of quantization alone, which then scores every real jump as
    maximally different from "the" rate even though nothing is actually
    unstable -- an adversarial review of this module's own history found
    every one of a real capture history's short-window segments scored the
    worst tier for exactly this reason. Folding is not a tuned tolerance: it
    only merges intervals that reported literally no change, so a segment
    with no quantization (every raw delta already nonzero) is untouched by
    it. ``intervals_used`` counts these post-folding intervals, so it can be
    smaller than ``samples_used - 1``; the difference is how many zero-delta
    readings were folded away.

    ``max_relative_deviation`` is the largest |interval_rate - median_rate| /
    median_rate across those intervals. LOW means every interval's rate sat
    close to the group's median; HIGH means at least one interval ran at a
    rate very different from the rest. The median can only be exactly zero if
    at least half the folded intervals report a zero rate. Folding guarantees
    every folded interval's accumulated delta is strictly positive by
    construction, but a strictly positive delta does NOT guarantee a strictly
    positive computed rate -- dividing a tiny (e.g. denormal) delta by an
    ordinary elapsed time can underflow to an exact 0.0 even though the true
    ratio is nonzero. An earlier version of this comment claimed that made
    the zero-median case structurally unreachable; a round of adversarial
    review disproved that with exactly this underflow. Rather than let an
    underflowed 0.0 masquerade as trivial (zero-usage) agreement in the
    median, that interval is treated as carrying no usable rate evidence at
    all: the whole projection declines with
    ``NoProjectionReason.RATE_UNDERFLOWED_TO_ZERO`` instead of reporting
    measurements built on it. So for any measurements this docstring's
    fields actually describe (``reason`` is None), every rate is genuinely,
    representably positive, and the zero-baseline case below is unreachable
    by explicit construction rather than by the delta-sign argument alone.
    The ratio's zero-baseline case is still defined, not left to divide by
    zero, as a defensive convention: 0.0 if every interval is exactly zero
    (trivial agreement) and 1.0 (maximal, but finite) otherwise. This field
    is always finite -- never inf or nan -- whenever it is not None.

    ``max_usage_share`` is the largest fraction of the segment's total usage
    delta contributed by any single interval. LOW means no one interval
    dominates the evidence; HIGH (approaching 1.0) means the fitted rate
    rests mostly on one interval -- which can mean a genuine anomaly, or,
    innocently, that the interval simply ran far longer than its neighbors
    (a laptop asleep overnight produces one long, unremarkable interval that
    still supplies most of the segment's elapsed time and usage). This field
    alone cannot tell those two cases apart.

    ``rate_drift`` compares the segment's early portion to its late portion:
    split the (folded) intervals in half by count, take each half's
    time-weighted rate (total delta over total elapsed within that half), and
    report the relative difference between them, using the same zero-baseline
    convention as ``max_relative_deviation``. Per-interval deviation and share
    are both LOCAL measures -- each compares one interval against the rest --
    so neither can see a rate that climbs steadily across many intervals that
    are each individually unremarkable. ``rate_drift`` is defined as 0.0 for
    a single-interval segment, where there is no second portion to compare.

    ``effective_intervals`` is the usual inverse-Herfindahl measure of how
    concentrated the usage-share weights are across the folded intervals:
    ``total_delta**2 / sum(delta**2 for delta in deltas)``. It is 1.0 if one
    interval supplies the entire usage delta and ``intervals_used`` if every
    interval supplies an equal share. ``max_usage_share`` alone answers "how
    big is the single worst interval"; it cannot see a small CLUSTER of
    intervals jointly dominating while each one individually stays under any
    per-interval share. A projection can have a low ``max_relative_deviation``
    and a low ``max_usage_share`` and still rest on very little independent
    evidence if ``effective_intervals`` is far below ``intervals_used``.

    ``zero_delta_fraction`` and ``max_raw_rate_ratio`` are two RAW (never
    zero-delta-folded) structural measurements reported alongside the five
    folded ones above. They replace an earlier five-field raw mirror of the
    same five folded measurements (``max_relative_deviation_raw`` and its
    four siblings), which a round of adversarial review proved did not
    discriminate what it was added to discriminate: an ordinary
    quantized-steady series and a genuine sub-second burst produced
    IDENTICAL values on every one of those ten fields, because any series
    containing so much as one zero-delta gap saturates the raw deviation at
    exactly 1.0 -- and real data is full of zero-delta gaps (that is the
    whole reason folding exists; see the folded fields above). The
    five-field mirror was a saturation flag wearing a measurement's name,
    not a discriminator. These two fields are built directly from the raw
    (unfolded) gaps between consecutive captures instead, and answer two
    different, narrower questions the folded fields cannot answer alone:

    ``zero_delta_fraction`` is the fraction of raw gaps between consecutive
    captures whose usage delta is EXACTLY zero. This is "how quantized is
    this data" made explicit -- the same fact folding uses internally
    (merging exactly these gaps forward) but never previously reported on
    its own. LOW (near 0.0) means almost every capture recorded some
    change; HIGH (near 1.0) means most captures are quantization repeats of
    the previous reading. On its own this says nothing about whether the
    underlying rate is steady or bursty -- a quantized-steady series and a
    quantized burst can report the same fraction of zero-delta gaps -- which
    is exactly why it is reported alongside, not instead of,
    ``max_raw_rate_ratio``, rather than relied on by itself. It is computed
    directly from every adjacent pair of records (see
    ``_raw_zero_delta_fraction``), independent of the elapsed-time
    carry-forward and folding ``_consecutive_intervals`` applies when
    building measurement intervals -- it is a property of the raw captures,
    not of any interval those captures get grouped into.

    ``max_raw_rate_ratio`` is the largest single raw interval's rate,
    divided by the segment's OVERALL rate (total usage delta over total
    elapsed time -- the rate the segment would report if treated as one
    interval start to finish). "Total elapsed time" means exactly that:
    the segment's actual start-to-finish span, ``records[-1].captured_at -
    records[0].captured_at`` -- the same span this projection reports as
    ``span_seconds`` -- NOT the sum of only the raw intervals that ended up
    retained as measurement intervals. Those two can differ by design: a
    segment's final raw gap can accumulate a real elapsed time too small to
    stand alone (below MIN_INTERVAL_SECONDS) with no following gap left to
    carry it into (see ``_consecutive_intervals``). Two different things
    can happen to that trailing gap, and they must not be confused with
    each other:

    * If its usage delta is EXACTLY zero, no usage was lost, so nothing
      declines -- the gap is silently dropped. But its elapsed time is
      still real and belongs in this ratio's denominator regardless, or
      the overall rate would be computed over less time than the segment
      actually spanned. That would make the overall rate too LARGE, which
      suppresses this ratio and can shorten
      ``longest_above_overall_rate_run``, for a reason that has nothing to
      do with the segment's actual burn pattern. [(0,0),(60,10),
      (60.0000005,10)] is the concrete case: the trailing ~5e-7s gap
      carries no usage, so summing only the retained intervals'
      elapsed time (60.0s) undercounts the true 60.0000005s span, and
      reports ``max_raw_rate_ratio=1.0`` and
      ``longest_above_overall_rate_run=0`` instead of the honest
      ``1.0000000083...`` and ``1``.
    * If its usage delta is instead NONZERO, it cannot be silently
      absorbed either way (dividing by ~microseconds is float noise, not
      evidence; merging it into the previous interval smears a possibly
      fast remainder across a span it never ran at -- see
      ``_TerminalRemainderUnmergeable``). The whole projection declines
      with ``NoProjectionReason.TERMINAL_REMAINDER_UNMERGEABLE`` instead,
      before this field is ever computed.

    Because the overall rate is the
    elapsed-time-weighted average of every raw interval's rate, this ratio
    is mathematically always >= 1.0, with equality only when every raw
    interval runs at exactly the same rate -- but that holds for the exact
    real-number ratio, not for what floating-point division actually
    computes. Two divisions (the per-interval rate and the overall rate)
    each round independently, and an ordinary-looking input can land the
    computed ratio a hair under 1.0 (e.g. 0.9999999999999998) even though
    no interval genuinely ran slower than the segment's average. The
    reported value is clamped to 1.0 from below to keep the field's
    documented floor honest in float, not just in the real numbers it
    approximates. LOW (near 1.0) means no single raw interval ran
    meaningfully faster than the segment's overall pace -- consistent with a
    genuinely steady rate, quantized or not. HIGH means one raw interval's
    rate hugely exceeds the segment average: the signature of a genuine
    burst, which is exactly the case a folded field can smooth away
    (folding merges a burst's neighboring flat gaps into it, diluting its
    rate down toward the ordinary-looking average of the whole merged
    span). A caller who sees a low folded ``max_relative_deviation`` next to
    a high ``max_raw_rate_ratio`` is looking at a burst folding smoothed
    over, not a genuinely steady rate.

    ``max_raw_rate_ratio`` is computed over the raw (unfolded) INTERVAL
    view: every gap between consecutive captures is its own interval,
    zero-delta gaps included, except that a gap whose own accumulated
    elapsed time is below MIN_INTERVAL_SECONDS is still carried forward
    (see ``_consecutive_intervals``) rather than divided into float noise.
    ``zero_delta_fraction`` deliberately does NOT go through that same
    interval-building step -- it is measured directly from raw adjacent
    RECORD pairs (see ``_raw_zero_delta_fraction``), because the
    elapsed-time carry-forward can coalesce a sub-threshold gap into a
    neighbor and change how many zero-delta gaps a per-interval count would
    see. All three raw fields (``zero_delta_fraction``,
    ``max_raw_rate_ratio``, and ``longest_above_overall_rate_run`` below)
    are still None exactly when no projection was made (``reason`` is
    set), same as the five folded fields; a projection with measurements
    always has all three.

    ``longest_above_overall_rate_run`` is the length of the longest run of
    CONSECUTIVE raw intervals (same raw view as ``max_raw_rate_ratio``)
    whose individual rate exceeds the segment's overall rate. Every field
    above is order-blind: computing any of them on a shuffled copy of the
    same intervals gives the same answer, because each is a max, a median,
    a sum, or a sum of squares -- none of them look at WHICH interval comes
    next to which. Two segments with the same interval rates in a different
    order are therefore invisible to all seven, even when the order is the
    entire story: a clustered burn pattern (several fast intervals in a row,
    then several slow ones) points at a specific contiguous window of heavy
    use, while the same rates strictly alternating fast/slow spread that
    same total usage evenly across the whole segment. This field is the one
    exception -- it is a property of the SEQUENCE, not just the multiset, of
    rates. HIGH means the segment had a sustained run of above-average
    intervals in a row (a burst with duration, not just magnitude); LOW
    (0 or 1) means no two above-average intervals were ever adjacent, no
    matter how many there were in total. It is always a non-negative
    integer, 0 when no interval exceeds the overall rate (impossible in
    practice once ``max_raw_rate_ratio`` > 1.0, since that means at least
    one interval exceeds it) up to ``intervals_used``-many raw intervals
    when every single one does.

    These eight fields, and the module as a whole, report summary
    statistics computed from a segment's captures -- they do not, and
    cannot, capture everything about the underlying series. For any finite
    set of summary numbers there exist two materially different histories
    that agree on every one of them: adding ``longest_above_overall_rate_run``
    closed the specific order-blindness gap found in round 9, not every
    possible gap. A caller that needs to distinguish two series these
    fields agree on has to read the underlying history itself; no
    additional summary statistic closes that for every possible pair of
    series, only for the ones it was specifically built to catch.

    ``latest_change_at`` is a ninth, differently-shaped fact: the
    ``captured_at`` of the most recent record in the segment whose
    ``used_percentage`` differs from the record immediately before it --
    when usage last demonstrably MOVED, as opposed to when the most recent
    capture merely HAPPENED. A capture repeating the prior capture's
    percentage confirms nothing changed; it is not evidence a trend
    continues, only evidence a capture occurred. Every measurement above is
    computed from the segment as a whole and is blind to where in the
    segment its evidence sits -- in particular, a trailing run of
    zero-delta captures (usage climbs, then genuinely stalls, and captures
    keep arriving reporting the same stalled value) is invisible to every
    folded field: folding exists to merge a zero-delta gap FORWARD into the
    next interval that changed, and a trailing run has no next interval to
    merge into, so its elapsed time is dropped from the folded view
    entirely rather than reported as "recent, but flat." A caller checking
    only "is there a recent capture" (my Confidence.FRESH state, kept
    elsewhere) cannot tell a genuinely continuing trend from a stalled one
    that merely keeps being re-captured -- ``latest_change_at`` is what
    that caller compares "now" against instead: if the gap between them
    exceeds what a normal capture cadence would explain, the segment has
    stalled since the last real change, no matter how fresh the most
    recent capture is or how clean the segment's own fitted rate looks.
    Like the eight fields above, this is a raw fact with no threshold
    attached -- how large a gap is too large is a policy question for a
    caller, not this module, to answer. It is None exactly when no
    projection was made (``reason`` is set), same as every field above;
    when a projection exists, it is always set, because
    ``exhaustion_precedes_reset``'s sibling check (``latest_usage !=
    first_usage``, see ``_project_group``) guarantees at least one
    adjacent pair in the segment differs.
    """

    source: str
    window: str
    rate_percent_per_second: Optional[float]
    projected_exhaustion_at: Optional[float]
    exhaustion_precedes_reset: Optional[bool]
    samples_used: int
    span_seconds: Optional[float]
    reason: Optional[NoProjectionReason] = None
    max_relative_deviation: Optional[float] = None
    max_usage_share: Optional[float] = None
    intervals_used: Optional[int] = None
    rate_drift: Optional[float] = None
    effective_intervals: Optional[float] = None
    zero_delta_fraction: Optional[float] = None
    max_raw_rate_ratio: Optional[float] = None
    longest_above_overall_rate_run: Optional[int] = None
    latest_change_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible representation with every field present.

        ``reason`` is serialized as its string value (not the enum member)
        so a caller debugging "why is there no projection" over JSON gets a
        stable, documented string rather than a Python repr. This mirrors
        Snapshot.to_dict and Reading.to_dict in bounds.py.
        """

        return {
            "source": self.source,
            "window": self.window,
            "rate_percent_per_second": self.rate_percent_per_second,
            "projected_exhaustion_at": self.projected_exhaustion_at,
            "exhaustion_precedes_reset": self.exhaustion_precedes_reset,
            "samples_used": self.samples_used,
            "span_seconds": self.span_seconds,
            "reason": self.reason.value if self.reason is not None else None,
            "max_relative_deviation": self.max_relative_deviation,
            "max_usage_share": self.max_usage_share,
            "intervals_used": self.intervals_used,
            "rate_drift": self.rate_drift,
            "effective_intervals": self.effective_intervals,
            "zero_delta_fraction": self.zero_delta_fraction,
            "max_raw_rate_ratio": self.max_raw_rate_ratio,
            "longest_above_overall_rate_run": self.longest_above_overall_rate_run,
            "latest_change_at": self.latest_change_at,
        }


@dataclass(frozen=True)
class _HistoryRecord:
    captured_at: float
    resets_at: Optional[float]
    source: str
    window: str
    used_percentage: float


PathValue = Union[str, "Path"]


def project_exhaustion(
    history_path: PathValue, now: Optional[float] = None
) -> List[BurnRateProjection]:
    """Return one exhaustion projection per source and window in history_path."""

    current_time = time.time() if now is None else _finite_float(now)
    if current_time is None:
        raise ValueError("now must be a finite number")

    groups: Dict[Tuple[str, str], List[_HistoryRecord]] = {}
    try:
        with Path(history_path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                record = _parse_record(line)
                if record is None or record.captured_at > current_time:
                    continue
                groups.setdefault((record.source, record.window), []).append(record)
    except OSError:
        # Matches state.read_state: an unreadable file (missing, a directory,
        # permission denied, or any other OS-level failure) means "no data",
        # not a crash. The caller already treats an empty list as "nothing to
        # report".
        return []

    projections = []
    for key in sorted(groups):
        records = sorted(groups[key], key=lambda item: item.captured_at)
        latest_segment = _records_since_latest_reset(records)
        # Cap AFTER segmentation: segmentation must see the full history to
        # find the true reset boundary, but only the most recent samples are
        # needed (and safe, see MAX_FIT_SAMPLES) to fit the current window.
        fit_segment = latest_segment[-MAX_FIT_SAMPLES:]
        projections.append(_project_group(key, fit_segment, current_time))
    return projections


def _parse_record(line: str) -> Optional[_HistoryRecord]:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None

    captured_at = _finite_float(value.get("captured_at"))
    used_percentage = _finite_float(value.get("used_percentage"))
    resets_value = value.get("resets_at")
    resets_at = None if resets_value is None else _finite_float(resets_value)
    source = value.get("source")
    window = value.get("window")
    if (
        captured_at is None
        or used_percentage is None
        or used_percentage < 0.0
        or (resets_value is not None and resets_at is None)
        or not isinstance(source, str)
        or not source
        or not isinstance(window, str)
        or not window
    ):
        return None

    return _HistoryRecord(
        captured_at=captured_at,
        resets_at=resets_at,
        source=source,
        window=window,
        used_percentage=used_percentage,
    )


def _finite_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _records_since_latest_reset(
    records: List[_HistoryRecord],
) -> List[_HistoryRecord]:
    """Return only the records that belong to the current (most recent) window.

    True within-window usage is monotone, but reported usage may contain
    noise. Real readings from this machine oscillate by about a point
    (57 -> 56 -> 57) at an unchanged resets_at, so a decrease is ambiguous:
    it may be a reset or a bad measurement. Nothing available distinguishes
    them. An unchanged resets_at is evidence against a reset, not proof,
    because it can be stale or captured non-atomically across a boundary,
    and resets.py only checks that it is numerically plausible. A tolerance
    band would be worse than useless: observing one-point errors shows such
    errors occur, not that they are bounded by one point, and any threshold
    would hide a genuine reset of the same size.

    So every decrease starts a new segment. Keeping pre-decrease records
    could mix two windows and fabricate evidence, whereas a false boundary
    costs only availability. A change in resets_at marks a boundary too,
    since the provider is calling it a different window.

    This knowingly declines to project more often, including on short
    windows where the jitter above is common. That is the honest outcome:
    this source does not carry enough information to tell noise from a
    reset, and inventing the difference is what soundness forbids.
    """

    if not records:
        return []

    segment_start = len(records) - 1
    while segment_start > 0:
        previous = records[segment_start - 1]
        current = records[segment_start]
        if previous.resets_at != current.resets_at:
            break
        if current.used_percentage < previous.used_percentage:
            break
        segment_start -= 1

    # Repeated captures can share a timestamp. Keeping only the last one avoids
    # counting observations that add no time information as independent evidence.
    by_timestamp: Dict[float, _HistoryRecord] = {}
    for record in records[segment_start:]:
        by_timestamp[record.captured_at] = record
    return [by_timestamp[captured_at] for captured_at in sorted(by_timestamp)]


def _project_group(
    key: Tuple[str, str], records: List[_HistoryRecord], now: float
) -> BurnRateProjection:
    source, window = key
    samples_used = len(records)
    raw_span = (
        records[-1].captured_at - records[0].captured_at
        if samples_used > 1
        else 0.0
    )
    # Validate the span the instant it is produced, not after several early
    # returns have already had a chance to hand out the raw (possibly
    # infinite) value. Both endpoints pass _finite_float individually, but
    # their difference can still overflow: captures near the float range
    # limit (e.g. -1e308 and 1e308) subtract to inf even though each is
    # finite on its own. records is sorted by captured_at, so if this total
    # span is finite, every interior pairwise difference used later is
    # bounded by it and therefore finite too -- validating once here is
    # sufficient, not just the first of many checks.
    span_seconds: Optional[float] = raw_span if math.isfinite(raw_span) else None

    def unavailable(
        reason: NoProjectionReason,
        rate: Optional[float] = None,
    ) -> BurnRateProjection:
        return BurnRateProjection(
            source=source,
            window=window,
            rate_percent_per_second=rate,
            projected_exhaustion_at=None,
            exhaustion_precedes_reset=None,
            samples_used=samples_used,
            span_seconds=span_seconds,
            reason=reason,
        )

    if span_seconds is None:
        return unavailable(NoProjectionReason.NON_FINITE_RESULT)

    # A window whose reset time has already passed is over; whatever usage
    # it last reported says nothing about the window in effect now. This is
    # the burn-rate analogue of bounds.bound_snapshot's POST_RESET handling:
    # stay consistent with it rather than projecting through a dead window.
    latest_resets_at = records[-1].resets_at if records else None
    if latest_resets_at is not None and latest_resets_at <= now:
        return unavailable(NoProjectionReason.WINDOW_ALREADY_RESET)

    if samples_used < MIN_SAMPLES:
        return unavailable(NoProjectionReason.TOO_FEW_SAMPLES)
    if span_seconds < MIN_SPAN_SECONDS:
        return unavailable(NoProjectionReason.SPAN_TOO_SHORT)
    if records[-1].used_percentage >= 100.0:
        return unavailable(NoProjectionReason.ALREADY_EXHAUSTED)

    slopes = _pairwise_slopes(records)
    rate = median(slopes) if slopes else None
    first_usage = records[0].used_percentage
    latest_usage = records[-1].used_percentage
    if latest_usage == first_usage:
        return unavailable(NoProjectionReason.FLAT_USAGE, rate)
    if latest_usage < first_usage:
        # Defense in depth: _records_since_latest_reset already guarantees a
        # non-decreasing segment (a decrease is itself a reset boundary), so
        # this should be unreachable through project_exhaustion. It stays as
        # a hard invariant check because a wrong confident projection is far
        # worse than a defensive branch that never fires.
        return unavailable(NoProjectionReason.USAGE_WENT_BACKWARDS, rate)
    if rate is None or rate <= 0.0:
        return unavailable(NoProjectionReason.NON_POSITIVE_RATE, rate)

    latest = records[-1]
    projected_at = latest.captured_at + (100.0 - latest_usage) / rate
    horizon_seconds = projected_at - latest.captured_at
    if not all(
        math.isfinite(value)
        for value in (rate, projected_at, horizon_seconds, span_seconds)
    ):
        return unavailable(NoProjectionReason.NON_FINITE_RESULT, None)

    if span_seconds < horizon_seconds * MIN_SPAN_TO_HORIZON_RATIO:
        return unavailable(NoProjectionReason.INSUFFICIENT_SPAN_FOR_HORIZON, rate)
    if projected_at < now:
        # The rate is stale enough that, projected forward, we'd already be
        # past exhaustion by "now" without a fresher reading confirming it.
        # Stating a past timestamp as a live projection is not defensible.
        return unavailable(NoProjectionReason.PROJECTED_EXHAUSTION_IN_PAST, rate)

    try:
        measurements = _interval_measurements(records)
    except _RateUnderflowedToZero:
        # A folded or raw interval carried a strictly positive usage delta
        # that divided to an exact 0.0 rate (float underflow, not a genuine
        # zero-usage interval). That interval has no representable rate to
        # offer the median it would otherwise be folded into, so the whole
        # projection declines rather than let an underflowed 0.0 pass as
        # trivial agreement. See BurnRateProjection's max_relative_deviation
        # docstring for why this is a fact about the data, not a policy call.
        return unavailable(NoProjectionReason.RATE_UNDERFLOWED_TO_ZERO, rate)
    except _TerminalRemainderUnmergeable:
        # The segment's final gap left a nonzero usage delta with no
        # interval to honestly attach it to: too short to stand alone, and
        # merging it into the preceding interval would smear its rate away
        # (see _consecutive_intervals's docstring and
        # _TerminalRemainderUnmergeable). This is not usage silently
        # dropped -- the caller is told via this distinct reason -- and it
        # is not a fabricated rate either. Declining is the honest outcome.
        return unavailable(NoProjectionReason.TERMINAL_REMAINDER_UNMERGEABLE, rate)
    if measurements is None:
        # Structurally unreachable in practice (see _interval_measurements),
        # but if it ever is reached, a non-finite diagnostic must not leak
        # out any more than a non-finite rate or span may -- decline the
        # whole projection rather than hand back a partially-finite result.
        return unavailable(NoProjectionReason.NON_FINITE_RESULT, rate)

    resets_at = latest.resets_at
    precedes_reset = None if resets_at is None else projected_at < resets_at
    return BurnRateProjection(
        source=source,
        window=window,
        rate_percent_per_second=rate,
        projected_exhaustion_at=projected_at,
        exhaustion_precedes_reset=precedes_reset,
        samples_used=samples_used,
        span_seconds=span_seconds,
        max_relative_deviation=measurements.max_relative_deviation,
        max_usage_share=measurements.max_usage_share,
        intervals_used=measurements.intervals_used,
        rate_drift=measurements.rate_drift,
        effective_intervals=measurements.effective_intervals,
        zero_delta_fraction=measurements.zero_delta_fraction,
        max_raw_rate_ratio=measurements.max_raw_rate_ratio,
        longest_above_overall_rate_run=measurements.longest_above_overall_rate_run,
        latest_change_at=_latest_change_at(records),
    )


def _latest_change_at(records: List[_HistoryRecord]) -> float:
    """The ``captured_at`` of the most recent record whose ``used_percentage``
    differs from the record immediately before it.

    Callable only once ``latest_usage != first_usage`` is already
    established (see ``_project_group``, right before ``BurnRateProjection``
    is built on the success path): that guarantees at least one adjacent
    pair in the segment differs, because if every adjacent pair were equal
    the first and last values would be transitively equal too. So this
    always finds a match before its loop reaches index 0; the function has
    no "nothing ever changed" branch to fall back to because that case is
    unreachable under its precondition.
    """

    for index in range(len(records) - 1, 0, -1):
        if records[index].used_percentage != records[index - 1].used_percentage:
            return records[index].captured_at
    # Unreachable given this function's precondition (see docstring); kept
    # as a defensive, honestly-labeled fallback rather than an assertion, so
    # a future caller that violates the precondition gets the segment's
    # earliest timestamp instead of an IndexError.
    return records[0].captured_at


def _pairwise_slopes(records: List[_HistoryRecord]) -> List[float]:
    # The Theil-Sen slope is the median of pairwise slopes. A single corrupt
    # sample affects only its pairs instead of pulling the whole fit toward it.
    slopes = []
    for left_index, left in enumerate(records[:-1]):
        for right in records[left_index + 1 :]:
            elapsed = right.captured_at - left.captured_at
            if elapsed < MIN_INTERVAL_SECONDS:
                continue
            slope = (right.used_percentage - left.used_percentage) / elapsed
            if math.isfinite(slope):
                slopes.append(slope)
    return slopes


class _RateUnderflowedToZero(Exception):
    """A folded interval's delta was strictly positive but its rate was not.

    Raised by ``_consecutive_intervals`` -- see that function's docstring for
    why this happens and why it is declined rather than folded in as if it
    were trivial zero-usage agreement.
    """


class _TerminalRemainderUnmergeable(Exception):
    """A segment's final gap left a nonzero usage delta with no interval to
    attach it to.

    Raised by ``_consecutive_intervals`` -- see that function's docstring for
    why the remainder cannot stand as its own interval, cannot be merged
    forward (there is no next gap), and must not be merged backward either.
    """


@dataclass(frozen=True)
class _IntervalMeasurements:
    """The eight diagnostic numbers (five folded, three raw structural)
    reported alongside a successful projection. See BurnRateProjection's
    docstring for what each one means and why both views are reported.
    """

    max_relative_deviation: float
    max_usage_share: float
    intervals_used: int
    rate_drift: float
    effective_intervals: float
    zero_delta_fraction: float
    max_raw_rate_ratio: float
    longest_above_overall_rate_run: int


def _consecutive_intervals(
    records: List[_HistoryRecord], *, fold_zero_delta: bool
) -> Tuple[List[float], List[float], List[float]]:
    """Build the (rate, delta, elapsed) triples the diagnostics are built from.

    Consecutive, non-overlapping intervals are used here (not the
    combinatorial pairwise slopes _pairwise_slopes builds for the fitted
    rate) because they are independent evidence: each raw capture appears in
    exactly one interval, so one bad reading corrupts only the interval it
    touches, unlike a pairwise slope where it corrupts every pair built from
    it.

    Every raw gap between consecutive captures is accumulated in order, and
    two kinds of raw gap cannot stand as their own interval, so both are
    carried forward into later gaps instead:

    * A gap whose ACCUMULATED elapsed time is still below
      MIN_INTERVAL_SECONDS. Dividing by an elapsed time that close to zero is
      float noise, not evidence (see MIN_INTERVAL_SECONDS's module-level
      comment) -- but the usage that occurred during that gap is real and
      must not vanish just because it arrived attached to a too-short
      elapsed time. An earlier version of this function checked each gap's
      own raw elapsed time before accumulating anything, which discarded
      that gap's usage delta entirely; checking the ACCUMULATED elapsed time
      after adding the gap's contribution, instead, is what actually carries
      the delta (and the elapsed time itself) forward until the running
      total clears the threshold. This carry-forward is unconditional: it
      applies whether or not ``fold_zero_delta`` is set, because losing
      usage data is never correct, independent of the folding policy below.
    * (``fold_zero_delta`` only) A gap whose usage delta is, after any
      carry-forward above, EXACTLY zero. This is the folding described in
      BurnRateProjection's docstring: real captures round ``used_percentage``
      to whole points, so a genuinely sub-point-per-interval rate reports as
      a long run of zero-delta readings interrupted by occasional
      single-point jumps, and treating each zero-delta reading as
      independent evidence manufactures instability out of quantization
      alone. This folding is exact, not a tuned tolerance: it only merges
      intervals that reported literally no change. With
      ``fold_zero_delta=False`` every gap becomes its own interval,
      zero-delta ones included -- this is the raw view ``zero_delta_fraction``
      and ``max_raw_rate_ratio`` are built from, and it is also what lets a
      genuine sub-second burst show up as a single wildly-off-average
      interval instead of being averaged away into the long flat run
      around it.

    A carried-forward accumulator can end up with a strictly positive delta
    whose rate underflows to exactly 0.0 on division (e.g. a denormal delta
    over an ordinary elapsed time, below the smallest representable positive
    float). That interval has real usage but no representable rate, so it
    cannot be folded in as one more zero-rate data point -- doing so would
    corrupt the median it feeds with a value that looks like trivial
    agreement but is really a measurement failure. ``_RateUnderflowedToZero``
    is raised instead in that case, for the caller to turn into a declined
    projection.

    A nonzero accumulator can also survive all the way to the end of the
    loop below, past the last gap, still short of MIN_INTERVAL_SECONDS --
    e.g. records ending [...,(60, 10), (60.0000005, 11)], where the final
    gap's ~5e-7s never clears the threshold and there is no further gap
    left to carry it forward into. Every other carry-forward case in this
    function has a "next" interval to fold into; this one does not. An
    earlier version of this function simply dropped it, which silently
    broke usage conservation (the segment's reported deltas summed to less
    than its actual total usage change); a later version folded it
    BACKWARD into the last already-measured interval instead, which fixed
    conservation but broke something worse: merging a fast terminal
    remainder into a slow preceding interval smears its rate across a span
    it never actually ran at, which can hide a genuine burst entirely (see
    ``_TerminalRemainderUnmergeable``'s docstring for the disproof case).
    Both the drop and the backward merge invent an answer the data does not
    support -- dropping invents "this usage never happened" and merging
    invents "this usage happened at the previous interval's pace." Neither
    is true, so ``_TerminalRemainderUnmergeable`` is raised instead: the
    caller declines the whole projection rather than report a number built
    on either invention. A trailing accumulator whose leftover delta is
    exactly zero needs no such handling: it contributes nothing to the
    usage total either way, so it is still dropped cleanly, same as before
    -- its DELTA, that is, not its elapsed time. That elapsed time is real
    (the gap between two actual captures happened, it just carried no
    usage) and is NOT lost: ``_measure_raw_structure`` computes
    ``max_raw_rate_ratio``'s overall-rate denominator from the segment's
    full start-to-finish span (``records[-1].captured_at -
    records[0].captured_at``), not from summing only the intervals this
    function returns, specifically so a dropped zero-delta remainder's
    elapsed time is not silently excluded from "the rate the segment would
    report treated as one interval start to finish." See that function and
    BurnRateProjection's ``max_raw_rate_ratio`` docstring for the worked
    example and for how this differs from the nonzero-remainder case
    handled above, which declines instead of reaching that computation at
    all.
    """

    rates: List[float] = []
    deltas: List[float] = []
    elapsed_list: List[float] = []
    accumulated_delta = 0.0
    accumulated_elapsed = 0.0
    for index in range(1, len(records)):
        previous = records[index - 1]
        current = records[index]
        accumulated_delta += current.used_percentage - previous.used_percentage
        accumulated_elapsed += current.captured_at - previous.captured_at
        if accumulated_elapsed < MIN_INTERVAL_SECONDS:
            continue
        if fold_zero_delta and accumulated_delta == 0.0:
            continue
        rate = accumulated_delta / accumulated_elapsed
        if rate == 0.0 and accumulated_delta != 0.0:
            raise _RateUnderflowedToZero(
                f"delta {accumulated_delta!r} over {accumulated_elapsed!r}s "
                "underflowed to a zero rate"
            )
        if math.isfinite(rate):
            rates.append(rate)
            deltas.append(accumulated_delta)
            elapsed_list.append(accumulated_elapsed)
        accumulated_delta = 0.0
        accumulated_elapsed = 0.0

    if accumulated_delta != 0.0:
        # See the docstring above: this is the terminal remainder, real
        # usage with no following gap to carry it into. A round-7 fix folded
        # it BACKWARD into the last measured interval on the theory that
        # merging is the same "carry a too-short gap into a real interval"
        # operation used everywhere else in this function. Round 9 disproved
        # that: merging backward doesn't just relocate the remainder, it
        # SMEARS its rate across the preceding interval's whole span,
        # erasing the evidence that a fast (possibly catastrophic) jump
        # happened at all. [(0,0),(60,1),(60.0000005,99)] backward-merges a
        # ~196,000,000 %/s terminal jump into the preceding minute and
        # reports a perfectly steady interval -- the tool lying about what
        # the data showed, which is worse than declining. The remainder's
        # usage is real and its elapsed time is real, but there is no
        # interval-sized bucket the two can honestly share: not the
        # too-short remainder itself (dividing by ~microseconds is float
        # noise, not evidence), and not the previous interval (its own rate
        # was measured over its own span, not this one). So this declines
        # instead of inventing a number either way -- distinct from
        # dropping the remainder (which the caller would never learn about)
        # and distinct from the ordinary _RateUnderflowedToZero case (a
        # rate that IS representable as a number, just not as a nonzero
        # one).
        raise _TerminalRemainderUnmergeable(
            f"terminal delta {accumulated_delta!r} over "
            f"{accumulated_elapsed!r}s has no interval to merge into and "
            "cannot stand as its own interval"
        )

    return rates, deltas, elapsed_list


def _folded_intervals(
    records: List[_HistoryRecord],
) -> Tuple[List[float], List[float], List[float]]:
    """The (rate, delta, elapsed) triples the folded (production) fields use.

    See ``_consecutive_intervals`` for the full folding rules. This is a
    thin, separately-named wrapper (rather than inlining the
    ``fold_zero_delta=True`` call everywhere) so a test can import and assert
    on the folded triples directly, independent of the five summary numbers
    ``_interval_measurements`` reduces them to.
    """
    return _consecutive_intervals(records, fold_zero_delta=True)


def _raw_intervals(
    records: List[_HistoryRecord],
) -> Tuple[List[float], List[float], List[float]]:
    """The (rate, delta, elapsed) triples ``zero_delta_fraction`` and
    ``max_raw_rate_ratio`` are built from.

    Same as ``_folded_intervals`` except zero-delta gaps are never folded
    forward -- every raw gap between consecutive captures is its own
    interval. See BurnRateProjection's docstring for why both views are
    reported.
    """
    return _consecutive_intervals(records, fold_zero_delta=False)


def _relative_difference(value: float, baseline: float) -> float:
    """|value - baseline| / baseline, with a finite convention at baseline 0.

    A zero baseline has no positive scale to express a RATIO against, so
    "how many times bigger" is undefined by division. This is defined
    instead as 0.0 when value also equals the (zero) baseline -- trivial
    agreement -- and 1.0 (maximal, but finite) for any other value, so the
    result is always finite here, never inf or nan, regardless of baseline.

    Every current caller passes a strictly positive baseline in practice:
    the folded view only ever emits intervals with a strictly positive delta
    (usage is non-decreasing within a segment and an interval is only
    recorded once its accumulated delta is nonzero) and a strictly positive,
    non-underflowed rate (an underflowed rate raises _RateUnderflowedToZero
    instead of reaching here -- see _consecutive_intervals), so every rate
    and every half-segment rate built from the folded view is itself
    strictly positive, and the baseline==0.0 branch below can never actually
    run through project_exhaustion for the folded fields. This function has
    no other caller (the raw view's ``zero_delta_fraction`` and
    ``max_raw_rate_ratio`` are computed by ``_measure_raw_structure``
    without going through ``_relative_difference`` at all), so the
    baseline==0.0 branch is unreachable through project_exhaustion
    entirely. It is kept as an explicit branch rather than an assumption so
    this function stays correct if a future caller reaches it with a
    genuinely zero baseline, and so the zero-baseline convention itself
    stays directly testable without needing to defeat the folding guarantee
    to construct a test case.
    """
    if baseline == 0.0:
        return 0.0 if value == 0.0 else 1.0
    return abs(value - baseline) / abs(baseline)


@dataclass(frozen=True)
class _SingleViewMeasurements:
    """The five folded summary numbers for a segment's consecutive
    intervals. See _measure_view for how these are computed and
    BurnRateProjection's docstring for what each one means.
    """

    max_relative_deviation: float
    max_usage_share: float
    intervals_used: int
    rate_drift: float
    effective_intervals: float


def _measure_view(
    rates: List[float], deltas: List[float], elapsed_list: List[float]
) -> Optional[_SingleViewMeasurements]:
    """Reduce the folded (rate, delta, elapsed) triples to five summary
    numbers. Only ever called on the folded view -- the raw view's two
    structural numbers are computed separately by
    ``_measure_raw_structure``, which does not need a median or a
    per-interval deviation.
    """

    if not rates:
        # For the folded view this is structurally unreachable through
        # project_exhaustion: by the time _interval_measurements runs,
        # latest_usage > first_usage is already established, so the raw
        # deltas summed across the whole segment are positive, which
        # guarantees the folded accumulator closes on a nonzero delta at
        # least once. The raw view has the same guarantee (the same total
        # delta, just distributed across more, possibly-zero, intervals),
        # so this is unreachable for either view in practice. Guarded anyway
        # so a future direct caller gets "no measurement" rather than a
        # crash from median()/max() on empty input.
        return None

    center = median(rates)
    max_relative_deviation = max(_relative_difference(rate, center) for rate in rates)

    total_delta = sum(deltas)
    # total_delta <= 0 should be unreachable: every accumulated delta above
    # is a sum of non-negative raw deltas (usage is non-decreasing within a
    # segment) that is nonzero by construction, so it is strictly positive,
    # and so is their sum. Guarded in the fail-safe direction anyway: this
    # can only make the reported share look MORE cautious, never less.
    max_usage_share = (max(deltas) / total_delta) if total_delta > 0.0 else 1.0

    # Effective number of intervals: the usual inverse-Herfindahl measure of
    # how concentrated the usage-share weights are (1.0 if one interval
    # supplies everything, len(deltas) if every interval supplies an equal
    # share). max_usage_share alone answers "how big is the single worst
    # interval"; it cannot tell a genuine single dominant interval apart
    # from a small CLUSTER of intervals that jointly dominate while each
    # individually stays under any per-interval share.
    sum_of_squares = sum(delta * delta for delta in deltas)
    effective_intervals = (total_delta * total_delta) / sum_of_squares

    # Whole-series drift: does the rate measured over the segment's first
    # half look like the rate measured over its second half? Per-interval
    # deviation and share are both LOCAL -- each compares one interval
    # against the rest -- so neither can see a rate that climbs steadily
    # across many intervals that are each individually unremarkable next to
    # their immediate neighbors.
    if len(rates) < 2:
        # One interval has no second portion to compare against; this is
        # defined as 0.0 (no evidence of drift) rather than left undefined.
        rate_drift = 0.0
    else:
        midpoint = len(rates) // 2
        early_elapsed = sum(elapsed_list[:midpoint])
        late_elapsed = sum(elapsed_list[midpoint:])
        early_rate = sum(deltas[:midpoint]) / early_elapsed
        late_rate = sum(deltas[midpoint:]) / late_elapsed
        rate_drift = _relative_difference(late_rate, early_rate)

    if not all(
        math.isfinite(value)
        for value in (
            max_relative_deviation,
            max_usage_share,
            effective_intervals,
            rate_drift,
        )
    ):
        # Never let a non-finite diagnostic escape to a caller, matching
        # every other finiteness gate in this module. Extreme finite inputs
        # (deltas near the float range limit) could in principle overflow a
        # sum of squares even though every individual value is finite; the
        # honest response is to decline, not hand back a partly-finite
        # result.
        return None

    return _SingleViewMeasurements(
        max_relative_deviation=max_relative_deviation,
        max_usage_share=max_usage_share,
        intervals_used=len(rates),
        rate_drift=rate_drift,
        effective_intervals=effective_intervals,
    )


@dataclass(frozen=True)
class _RawStructuralMeasurements:
    """The three raw (never zero-delta-folded) structural numbers. See
    BurnRateProjection's docstring for what each one means and why the
    folded fields cannot substitute for them.
    """

    zero_delta_fraction: float
    max_raw_rate_ratio: float
    longest_above_overall_rate_run: int


def _raw_zero_delta_fraction(records: List[_HistoryRecord]) -> float:
    """Fraction of raw adjacent-capture gaps whose usage delta is exactly
    zero, computed directly from consecutive record pairs.

    This is deliberately NOT derived from ``_raw_intervals``'s deltas.
    ``_consecutive_intervals`` unconditionally carries a gap's elapsed time
    (and delta) forward whenever the ACCUMULATED elapsed time is still below
    MIN_INTERVAL_SECONDS -- that carry-forward applies regardless of
    ``fold_zero_delta`` (see its docstring), so a sub-microsecond gap gets
    coalesced into its neighbor even in the "raw" view. [(0,96),(60,96),
    (60.0000005,97),(120,98)] has one zero-delta gap among three raw
    capture-to-capture gaps (1/3), but the coalesced view merges the
    sub-microsecond (60,96)->(60.0000005,97) gap into a neighbor and reports
    0.5. ``zero_delta_fraction`` is documented as a property of the raw
    captures ("how quantized is this data"), not of the measurement
    intervals folding or coalescing produces, so it is measured here
    independently of both.
    """

    gap_count = len(records) - 1
    if gap_count <= 0:
        # A single-record segment has no gap to measure. Unreachable through
        # project_exhaustion (MIN_SAMPLES requires at least 3 records), but
        # guarded rather than left to divide by zero for a future direct
        # caller.
        return 0.0
    zero_gaps = sum(
        1
        for previous, current in zip(records, records[1:])
        if current.used_percentage == previous.used_percentage
    )
    return zero_gaps / gap_count


def _longest_above_overall_rate_run(rates: List[float], overall_rate: float) -> int:
    """Longest run of consecutive raw intervals whose rate strictly exceeds
    ``overall_rate``.

    (MEDIUM finding, round 9) Every other measurement in this module is
    order-blind: a max, a median, a sum, or a sum of squares over the
    interval rates gives the same answer no matter what order the intervals
    came in, so two segments with the same rates in a different order are
    indistinguishable to all of them. Concretely, 60-second deltas
    [10,10,1,1,10,10,1,1] (two clusters of fast intervals) and
    [10,1,10,1,10,1,10,1] (the same eight rates, strictly alternating)
    produce identical values for every one of the seven measurements above,
    the fitted rate, and the exhaustion time -- yet they describe different
    burn patterns: one has a sustained burst, the other spreads the same
    total usage evenly across the whole segment.

    This field is the one exception, by construction: it walks the
    intervals IN ORDER and counts the longest streak of adjacent intervals
    each running faster than the segment's overall pace. The clustered
    series above scores 2 (each pair of adjacent "10"s); the alternating
    series scores 1 (no "10" is ever next to another "10"). That is the
    only additional measurement this module adds for order-sensitivity --
    see BurnRateProjection's docstring for why one field is the deliberate
    stopping point, not the start of an unbounded search for the next
    counterexample.
    """

    longest = 0
    current = 0
    for rate in rates:
        if rate > overall_rate:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _measure_raw_structure(
    records: List[_HistoryRecord],
) -> Optional[_RawStructuralMeasurements]:
    """Reduce the raw (unfolded) view of a segment's intervals to
    ``zero_delta_fraction``, ``max_raw_rate_ratio``, and
    ``longest_above_overall_rate_run``.

    This replaced a five-field raw mirror of the folded summary numbers
    (built by running the raw triples through ``_measure_view``, the same
    function the folded view uses). A round of adversarial review proved
    that mirror did not discriminate a quantized-steady series from a
    genuine burst -- both saturate ``_measure_view``'s median-based
    deviation at 1.0 the moment the raw view contains any zero-delta gap,
    which real data always does. These numbers are computed directly
    instead, without ever taking a median of the raw rates.

    May raise _RateUnderflowedToZero -- from ``_raw_intervals`` itself (a
    per-interval underflow, see ``_consecutive_intervals``), or from the
    overall-rate division below (a whole-segment underflow, the same
    failure mode at a different granularity). Either way the caller
    (``_interval_measurements``) lets it propagate to ``_project_group``,
    which declines the whole projection rather than report a ratio built
    on an unrepresentable rate.
    """

    rates, deltas, _elapsed_list = _raw_intervals(records)
    if not rates:
        # Mirrors _measure_view's own guard and is unreachable through
        # project_exhaustion for the same reason: a nonzero total delta
        # (already established before this function is ever reached)
        # guarantees the raw accumulator closes on at least one interval.
        # Kept so a future direct caller gets "no measurement" instead of a
        # crash from max()/sum() on empty input.
        return None

    zero_delta_fraction = _raw_zero_delta_fraction(records)

    # The overall rate is "the rate the segment would report if treated as
    # one interval start to finish" (see BurnRateProjection's
    # max_raw_rate_ratio docstring) -- so its denominator is the segment's
    # actual start-to-finish span, ``records[-1].captured_at -
    # records[0].captured_at`` (the same formula _project_group uses for
    # span_seconds, recomputed here from the identical records list rather
    # than threaded through as a parameter). This is deliberately NOT
    # sum(_elapsed_list): _raw_intervals silently drops a trailing
    # accumulator whose delta is exactly zero (see _consecutive_intervals's
    # docstring, "A trailing accumulator...exactly zero") -- no usage was
    # lost, so nothing declines, but that gap's ELAPSED time is real and
    # summing only the retained intervals would exclude it, shrinking the
    # denominator. That makes the overall rate too LARGE, which suppresses
    # this ratio and can shorten longest_above_overall_rate_run, for no
    # reason connected to the segment's actual burn pattern. Measured on
    # the case below: excluding the gap gives ratio 1.0 and run 0, while
    # including it gives 1.0000000083 and 1.
    # [(0,0),(60,10),(60.0000005,10)] is the
    # concrete case: the final 5e-7s gap carries no usage (delta 0) so it is
    # dropped from _raw_intervals's own elapsed sum, but it still elapsed --
    # sum(_elapsed_list) here would be 60.0, undercounting the true 60.0000005s
    # span. A NONZERO terminal remainder is a different case entirely,
    # already handled upstream: it cannot be silently absorbed either way
    # (see _TerminalRemainderUnmergeable), so the whole projection declines
    # with NoProjectionReason.TERMINAL_REMAINDER_UNMERGEABLE before this
    # function is ever reached, and this ratio is not computed at all.
    total_delta = sum(deltas)
    span_seconds = records[-1].captured_at - records[0].captured_at
    overall_rate = total_delta / span_seconds
    if overall_rate == 0.0:
        # total_delta > 0 here is the same invariant _measure_view relies
        # on (this function is never reached unless latest_usage >
        # first_usage), so a zero overall rate is float underflow, not
        # genuine zero usage -- the same failure _RateUnderflowedToZero
        # already names at the single-interval level. Dividing by it would
        # report an infinite ratio for data that is not infinitely bursty,
        # so this declines instead. Structurally this should be
        # unreachable in practice (every per-gap delta that fed total_delta
        # already passed through _raw_intervals's own underflow guard), but
        # is guarded rather than assumed away.
        raise _RateUnderflowedToZero(
            f"total delta {total_delta!r} over {span_seconds!r}s "
            "underflowed to a zero overall rate"
        )

    # This ratio is >= 1.0 mathematically (the overall rate is the
    # elapsed-time-weighted AVERAGE of the per-interval rates, so no single
    # rate can be below it without another being above it enough to
    # compensate, and max() picks the largest). But float division does not
    # honor that exactly: [(0,0),(227.5307793480892,0.00011422248629255103),
    # (90941578.55322355,45.65348582499713)] computes 0.9999999999999998,
    # a hair under 1.0, from ordinary rounding in the two divisions (the
    # per-interval rate and the overall rate) rather than any real interval
    # running slower than the segment's average. Clamping to the
    # mathematical floor here is honest -- it is not manufacturing evidence,
    # it is refusing to let a rounding artifact contradict a fact the
    # numbers themselves proved -- unlike, say, clamping a genuinely
    # ambiguous measurement to a convenient value.
    max_raw_rate_ratio = max(1.0, max(rates) / overall_rate)
    longest_above_overall_rate_run = _longest_above_overall_rate_run(rates, overall_rate)
    if not math.isfinite(zero_delta_fraction) or not math.isfinite(max_raw_rate_ratio):
        # Never let a non-finite diagnostic escape, matching every other
        # finiteness gate in this module. longest_above_overall_rate_run is
        # a plain non-negative integer count, not a division result, so it
        # has no non-finite case to guard against.
        return None

    return _RawStructuralMeasurements(
        zero_delta_fraction=zero_delta_fraction,
        max_raw_rate_ratio=max_raw_rate_ratio,
        longest_above_overall_rate_run=longest_above_overall_rate_run,
    )


def _interval_measurements(
    records: List[_HistoryRecord],
) -> Optional[_IntervalMeasurements]:
    """Measure how consistent the segment's intervals are: five folded
    summary numbers plus three raw structural numbers.

    See BurnRateProjection's docstring for what each of the eight numbers
    means and why the library reports both views instead of collapsing them
    into a tier. May raise _RateUnderflowedToZero or
    _TerminalRemainderUnmergeable -- see _consecutive_intervals and
    _measure_raw_structure -- which the caller (_project_group) turns into
    a declined projection rather than catching here, so those stay distinct
    from the ordinary "no measurement" None case below.
    """

    folded = _measure_view(*_folded_intervals(records))
    raw_structure = _measure_raw_structure(records)
    if folded is None or raw_structure is None:
        return None

    return _IntervalMeasurements(
        max_relative_deviation=folded.max_relative_deviation,
        max_usage_share=folded.max_usage_share,
        intervals_used=folded.intervals_used,
        rate_drift=folded.rate_drift,
        effective_intervals=folded.effective_intervals,
        zero_delta_fraction=raw_structure.zero_delta_fraction,
        max_raw_rate_ratio=raw_structure.max_raw_rate_ratio,
        longest_above_overall_rate_run=raw_structure.longest_above_overall_rate_run,
    )
