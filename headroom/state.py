"""Atomic persistence for snapshots and append-only history."""

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Iterable, Optional, Sequence

from .bounds import Snapshot
from .config import resolve_state_dir
from .freshness import freshness_seconds


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
    """

    state = read_state(state_dir)
    sources = state.setdefault("sources", {})
    if not isinstance(sources, dict):
        sources = {}
        state["sources"] = sources
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


def _context_entry_is_fresh(value: Any, now: float, fresh_for_seconds: float) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        captured_at = float(value["captured_at"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return (now - captured_at) <= max(0.0, fresh_for_seconds)


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
