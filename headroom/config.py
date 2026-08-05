"""Resolve headroom environment configuration in one place."""

import os
from pathlib import Path
from typing import Mapping, Optional


STATE_DIR_ENV = "HEADROOM_STATE_DIR"
CODEX_HOME_ENV = "HEADROOM_CODEX_HOME"
FRESHNESS_ENV_VARS = {
    "claude": "HEADROOM_FRESH_CLAUDE_SECONDS",
    "codex": "HEADROOM_FRESH_CODEX_SECONDS",
}


def resolve_state_dir(state_dir: Optional[Path] = None) -> Path:
    """Resolve an explicit, configured, or default state directory."""

    if state_dir is not None:
        return Path(state_dir).expanduser()
    configured = os.environ.get(STATE_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".headroom"


def resolve_codex_sessions_dir() -> Path:
    """Resolve the Codex sessions directory from its configured home."""

    configured = os.environ.get(CODEX_HOME_ENV)
    codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return codex_home / "sessions"


def freshness_override(
    source: str, environ: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Return the configured freshness value for a source, if present."""

    variable = FRESHNESS_ENV_VARS.get(source)
    if variable is None:
        return None
    environment = os.environ if environ is None else environ
    return environment.get(variable)
