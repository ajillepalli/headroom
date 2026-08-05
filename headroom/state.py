"""Atomic persistence for snapshots and append-only history."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, Iterable, Optional, Sequence

from .bounds import Snapshot
from .config import resolve_state_dir
from .context_window import resolve_age
from .freshness import freshness_seconds

# Exactly one of these two exists on any given platform: fcntl.flock is
# POSIX-only, msvcrt.locking is Windows-only. Selected once at import time
# rather than probed per call, since the answer never changes for the life
# of a process. Both are OS-level advisory locks tied to an open file
# descriptor -- unlike a hand-rolled "lock file that gets deleted", the OS
# itself releases the lock the instant the holding process exits or the
# descriptor closes, crash included, so there is no separate cleanup step
# that itself needs to be crash-safe (finding #1, context-window
# adversarial review).
try:
    import fcntl  # type: ignore
except ImportError:  # Windows
    fcntl = None  # type: ignore
try:
    import msvcrt  # type: ignore
except ImportError:  # POSIX
    msvcrt = None  # type: ignore

_LOCK_FILENAME = ".state.lock"
# How long to keep retrying a non-blocking lock attempt before giving up and
# proceeding WITHOUT the lock. Blocking forever is not acceptable here: the
# statusline contract (cli.py) is "always print, always exit 0", and a wedged
# lock (e.g. a previous holder killed in a way exotic enough to survive OS
# cleanup, or a filesystem that does not honor advisory locks at all) must
# degrade to the pre-lock race rather than hang a command whose entire job is
# a quick, best-effort write.
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05


def read_state(state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Read a complete state document, returning an empty state on failure."""

    path = resolve_state_dir(state_dir) / "state.json"
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return _empty_state()
    return value if isinstance(value, dict) else _empty_state()


def write_state(state: Dict[str, Any], state_dir: Optional[Path] = None) -> None:
    """Atomically replace the state document with a complete JSON value."""

    directory = resolve_state_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "state.json"
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(directory),
            prefix=".state-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


@contextmanager
def _locked_state_transaction(directory: Path):
    """Serialize save_snapshots' read-merge-write across processes.

    Two headroom invocations (two terminal tabs, each running Claude Code's
    statusline command at nearly the same instant) can both call
    save_snapshots close enough together that each reads the SAME on-disk
    state, merges its own change into its own in-memory copy, and writes
    back -- silently dropping whichever write loses the race (finding #1,
    context-window adversarial review; concretely, session-a's 92% capture
    and session-b's 8% capture arriving within milliseconds of each other
    could leave only one of the two in state.json). An OS-level advisory
    lock on a DEDICATED lock file (never state.json itself, so this never
    blocks or is blocked by write_state's own atomic os.replace) closes that
    window.

    Locking degrades to a no-op, rather than raising, whenever it cannot be
    used: the lock file itself unopenable (e.g. a read-only or unusual
    filesystem), neither platform locking module available (should not
    happen -- exactly one of fcntl/msvcrt exists on every platform headroom
    supports -- but guarded rather than assumed), or every acquisition
    attempt within _LOCK_TIMEOUT_SECONDS failing. Dropping the safety this
    lock buys, on the rare host or rare wedge where it cannot be taken at
    all, is preferable to hanging or crashing a command that must always
    print and exit 0 (see cli.py's statusline contract).
    """

    handle = None
    locked = False
    try:
        try:
            handle = open(str(directory / _LOCK_FILENAME), "a+b")
        except OSError:
            handle = None
        if handle is not None:
            locked = _acquire_lock(handle)
        yield
    finally:
        if handle is not None:
            if locked:
                _release_lock(handle)
            try:
                handle.close()
            except OSError:
                pass


def _acquire_lock(handle: Any) -> bool:
    """Take an exclusive, non-blocking lock, retrying until it succeeds or
    _LOCK_TIMEOUT_SECONDS elapses. Never blocks indefinitely (see
    _locked_state_transaction's docstring for why that matters here).

    The deadline is computed lazily, only once the FIRST attempt fails,
    rather than unconditionally up front: the overwhelmingly common case is
    an uncontended lock that succeeds on the first try, and that path costs
    zero calls to time.monotonic() this way -- it does not matter for
    correctness, but it does matter for staying out of the way of anything
    else timing itself against the same clock during the same call.
    """

    deadline: Optional[float] = None
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            if msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            # Neither locking module is importable: no platform-appropriate
            # mechanism exists to take the lock, so proceed unlocked rather
            # than raise (see this module's own docstring above).
            return False
        except OSError:
            if deadline is None:
                deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            if time.monotonic() >= deadline:
                return False
            time.sleep(_LOCK_POLL_SECONDS)


def _release_lock(handle: Any) -> None:
    """Best-effort unlock. A failure here is not fatal: the OS releases an
    advisory lock automatically once ``handle`` is closed regardless (see
    _locked_state_transaction's docstring), so this is tidiness, not the
    only path to safety."""

    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def save_snapshots(
    snapshots: Sequence[Snapshot],
    state_dir: Optional[Path] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
    context_capture: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge snapshots into state and append each captured reading to history.

    ``context_capture``, when given, is folded into the SAME read-modify-
    write transaction as the rate-limit snapshots above rather than given
    its own ``state.py`` entry point: two separate read-modify-write round
    trips inside one statusline invocation would not be atomic with each
    other, and the concurrent-session case (two terminal tabs writing at
    close to the same moment) is exactly the failure this whole feature
    exists to avoid.

    The whole read-merge-write below runs under an OS-level advisory lock
    (see ``_locked_state_transaction``) so that two overlapping calls to
    this function -- from two separate headroom processes -- cannot both
    read the same on-disk state and then each write back a version that
    silently drops the other's update (finding #1, context-window
    adversarial review).
    """

    directory = resolve_state_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with _locked_state_transaction(directory):
        state = read_state(state_dir)
        sources = state.setdefault("sources", {})
        if not isinstance(sources, dict):
            sources = {}
            state["sources"] = sources
        # Runs on EVERY call, not just ones that supply a context_capture:
        # claude.py and context_window.py now both refuse to accept or
        # decode a session_id that cannot round-trip through JSON (a lone
        # UTF-16 surrogate), but that alone does not un-persist an entry
        # that reached state.json before those checks existed (a
        # hand-edit, or a state file written by a pre-fix version). Left
        # alone, that single entry would keep crashing the write_state
        # call below on every SUBSEQUENT command -- including ones that
        # only refresh a Codex snapshot and never touch context at all --
        # turning one stale corrupt session into a permanent exit-1 for
        # `headroom json`/`status`/`hook` (finding #9, context-window
        # adversarial review). Sanitizing here, unconditionally, means the
        # very next command heals it instead.
        _drop_unencodable_context_entries(sources)
        accepted = []
        for snapshot in snapshots:
            source = sources.setdefault(snapshot.source, {})
            if not isinstance(source, dict):
                source = {}
                sources[snapshot.source] = source
            current = source.get(snapshot.window)
            if isinstance(current, dict):
                try:
                    current_captured_at = float(current["captured_at"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    current_captured_at = None
                if current_captured_at is not None and current_captured_at > snapshot.captured_at:
                    continue
            source[snapshot.window] = snapshot.to_dict()
            accepted.append(snapshot)
        if context_capture is not None:
            _merge_context_capture(sources, context_capture)
        state["version"] = 1
        if diagnostics is not None:
            stored = state.setdefault("diagnostics", {})
            if not isinstance(stored, dict):
                stored = {}
                state["diagnostics"] = stored
            stored.update(diagnostics)
        write_state(state, state_dir)
        append_history(accepted, state_dir)
        return state


def _drop_unencodable_context_entries(sources: Dict[str, Any]) -> None:
    """Remove any stored context entry that cannot itself be written back
    out as JSON text, encoded as UTF-8.

    ``json.dumps(..., ensure_ascii=False)`` does NOT raise for a lone UTF-16
    surrogate on its own -- it happily returns a ``str`` still containing
    the surrogate. The failure only surfaces later, when that ``str`` is
    encoded to bytes: ``write_state``'s file handle is opened with
    ``encoding="utf-8"``, and text-mode ``write()`` performs exactly that
    encoding. So the check here mirrors that real failure point
    (``.encode("utf-8")``), not just a ``json.dumps`` call, or this would
    pass entries that still crash the write immediately afterward.
    """

    claude_source = sources.get("claude")
    if not isinstance(claude_source, dict):
        return
    contexts = claude_source.get("context")
    if not isinstance(contexts, dict):
        return
    bad_keys = []
    for key, value in contexts.items():
        try:
            json.dumps({key: value}, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError):
            bad_keys.append(key)
    for key in bad_keys:
        del contexts[key]


def _merge_context_capture(sources: Dict[str, Any], capture: Dict[str, Any]) -> None:
    """Store one session's context capture and prune entries gone stale.

    Context is per-session (context_window.py), so this is keyed by
    session_id under ``sources["claude"]["context"]`` rather than the flat
    ``(source, window)`` slot rate-limit snapshots use above -- a flat slot
    would let one terminal tab's context answer for a different session's
    prompt (the exact bug the ENG review phase caught).

    Pruning runs on every call that supplies a capture, using the new
    capture's own ``captured_at`` as "now" rather than calling
    ``time.time()`` here: ``state.py`` otherwise never reads the real clock,
    every timestamp it handles is passed in, and this keeps that property so
    callers stay deterministic in tests. A session that stops writing (a
    closed terminal tab) lingers until the NEXT successful capture from any
    session sweeps it -- acceptable, since a lingering entry is inert until
    then (fresh-or-nothing already refuses to read anything stale) and
    "prune on write" does not require every write to also be the sweep.
    """

    claude_source = sources.setdefault("claude", {})
    if not isinstance(claude_source, dict):
        claude_source = {}
        sources["claude"] = claude_source
    contexts = claude_source.setdefault("context", {})
    if not isinstance(contexts, dict):
        contexts = {}
        claude_source["context"] = contexts

    session_id = capture.get("session_id")
    captured_at = capture.get("captured_at")
    if isinstance(session_id, str) and session_id:
        if _should_replace_context_capture(contexts.get(session_id), capture):
            contexts[session_id] = capture

    if not isinstance(captured_at, (int, float)) or isinstance(captured_at, bool):
        return
    fresh_for = freshness_seconds("context")
    stale_keys = [
        key
        for key, value in contexts.items()
        if not _context_entry_is_fresh(value, float(captured_at), fresh_for)
    ]
    for key in stale_keys:
        del contexts[key]

    _evict_oldest_context_entries(contexts, _MAX_TRACKED_CONTEXT_SESSIONS)


def _should_replace_context_capture(current: Any, capture: Dict[str, Any]) -> bool:
    """Whether ``capture`` should replace ``current`` for the same session_id.

    Mirrors save_snapshots' own "an older snapshot never replaces a newer
    one" rule for rate-limit snapshots above: a delayed capture reaching state.json
    out of order (e.g. a slow write racing a faster subsequent one) must not
    resurrect a stale reading over one already known to be newer for the
    SAME session -- concretely, a delayed 92%-used capture timestamped 1000
    must not overwrite an already-stored, newer 8%-used capture timestamped
    1001, which would manufacture a false critical warning (finding #2,
    context-window adversarial review).

    Absent a decodable existing entry, there is nothing to protect, so a new
    capture always replaces it. Absent a decodable timestamp on the INCOMING
    capture, there is nothing to confirm it is not older than a decodable
    existing entry, so it does not replace one -- silence over a guess.
    """

    if not isinstance(current, dict):
        return True
    current_captured_at = _decoded_captured_at(current)
    if current_captured_at is None:
        return True
    incoming_captured_at = _decoded_captured_at(capture)
    if incoming_captured_at is None:
        return False
    return incoming_captured_at >= current_captured_at


def _decoded_captured_at(value: Any) -> Optional[float]:
    if not isinstance(value, dict):
        return None
    try:
        return float(value["captured_at"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


# Staleness pruning above only removes entries past the freshness window; it
# does nothing to stop a caller that sends many distinct, all-still-fresh
# session_ids from growing this dict without bound (finding #8,
# context-window adversarial review) -- a long-lived install seeing enough
# distinct session_ids arrive inside one freshness window would otherwise
# make state.json grow forever. This bounds how many sessions are tracked at
# once; ordinary use (a handful of concurrent terminal tabs) never comes
# close to it.
_MAX_TRACKED_CONTEXT_SESSIONS = 64


def _evict_oldest_context_entries(contexts: Dict[str, Any], max_entries: int) -> None:
    """Evict the oldest (by captured_at) tracked sessions once ``contexts``
    exceeds ``max_entries``.

    Oldest is evicted first: a session's context reading is fresh-or-nothing
    anyway (context_window.py), so an evicted session simply becomes "no
    reading yet" the next time it is looked up -- the same as one that had
    never captured at all, not a lie, just an absence. An entry with no
    decodable captured_at sorts as the oldest possible value so it is
    evicted before any entry pruning could otherwise validate, rather than
    being kept indefinitely because it cannot be dated.
    """

    if max_entries < 0 or len(contexts) <= max_entries:
        return
    ordered = sorted(
        contexts.items(),
        key=lambda item: (
            _decoded_captured_at(item[1])
            if _decoded_captured_at(item[1]) is not None
            else float("-inf")
        ),
    )
    for key, _value in ordered[: len(contexts) - max_entries]:
        del contexts[key]


def _context_entry_is_fresh(value: Any, now: float, fresh_for_seconds: float) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        captured_at = float(value["captured_at"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    # resolve_age (context_window.py) rejects a non-finite or implausibly
    # future-dated captured_at rather than clamping it to age 0.0: the naive
    # clamp used before this existed made (now - captured_at) negative,
    # which is always <= any non-negative freshness window, so a
    # future-dated entry was reported as "fresh" and never pruned, no
    # matter how long it sat there (finding #3, context-window adversarial
    # review, the pruning half of the clock-rollback bug).
    age = resolve_age(captured_at, now)
    if age is None:
        return False
    return age <= max(0.0, fresh_for_seconds)


def append_history(snapshots: Iterable[Snapshot], state_dir: Optional[Path] = None) -> None:
    """Append compact, one-object-per-line snapshot history."""

    values = list(snapshots)
    if not values:
        return
    directory = resolve_state_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "history.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for snapshot in values:
            json.dump(snapshot.to_dict(), handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def clear_state(state_dir: Optional[Path] = None) -> bool:
    """Remove persisted snapshots, diagnostics, and history."""

    directory = resolve_state_dir(state_dir)
    removed = False
    for name in ("state.json", "history.jsonl"):
        path = directory / name
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            pass
    return removed


def snapshots_from_state(state: Dict[str, Any]) -> Dict[str, Snapshot]:
    """Decode valid persisted snapshots keyed by source and window."""

    result: Dict[str, Snapshot] = {}
    sources = state.get("sources")
    if not isinstance(sources, dict):
        return result
    for source_name, windows in sources.items():
        if not isinstance(windows, dict):
            continue
        for window_name, value in windows.items():
            if not isinstance(value, dict):
                continue
            try:
                snapshot = Snapshot.from_dict(value)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if snapshot.source != source_name or snapshot.window != window_name:
                continue
            result["{}:{}".format(source_name, window_name)] = snapshot
    return result


def context_captures_from_state(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return raw persisted context captures keyed by session_id.

    This intentionally returns raw dicts, not decoded ``ContextReading``
    objects: decoding needs ``now`` and the context freshness window, which
    only a caller with a specific point in time to bind against has (see
    ``context_window.ContextReading.from_dict``). Every value here is at
    least a dict; further validation happens at decode time, wrapped in the
    same ``(KeyError, TypeError, ValueError, OverflowError)`` catch
    ``snapshots_from_state`` uses for rate-limit snapshots above.
    """

    sources = state.get("sources")
    if not isinstance(sources, dict):
        return {}
    claude_source = sources.get("claude")
    if not isinstance(claude_source, dict):
        return {}
    contexts = claude_source.get("context")
    if not isinstance(contexts, dict):
        return {}
    return {
        session_id: value
        for session_id, value in contexts.items()
        if isinstance(session_id, str) and isinstance(value, dict)
    }


def _empty_state() -> Dict[str, Any]:
    return {"version": 1, "sources": {}}
