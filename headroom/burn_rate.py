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
    ``max_raw_rate_ratio``, rather than relied on by itself.

    ``max_raw_rate_ratio`` is the largest single raw interval's rate,
    divided by the segment's OVERALL rate (total usage delta over total
    elapsed time -- the rate the segment would report if treated as one
    interval start to finish). Because the overall rate is the
    elapsed-time-weighted average of every raw interval's rate, this ratio
    is always >= 1.0, with equality only when every raw interval runs at
    exactly the same rate. LOW (near 1.0) means no single raw interval ran
    meaningfully faster than the segment's overall pace -- consistent with a
    genuinely steady rate, quantized or not. HIGH means one raw interval's
    rate hugely exceeds the segment average: the signature of a genuine
    burst, which is exactly the case a folded field can smooth away
    (folding merges a burst's neighboring flat gaps into it, diluting its
    rate down toward the ordinary-looking average of the whole merged
    span). A caller who sees a low folded ``max_relative_deviation`` next to
    a high ``max_raw_rate_ratio`` is looking at a burst folding smoothed
    over, not a genuinely steady rate.

    Both fields are computed over the raw (unfolded) view: every gap
    between consecutive captures is its own interval, zero-delta gaps
    included, except that a gap whose own accumulated elapsed time is below
    MIN_INTERVAL_SECONDS is still carried forward (see
    ``_consecutive_intervals``) rather than divided into float noise. Both
    are None exactly when no projection was made (``reason`` is set), same
    as the five folded fields; a projection with measurements always has
    both.
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
    )


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


@dataclass(frozen=True)
class _IntervalMeasurements:
    """The seven diagnostic numbers (five folded, two raw structural)
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
    function has a "next" interval to fold into; this one does not, and an
    earlier version of this function simply dropped it, which silently
    broke usage conservation (the segment's reported deltas summed to less
    than its actual total usage change). The fix folds it BACKWARD into the
    last already-measured interval instead of forward: widening that
    interval's own delta and elapsed time by the leftover amounts and
    recomputing its rate. That is the conservation-preserving choice that
    invents nothing -- it is the same "merge a too-short gap into a real
    interval" operation used everywhere else in this function, just run in
    the only direction available at the end of a segment. A trailing
    accumulator whose leftover delta is exactly zero needs no such fix: it
    contributes nothing to the usage total either way, so it can still be
    dropped cleanly the same as before.
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
        # usage with no following gap to carry it into. Fold it backward
        # into the last measured interval so it is not silently dropped.
        if rates:
            deltas[-1] += accumulated_delta
            elapsed_list[-1] += accumulated_elapsed
            merged_rate = deltas[-1] / elapsed_list[-1]
            if merged_rate == 0.0:
                # deltas[-1] is strictly positive after the merge (it is a
                # non-negative prior value plus a strictly positive
                # accumulated_delta, since usage never decreases within a
                # segment), so a zero merged_rate here is the same
                # underflow-not-genuine-zero failure the loop above already
                # guards against, not trivial agreement.
                raise _RateUnderflowedToZero(
                    f"terminal delta {accumulated_delta!r} merged into the "
                    "previous interval underflowed its rate to zero"
                )
            rates[-1] = merged_rate
        else:
            # No previous interval exists to merge into. Structurally
            # unreachable through project_exhaustion: _project_group only
            # reaches this function after MIN_SPAN_SECONDS (>=60s of total
            # span) and FLAT_USAGE (a nonzero total delta) have both been
            # established, which together guarantee at least one interval
            # flushes in the loop above before any trailing sub-threshold
            # remainder could appear. Declined rather than assumed away, in
            # case a future direct caller reaches this function with
            # unsegmented or single-gap input.
            raise _RateUnderflowedToZero(
                f"terminal delta {accumulated_delta!r} over "
                f"{accumulated_elapsed!r}s has no prior interval to merge "
                "into"
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
    """The two raw (never zero-delta-folded) structural numbers. See
    BurnRateProjection's docstring for what each one means and why the
    folded fields cannot substitute for them.
    """

    zero_delta_fraction: float
    max_raw_rate_ratio: float


def _measure_raw_structure(
    records: List[_HistoryRecord],
) -> Optional[_RawStructuralMeasurements]:
    """Reduce the raw (unfolded) view of a segment's intervals to
    ``zero_delta_fraction`` and ``max_raw_rate_ratio``.

    This replaced a five-field raw mirror of the folded summary numbers
    (built by running the raw triples through ``_measure_view``, the same
    function the folded view uses). A round of adversarial review proved
    that mirror did not discriminate a quantized-steady series from a
    genuine burst -- both saturate ``_measure_view``'s median-based
    deviation at 1.0 the moment the raw view contains any zero-delta gap,
    which real data always does. These two numbers are computed directly
    instead, without ever taking a median of the raw rates.

    May raise _RateUnderflowedToZero -- from ``_raw_intervals`` itself (a
    per-interval underflow, see ``_consecutive_intervals``), or from the
    overall-rate division below (a whole-segment underflow, the same
    failure mode at a different granularity). Either way the caller
    (``_interval_measurements``) lets it propagate to ``_project_group``,
    which declines the whole projection rather than report a ratio built
    on an unrepresentable rate.
    """

    rates, deltas, elapsed_list = _raw_intervals(records)
    if not rates:
        # Mirrors _measure_view's own guard and is unreachable through
        # project_exhaustion for the same reason: a nonzero total delta
        # (already established before this function is ever reached)
        # guarantees the raw accumulator closes on at least one interval.
        # Kept so a future direct caller gets "no measurement" instead of a
        # crash from max()/sum() on empty input.
        return None

    zero_delta_fraction = sum(1 for delta in deltas if delta == 0.0) / len(deltas)

    # The overall rate is the elapsed-time-weighted average of every raw
    # interval's rate: sum(delta)/sum(elapsed) == sum(rate*elapsed)/sum(elapsed).
    # That makes it a fixed reference point comparable across segments with
    # different interval counts, unlike comparing against the (also
    # interval-count-sensitive) median _measure_view uses for the folded
    # deviation.
    total_delta = sum(deltas)
    total_elapsed = sum(elapsed_list)
    overall_rate = total_delta / total_elapsed
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
            f"total delta {total_delta!r} over {total_elapsed!r}s "
            "underflowed to a zero overall rate"
        )

    max_raw_rate_ratio = max(rates) / overall_rate
    if not math.isfinite(zero_delta_fraction) or not math.isfinite(max_raw_rate_ratio):
        # Never let a non-finite diagnostic escape, matching every other
        # finiteness gate in this module.
        return None

    return _RawStructuralMeasurements(
        zero_delta_fraction=zero_delta_fraction,
        max_raw_rate_ratio=max_raw_rate_ratio,
    )


def _interval_measurements(
    records: List[_HistoryRecord],
) -> Optional[_IntervalMeasurements]:
    """Measure how consistent the segment's intervals are: five folded
    summary numbers plus two raw structural numbers.

    See BurnRateProjection's docstring for what each of the seven numbers
    means and why the library reports both views instead of collapsing them
    into a tier. May raise _RateUnderflowedToZero -- see
    _consecutive_intervals and _measure_raw_structure -- which the caller
    (_project_group) turns into a declined projection rather than catching
    here, so it stays distinct from the ordinary "no measurement" None case
    below.
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
    )
