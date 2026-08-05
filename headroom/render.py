"""Pure text rendering for status, reports, and model context."""

from typing import Dict, List, Optional, Sequence

from .bounds import Confidence, Reading
from .burn_rate import BurnRateProjection, NoProjectionReason
from .severity import Severity, burn_rate_projection_is_trustworthy, reading_severity


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


_BURN_RATE_ACTION = "Slow down now: use cheaper models and checkpoint work before it runs out."


def render_hook(
    readings: Sequence[Reading],
    now: float,
    projections: Sequence[BurnRateProjection] = (),
    forced_severity: Optional[Severity] = None,
) -> str:
    """Render brief model guidance, or an empty string when no action is needed.

    Composes two independent signals under one ~60-word budget: the
    existing rate-limit severity ladder (bounds.py + severity.py, "how much
    headroom is left right now") and the burn-rate policy
    (severity.py's ``burn_rate_projection_is_trustworthy`` and its trust
    constants, "will this window run out before it resets"). They answer
    different questions -- a reading can have plenty of headroom right now
    and still be on a trajectory that exhausts before reset, which is
    exactly the case a burn-rate warning exists to catch (the model can only
    change behavior if it learns this BEFORE the reset, not after).
    Composition rule, stated explicitly so it isn't left to accident:

    * A CRITICAL rate-limit reading always wins and is shown ALONE. It is
      the nearer-term, already-confirmed signal; a burn-rate warning is a
      trend projected on top of it, and the word budget is tightest exactly
      when brevity matters most. A CRITICAL reading is never displaced or
      diluted by appending a second topic.
    * Otherwise, a trustworthy burn-rate warning (one whose
      ``exhaustion_precedes_reset`` is ``True`` -- never ``False`` or
      ``None``, see BurnRateProjection's tri-state docstring -- AND that
      clears severity.py's trust bar) is shown alongside whatever the
      severity ladder already has to say. If the ladder has nothing
      actionable (every reading is OK), the burn-rate lines are the only
      output. If the ladder has a NOTICE or WARN reading, both appear.
    * If neither has anything to say, this returns "" and the hook prints
      nothing, matching the documented "silent when nothing actionable"
      hook contract.
    """

    actionable = [reading for reading in readings if reading_severity(reading) is not Severity.OK]
    # Forced-severity output is a diagnostic test path (HEADROOM_FORCE_SEVERITY),
    # not a real usage signal, so it stays isolated from burn-rate composition
    # rather than mixing synthetic severity with a genuine projection.
    burn_warning = None if forced_severity is not None else _earliest_burn_rate_warning(projections)
    if not actionable and forced_severity is None and burn_warning is None:
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
    severity = forced_severity or (
        reading_severity(reading) if reading is not None else Severity.OK
    )

    lines: List[str] = []
    if forced_severity is not None:
        lines.append(
            "FORCED TEST ({}): diagnostic output, not a real usage warning.".format(
                forced_severity
            )
        )
    show_severity_section = bool(actionable) or forced_severity is not None
    if show_severity_section:
        if reading is None:
            lines.append("Usage headroom: no reading is available.")
        else:
            lines.append(_severity_reading_line(reading, now))
        lines.append(_severity_action(severity))
        if severity is Severity.CRITICAL:
            # Never displaced: return here, before the burn-rate section
            # below is ever reached. See this function's docstring.
            return "\n".join(lines)

    if burn_warning is not None:
        lines.append(_burn_rate_warning_line(burn_warning, now))
        lines.append(_BURN_RATE_ACTION)

    return "\n".join(lines)


def _severity_reading_line(reading: Reading, now: float) -> str:
    reset = _reset_plain(reading, now)
    return "Usage headroom: {} {} {} (reading {} old), {}.".format(
        _source_name(reading.source),
        reading.window,
        _usage(reading),
        _duration(reading.age_seconds),
        reset,
    )


def _severity_action(severity: Severity) -> str:
    if severity is Severity.CRITICAL:
        return "Stop parallel subagent fan-out, use cheaper models, and checkpoint work now."
    if severity is Severity.WARN:
        return "Prefer cheaper models, avoid parallel subagent fan-out, and checkpoint soon."
    return "Conserve usage and avoid unnecessary parallel work."


def _earliest_burn_rate_warning(
    projections: Sequence[BurnRateProjection],
) -> Optional[BurnRateProjection]:
    """The single most urgent trustworthy burn-rate warning, if any.

    "Trustworthy" is answered by severity.py's policy; this only asks the
    question and, among the trustworthy yeses, picks the soonest projected
    exhaustion. Filters on ``exhaustion_precedes_reset is True`` specifically
    (never truthiness) -- ``False`` means exhaustion is projected AFTER
    reset (nothing to warn about) and ``None`` means either the reset time
    is unknown or no projection exists at all; neither is "before reset."
    """

    candidates = [
        projection
        for projection in projections
        if projection.exhaustion_precedes_reset is True
        and burn_rate_projection_is_trustworthy(projection)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda projection: (
            projection.projected_exhaustion_at
            if projection.projected_exhaustion_at is not None
            else float("inf")
        ),
    )


def _burn_rate_warning_line(projection: BurnRateProjection, now: float) -> str:
    exhaustion_at = projection.projected_exhaustion_at if projection.projected_exhaustion_at is not None else now
    eta = _duration(max(0.0, exhaustion_at - now))
    return "Burn rate: {} {} is on pace to exhaust in {}, before its reset.".format(
        _source_name(projection.source), _window(projection.window), eta
    )


_DECLINE_REASON_TEXT: Dict[NoProjectionReason, str] = {
    NoProjectionReason.TOO_FEW_SAMPLES: "not enough usage samples recorded yet",
    NoProjectionReason.SPAN_TOO_SHORT: "the recorded samples span too little time to fit a rate",
    NoProjectionReason.INSUFFICIENT_SPAN_FOR_HORIZON: "the observed span is too short relative to how far off exhaustion looks",
    NoProjectionReason.ALREADY_EXHAUSTED: "usage already reached 100% in this window",
    NoProjectionReason.FLAT_USAGE: "usage has not changed across the recorded samples",
    NoProjectionReason.USAGE_WENT_BACKWARDS: "usage decreased within the window, which blocks the fit",
    NoProjectionReason.NON_POSITIVE_RATE: "the fitted rate is zero or negative",
    NoProjectionReason.WINDOW_ALREADY_RESET: "the window's reset time has already passed",
    NoProjectionReason.NON_FINITE_RESULT: "the computation produced a non-finite result",
    NoProjectionReason.PROJECTED_EXHAUSTION_IN_PAST: "the projected exhaustion time is already in the past, so the rate is too stale to trust",
    NoProjectionReason.RATE_UNDERFLOWED_TO_ZERO: "a usage change was too small to produce a measurable rate",
    NoProjectionReason.TERMINAL_REMAINDER_UNMERGEABLE: "the most recent usage change has no interval it can be honestly attributed to",
}


def render_burn_rate_doctor_lines(
    projections: Sequence[BurnRateProjection], now: float
) -> List[str]:
    """Render one diagnostic line per source/window burn-rate projection.

    Unlike ``status``, this always reports every projection, including
    declined ones: doctor is where a user works out WHY no projection
    appeared, so decline reasons are translated to plain language
    (``_DECLINE_REASON_TEXT``) here rather than exposed as raw enum names.
    """

    lines: List[str] = []
    for projection in projections:
        label = "{} {}".format(_source_name(projection.source), _window(projection.window))
        if projection.reason is not None:
            reason_text = _DECLINE_REASON_TEXT.get(projection.reason, projection.reason.value)
            lines.append("  {}: no projection ({})".format(label, reason_text))
            continue

        precedes = projection.exhaustion_precedes_reset
        if precedes is True:
            reset_phrase = "before reset"
        elif precedes is False:
            reset_phrase = "after reset"
        else:
            reset_phrase = "reset time unknown"
        exhaustion_at = projection.projected_exhaustion_at if projection.projected_exhaustion_at is not None else now
        eta = _duration(max(0.0, exhaustion_at - now))
        rate = projection.rate_percent_per_second or 0.0
        lines.append(
            "  {}: exhaustion projected in {} ({}); rate {:.4g}%/s over {} samples; "
            "deviation {:.2f}, usage share {:.2f}, intervals {}, drift {:.2f}, "
            "effective intervals {:.2f}, zero-delta fraction {:.2f}, raw rate ratio {:.2f}, "
            "longest above-average run {}".format(
                label,
                eta,
                reset_phrase,
                rate,
                projection.samples_used,
                projection.max_relative_deviation,
                projection.max_usage_share,
                projection.intervals_used,
                projection.rate_drift,
                projection.effective_intervals,
                projection.zero_delta_fraction,
                projection.max_raw_rate_ratio,
                projection.longest_above_overall_rate_run,
            )
        )
    return lines


def render_burn_rate_status_lines(
    projections: Sequence[BurnRateProjection], now: float
) -> List[str]:
    """Render one line per window whose projection exists and, per
    severity.py's burn-rate policy, is trustworthy enough to show a human.

    A declined projection says nothing here -- explaining the structural
    reason is doctor's job (``render_burn_rate_doctor_lines``), not
    status's. This deliberately does not require
    ``exhaustion_precedes_reset is True``: status reports any trustworthy
    projection so a user can see the projected pace even when exhaustion
    falls comfortably after reset, unlike ``hook``, which only ever speaks
    about the "runs out before reset" case.
    """

    lines: List[str] = []
    for projection in projections:
        if not burn_rate_projection_is_trustworthy(projection):
            continue
        exhaustion_at = projection.projected_exhaustion_at
        if exhaustion_at is None:
            continue
        precedes = projection.exhaustion_precedes_reset
        if precedes is True:
            reset_phrase = "before the window resets"
        elif precedes is False:
            reset_phrase = "after the window resets"
        else:
            reset_phrase = "reset time unknown"
        eta = _duration(max(0.0, exhaustion_at - now))
        label = "{} {}".format(_source_name(projection.source), _window(projection.window))
        lines.append(
            "  {} burn rate: projected exhaustion in {} ({})".format(label, eta, reset_phrase)
        )
    return lines


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
