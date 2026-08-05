"""Defensive parsing of Claude Code statusline payloads."""

from dataclasses import dataclass
import json
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .bounds import Snapshot, valid_percent
from .resets import parse_plausible_reset_time, parse_reset_time


_USED_KEYS = ("used_percentage", "usedPercentage", "used_percent", "usedPercent")
_RESET_KEYS = ("resets_at", "resetsAt")
_WINDOW_KEYS = ("window_minutes", "windowMinutes")
_NAMED_WINDOWS = {"five_hour": "short", "seven_day": "weekly"}
_CONTEXT_WINDOW_KEYS = frozenset(("context_window", "contextWindow"))


@dataclass(frozen=True)
class ParseResult:
    """Snapshots understood from a payload plus diagnostic notes."""

    snapshots: Tuple[Snapshot, ...]
    unparsed: Tuple[Dict[str, Any], ...]


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
    return ParseResult(tuple(found[key] for key in ("short", "weekly") if key in found), tuple(notes))


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
