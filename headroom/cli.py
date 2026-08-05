"""Command-line entry point for headroom."""

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from .bounds import Reading, bound_snapshot
from .claude import parse_stdin
from .codexsrc import CodexResult, read_latest
from .freshness import freshness_seconds
from .render import render_hook, render_report, render_statusline
from .state import read_state, resolve_state_dir, save_snapshots, snapshots_from_state


def build_parser() -> argparse.ArgumentParser:
    """Build the command parser."""

    parser = argparse.ArgumentParser(prog="headroom")
    parser.add_argument("command", choices=("statusline", "status", "json", "hook", "doctor"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run a headroom command and return its process status."""

    arguments = build_parser().parse_args(argv)
    if arguments.command == "statusline":
        return _statusline()
    try:
        if arguments.command == "doctor":
            return _doctor()
        now = time.time()
        _refresh_codex()
        state = read_state()
        readings = _readings(state, now)
        if arguments.command == "status":
            print(render_report(readings, now))
        elif arguments.command == "json":
            print(json.dumps(_json_document(state, readings), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        elif arguments.command == "hook":
            text = render_hook(readings, now)
            if text:
                print(text)
        return 0
    except (OSError, ValueError, TypeError) as error:
        print("headroom: {}".format(error), file=sys.stderr)
        return 1


def _statusline() -> int:
    now = time.time()
    try:
        result = parse_stdin(sys.stdin.read(), now)
        diagnostics: Dict[str, Any] = {"claude": {"unparsed": list(result.unparsed)}}
        if result.snapshots:
            save_snapshots(result.snapshots, diagnostics=diagnostics)
        else:
            save_snapshots((), diagnostics=diagnostics)
        readings = _readings(read_state(), now)
        print(render_statusline(readings, now))
    except Exception:
        # Claude Code's terminal contract is more important than diagnostics here.
        try:
            print("headroom: usage unavailable")
        except Exception:
            pass
    return 0


def _refresh_codex() -> CodexResult:
    result = read_latest()
    diagnostics: Dict[str, Any] = {
        "codex": {
            "file": result.file,
            "files_checked": result.files_checked,
            "notes": list(result.notes),
        }
    }
    save_snapshots(result.snapshots, diagnostics=diagnostics)
    return result


def _readings(state: Dict[str, Any], now: float) -> List[Reading]:
    snapshots = snapshots_from_state(state)
    return [
        bound_snapshot(
            snapshots.get("{}:{}".format(source, window)),
            now,
            source,
            window,
            fresh_for_seconds=freshness_seconds(source),
        )
        for source in ("claude", "codex")
        for window in ("short", "weekly")
    ]


def _json_document(state: Dict[str, Any], readings: Sequence[Reading]) -> Dict[str, Any]:
    result = dict(state)
    result["readings"] = [reading.to_dict() for reading in readings]
    return result


def _doctor() -> int:
    directory = resolve_state_dir()
    state_path = directory / "state.json"
    state = read_state()
    existing = snapshots_from_state(state)
    codex = read_latest()
    print("State directory: {}".format(directory))
    print("State file: {}".format("found" if state_path.is_file() else "missing"))
    print("Claude readings: {}".format(_found_windows(existing, "claude")))
    print("Codex sessions: {}".format("found" if codex.files_checked else "missing"))
    print("Codex rollout: {}".format(codex.file or "no usable snapshot"))
    print("Codex readings: {}".format(", ".join(snapshot.window for snapshot in codex.snapshots) or "missing"))
    if codex.notes:
        print("Notes: {}".format("; ".join(codex.notes)))
    return 0


def _found_windows(snapshots: Dict[str, Any], source: str) -> str:
    windows = [window for window in ("short", "weekly") if "{}:{}".format(source, window) in snapshots]
    return ", ".join(windows) if windows else "missing"


if __name__ == "__main__":
    raise SystemExit(main())
