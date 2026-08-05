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
from typing import Any, Dict, Optional


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
        session_id = str(value["session_id"])
        if not session_id:
            raise ValueError("empty session_id")
        size_raw = value.get("size")
        size = int(size_raw) if size_raw is not None else None
        age = max(0.0, float(now) - captured_at)
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
