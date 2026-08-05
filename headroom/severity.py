"""Pure severity calculation for bounded readings."""

from enum import IntEnum

from .bounds import Confidence, Reading


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
