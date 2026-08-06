"""ContextReading: fresh-or-nothing bounding for Claude's per-session
context-window usage.

Context usage cannot reuse bounds.py's Reading model. That model depends on
two properties every rate-limit window has and context usage does not:
usage is monotonic within a window, and a window has an absolute
``resets_at`` after which a stale value becomes known-good again (see
docs/explanation-bounds.md). Context usage is monotonic non-decreasing
EXCEPT for compaction, an event the payload never reports, and there is no
future time at which a stale reading becomes known-good. A stale context
reading is therefore reported as nothing -- never a bound, never a guess.
See docs/explanation-context.md for the full argument.

Context is also per-session where every other headroom signal is
account-wide. ``claude.py`` extracts the top-level ``session_id`` alongside
the ``context_window`` subtree, and ``state.py`` keys stored captures by
that session_id, so a reading here always names the one session it
describes. Nothing in this module resolves a session_id; a caller with no
session_id has nothing to look up and must treat that the same as a missing
reading (see cli.py).
"""

from dataclasses import dataclass
import math
from typing import Any, Dict, Optional


# Ordinary wall-clock behavior (NTP step corrections, VM pause/resume, or
# simply reading two different process's time.time() calls a moment apart)
# can put a capture's own recorded captured_at a hair ahead of a caller's
# later `now`. update_check.py's CLOCK_SKEW_ALLOWANCE_SECONDS (5 minutes)
# exists for the same reason but is deliberately NOT reused here: that
# allowance is sized against a 24-HOUR cache window, where 5 minutes is a
# rounding error. Context's own freshness window defaults to 300 SECONDS
# (freshness.py) -- reusing a 300-second allowance here would let a reading
# dated up to the ENTIRE freshness window into the future still count as
# "fresh", which defeats fresh-or-nothing rather than merely tolerating
# ordinary skew. A much smaller allowance, sized to how far apart two nearby
# time.time() calls in the same statusline/hook pipeline can plausibly land,
# still absorbs real skew without swallowing the window it is meant to bound.
CLOCK_SKEW_ALLOWANCE_SECONDS = 5.0


def resolve_age(
    captured_at: float,
    now: float,
    skew_allowance_seconds: float = CLOCK_SKEW_ALLOWANCE_SECONDS,
) -> Optional[float]:
    """Return a captured_at's age relative to now, or None when the pair
    cannot support a sound answer.

    None covers two cases (finding #3, context-window adversarial review):
    a non-finite captured_at or now (unreachable through ordinary capture,
    but reachable through a hand-edited or corrupted state.json), and a
    captured_at dated more than ``skew_allowance_seconds`` into the future
    relative to now (a clock rollback, or corrupt data claiming to be from
    the future). The naive ``max(0.0, now - captured_at)`` clamp used before
    this existed silently turned every future-dated captured_at into age
    0.0 -- "just captured" -- rather than flagging it as unsound, which let
    a stale reading look perpetually fresh once the clock went backwards,
    and also let a pruning sweep keyed on the same clamp retain it forever.

    ``skew_allowance_seconds`` defaults to ``CLOCK_SKEW_ALLOWANCE_SECONDS``
    (this module's own tight, decode-time tolerance -- appropriate when
    ``now`` is a real ``time.time()`` reading, as ``ContextReading.
    from_dict``'s caller always supplies). ``state.py``'s multi-session
    pruning sweep passes a much wider allowance instead: its own "now" is
    not a real clock reading at all, only the MOST RECENTLY PROCESSED
    capture's own timestamp (state.py deliberately never reads the real
    clock), and different sessions' captures can be reordered in EXECUTION
    relative to when they were originally timestamped -- a session whose
    write is delayed behind others (lock contention, process scheduling)
    can still be processed after a different session's genuinely newer
    capture already committed. Using the tight decode-time allowance for
    that sweep would let the slower session's older timestamp prune the
    faster session's already-stored, perfectly valid entry as
    "implausibly future" -- confirmed by direct reproduction, not merely
    theorized. A wider allowance there tolerates realistic cross-session
    reordering while a genuinely corrupt, wildly future-dated entry (the
    scenario this function exists to catch) still exceeds it.
    """

    if not math.isfinite(captured_at) or not math.isfinite(now):
        return None
    if captured_at - now > skew_allowance_seconds:
        return None
    return max(0.0, now - captured_at)


@dataclass(frozen=True)
class ContextReading:
    """A session's context usage, bound to a specific point in time.

    Unlike ``bounds.Reading``, there is only one confidence state: fresh or
    nothing. ``fresh`` says whether this reading is still usable; a caller
    holding a non-fresh reading must treat it exactly like a missing one,
    never degrade it to a lower-bound guess the way a stale rate-limit
    snapshot is degraded.
    """

    used_percent: float
    size: Optional[int]
    captured_at: float
    age_seconds: float
    fresh: bool
    session_id: str
    source: str = "claude"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible representation, including computed fields.

        This is the shape shown to a human or model (``json``, ``doctor``),
        not the shape persisted to ``state.json`` -- ``age_seconds`` and
        ``fresh`` are relative to a ``now`` that only exists at read time.
        The persisted capture is a plain dict built directly by
        ``claude.py``'s extraction and merged in ``state.py``.
        """

        return {
            "used_percent": self.used_percent,
            "size": self.size,
            "captured_at": self.captured_at,
            "age_seconds": self.age_seconds,
            "fresh": self.fresh,
            "session_id": self.session_id,
            "source": self.source,
        }

    @classmethod
    def from_dict(
        cls, value: Dict[str, Any], now: float, fresh_for_seconds: float
    ) -> "ContextReading":
        """Decode a stored capture into a reading bound to ``now``.

        Raises ``KeyError``, ``TypeError``, ``ValueError``, or
        ``OverflowError`` on bad data, matching
        ``bounds.Snapshot.from_dict``. The caller (``state.py``, ``cli.py``)
        wraps this call the same way ``snapshots_from_state`` already wraps
        ``Snapshot.from_dict``, because ``hook``/``json``/``status``/
        ``doctor`` catch only ``(OSError, ValueError, TypeError)`` at their
        outermost level, not a blanket ``Exception``.
        """

        captured_at = float(value["captured_at"])
        used_percent = float(value["used_percentage"])
        if not math.isfinite(used_percent) or used_percent < 0.0 or used_percent > 100.0:
            # The PARSE path (claude.py's _context_percent) already rejects
            # anything outside [0, 100] before a capture is ever stored; the
            # DECODE path did not, so a hand-edited or corrupted state.json
            # could report "150% used" or worse (finding #4, context-window
            # adversarial review). Mirroring the same bound here closes that
            # gap; a rejection collapses to the same silence as every other
            # bad-data case this method already raises for.
            raise ValueError("used_percentage out of range")
        session_id = str(value["session_id"])
        if not session_id:
            raise ValueError("empty session_id")
        try:
            session_id.encode("utf-8")
        except UnicodeEncodeError as error:
            # A lone UTF-16 surrogate (reachable through a hand-crafted or
            # corrupted state.json: JSON's \uXXXX escapes accept any code
            # unit, paired or not) decodes here as an ordinary Python str
            # and then fails much later at json.dumps(..., ensure_ascii=False)
            # time (finding #9), turning `headroom json`'s exit code into 1.
            # Rejecting it here, at decode time, keeps that surface's exit
            # code intact through the same "corrupt state collapses to
            # silence" path every other bad-data case in this method takes,
            # rather than switching every writer to ensure_ascii=True.
            raise ValueError("session_id is not valid Unicode") from error
        size_raw = value.get("size")
        if size_raw is None:
            size = None
        elif isinstance(size_raw, bool):
            raise ValueError("size must not be a boolean")
        elif isinstance(size_raw, int):
            size = size_raw
        elif isinstance(size_raw, float):
            # int() TRUNCATES a fractional float (int(1.9) == 1) instead of
            # rejecting it, which would report a size the payload never
            # actually supplied (the same failure mode as finding #10 in
            # claude.py's parse-side _context_size, mirrored here for the
            # decode path).
            if not math.isfinite(size_raw) or not size_raw.is_integer():
                raise ValueError("size must be an integer-valued number")
            size = int(size_raw)
        else:
            size = int(size_raw)
        if size is not None and size <= 0:
            raise ValueError("size must be positive")
        age = resolve_age(captured_at, float(now))
        if age is None:
            raise ValueError("captured_at is non-finite or implausibly far in the future")
        fresh = age <= max(0.0, float(fresh_for_seconds))
        return cls(
            used_percent=used_percent,
            size=size,
            captured_at=captured_at,
            age_seconds=age,
            fresh=fresh,
            session_id=session_id,
            source=str(value.get("source", "claude")),
        )
