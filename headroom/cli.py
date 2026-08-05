"""Command-line entry point for headroom."""

import argparse
from dataclasses import dataclass
from importlib import metadata
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .bounds import Reading, Snapshot, bound_snapshot
from .claude import parse_stdin
from .codexrpc import CodexRpcResult, read_rate_limits
from .codexsrc import CodexResult, read_latest
from .freshness import freshness_seconds
from .render import render_hook, render_report, render_statusline
from .resets import reset_time_is_plausible, window_minutes_from_raw
from .severity import Severity
from .settings import run_init
from .state import (
    clear_state,
    read_state,
    resolve_state_dir,
    save_snapshots,
    snapshots_from_state,
)


@dataclass(frozen=True)
class _CodexRefreshResult:
    snapshots: Tuple[Snapshot, ...]
    source: str
    rpc: CodexRpcResult
    rollout: Optional[CodexResult]


def build_parser() -> argparse.ArgumentParser:
    """Build the command parser."""

    parser = argparse.ArgumentParser(prog="headroom")
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(_installed_version()),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "statusline",
        "status",
        "json",
        "doctor",
        "reset",
    ):
        subparsers.add_parser(command)
    hook_parser = subparsers.add_parser("hook")
    hook_parser.add_argument(
        "--plain",
        action="store_true",
        help="print human-readable text even when hook JSON is received",
    )
    init_parser = subparsers.add_parser("init", help="configure Claude Code")
    init_parser.add_argument(
        "--settings",
        type=Path,
        default=None,
        metavar="PATH",
        help="settings file to update (default: ~/.claude/settings.json)",
    )
    init_parser.add_argument("--dry-run", action="store_true", help="print the diff without writing files")
    init_parser.add_argument("--print", dest="print_only", action="store_true", help="print the settings snippet only")
    return parser


def _installed_version() -> str:
    try:
        return metadata.version("headroom-cli")
    except metadata.PackageNotFoundError:
        return __version__


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run a headroom command and return its process status."""

    arguments = build_parser().parse_args(argv)
    if arguments.command == "init":
        try:
            return run_init(arguments.settings, dry_run=arguments.dry_run, print_only=arguments.print_only)
        except OSError as error:
            print("headroom init: {}".format(error), file=sys.stderr)
            return 1
    if arguments.command == "statusline":
        return _statusline()
    if arguments.command == "reset":
        try:
            removed = clear_state()
            print(
                "Cleared stored state."
                if removed
                else "Stored state is already clear."
            )
            return 0
        except OSError as error:
            print("headroom reset: {}".format(error), file=sys.stderr)
            return 1
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
            text = render_hook(readings, now, forced_severity=_forced_hook_severity())
            if text:
                if arguments.plain or not _user_prompt_submit_input():
                    print(text)
                else:
                    print(
                        json.dumps(
                            {
                                "hookSpecificOutput": {
                                    "hookEventName": "UserPromptSubmit",
                                    "additionalContext": text,
                                }
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
        return 0
    except (OSError, ValueError, TypeError) as error:
        print("headroom: {}".format(error), file=sys.stderr)
        return 1


def _user_prompt_submit_input() -> bool:
    """Return whether stdin contains a Claude UserPromptSubmit payload."""

    if sys.stdin.isatty():
        return False
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("hook_event_name") == "UserPromptSubmit"
    )


def _forced_hook_severity() -> Optional[Severity]:
    """Read the opt-in diagnostic severity, ignoring unsupported values."""

    value = os.environ.get("HEADROOM_FORCE_SEVERITY", "").strip().lower()
    return {
        "notice": Severity.NOTICE,
        "warn": Severity.WARN,
        "critical": Severity.CRITICAL,
    }.get(value)


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


def _refresh_codex() -> _CodexRefreshResult:
    result = _read_codex_on_demand()
    diagnostics: Dict[str, Any] = {"codex": _codex_diagnostics(result)}
    save_snapshots(result.snapshots, diagnostics=diagnostics)
    return result


def _read_codex_on_demand() -> _CodexRefreshResult:
    rpc = read_rate_limits()
    if rpc.snapshots:
        return _CodexRefreshResult(rpc.snapshots, "app-server", rpc, None)
    rollout = read_latest()
    source = "rollout" if rollout.snapshots else "none"
    return _CodexRefreshResult(rollout.snapshots, source, rpc, rollout)


def _codex_diagnostics(result: _CodexRefreshResult) -> Dict[str, Any]:
    rollout = result.rollout
    rollout_notes = rollout.notes if rollout is not None else ()
    return {
        "source": result.source,
        "rpc_attempted": result.rpc.attempted,
        "rpc_notes": list(result.rpc.notes),
        "file": rollout.file if rollout is not None else None,
        "files_checked": rollout.files_checked if rollout is not None else 0,
        "notes": list(result.rpc.notes + rollout_notes),
    }


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
    codex = _read_codex_on_demand()
    rollout = codex.rollout
    print("State directory: {}".format(directory))
    print("State file: {}".format("found" if state_path.is_file() else "missing"))
    print("Claude readings: {}".format(_found_windows(existing, "claude")))
    print("Codex source: {}".format(codex.source))
    print(
        "Codex sessions: {}".format(
            "not checked"
            if rollout is None
            else "found" if rollout.files_checked else "missing"
        )
    )
    print(
        "Codex rollout: {}".format(
            "not checked"
            if rollout is None
            else rollout.file or "no usable snapshot"
        )
    )
    print("Codex readings: {}".format(", ".join(snapshot.window for snapshot in codex.snapshots) or "missing"))
    notes = tuple(_stored_diagnostic_notes(state))
    notes += tuple(_stored_snapshot_reset_notes(existing))
    notes += codex.rpc.notes + (rollout.notes if rollout is not None else ())
    if notes:
        print("Notes: {}".format("; ".join(dict.fromkeys(notes))))
    return 0


def _found_windows(snapshots: Dict[str, Any], source: str) -> str:
    windows = [window for window in ("short", "weekly") if "{}:{}".format(source, window) in snapshots]
    return ", ".join(windows) if windows else "missing"


def _stored_diagnostic_notes(state: Dict[str, Any]) -> List[str]:
    result: List[str] = []
    diagnostics = state.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return result
    for source, details in diagnostics.items():
        if not isinstance(details, dict):
            continue
        notes = details.get("notes")
        if isinstance(notes, list):
            result.extend(
                "stored {}: {}".format(source, note)
                for note in notes
                if isinstance(note, str)
            )
        unparsed = details.get("unparsed")
        if not isinstance(unparsed, list):
            continue
        for note in unparsed:
            if not isinstance(note, dict):
                continue
            reason = note.get("reason")
            if not isinstance(reason, str):
                continue
            path = note.get("path")
            location = (
                "/".join(str(part) for part in path)
                if isinstance(path, list)
                else ""
            )
            result.append(
                "stored {}{}: {}".format(
                    source,
                    " at {}".format(location) if location else "",
                    reason,
                )
            )
    return result


def _stored_snapshot_reset_notes(snapshots: Dict[str, Snapshot]) -> List[str]:
    result: List[str] = []
    for snapshot in snapshots.values():
        resets_at = snapshot.resets_at
        if resets_at is None or reset_time_is_plausible(
            resets_at,
            snapshot.captured_at,
            window_minutes_from_raw(snapshot.raw),
        ):
            continue
        result.append(
            "stored {} {}: rejected implausible resets_at {!r}".format(
                snapshot.source,
                snapshot.window,
                resets_at,
            )
        )
    return result


def init_main() -> int:
    """Run init with arguments supplied to the legacy installer shim."""
    return main(["init"] + sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
