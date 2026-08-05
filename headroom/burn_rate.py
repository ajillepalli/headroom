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


@dataclass(frozen=True)
class BurnRateProjection:
    """The fitted burn rate and exhaustion decision for one usage window."""

    source: str
    window: str
    rate_percent_per_second: Optional[float]
    projected_exhaustion_at: Optional[float]
    exhaustion_precedes_reset: Optional[bool]
    samples_used: int
    span_seconds: float
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
    except (FileNotFoundError, IsADirectoryError):
        return []

    projections = []
    for key in sorted(groups):
        records = sorted(groups[key], key=lambda item: item.captured_at)
        latest_segment = _records_since_latest_reset(records)
        projections.append(_project_group(key, latest_segment))
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
    if not records:
        return []

    latest_resets_at = records[-1].resets_at
    segment_start = len(records) - 1
    while (
        segment_start > 0
        and records[segment_start - 1].resets_at == latest_resets_at
    ):
        segment_start -= 1

    # Repeated captures can share a timestamp. Keeping only the last one avoids
    # counting observations that add no time information as independent evidence.
    by_timestamp: Dict[float, _HistoryRecord] = {}
    for record in records[segment_start:]:
        by_timestamp[record.captured_at] = record
    return [by_timestamp[captured_at] for captured_at in sorted(by_timestamp)]


def _project_group(
    key: Tuple[str, str], records: List[_HistoryRecord]
) -> BurnRateProjection:
    source, window = key
    samples_used = len(records)
    span_seconds = (
        records[-1].captured_at - records[0].captured_at
        if samples_used > 1
        else 0.0
    )

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
        return unavailable(NoProjectionReason.USAGE_WENT_BACKWARDS, rate)
    if rate is None or rate <= 0.0:
        return unavailable(NoProjectionReason.NON_POSITIVE_RATE, rate)

    latest = records[-1]
    projected_at = latest.captured_at + (100.0 - latest_usage) / rate
    horizon_seconds = projected_at - latest.captured_at
    confidence = _confidence_for_slopes(slopes)
    if span_seconds < horizon_seconds * MIN_SPAN_TO_HORIZON_RATIO:
        return unavailable(
            NoProjectionReason.INSUFFICIENT_SPAN_FOR_HORIZON,
            rate,
            confidence,
        )
    if confidence is ProjectionConfidence.LOW:
        return unavailable(NoProjectionReason.LOW_CONFIDENCE, rate, confidence)

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
            if elapsed > 0.0:
                slopes.append(
                    (right.used_percentage - left.used_percentage) / elapsed
                )
    return slopes


def _confidence_for_slopes(slopes: List[float]) -> ProjectionConfidence:
    positive_fraction = sum(slope > 0.0 for slope in slopes) / len(slopes)
    # This marker describes directional agreement, not a probability. That
    # keeps it tied to evidence in the samples rather than inventing precision.
    if positive_fraction >= 0.9:
        return ProjectionConfidence.HIGH
    if positive_fraction >= 0.75:
        return ProjectionConfidence.MEDIUM
    return ProjectionConfidence.LOW
