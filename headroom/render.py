"""Pure text rendering for status, reports, and model context."""

from typing import List, Optional, Sequence

from .bounds import Confidence, Reading
from .severity import Severity, reading_severity


def render_statusline(readings: Sequence[Reading], now: float) -> str:
    """Render a compact line that is safe to print for every statusline call."""

    known = [reading for reading in readings if reading.lower_bound_percent is not None]
    if not known:
        return "headroom: usage unavailable"
    parts = ["{} {} {}".format(_source(reading), _window(reading.window), _usage(reading)) for reading in known]
    return "headroom: " + " | ".join(parts)


def render_report(readings: Sequence[Reading], now: float) -> str:
    """Render all tool and window combinations as a human report."""

    lookup = {(reading.source, reading.window): reading for reading in readings}
    lines: List[str] = []
    for source in ("claude", "codex"):
        lines.append(_source_name(source))
        for window in ("short", "weekly"):
            reading = lookup.get((source, window))
            if reading is None or reading.lower_bound_percent is None:
                lines.append("  {}: unavailable".format(_window(window)))
                continue
            severity = str(reading_severity(reading))
            reset = _reset_phrase(reading, now)
            lines.append("  {}: {} [{}]{}".format(_window(window), _usage(reading), severity, reset))
    return "\n".join(lines)


def render_hook(
    readings: Sequence[Reading],
    now: float,
    forced_severity: Optional[Severity] = None,
) -> str:
    """Render brief model guidance, or an empty string when no action is needed."""

    actionable = [reading for reading in readings if reading_severity(reading) is not Severity.OK]
    if not actionable and forced_severity is None:
        return ""
    candidates = actionable or [
        reading
        for reading in readings
        if reading.lower_bound_percent is not None
    ]
    candidates.sort(
        key=lambda item: (
            int(reading_severity(item)),
            item.lower_bound_percent or 0.0,
        ),
        reverse=True,
    )
    reading = candidates[0] if candidates else None
    lines: List[str] = []
    if forced_severity is not None:
        lines.append(
            "FORCED TEST ({}): diagnostic output, not a real usage warning.".format(
                forced_severity
            )
        )
    if reading is None:
        lines.append("Usage headroom: no reading is available.")
    else:
        reset = _reset_plain(reading, now)
        lines.append(
            "Usage headroom: {} {} {} (reading {} old), {}.".format(
                _source_name(reading.source),
                reading.window,
                _usage(reading),
                _duration(reading.age_seconds),
                reset,
            )
        )
    severity = forced_severity or reading_severity(reading)
    if severity is Severity.CRITICAL:
        action = "Stop parallel subagent fan-out, use cheaper models, and checkpoint work now."
    elif severity is Severity.WARN:
        action = "Prefer cheaper models, avoid parallel subagent fan-out, and checkpoint soon."
    else:
        action = "Conserve usage and avoid unnecessary parallel work."
    lines.append(action)
    return "\n".join(lines)


def _usage(reading: Reading) -> str:
    if reading.lower_bound_percent is None:
        return "unknown"
    marker = ">=" if reading.confidence is Confidence.STALE_BOUNDED else ""
    return "{}{}% used".format(marker, _number(reading.lower_bound_percent))


def _number(value: float) -> str:
    return "{:.0f}".format(value) if value.is_integer() else "{:.1f}".format(value)


def _source(reading: Reading) -> str:
    return "C" if reading.source == "claude" else "X"


def _source_name(source: str) -> str:
    return "Claude" if source == "claude" else "Codex"


def _window(window: str) -> str:
    return "5h" if window == "short" else "7d"


def _reset_phrase(reading: Reading, now: float) -> str:
    if reading.resets_at is None:
        return ", reset time unknown"
    if reading.confidence is Confidence.POST_RESET:
        return ", previous window reset"
    return ", resets in {}".format(_duration(max(0.0, reading.resets_at - now)))


def _reset_plain(reading: Reading, now: float) -> str:
    if reading.resets_at is None:
        return "reset time unknown"
    remaining = reading.resets_at - now
    if remaining <= 0.0:
        return "the previous window has reset"
    return "resets in {}".format(_duration(remaining))


def _duration(seconds: float) -> str:
    total_minutes = max(0, int(seconds // 60))
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    if days:
        return "{}d {}h".format(days, hours)
    if hours:
        return "{}h {}m".format(hours, minutes)
    return "{}m".format(minutes)
