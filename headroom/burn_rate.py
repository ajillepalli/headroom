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
    at least half the folded intervals report a zero rate, which folding
    itself rules out (every folded interval has a strictly positive delta by
    construction, so every rate here is strictly positive too) -- but the
    ratio's zero-baseline case is still defined, not left to divide by zero,
    as a defensive convention: 0.0 if every interval is exactly zero (trivial
    agreement) and 1.0 (maximal, but finite) otherwise. This field is always
    finite -- never inf or nan -- whenever it is not None.

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

    measurements = _interval_measurements(records)
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


@dataclass(frozen=True)
class _IntervalMeasurements:
    """The five diagnostic numbers folded into a successful projection."""

    max_relative_deviation: float
    max_usage_share: float
    intervals_used: int
    rate_drift: float
    effective_intervals: float


def _folded_intervals(
    records: List[_HistoryRecord],
) -> Tuple[List[float], List[float], List[float]]:
    """Build the (rate, delta, elapsed) triples _interval_measurements uses.

    Consecutive, non-overlapping intervals are used here (not the
    combinatorial pairwise slopes _pairwise_slopes builds for the fitted
    rate) because they are independent evidence: each raw capture appears in
    exactly one interval, so one bad reading corrupts only the interval it
    touches, unlike a pairwise slope where it corrupts every pair built from
    it.

    A raw interval whose usage delta is EXACTLY zero is folded forward into
    the next reading that actually changed, instead of being kept as its own
    zero-rate interval. See BurnRateProjection's docstring for why: real
    captures round used_percentage to whole points, so a genuinely
    sub-point-per-interval rate reports as a long run of zero-delta readings
    interrupted by occasional single-point jumps, and treating each
    zero-delta reading as independent evidence manufactures instability out
    of quantization alone. This folding is exact, not a tuned tolerance: it
    only merges intervals that reported literally no change, so a segment
    where every raw delta is already nonzero is completely unaffected.
    """

    rates: List[float] = []
    deltas: List[float] = []
    elapsed_list: List[float] = []
    accumulated_delta = 0.0
    accumulated_elapsed = 0.0
    for index in range(1, len(records)):
        previous = records[index - 1]
        current = records[index]
        elapsed = current.captured_at - previous.captured_at
        if elapsed < MIN_INTERVAL_SECONDS:
            continue
        accumulated_delta += current.used_percentage - previous.used_percentage
        accumulated_elapsed += elapsed
        if accumulated_delta == 0.0:
            continue
        rate = accumulated_delta / accumulated_elapsed
        if math.isfinite(rate):
            rates.append(rate)
            deltas.append(accumulated_delta)
            elapsed_list.append(accumulated_elapsed)
        accumulated_delta = 0.0
        accumulated_elapsed = 0.0
    return rates, deltas, elapsed_list


def _relative_difference(value: float, baseline: float) -> float:
    """|value - baseline| / baseline, with a finite convention at baseline 0.

    A zero baseline has no positive scale to express a RATIO against, so
    "how many times bigger" is undefined by division. This is defined
    instead as 0.0 when value also equals the (zero) baseline -- trivial
    agreement -- and 1.0 (maximal, but finite) for any other value, so the
    result is always finite here, never inf or nan, regardless of baseline.

    Every current caller passes a strictly positive baseline in practice:
    _folded_intervals only ever emits intervals with a strictly positive
    delta (usage is non-decreasing within a segment and an interval is only
    recorded once its accumulated delta is nonzero), so every rate and every
    half-segment rate built from those intervals is itself strictly
    positive, and the baseline==0.0 branch below can never actually run
    through project_exhaustion. It is kept as an explicit branch rather than
    an assumption so this function stays correct if that guarantee is ever
    loosened, and so the zero-baseline convention itself stays directly
    testable without needing to defeat the folding guarantee to construct a
    test case.
    """
    if baseline == 0.0:
        return 0.0 if value == 0.0 else 1.0
    return abs(value - baseline) / abs(baseline)


def _interval_measurements(
    records: List[_HistoryRecord],
) -> Optional[_IntervalMeasurements]:
    """Measure how consistent the segment's folded intervals are.

    See BurnRateProjection's docstring for what each of the five numbers
    means and why the library reports them instead of collapsing them into
    a tier.
    """

    rates, deltas, elapsed_list = _folded_intervals(records)
    if not rates:
        # Structurally unreachable through project_exhaustion: by the time
        # this runs, latest_usage > first_usage is already established, so
        # the raw deltas summed across the whole segment are positive, which
        # guarantees the accumulator above closes on a nonzero delta at
        # least once. Guarded anyway so a future direct caller of this
        # private function gets "no measurement" rather than a crash from
        # median()/max() on empty input.
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

    return _IntervalMeasurements(
        max_relative_deviation=max_relative_deviation,
        max_usage_share=max_usage_share,
        intervals_used=len(rates),
        rate_drift=rate_drift,
        effective_intervals=effective_intervals,
    )
