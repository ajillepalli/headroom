"""Pure text rendering for status, reports, and model context."""

from typing import Dict, List, Optional, Sequence

from .bounds import Confidence, Reading
from .burn_rate import BurnRateProjection, NoProjectionReason
from .context_window import ContextReading
from .severity import (
    Severity,
    burn_rate_evidence_is_current,
    burn_rate_projection_is_trustworthy,
    context_reading_severity,
    reading_severity,
)


def render_statusline(
    readings: Sequence[Reading], now: float, context: Optional[ContextReading] = None
) -> str:
    """Render a compact line that is safe to print for every statusline call.

    A context segment is appended only when it is not ok (fresh AND above
    the ok threshold) -- the original design's own words, kept literally:
    the number already exists in Claude Code's native UI, so this line
    stays quiet unless it has something worth adding.
    """

    known = [reading for reading in readings if reading.lower_bound_percent is not None]
    parts = ["{} {} {}".format(_source(reading), _window(reading.window), _usage(reading)) for reading in known]
    context_segment = _context_statusline_segment(context)
    if context_segment is not None:
        parts.append(context_segment)
    if not parts:
        return "headroom: usage unavailable"
    return "headroom: " + " | ".join(parts)


def _context_statusline_segment(context: Optional[ContextReading]) -> Optional[str]:
    severity = context_reading_severity(context)
    if severity is Severity.OK:
        return None
    assert context is not None  # severity is only non-OK for a fresh reading
    return "ctx {}% [{}]".format(_number(context.used_percent), severity)


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


def render_context_status_lines(readings: Sequence[ContextReading], now: float) -> List[str]:
    """Render one line per currently-fresh session's context reading.

    ``status`` has no stdin and no session of its own (same as ``json``), so
    this reports every session with a fresh reading rather than picking one
    -- picking one would relocate the exact cross-session bug the ENG review
    phase caught to a different command. A stale or absent reading is not
    listed at all here: fresh-or-nothing means there is nothing sound to say
    about it, unlike a rate-limit window's stale-but-bounded reading.
    """

    if not readings:
        return ["  unavailable"]
    lines: List[str] = []
    for reading in sorted(readings, key=lambda item: item.captured_at, reverse=True):
        severity = context_reading_severity(reading)
        lines.append(
            "  session {}: {}% used [{}], reading {} old".format(
                reading.session_id[:8],
                _number(reading.used_percent),
                severity,
                _duration(reading.age_seconds),
            )
        )
    return lines


def render_context_doctor_line(
    reading: Optional[ContextReading], fresh_for_seconds: float
) -> str:
    """Render doctor's dedicated, always-present context line.

    This exists because the generic notes scraper (``cli._stored_diagnostic_notes``)
    only ever fires on parse REJECTIONS. "No session_id" is an ABSENCE, not a
    rejection, so it produces no note at all and doctor would otherwise say
    nothing about context whatsoever -- required for ship once statusline
    and status are the only other self-serve surfaces (see the plan's DX
    review). ``reading`` here is the single most-recently-captured context
    entry across every session in state (doctor has no session of its own),
    already decoded by the caller; a caller passes ``None`` for either "no
    context was ever captured" or "the stored entry could not be decoded" --
    both collapse to the same message here because a corrupt entry and an
    absent one are equally undebuggable from doctor's own vantage point, and
    the specific decode failure (if any) already surfaces through the
    generic notes scraper's context_unparsed entries.
    """

    if reading is None:
        return "Claude context: not available (no session_id in last statusline payload)"
    if not reading.fresh:
        return "Claude context: stale (last capture {} ago, exceeds {}s freshness)".format(
            _duration(reading.age_seconds), _number(fresh_for_seconds)
        )
    severity = context_reading_severity(reading)
    if severity is Severity.OK:
        return "Claude context: ok ({}% used, below notice threshold)".format(
            _number(reading.used_percent)
        )
    return "Claude context: {} ({}% used)".format(severity, _number(reading.used_percent))


_BURN_RATE_ACTION = "Slow down now: use cheaper models and checkpoint work before it runs out."

# Adopted verbatim from the context-window plan's FINAL SCOPE. "Skip
# subagent fan-out" (the rate-limit ladder's own wording, _severity_action
# below) is backwards for context: a subagent spends its OWN context window
# and returns only a condensed result, so delegating a large read is the
# best move at high context and the closest thing to compaction the model
# can actually invoke. Never instructs the model to compact -- it cannot.
# "may compact without warning" is descriptive (explains WHY to act), not
# an instruction to compact; see test_context_window.py's word-ban assertion.
_CONTEXT_ADVICE: Dict[Severity, str] = {
    Severity.NOTICE: (
        "Context is filling ({}% used). Avoid opening large files unless the "
        "current step needs them."
    ),
    Severity.WARN: (
        "Context is high ({}% used) and may compact without warning. Delegate "
        "large file reads or broad exploration to a subagent instead of "
        "reading directly, and note current progress somewhere durable."
    ),
    Severity.CRITICAL: (
        "Context is nearly full ({}% used) and may compact without warning. "
        "Stop reading large files directly, delegate exploration to a "
        "subagent, and save a short progress summary now."
    ),
}


def _context_advice(reading: ContextReading, severity: Severity) -> str:
    return _CONTEXT_ADVICE[severity].format(_number(reading.used_percent))


def _context_trailing_clause(reading: ContextReading, severity: Severity) -> str:
    """One short, factual trailing clause -- no second action line (the
    arbitration rule's own words) -- for context as the SUBORDINATE signal."""

    return "Context is also at {} ({}% used).".format(severity, _number(reading.used_percent))


def _rate_trailing_clause(reading: Reading, severity: Severity) -> str:
    """The rate-ladder equivalent of ``_context_trailing_clause``, for when
    context wins arbitration and a live rate warning is the loser.

    Uses the raw ``reading.window`` name ("weekly"/"short"), matching
    ``_severity_reading_line``'s own convention, not the "7d"/"5h" shorthand
    ``_window`` produces for ``status``/``statusline``.
    """

    return "{} {} usage is also at {} ({}).".format(
        _source_name(reading.source), reading.window, severity, _usage(reading)
    )


def render_hook(
    readings: Sequence[Reading],
    now: float,
    projections: Sequence[BurnRateProjection] = (),
    forced_severity: Optional[Severity] = None,
    context: Optional[ContextReading] = None,
) -> str:
    """Render brief model guidance, or an empty string when no action is needed.

    Composes THREE independent signals under one ~60-word budget: the
    rate-limit severity ladder (bounds.py + severity.py, "how much headroom
    is left right now"), the burn-rate policy (severity.py's
    ``burn_rate_projection_is_trustworthy``, "will this window run out
    before it resets"), and -- new -- this session's context-window
    severity (``severity.context_reading_severity``, "is this conversation
    about to compact"). Rate and burn-rate compose exactly as before (see
    below); context arbitrates against rate specifically, per the plan's
    six numbered rules:

    1. Compute the worst rate severity and this session's context severity
       separately (``rate_severity``/``context_severity`` below).
    2. Both ok or absent: this function returns "".
    3. Only one above ok: that one renders as the WHOLE primary block, same
       as if the other did not exist.
    4. Both above ok: CRITICAL > WARN > NOTICE; a tie goes to RATE, because
       rate exhaustion blocks work for hours or days while context resolves
       in seconds. The winner is the primary block.
    5. If the LOSER is WARN or CRITICAL, ONE short trailing clause is
       appended -- no second action line. A loser that is only NOTICE is
       dropped to protect the word budget.
    6. A CRITICAL rate line is never omitted for context: since CRITICAL is
       the top of the ladder, context can never out-rank it, so rule 4
       structurally guarantees this without extra code.

    Burn-rate stays a purely rate-side addition, exactly as before: it
    never appears when context wins arbitration (context winning already
    means rate has nothing more urgent to add), and a CRITICAL rate line
    still never gets a burn-rate addendum, only at most one trailing
    context clause per rule 5. The forced-severity diagnostic path
    (``HEADROOM_FORCE_SEVERITY``) stays isolated from context exactly like
    it already stays isolated from burn-rate: a synthetic test severity is
    never blended with a genuine context reading.
    """

    actionable = [reading for reading in readings if reading_severity(reading) is not Severity.OK]
    # Forced-severity output is a diagnostic test path (HEADROOM_FORCE_SEVERITY),
    # not a real usage signal, so it stays isolated from burn-rate composition
    # rather than mixing synthetic severity with a genuine projection.
    burn_warning = (
        None if forced_severity is not None else _earliest_burn_rate_warning(projections, readings, now)
    )
    context_severity = (
        Severity.OK if forced_severity is not None else context_reading_severity(context)
    )
    if (
        not actionable
        and forced_severity is None
        and burn_warning is None
        and context_severity is Severity.OK
    ):
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
    rate_reading = candidates[0] if candidates else None
    rate_severity = forced_severity or (
        reading_severity(rate_reading) if rate_reading is not None else Severity.OK
    )

    lines: List[str] = []
    if forced_severity is not None:
        lines.append(
            "FORCED TEST ({}): diagnostic output, not a real usage warning.".format(
                forced_severity
            )
        )

    # Arbitration rule 4: strictly greater wins; a tie (including OK == OK,
    # already excluded above) goes to rate. Structurally impossible while
    # forced_severity is set (context_severity is forced to OK above).
    context_wins = context_severity > rate_severity
    if context_wins:
        lines.append(_context_advice(context, context_severity))
        if rate_severity in (Severity.WARN, Severity.CRITICAL) and rate_reading is not None:
            lines.append(_rate_trailing_clause(rate_reading, rate_severity))
        return "\n".join(lines)

    show_severity_section = bool(actionable) or forced_severity is not None
    if show_severity_section:
        if rate_reading is None:
            lines.append("Usage headroom: no reading is available.")
        else:
            lines.append(_severity_reading_line(rate_reading, now))
        lines.append(_severity_action(rate_severity))
        if rate_severity is Severity.CRITICAL:
            # Never displaced by burn-rate (existing rule) but MAY still
            # carry one trailing context clause (arbitration rule 5) before
            # returning, so this returns here rather than falling through
            # to the burn-rate section below.
            if context_severity in (Severity.WARN, Severity.CRITICAL):
                lines.append(_context_trailing_clause(context, context_severity))
            return "\n".join(lines)

    if context_severity in (Severity.WARN, Severity.CRITICAL):
        lines.append(_context_trailing_clause(context, context_severity))

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
    readings: Sequence[Reading],
    now: float,
) -> Optional[BurnRateProjection]:
    """The single most urgent trustworthy, currently-evidenced burn-rate
    warning, if any.

    "Trustworthy" (is the fit internally consistent) and "currently
    evidenced" (is there a fresh reading AND a recent real change confirming
    the same source and window right now) are both answered by severity.py's
    policy functions; this only asks both questions and, among the yeses,
    picks the soonest projected exhaustion. Filters on
    ``exhaustion_precedes_reset is True`` specifically (never truthiness) --
    ``False`` means exhaustion is projected AFTER reset (nothing to warn
    about) and ``None`` means either the reset time is unknown or no
    projection exists at all; neither is "before reset."
    """

    readings_by_window = {(reading.source, reading.window): reading for reading in readings}
    candidates = [
        projection
        for projection in projections
        if projection.exhaustion_precedes_reset is True
        and burn_rate_projection_is_trustworthy(projection)
        and burn_rate_evidence_is_current(
            projection, readings_by_window.get((projection.source, projection.window)), now
        )
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
    projections: Sequence[BurnRateProjection], now: float, readings: Sequence[Reading] = ()
) -> List[str]:
    """Render one line per window whose projection exists and, per
    severity.py's burn-rate policy, is trustworthy enough AND currently
    evidenced enough to show a human.

    A declined projection says nothing here -- explaining the structural
    reason is doctor's job (``render_burn_rate_doctor_lines``), not
    status's. This deliberately does not require
    ``exhaustion_precedes_reset is True``: status reports any qualifying
    projection so a user can see the projected pace even when exhaustion
    falls comfortably after reset, unlike ``hook``, which only ever speaks
    about the "runs out before reset" case. It does still require
    ``burn_rate_evidence_is_current`` (a fresh matching reading), for the
    same reason ``hook`` does: an internally consistent fit built from a
    source that stopped reporting hours ago is not evidence of a live
    trend, just a historical one (Codex review, round 1, P2).
    """

    readings_by_window = {(reading.source, reading.window): reading for reading in readings}
    lines: List[str] = []
    for projection in projections:
        if not burn_rate_projection_is_trustworthy(projection):
            continue
        if not burn_rate_evidence_is_current(
            projection, readings_by_window.get((projection.source, projection.window)), now
        ):
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
