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

# Two matching intervals is one coincidence, not a pattern: it is
# indistinguishable from two readings that happen to agree by chance. HIGH
# confidence claims the rate is dependably steady, so it needs a third
# independent interval before that claim is defensible.
MIN_INTERVALS_FOR_HIGH_CONFIDENCE = 3

# HIGH confidence asserts that EVERY interval sampled the same underlying
# rate, not merely that the intervals agree "on average". A mean-based
# statistic -- however it is weighted -- can always be satisfied by one
# catastrophic interval as long as enough agreeing intervals surround it,
# because averaging is precisely the operation that dilutes an outlier by
# the size of the crowd around it. This is a hard mathematical ceiling: no
# choice of weights fixes it (three rounds of review confirmed that: a
# sign-based check, then a recency-weighted mean, then the worse of a
# recency-weighted and an unweighted mean all still passed a 500-interval
# segment where a single interval carried 20% of the total usage change).
# The only fix is a check that a mean can never launder: the single WORST
# interval, compared on its own to the group's median rate. A factor of 4
# marks the line between "noisy but recognizably the same rate" and "a
# different regime entirely" -- ordinary sampling jitter on a steady
# process moves a reading by tens of percent, not by multiples of itself,
# so an interval running at 4x (or 1/4) the median rate is not the same
# process sampled twice, and HIGH must not claim it is.
MAX_INTERVAL_RATE_RATIO = 4.0


class ProjectionConfidence(str, Enum):
    """How consistently the history supports the fitted direction."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class NoProjectionReason(str, Enum):
    """Why an exhaustion time could not be defended."""

    TOO_FEW_SAMPLES = "too_few_samples"
    SPAN_TOO_SHORT = "span_too_short"
    INSUFFICIENT_SPAN_FOR_HORIZON = "insufficient_span_for_horizon"
    ALREADY_EXHAUSTED = "already_exhausted"
    FLAT_USAGE = "flat_usage"
    USAGE_WENT_BACKWARDS = "usage_went_backwards"
    NON_POSITIVE_RATE = "non_positive_rate"
    LOW_CONFIDENCE = "low_confidence"
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
    """

    source: str
    window: str
    rate_percent_per_second: Optional[float]
    projected_exhaustion_at: Optional[float]
    exhaustion_precedes_reset: Optional[bool]
    samples_used: int
    span_seconds: Optional[float]
    confidence: ProjectionConfidence
    reason: Optional[NoProjectionReason] = None


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
        confidence: ProjectionConfidence = ProjectionConfidence.NONE,
    ) -> BurnRateProjection:
        return BurnRateProjection(
            source=source,
            window=window,
            rate_percent_per_second=rate,
            projected_exhaustion_at=None,
            exhaustion_precedes_reset=None,
            samples_used=samples_used,
            span_seconds=span_seconds,
            confidence=confidence,
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

    confidence = _confidence_for_records(records)
    if span_seconds < horizon_seconds * MIN_SPAN_TO_HORIZON_RATIO:
        return unavailable(
            NoProjectionReason.INSUFFICIENT_SPAN_FOR_HORIZON,
            rate,
            confidence,
        )
    if confidence is ProjectionConfidence.LOW:
        return unavailable(NoProjectionReason.LOW_CONFIDENCE, rate, confidence)
    if projected_at < now:
        # The rate is stale enough that, projected forward, we'd already be
        # past exhaustion by "now" without a fresher reading confirming it.
        # Stating a past timestamp as a live projection is not defensible.
        return unavailable(
            NoProjectionReason.PROJECTED_EXHAUSTION_IN_PAST, rate, confidence
        )

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
        confidence=confidence,
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


def _confidence_for_records(records: List[_HistoryRecord]) -> ProjectionConfidence:
    """Rate confidence by how CONSISTENT the burn rate is, not by its sign.

    Usage is monotonic within a segment, so nearly every pairwise slope is
    positive; a sign-based check is close to tautological and was the root
    cause of a real bug (a single 1-second terminal burst after hours of
    near-flat usage previously scored HIGH confidence). Instead this looks at
    consecutive, non-overlapping intervals (independent evidence, unlike the
    combinatorial pairwise slopes used for the rate itself) and measures how
    much they disagree: the mean absolute deviation from the median interval
    rate, normalized by that median, computed two ways and combined.

    Recency weighting (oldest interval weight 1, newest weight N) matters
    because a burst in the most recent interval is exactly what would
    corrupt a near-term projection. But recency weighting alone can also
    dilute a single wildly deviant interval into irrelevance purely because
    of where it sits in the sequence: 500 one-second readings where the
    FIRST interval carries half the entire quota still scored a
    recency-weighted dispersion of ~0.004 (HIGH) because that interval's
    weight of 1 was swamped by 499 agreeing neighbors weighted up to 499.
    An unweighted (equal-weight) mean absolute deviation acts as a veto
    against exactly that: it cannot be diluted by position, so a single
    interval that disagrees sharply with the rest keeps its full statistical
    weight regardless of whether it is oldest or newest. Taking the WORSE
    (larger) of the weighted and unweighted ratios means either kind of
    inconsistency -- a recent burst or a single buried outlier -- can veto
    HIGH confidence.

    Both of the above are still MEANS, and a mean of N numbers can always be
    kept small by making N large enough: a single interval consuming 20% of
    the segment's total usage change, spread across 500 total intervals,
    produces an unweighted mean deviation ratio that lands just UNDER the
    HIGH cutoff (proven in
    test_dominant_interval_diluted_across_many_samples_is_not_high_confidence
    below). No mean-based statistic can close this gap, because dilution by
    sample count is exactly what a mean does. What cannot be diluted by
    sample count is a MAXIMUM: comparing each interval's rate to the
    group's median individually, keeping only the single worst ratio. See
    MAX_INTERVAL_RATE_RATIO for what that ratio means and why 4x is the
    cutoff. The mean-based ratios above are kept because they still add
    value for cases the max-based check does not target (e.g. broad,
    evenly-spread disagreement where no single interval is an outlier), but
    HIGH now also requires the max-based check to pass on its own.

    HIGH additionally requires at least MIN_INTERVALS_FOR_HIGH_CONFIDENCE
    independent intervals: two intervals agreeing is one coincidence, not a
    demonstrated pattern, and is indistinguishable from chance agreement.
    """

    intervals = []
    for index in range(1, len(records)):
        previous = records[index - 1]
        current = records[index]
        elapsed = current.captured_at - previous.captured_at
        if elapsed < MIN_INTERVAL_SECONDS:
            continue
        rate = (current.used_percentage - previous.used_percentage) / elapsed
        if math.isfinite(rate):
            intervals.append(rate)

    if len(intervals) < 2:
        # Only one independent interval means there is no second, separate
        # observation to check it against: nothing here supports HIGH.
        return ProjectionConfidence.LOW

    center = median(intervals)
    if center == 0.0:
        return ProjectionConfidence.LOW

    weighted_deviation = 0.0
    weight_total = 0.0
    unweighted_deviation = 0.0
    for rank, rate in enumerate(intervals, start=1):
        deviation = abs(rate - center)
        weighted_deviation += rank * deviation
        weight_total += rank
        unweighted_deviation += deviation
    weighted_ratio = (weighted_deviation / weight_total) / abs(center)
    unweighted_ratio = (unweighted_deviation / len(intervals)) / abs(center)
    dispersion_ratio = max(weighted_ratio, unweighted_ratio)

    # The max-based veto: how far the SINGLE worst interval sits from the
    # median, as a multiple (>= 1.0), regardless of how many agreeing
    # intervals surround it. An interval at exactly the median rate scores
    # 1.0; an interval at 0 while the median is positive scores infinity,
    # since a rate of zero is not "close to" a positive median by any
    # multiple -- it is a different regime (e.g. a stall), which is exactly
    # the kind of single-interval evidence a mean can hide but a max cannot.
    max_interval_ratio = 1.0
    for rate in intervals:
        higher, lower = (rate, center) if rate >= center else (center, rate)
        if lower <= 0.0:
            max_interval_ratio = math.inf
            break
        max_interval_ratio = max(max_interval_ratio, higher / lower)

    if (
        len(intervals) >= MIN_INTERVALS_FOR_HIGH_CONFIDENCE
        and dispersion_ratio <= 0.25
        and max_interval_ratio <= MAX_INTERVAL_RATE_RATIO
    ):
        return ProjectionConfidence.HIGH
    if dispersion_ratio <= 0.75:
        return ProjectionConfidence.MEDIUM
    return ProjectionConfidence.LOW
