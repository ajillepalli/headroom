"""Resolve quotagauge environment configuration in one place."""

import os
from pathlib import Path
from typing import Mapping, Optional


STATE_DIR_ENV = "QUOTAGAUGE_STATE_DIR"
CODEX_HOME_ENV = "QUOTAGAUGE_CODEX_HOME"
FRESHNESS_ENV_VARS = {
    "claude": "QUOTAGAUGE_FRESH_CLAUDE_SECONDS",
    "codex": "QUOTAGAUGE_FRESH_CODEX_SECONDS",
    # "context" is not a rate-limit source like "claude"/"codex" above --
    # it is Claude's own per-session context-window signal, named by signal
    # type rather than by source because there is exactly one source (see
    # context_window.py) and "context" reads clearer than a source-shaped
    # name would here. freshness_seconds() and freshness_override() are
    # already generic over any lookup key, so this reuses both with no new
    # resolution or validation code.
    "context": "QUOTAGAUGE_FRESH_CONTEXT_SECONDS",
}

# The pre-rename state directory. This project was named headroom before
# colliding with an unrelated, already-established PyPI package of that
# name; every other reference to the old name was mechanically renamed
# away, but this one path is deliberately kept, because on a machine that
# ran the old command it is the only place months of real, irreplaceable
# usage history (state.json, history.jsonl) can still be found. Losing
# reach of it would be strictly worse than a stray old name in the source.
_LEGACY_STATE_DIR_NAME = ".headroom"
_STATE_DIR_NAME = ".quotagauge"


def resolve_state_dir(state_dir: Optional[Path] = None) -> Path:
    """Resolve an explicit, configured, or default state directory.

    When neither an explicit override nor the environment variable applies,
    this also performs a one-time migration of the legacy ``~/.headroom``
    directory (see ``_LEGACY_STATE_DIR_NAME`` above) to its renamed home,
    ``~/.quotagauge``, the first time this process (or any earlier one)
    finds the new location missing and the old one present.
    """

    if state_dir is not None:
        # An explicit caller-supplied directory is never migrated: it is
        # trivially re-specified by whoever passed it, unlike the two
        # default-location branches below where real accumulated history
        # could otherwise become unreachable.
        return Path(state_dir).expanduser()
    configured = os.environ.get(STATE_DIR_ENV)
    if configured:
        # Same reasoning as the explicit-parameter case above: an
        # environment variable is trivially re-set, so there is nothing to
        # migrate on this path either.
        return Path(configured).expanduser()
    return _resolve_default_state_dir()


def _resolve_default_state_dir() -> Path:
    """Resolve the default state directory, migrating the legacy one at
    most once.

    Checking the NEW location first, before ever looking at the legacy one,
    is what makes this idempotent without any extra bookkeeping: once a
    migration succeeds (by this process or a concurrent one racing it), the
    new directory exists, so every later call -- later in this same
    process, or in a wholly separate later invocation, since this tool runs
    as a fresh process per command with no persistent daemon to remember
    "already migrated" -- returns here before ever re-examining the legacy
    directory below.
    """

    target = Path.home() / _STATE_DIR_NAME
    if target.exists():
        # This branch wins unconditionally, even over a legacy directory
        # that still exists right beside it: an existing target could be a
        # genuine prior migration (the common case), a fresh install that
        # simply reached this name first, OR a directory unrelated to any
        # migration (a stray empty ~/.quotagauge, however that got there --
        # a typo, a separate local tool). This function cannot tell those
        # apart from an OSError alone, and it deliberately does not try:
        # ~/.headroom is never deleted or written to on this path (see
        # below), so nothing here is destructive even in the unlikely stray
        # case, and the legacy data stays fully recoverable by hand.
        # Preferring "trust an existing target" over "guess and possibly
        # pick the wrong directory automatically" is the intentional
        # trade-off.
        return target

    legacy = Path.home() / _LEGACY_STATE_DIR_NAME
    if not legacy.exists():
        # Neither directory exists yet: an ordinary fresh install, nothing
        # to migrate. The caller creates this directory on first write,
        # exactly as it always has.
        return target

    try:
        legacy.rename(target)
        return target
    except OSError:
        # A failed rename is ambiguous by itself: it can mean "a concurrent
        # process's own migration already won this exact race" (visible
        # mainly on Windows, where a rename never silently replaces an
        # existing target the way POSIX's can) or it can mean "this rename
        # can never succeed here" (no permission, a stray file already
        # occupying `target`, crossing a filesystem boundary, and so on).
        # Both raise the same OSError, so re-checking `target` is the only
        # way to tell a race that already resolved itself from a real
        # failure.
        if target.exists():
            return target
        # A genuine, non-racing failure: the legacy directory still holds
        # the only copy of this data, so falling back to it IN PLACE keeps
        # every reading reachable under its old path rather than losing
        # access to it -- the one outcome this migration must never cause.
        # This is deliberately not copy-then-delete: a copy that fails
        # partway (a full disk, a killed process) could leave neither
        # location complete, whereas an in-place fallback never removes
        # anything, so a still-failing migration is never worse than "not
        # migrated yet." The next call -- this process's or a later one --
        # simply tries again from the same starting state.
        return legacy


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
