"""Read current rate limits from Codex rollout files."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .bounds import Snapshot, valid_percent
from .claude import classify_window, parse_reset_time
from .resets import parse_plausible_reset_time
from .config import resolve_codex_sessions_dir


_USED_KEYS = ("used_percent", "used_percentage", "usedPercent", "usedPercentage")
_WINDOW_KEYS = ("window_minutes", "windowMinutes", "windowDurationMins")
_RESET_KEYS = ("resets_at", "resetsAt")


@dataclass(frozen=True)
class CodexResult:
    """The most recent usable rollout snapshot and discovery details."""

    snapshots: Tuple[Snapshot, ...]
    file: Optional[str]
    files_checked: int
    notes: Tuple[str, ...]


def default_sessions_dir() -> Path:
    """Return the default Codex rollout directory."""

    return resolve_codex_sessions_dir()


def parse_rate_limits(
    payload: Any,
    captured_at: Optional[float] = None,
    notes: Optional[List[str]] = None,
) -> Tuple[Snapshot, ...]:
    """Extract Codex window buckets from an object containing rate_limits."""

    captured = time.time() if captured_at is None else float(captured_at)
    limits_objects = list(_rate_limit_objects(payload))
    if isinstance(payload, dict) and any(
        key in payload for key in ("primary", "secondary", "limit_id", "limit_name")
    ):
        limits_objects.insert(0, payload)
    found: Dict[str, Snapshot] = {}
    for limits in limits_objects:
        for path, bucket in _walk(limits):
            if not isinstance(bucket, dict):
                continue
            used_key = _first_key(bucket, _USED_KEYS)
            if used_key is None:
                continue
            used = valid_percent(bucket.get(used_key))
            if used is None:
                continue
            duration_key = _first_key(bucket, _WINDOW_KEYS)
            duration = bucket.get(duration_key) if duration_key is not None else None
            window = classify_window(duration, path)
            if window is None:
                continue
            reset_key = _first_key(bucket, _RESET_KEYS)
            reset_value = bucket.get(reset_key) if reset_key is not None else None
            resets_at, reset_note = parse_plausible_reset_time(reset_value, captured, duration)
            if reset_note is not None and notes is not None:
                notes.append("{} for {} window".format(reset_note, window))
            found[window] = Snapshot(
                used_percentage=used,
                captured_at=captured,
                resets_at=resets_at,
                window=window,
                source="codex",
                limit_reached=_limit_reached(limits, bucket, used),
                raw=dict(bucket),
            )
    return tuple(found[key] for key in ("short", "weekly") if key in found)


def read_latest(
    sessions_dir: Optional[Path] = None,
    *,
    deadline: Optional[float] = None,
    max_files: Optional[int] = None,
) -> CodexResult:
    """Scan newest rollout files until one yields a usable last snapshot."""

    root = default_sessions_dir() if sessions_dir is None else Path(sessions_dir)
    if not root.is_dir():
        return CodexResult((), None, 0, ("sessions directory not found",))
    try:
        files, discovery_note = _rollout_files(root, deadline, max_files)
    except OSError as error:
        return CodexResult((), None, 0, (str(error),))

    checked = 0
    notes: List[str] = [discovery_note] if discovery_note is not None else []
    for path in files:
        if deadline is not None and time.monotonic() >= deadline:
            notes.append("rollout scan deadline reached")
            break
        checked += 1
        snapshots = _last_snapshot_in_file(path, notes, deadline)
        if snapshots:
            return CodexResult(snapshots, str(path), checked, tuple(notes))
    return CodexResult((), None, checked, tuple(notes or ["no usable rate limits found"]))


def _rollout_files(
    root: Path,
    deadline: Optional[float],
    max_files: Optional[int],
) -> Tuple[List[Path], Optional[str]]:
    if max_files is None:
        files = list(root.glob("*/*/*/rollout-*.jsonl"))
        return sorted(files, key=_file_sort_key, reverse=True), None

    limit = max(0, max_files)
    if limit == 0:
        return [], "rollout scan limited to 0 files"
    entry_budget = max(16, limit * 8)
    candidates: List[Path] = []
    exhausted = False

    def children(path: Path, directories: bool) -> List[Path]:
        nonlocal entry_budget, exhausted
        found: List[Path] = []
        with os.scandir(str(path)) as entries:
            for entry in entries:
                if deadline is not None and time.monotonic() >= deadline:
                    exhausted = True
                    break
                if entry_budget <= 0:
                    exhausted = True
                    break
                entry_budget -= 1
                try:
                    matches = entry.is_dir() if directories else entry.is_file()
                except OSError:
                    continue
                if matches:
                    found.append(Path(entry.path))
        return sorted(found, key=lambda child: child.name, reverse=True)

    for year in children(root, True):
        for month in children(year, True):
            for day in children(month, True):
                for path in children(day, False):
                    if path.name.startswith("rollout-") and path.name.endswith(".jsonl"):
                        candidates.append(path)
                        if len(candidates) >= limit:
                            exhausted = True
                            break
                if exhausted:
                    break
            if exhausted:
                break
        if exhausted:
            break

    note = "rollout scan bounded at {} files".format(limit) if exhausted else None
    return sorted(candidates, key=_file_sort_key, reverse=True), note


def _file_sort_key(path: Path) -> Tuple[float, str]:
    return path.stat().st_mtime, str(path)


def _last_snapshot_in_file(
    path: Path,
    notes: List[str],
    deadline: Optional[float] = None,
) -> Tuple[Snapshot, ...]:
    latest: Tuple[Snapshot, ...] = ()
    try:
        captured_fallback = path.stat().st_mtime
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if deadline is not None and time.monotonic() >= deadline:
                    notes.append("rollout scan deadline reached while reading {}".format(path))
                    break
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                captured = _payload_timestamp(payload, captured_fallback)
                candidate = parse_rate_limits(payload, captured, notes)
                if candidate:
                    latest = candidate
    except OSError as error:
        notes.append("{}: {}".format(path, error))
    return latest


def _payload_timestamp(payload: Any, fallback: float) -> float:
    if isinstance(payload, dict):
        for key in ("timestamp", "created_at", "createdAt"):
            if key not in payload:
                continue
            parsed = parse_reset_time(payload[key])
            if parsed is not None:
                return parsed
    return fallback


def _rate_limit_objects(value: Any) -> Iterator[Dict[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "rate_limits" and isinstance(child, dict):
                yield child
            yield from _rate_limit_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _rate_limit_objects(child)


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


def _limit_reached(limits: Dict[str, Any], bucket: Dict[str, Any], used: float) -> bool:
    values = (
        bucket.get("limit_reached"),
        bucket.get("limitReached"),
        limits.get("rate_limit_reached_type"),
        limits.get("rateLimitReachedType"),
        limits.get("spend_control_reached"),
        limits.get("spendControlReached"),
    )
    return used >= 100.0 or any(bool(value) for value in values)
