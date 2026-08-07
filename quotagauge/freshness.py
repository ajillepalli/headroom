"""Freshness windows for snapshots from each supported source."""

import math
from typing import Mapping, Optional

from .config import FRESHNESS_ENV_VARS, freshness_override


# Claude's statusline may run repeatedly, so five minutes covers ordinary gaps
# without presenting an old reading as exact for too long. Codex usage is only
# captured while a Codex session runs, so its less frequent updates need a
# wider thirty-minute window.
#
# "context" shares the 300s Claude default deliberately, not coincidentally:
# quotagauge init sets the statusline refreshInterval to 300s (settings.py),
# and context rides the exact same statusline payload as the Claude rate
# limits above. A tighter window here would be tighter than the tool's own
# configured sample rate and would go silent on a clean install with nothing
# actually wrong (see docs/explanation-context.md). In practice Claude Code
# re-runs the statusline command on several event-driven triggers (a new
# assistant message, /compact finishing, a permission-mode change, a vim
# mode toggle) well inside 300s; refreshInterval only adds a periodic
# re-render on top of those events during an idle session, it does not
# throttle invocation down to once per 300s (confirmed against Anthropic's
# published statusline documentation, since a live capture of this specific
# cadence claim was not obtainable from this project's own sandboxed
# environment -- see the context-window plan's verification notes).
DEFAULT_FRESHNESS_SECONDS = {
    "claude": 300.0,
    "codex": 1800.0,
    "context": 300.0,
}

def freshness_seconds(
    source: str, environ: Optional[Mapping[str, str]] = None
) -> float:
    """Return the configured freshness window for one source."""

    default = DEFAULT_FRESHNESS_SECONDS.get(source, 0.0)
    variable = FRESHNESS_ENV_VARS.get(source)
    if variable is None:
        return default
    raw_value = freshness_override(source, environ)
    if raw_value is None:
        return default

    value = float(raw_value)
    if not math.isfinite(value) or value < 0.0:
        raise ValueError("{} must be a finite, non-negative number".format(variable))
    return value
