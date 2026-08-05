"""Freshness windows for snapshots from each supported source."""

import math
from typing import Mapping, Optional

from .config import FRESHNESS_ENV_VARS, freshness_override


# Claude's statusline may run repeatedly, so five minutes covers ordinary gaps
# without presenting an old reading as exact for too long. Codex usage is only
# captured while a Codex session runs, so its less frequent updates need a
# wider thirty-minute window.
DEFAULT_FRESHNESS_SECONDS = {
    "claude": 300.0,
    "codex": 1800.0,
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
