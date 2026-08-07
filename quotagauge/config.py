"""Resolve quotagauge environment configuration in one place."""

import os
from pathlib import Path
import time
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

# How many times to retry the migration rename on a transient Windows
# access-denied failure, and how long to wait between attempts. Mirrors
# state.py's _replace_with_retry, which retries os.replace for exactly this
# reason: MoveFileEx (what Path.rename wraps on Windows) can fail with
# ERROR_ACCESS_DENIED for a few milliseconds if something else -- commonly
# antivirus real-time scanning or the Windows Search Indexer -- briefly has
# the directory open, not because the rename can never succeed. Retrying
# narrows a real-world instance of the race a Codex review round flagged:
# two processes racing this exact migration at nearly the same moment can
# otherwise see one process's rename lose to a momentary external lock
# while the other's succeeds a few milliseconds later, after the first
# process has already committed to the legacy-directory fallback below and
# may go on to write there. A bounded retry gives the losing side a real
# chance to become the winner instead, the same way it does for
# _replace_with_retry's analogous case.
_MIGRATION_RETRY_ATTEMPTS = 5
_MIGRATION_RETRY_DELAY_SECONDS = 0.02

# Sticky, per-process cache of the resolved default state directory. Every
# state.py read and write resolves the state directory independently, on
# its own, rather than sharing one resolution threaded through a whole
# command invocation (a pre-existing design, unrelated to this rename).
# Without this cache, a single process could see the DEFAULT resolution
# answer change mid-invocation if a concurrent process completes a
# migration between two of this process's own calls -- for example
# reading from the freshly migrated ~/.quotagauge on one call after having
# already written to ~/.headroom on an earlier call in the very same
# invocation, an internally inconsistent result a single process should
# never produce. Caching the FIRST answer this process computes, and
# reusing it for the rest of this process's life, closes that within-
# process split entirely and narrows the cross-process race to the
# shortest possible window (this process's own first resolution attempt),
# without requiring every state.py call site to be restructured to share
# one resolution explicitly. This is safe to keep for a whole process
# lifetime specifically because quotagauge has no persistent daemon: a
# fresh process starts with a fresh, unset cache and resolves again from
# scratch, so nothing here is ever stale across separate invocations.
_default_state_dir_cache: Optional[Path] = None


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
    global _default_state_dir_cache
    if _default_state_dir_cache is None:
        _default_state_dir_cache = _resolve_default_state_dir()
    return _default_state_dir_cache


def _resolve_default_state_dir() -> Path:
    """Resolve the default state directory, migrating the legacy one at
    most once.

    Checking the NEW location first, before ever looking at the legacy one,
    is what makes this idempotent without any extra bookkeeping: once a
    migration succeeds (by this process or a concurrent one racing it), the
    new directory exists, so every later call -- later in this same
    process (though ``resolve_state_dir`` above now caches this function's
    own answer, so within one process this only ever runs once), or in a
    wholly separate later invocation, since this tool runs as a fresh
    process per command with no persistent daemon to remember "already
    migrated" -- returns here before ever re-examining the legacy
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
        _rename_with_retry(legacy, target)
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
        # failure. _rename_with_retry has already absorbed a few
        # milliseconds of transient Windows lock contention by this point,
        # so what remains here is either a genuine winner or a genuine
        # failure, not a spurious loss to a lock that would have cleared a
        # moment later.
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
        #
        # Residual risk, narrowed but not eliminated: resolve_state_dir's
        # own cache (see _default_state_dir_cache above) means THIS
        # process is stuck with this exact fallback decision for the rest
        # of its life -- it can no longer flip to a DIFFERENT directory
        # partway through its own invocation, so every read and write this
        # process makes stays internally consistent with itself. What
        # remains is purely a CROSS-process race: if this process's write
        # against the returned legacy path lands after a different,
        # concurrent process has already migrated that same directory
        # away and started using the new one, this process's write is not
        # lost -- it is still real data on disk under ~/.headroom -- but
        # it would no longer be picked up automatically by anything that
        # resolves the state directory afterward, the same
        # recoverable-but-not-automatic outcome documented for a stray
        # pre-existing target above. The retry above already narrows the
        # window for reaching this fallback at all to a genuinely
        # non-transient failure; closing the remaining cross-process
        # window completely would require actual interprocess
        # coordination (a lock file spanning the whole migration
        # decision, held across process boundaries), which is
        # disproportionate to a one-time, self-healing migration for a
        # single-user local CLI tool with no persistent daemon.
        return legacy


def _rename_with_retry(source: Path, target: Path) -> None:
    """Path.rename, retrying a bounded number of times on a transient
    Windows access-denied failure. See _MIGRATION_RETRY_ATTEMPTS's own
    comment for why this exists; mirrors state.py's _replace_with_retry.
    """

    for attempt in range(_MIGRATION_RETRY_ATTEMPTS):
        try:
            source.rename(target)
            return
        except PermissionError:
            if attempt == _MIGRATION_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_MIGRATION_RETRY_DELAY_SECONDS)


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
