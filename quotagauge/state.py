"""Atomic persistence for snapshots and append-only history."""

from contextlib import contextmanager
import errno
import json
import math
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

# --- History of this constant (three prior Codex review rounds each found
# a real defect; read this before touching the value again) ---
#
# round 1: judged implausibility by comparing a stored entry's captured_at
# against ANOTHER capture's own timestamp (deterministic, state.py's usual
# "never read the real clock" design). Two different sessions' captures
# can be reordered in EXECUTION relative to when they were originally
# timestamped (lock queueing or OS scheduling can delay one session's
# write behind another's faster one; reproduced directly with real
# concurrent processes), so a slower session's older timestamp could
# treat a faster session's already-stored, perfectly valid entry as
# "implausibly future" and erase it.
#
# round 2 tried reusing freshness_seconds("context") as the tolerance,
# which broke the moment that user-configurable value was set below
# realistic scheduling delay -- "how long should a reading stay visible"
# and "how much writer timing slop is plausible" are unrelated questions.
#
# round 3 introduced a large FIXED tolerance (3600s) sized only from the
# "stay below genuine corruption" direction, and found ITS cost: a
# merely-moderately-future entry (a real few-minute clock rollback, not
# an attack) could then block legitimate same-session writes for up to an
# hour, even though it could never be DISPLAYED either way (
# ContextReading.from_dict's own much tighter decode-time check already
# refuses that) -- so the hour bought no soundness benefit.
#
# round 4 tightened that same fixed tolerance to 60s and found the
# opposite failure: a writer legitimately suspended for MORE than 60s
# (laptop sleep/resume, SIGSTOP, a scheduler stall) between timestamping
# its capture and actually executing would have its own perfectly valid,
# merely-delayed capture treated as implausible.
#
# All four rounds share one root cause: comparing two DIFFERENT
# CAPTURES' timestamps against each other cannot distinguish "this one
# is corrupt" from "this one is simply older, or reordered in execution"
# -- both look identical as a raw time gap, no matter what threshold is
# chosen, because there is no ground truth in the comparison itself.
#
# The fix (Codex review round 4's own suggestion) is to stop comparing
# captures against EACH OTHER for this question and instead validate
# each one against REAL wall-clock time (``time.time()``), which IS
# ground truth: a legitimate captured_at, however delayed in execution,
# can never be dated later than the real moment it is checked, no matter
# how long that delay was -- only genuinely wrong data (corruption, or a
# badly rolled-back system clock) claims a time later than "now" by a
# meaningful margin. This does mean state.py is no longer fully immune to
# reading the real clock (a deliberate, narrow exception to the "never
# call time.time()" property described elsewhere in this file): it is
# used ONLY for this implausibility check, never for the deterministic
# staleness/ordering logic that the rest of this module still bases on
# whatever timestamp a caller passes in.
#
# With a REAL reference point, the tolerance only needs to cover the gap
# between "when a genuinely valid capture was recorded" and "when THIS
# check happens to run" -- ordinary clock-read skew plus, worst case,
# this process's own lock queueing (bounded by _LOCK_TIMEOUT_SECONDS,
# 5s). 30 seconds is a generous multiple of that bound, verified against
# this project's own stress testing (real concurrent OS processes, which
# never observed an actual lock wait exceeding roughly a tenth of a
# second even under several dozen simultaneous processes), while staying
# far below what any of the corruption scenarios in this project's own
# tests and hostile-input fixtures produce (10,000+ seconds).
_FUTURE_IMPLAUSIBILITY_TOLERANCE_SECONDS = 30.0
# The errno values that mean "someone else holds this lock right now, try
# again" -- as opposed to "this platform/filesystem cannot do this at all",
# which retrying will never fix. fcntl.flock(LOCK_NB) raises EWOULDBLOCK (or
# the numerically-identical EAGAIN on every platform Python defines both
# names for) when contended; msvcrt.locking(LK_NBLCK) raises EACCES for the
# same case (confirmed empirically: contending for an already-locked region
# raises ``PermissionError(13, 'Permission denied')``). Anything else (e.g.
# ENOSYS/EINVAL/ENOTSUP on a filesystem that does not support advisory
# locking at all) is permanent, not contention, and retrying it for the
# full timeout would only add needless latency to every write on such a
# host for no chance of eventual success (Codex review, round 1, P2).
_RETRYABLE_LOCK_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


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
        _replace_with_retry(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


# How many times to retry os.replace on a transient Windows access-denied
# failure, and how long to wait between attempts. See
# _replace_with_retry's own docstring for why this exists.
_REPLACE_RETRY_ATTEMPTS = 5
_REPLACE_RETRY_DELAY_SECONDS = 0.02


def _replace_with_retry(source: str, target: Path) -> None:
    """os.replace, retrying a bounded number of times on a transient
    Windows access-denied failure.

    MoveFileEx (what os.replace wraps on Windows) can fail with
    ERROR_ACCESS_DENIED (Python's ``PermissionError``) for a few
    milliseconds if something else has the TARGET file open without
    FILE_SHARE_DELETE at the exact instant of the rename -- commonly
    antivirus real-time scanning or the Windows Search Indexer briefly
    opening a freshly-written file, neither of which quotagauge controls.
    Reproduced directly under real, heavy concurrent multi-process load
    (dozens of separate ``quotagauge`` processes writing state.json back to
    back). This is not a quotagauge locking bug: ``_locked_state_
    transaction`` already guarantees only one quotagauge process is ever
    inside this function at a time, so the OTHER holder of the file here
    is always something external to quotagauge. POSIX ``rename(2)``, which
    os.replace uses on POSIX, has no equivalent failure mode -- there,
    the first attempt always either succeeds or fails for a durable
    reason, so this retries a genuine POSIX failure a few times for no
    benefit, which costs at most a few tens of milliseconds before
    propagating the same way it always did.

    Only ``PermissionError`` is retried, not every ``OSError``: anything
    else (a full disk, an invalid path) is not a momentary external lock
    and would not be fixed by waiting, matching the same
    retry-only-what-retrying-can-fix principle ``_acquire_lock`` applies
    to its own lock contention above.
    """

    for attempt in range(_REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS)


@contextmanager
def _locked_state_transaction(directory: Path):
    """Serialize save_snapshots' read-merge-write across processes.

    Two quotagauge invocations (two terminal tabs, each running Claude Code's
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
    happen -- exactly one of fcntl/msvcrt exists on every platform quotagauge
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
    """Take an exclusive, non-blocking lock, retrying only CONTENDED
    attempts until one succeeds or _LOCK_TIMEOUT_SECONDS elapses. Never
    blocks indefinitely (see _locked_state_transaction's docstring for why
    that matters here).

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
        except OSError as error:
            if error.errno not in _RETRYABLE_LOCK_ERRNOS:
                # A permanent failure (locking unsupported on this
                # filesystem, a bad descriptor, etc.), not another holder
                # in the way: retrying would only burn the whole timeout
                # for no chance of success (Codex review, round 1, P2).
                return False
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
    this function -- from two separate quotagauge processes -- cannot both
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
        # `quotagauge json`/`status`/`hook` (finding #9, context-window
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

    Pruning runs on every call whose capture was actually ACCEPTED (see
    below), using that capture's own ``captured_at`` as "now" rather than
    calling ``time.time()`` here: ``state.py`` otherwise never reads the
    real clock, every timestamp it handles is passed in, and this keeps
    that property so callers stay deterministic in tests. A session that
    stops writing (a closed terminal tab) lingers until the NEXT
    successful capture from any session sweeps it -- acceptable, since a
    lingering entry is inert until then (fresh-or-nothing already refuses
    to read anything stale) and "prune on write" does not require every
    write to also be the sweep.

    A REJECTED capture (one older than what is already stored for its own
    session, see ``_should_replace_context_capture``) is never used as
    "now" for that sweep, even though it is still a real, valid
    ``captured_at`` value: it is, by definition, older than something
    already known to be more current (Codex review, round 1, P2).
    ``_context_entry_is_fresh``'s implausible-future check no longer
    depends on this "now" at all (round 4 moved that check to a real
    ``time.time()`` reference instead), so a rejected capture's timestamp
    can no longer cause a false PRUNE of another entry the way it
    originally could -- but this gate is kept regardless, both as
    defense in depth and because a rejected write has nothing new to
    contribute to a sweep pass in the first place. Skipping it only
    defers cleanup, not correctness: the next ACCEPTED write, from any
    session, still runs it.
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
    accepted = False
    if isinstance(session_id, str) and session_id:
        if _should_replace_context_capture(contexts.get(session_id), capture):
            contexts[session_id] = capture
            accepted = True

    if not accepted:
        return
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

    A decodable but IMPLAUSIBLY FUTURE-DATED existing entry is treated the
    same as an undecodable one -- nothing to protect, so a new capture
    always replaces it. Without this, a stored entry that is finite but
    wildly future-dated (corrupt data, or a hand-edited state.json) would
    make every subsequent LEGITIMATE capture for this same session compare
    as "older" and be rejected forever; since a rejected capture also
    skips the pruning sweep (see ``_merge_context_capture``), that stuck
    session could never recover on its own -- only a DIFFERENT session's
    write happening to run afterward could ever clean it up (Codex review
    round 2, P2).

    "Implausibly future-dated" is judged against REAL wall-clock time
    (``time.time()``), not against the incoming capture's own timestamp:
    see ``_FUTURE_IMPLAUSIBILITY_TOLERANCE_SECONDS``'s own comment for why
    comparing two captures against each other cannot soundly distinguish
    "corrupt" from "simply older or execution-reordered" (Codex review
    round 4, P2, after rounds 1-3 each found a different failure mode of
    that approach).
    """

    if not isinstance(current, dict):
        return True
    current_captured_at = _decoded_captured_at(current)
    if current_captured_at is None:
        return True
    incoming_captured_at = _decoded_captured_at(capture)
    if incoming_captured_at is None:
        return False
    if (
        resolve_age(
            current_captured_at,
            time.time(),
            skew_allowance_seconds=_FUTURE_IMPLAUSIBILITY_TOLERANCE_SECONDS,
        )
        is None
    ):
        return True
    return incoming_captured_at >= current_captured_at


def _decoded_captured_at(value: Any) -> Optional[float]:
    """A stored entry's decoded captured_at, or None when it is missing,
    malformed, or non-finite.

    ``float(...)`` does not raise for a stored NaN or +/-inf (JSON's json
    module accepts those non-standard tokens on decode), so without the
    ``math.isfinite`` check below a corrupt entry with ``captured_at: NaN``
    would decode as a "valid" timestamp -- one that a comparison against
    (``_should_replace_context_capture``'s ``incoming_captured_at >=
    current_captured_at``) can never satisfy, since NaN compares false
    against everything. A legitimate new capture would then be rejected
    forever by that corrupt entry, which the later pruning sweep still
    correctly deletes (``resolve_age`` already checks finiteness) --
    leaving the session with the WORST of both outcomes: the good capture
    discarded and the bad one gone too (Codex review, round 1, P2).
    Treating a non-finite value the same as absent here means the
    "nothing to protect against" replacement rule already documented above
    actually holds for this case too.
    """

    if not isinstance(value, dict):
        return None
    try:
        result = float(value["captured_at"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


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
    """Whether a stored context entry is both plausible and still fresh.

    Two SEPARATE questions, deliberately answered against two DIFFERENT
    reference points (Codex review round 4, P2, after rounds 1-3 each
    found a different failure mode of using one shared reference for
    both -- see _FUTURE_IMPLAUSIBILITY_TOLERANCE_SECONDS's own comment
    for the full history):

    * Is captured_at PLAUSIBLE at all (not non-finite, not implausibly
      future-dated -- corrupt data or a badly rolled-back clock)? Judged
      against REAL wall-clock time (``time.time()``), the only sound
      ground truth for "could this timestamp be real": a legitimate
      captured_at, however long its write was delayed in EXECUTION, can
      never be dated later than the real moment it is checked.
    * Is captured_at STALE (too long ago to still show)? Judged against
      ``now`` -- the MOST RECENTLY PROCESSED capture's own timestamp, not
      a real clock reading (state.py otherwise never calls time.time()).
      This deterministic reference is safe for staleness specifically
      because fresh-or-nothing means an entry found "too old" here is
      simply garbage-collected a little earlier or later than a real
      clock would have called it -- never DISPLAYED either way, so a
      loose reference point costs nothing here, unlike for the
      plausibility question above.
    """

    if not isinstance(value, dict):
        return False
    try:
        captured_at = float(value["captured_at"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if (
        resolve_age(
            captured_at, time.time(), skew_allowance_seconds=_FUTURE_IMPLAUSIBILITY_TOLERANCE_SECONDS
        )
        is None
    ):
        return False
    age = max(0.0, now - captured_at)
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
