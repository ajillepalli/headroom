"""Atomic persistence for snapshots and append-only history."""

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Iterable, Optional, Sequence

from .bounds import Snapshot
from .config import resolve_state_dir


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
) -> Dict[str, Any]:
    """Merge snapshots into state and append each captured reading to history."""

    state = read_state(state_dir)
    sources = state.setdefault("sources", {})
    if not isinstance(sources, dict):
        sources = {}
        state["sources"] = sources
    for snapshot in snapshots:
        source = sources.setdefault(snapshot.source, {})
        if not isinstance(source, dict):
            source = {}
            sources[snapshot.source] = source
        source[snapshot.window] = snapshot.to_dict()
    state["version"] = 1
    if diagnostics is not None:
        stored = state.setdefault("diagnostics", {})
        if not isinstance(stored, dict):
            stored = {}
            state["diagnostics"] = stored
        stored.update(diagnostics)
    write_state(state, state_dir)
    append_history(snapshots, state_dir)
    return state


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


def _empty_state() -> Dict[str, Any]:
    return {"version": 1, "sources": {}}
