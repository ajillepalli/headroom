"""Defensive parsing of Claude Code statusline payloads."""

from dataclasses import dataclass
import json
import math
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .bounds import Snapshot, valid_percent
from .resets import parse_plausible_reset_time, parse_reset_time


_USED_KEYS = ("used_percentage", "usedPercentage", "used_percent", "usedPercent")
_RESET_KEYS = ("resets_at", "resetsAt")
_WINDOW_KEYS = ("window_minutes", "windowMinutes")
_NAMED_WINDOWS = {"five_hour": "short", "seven_day": "weekly"}
_CONTEXT_WINDOW_KEYS = frozenset(("context_window", "contextWindow"))
_CONTEXT_REMAINING_KEYS = ("remaining_percentage", "remainingPercentage")
_CONTEXT_SIZE_KEYS = ("context_window_size", "contextWindowSize")
# Claude Code's own statusline and UserPromptSubmit payloads (Anthropic's
# published schema) use exactly this spelling for both -- unlike the
# rate-limit bucket fields above, which come from a heterogeneous API
# surface and need snake_case/camelCase tolerance, session_id is generated
# by Claude Code itself and has one spelling.
_SESSION_ID_KEY = "session_id"
# Sentinel distinguishing "context_window_size absent" (size stays None,
# nothing to report) from "context_window_size present but invalid" (size
# drops to None too, but a diagnostic note is warranted).
_INVALID_CONTEXT_SIZE = object()
# Real session_ids are UUIDs (~36 characters); this is generous headroom for
# whatever format a future Claude Code version might use, while still
# bounding a pathological or hostile payload from being accepted and then
# stored as both a dict key and a value without limit (finding #8,
# context-window adversarial review).
_MAX_SESSION_ID_LENGTH = 256


@dataclass(frozen=True)
class ParseResult:
    """Snapshots understood from a payload plus diagnostic notes.

    ``context`` is a plain dict ready for ``state.save_snapshots``'s
    ``context_capture`` argument (``used_percentage``, ``size``,
    ``session_id``, ``captured_at``, ``source``), or ``None`` when no usable
    context reading could be extracted. It is intentionally not a
    ``context_window.ContextReading`` -- that type also carries
    ``age_seconds``/``fresh``, which only exist relative to a ``now`` at
    read time, not at capture time.
    """

    snapshots: Tuple[Snapshot, ...]
    unparsed: Tuple[Dict[str, Any], ...]
    context: Optional[Dict[str, Any]] = None
    context_unparsed: Tuple[Dict[str, Any], ...] = ()


def classify_window(window_minutes: Any, path: Sequence[str]) -> Optional[str]:
    """Classify a rate-limit window by duration, then by its key path."""

    duration = valid_percent(window_minutes)
    if duration is not None:
        return "weekly" if duration >= 1440.0 else "short"
    if path:
        named_window = _NAMED_WINDOWS.get(path[-1].lower())
        if named_window is not None:
            return named_window
    name = "/".join(path).lower()
    if "week" in name or "7d" in name:
        return "weekly"
    if "5" in name or "hour" in name:
        return "short"
    return None


def parse_payload(payload: Any, captured_at: Optional[float] = None) -> ParseResult:
    """Recursively extract rate-limit snapshots from a Claude payload."""

    captured = time.time() if captured_at is None else float(captured_at)
    found: Dict[str, Snapshot] = {}
    notes: List[Dict[str, Any]] = []
    context, context_notes = _extract_context(payload, captured)

    for path, value in _walk(payload):
        if any(part in _CONTEXT_WINDOW_KEYS for part in path):
            continue
        if not isinstance(value, dict):
            continue
        used_key = _first_key(value, _USED_KEYS)
        if used_key is None:
            continue
        used = valid_percent(value.get(used_key))
        if used is None:
            notes.append(
                {
                    "path": list(path),
                    "reason": "invalid used percentage",
                    "value": _without_context_windows(value),
                }
            )
            continue
        duration_key = _first_key(value, _WINDOW_KEYS)
        duration = value.get(duration_key) if duration_key is not None else None
        window = classify_window(duration, path)
        if window is None:
            notes.append(
                {
                    "path": list(path),
                    "reason": "unknown window",
                    "value": _without_context_windows(value),
                }
            )
            continue
        reset_key = _first_key(value, _RESET_KEYS)
        reset_value = value.get(reset_key) if reset_key is not None else None
        resets_at, reset_note = parse_plausible_reset_time(reset_value, captured, duration)
        if reset_note is not None:
            notes.append({"path": list(path), "reason": reset_note, "value": reset_value})
        snapshot = Snapshot(
            used_percentage=used,
            captured_at=captured,
            resets_at=resets_at,
            window=window,
            source="claude",
            limit_reached=bool(value.get("limit_reached", value.get("limitReached", used >= 100.0))),
            raw=dict(value),
        )
        found[window] = snapshot

    if not found:
        notes.append(
            {
                "path": [],
                "reason": "no usable rate limits",
                "value": _without_context_windows(payload),
            }
        )
    return ParseResult(
        tuple(found[key] for key in ("short", "weekly") if key in found),
        tuple(notes),
        context,
        context_notes,
    )


def parse_stdin(text: str, captured_at: Optional[float] = None) -> ParseResult:
    """Decode and parse a statusline JSON document without raising."""

    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as error:
        return ParseResult((), ({"path": [], "reason": "invalid JSON", "value": str(error)},))
    try:
        return parse_payload(payload, captured_at)
    except (TypeError, ValueError, OverflowError) as error:
        return ParseResult((), ({"path": [], "reason": "unexpected payload", "value": str(error)},))


def _walk(value: Any, path: Tuple[str, ...] = ()) -> Iterator[Tuple[Tuple[str, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, path + (str(index),))


def _first_key(value: Dict[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        if key in value:
            return key
    return None


def _extract_context(
    payload: Any, captured_at: float
) -> Tuple[Optional[Dict[str, Any]], Tuple[Dict[str, Any], ...]]:
    """Extract the first ``context_window`` subtree, plus the top-level
    ``session_id`` it is reported against.

    Runs independently of the rate-limit walk above, but reuses the same
    ``_walk``/``_CONTEXT_WINDOW_KEYS`` the issue #9 exclusion guard already
    relies on to find every ``context_window``/``contextWindow`` subtree at
    any depth. The first match in document order wins; a second match only
    adds a diagnostic note rather than being tried as a fallback -- a
    payload should have exactly one context_window subtree, so more than
    one is itself worth surfacing, not silently resolving. Never raises;
    matches this module's other parsing functions.

    Context is per-session (see context_window.py's module docstring), so a
    capture with no session_id is not just unusable, it is not returned as a
    capture at all -- the caller must fall silent, not fall back to storing
    it under some other key.
    """

    notes: List[Dict[str, Any]] = []
    matches = [
        (path, value)
        for path, value in _walk(payload)
        if path and path[-1] in _CONTEXT_WINDOW_KEYS and isinstance(value, dict)
    ]
    if not matches:
        return None, ()
    path, value = matches[0]
    if len(matches) > 1:
        notes.append(
            {
                "path": list(matches[1][0]),
                "reason": "duplicate context_window subtree ignored",
            }
        )

    session_id = payload.get(_SESSION_ID_KEY) if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not session_id:
        notes.append({"path": list(path), "reason": "missing session_id"})
        return None, tuple(notes)
    if len(session_id) > _MAX_SESSION_ID_LENGTH:
        # An oversized session_id accepted here would be stored as both a
        # state.json dict key and a value with no bound (finding #8,
        # context-window adversarial review); reject the whole capture
        # rather than silently truncating or accepting it.
        notes.append({"path": list(path), "reason": "session_id exceeds max length"})
        return None, tuple(notes)

    used = _context_percent(value, _USED_KEYS)
    remaining = _context_percent(value, _CONTEXT_REMAINING_KEYS)
    if used is not None and remaining is not None:
        if abs((used + remaining) - 100.0) > 0.5:
            # Both fields are individually valid but contradict each other
            # (e.g. {"used_percentage": 5, "remaining_percentage": 5},
            # which implies 95% used if remaining is trusted instead).
            # Reporting either one would assert something the payload
            # itself does not actually support, so the whole capture is
            # rejected (finding #6, context-window adversarial review)
            # rather than silently preferring "used" the way an earlier
            # version of this function did.
            notes.append(
                {
                    "path": list(path),
                    "reason": "used and remaining do not sum to 100",
                    "value": {"used": used, "remaining": remaining},
                }
            )
            return None, tuple(notes)
        # used wins when both are present, valid, and agree (validation table).
    elif used is None and remaining is not None:
        # remaining was already validated into [0, 100] by _context_percent
        # before this subtraction runs.
        used = 100.0 - remaining
    if used is None:
        notes.append({"path": list(path), "reason": "invalid context percentage"})
        return None, tuple(notes)

    size = _context_size(value)
    if size is _INVALID_CONTEXT_SIZE:
        notes.append({"path": list(path), "reason": "invalid context_window_size"})
        size = None

    return (
        {
            "used_percentage": used,
            "size": size,
            "session_id": session_id,
            "captured_at": captured_at,
            "source": "claude",
        },
        tuple(notes),
    )


def _context_percent(value: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    """A context percentage, rejecting anything valid_percent accepts but a
    context reading must not: values above 100 (never render "150% used")."""

    key = _first_key(value, keys)
    if key is None:
        return None
    percent = valid_percent(value.get(key))
    if percent is None or percent > 100.0:
        return None
    return percent


def _context_size(value: Dict[str, Any]) -> Any:
    """The validated context window token size, or _INVALID_CONTEXT_SIZE
    when present but unusable. Absent is None, distinct from invalid.

    Only a positive integer-valued number, or a string of decimal digits,
    is accepted. Python's int() on a float silently TRUNCATES a fractional
    value (int(1.9) == 1) rather than rejecting it, which would report a
    context_window_size the payload never actually supplied (finding #10,
    context-window adversarial review) -- a payload reporting a fractional
    token count is not "size 1 with some noise", it is not a usable size.
    """

    key = _first_key(value, _CONTEXT_SIZE_KEYS)
    if key is None:
        return None
    raw = value.get(key)
    if isinstance(raw, bool):
        return _INVALID_CONTEXT_SIZE
    if isinstance(raw, int):
        size = raw
    elif isinstance(raw, float):
        if not math.isfinite(raw) or not raw.is_integer():
            return _INVALID_CONTEXT_SIZE
        size = int(raw)
    else:
        try:
            size = int(raw)
        except (TypeError, ValueError, OverflowError):
            return _INVALID_CONTEXT_SIZE
    if size <= 0:
        return _INVALID_CONTEXT_SIZE
    return size


def _without_context_windows(value: Any) -> Any:
    """Copy diagnostic data while omitting Claude's context-usage subtree."""

    if isinstance(value, dict):
        return {
            key: _without_context_windows(child)
            for key, child in value.items()
            if str(key) not in _CONTEXT_WINDOW_KEYS
        }
    if isinstance(value, list):
        return [_without_context_windows(child) for child in value]
    return value
