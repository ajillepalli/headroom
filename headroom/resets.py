"""Reset timestamp parsing and plausibility checks."""

from datetime import datetime, timezone
import math
from typing import Any, Mapping, Optional, Tuple


# Contemporary Unix seconds are about 1e9 while milliseconds are about 1e12.
# 1e11 is an unambiguous midpoint and remains above epoch seconds until year 5138.
EPOCH_MILLISECONDS_THRESHOLD = 100_000_000_000.0
RESET_GRACE_SECONDS = 5 * 60.0
UNKNOWN_WINDOW_MAX_AHEAD_SECONDS = 14 * 24 * 60 * 60.0
_WINDOW_KEYS = ("window_minutes", "windowMinutes", "windowDurationMins")


def parse_reset_time(value: Any) -> Optional[float]:
    """Parse epoch seconds, epoch milliseconds, or an ISO-8601 timestamp."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number < 0.0:
            return None
        if number >= EPOCH_MILLISECONDS_THRESHOLD:
            number /= 1000.0
        return number
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            number = float(stripped)
        except ValueError:
            iso_value = (
                stripped[:-1] + "+00:00"
                if stripped.endswith("Z")
                else stripped
            )
            try:
                parsed = datetime.fromisoformat(iso_value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except (ValueError, OverflowError, OSError):
                return None
        return parse_reset_time(number)
    return None


def parse_plausible_reset_time(
    value: Any,
    reference_time: float,
    window_minutes: Any = None,
) -> Tuple[Optional[float], Optional[str]]:
    """Parse a reset time and reject values outside a realistic window."""

    if value is None:
        return None, None
    parsed = parse_reset_time(value)
    if parsed is None:
        return None, "invalid resets_at {!r}".format(value)
    if not reset_time_is_plausible(parsed, reference_time, window_minutes):
        return None, "rejected implausible resets_at {!r}".format(value)
    return parsed, None


def reset_time_is_plausible(
    resets_at: float,
    reference_time: float,
    window_minutes: Any = None,
) -> bool:
    """Return whether a reset falls near the capture's rate-limit window."""

    try:
        reset = float(resets_at)
        reference = float(reference_time)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(reset) or not math.isfinite(reference):
        return False
    duration = _positive_finite_number(window_minutes)
    ahead = (
        duration * 60.0 + RESET_GRACE_SECONDS
        if duration is not None
        else UNKNOWN_WINDOW_MAX_AHEAD_SECONDS
    )
    return reference - RESET_GRACE_SECONDS <= reset <= reference + ahead


def window_minutes_from_raw(raw: Mapping[str, Any]) -> Optional[float]:
    """Read a supported window duration field from raw snapshot data."""

    for key in _WINDOW_KEYS:
        if key in raw:
            return _positive_finite_number(raw[key])
    return None


def _positive_finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None
