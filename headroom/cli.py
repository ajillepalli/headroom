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
from .burn_rate import BurnRateProjection, project_exhaustion
from .claude import parse_stdin
from .codexrpc import CodexRpcResult, read_rate_limits
from .codexsrc import CodexResult, read_latest
from .freshness import freshness_seconds
from .install_info import format_modified_time, inspect_install, source_commit
from .render import (
    render_burn_rate_doctor_lines,
    render_burn_rate_status_lines,
    render_hook,
    render_report,
    render_statusline,
)
from .resets import reset_time_is_plausible, window_minutes_from_raw
from .severity import Severity
from .settings import (
    codex_hook_command_availability,
    codex_hook_registration,
    run_init,
)
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


HOOK_DEADLINE_SECONDS = 7.0
HOOK_MAX_ROLLOUT_FILES = 32


def build_parser() -> argparse.ArgumentParser:
    """Build the command parser."""

    parser = argparse.ArgumentParser(prog="headroom")
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(_display_version()),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "statusline",
        "status",
        "json",
        "doctor",
        "reset",
        "update",
    ):
        subparsers.add_parser(command)
    hook_parser = subparsers.add_parser("hook")
    hook_parser.add_argument(
        "--plain",
        action="store_true",
        help="print human-readable text even when hook JSON is received",
    )
    hook_parser.add_argument("--stored-only", action="store_true", help=argparse.SUPPRESS)
    init_parser = subparsers.add_parser("init", help="configure Claude Code and Codex hooks")
    init_targets = init_parser.add_mutually_exclusive_group()
    init_targets.add_argument(
        "--codex",
        action="store_true",
        help="configure Codex only",
    )
    init_targets.add_argument(
        "--all",
        dest="all_targets",
        action="store_true",
        help="configure both Claude Code and Codex",
    )
    init_parser.add_argument(
        "--settings",
        type=Path,
        default=None,
        metavar="PATH",
        help="settings file to update (default: ~/.claude/settings.json)",
    )
    init_parser.add_argument(
        "--codex-home",
        type=Path,
        default=None,
        metavar="PATH",
        help="Codex home containing hooks.json (default: CODEX_HOME or ~/.codex)",
    )
    init_parser.add_argument("--dry-run", action="store_true", help="print the diff without writing files")
    init_parser.add_argument("--print", dest="print_only", action="store_true", help="print the settings snippet only")
    return parser


def _installed_version() -> str:
    try:
        return metadata.version("headroom-cli")
    except Exception:
        return __version__


def _display_version() -> str:
    version = _installed_version()
    commit = source_commit()
    return "{} ({})".format(version, commit) if commit else version


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run a headroom command and return its process status."""

    arguments = build_parser().parse_args(argv)
    if arguments.command == "init":
        try:
            return run_init(
                arguments.settings,
                codex=arguments.codex,
                all_targets=arguments.all_targets,
                codex_home=arguments.codex_home,
                dry_run=arguments.dry_run,
                print_only=arguments.print_only,
            )
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
    if arguments.command == "update":
        return _update()
    try:
        if arguments.command == "doctor":
            return _doctor()
        if arguments.command != "hook" or not arguments.stored_only:
            deadline = (
                time.monotonic() + HOOK_DEADLINE_SECONDS
                if arguments.command == "hook"
                else None
            )
            _refresh_codex(deadline=deadline)
        # Captured AFTER the refresh above (not before): a successful
        # app-server refresh timestamps its snapshot with its own
        # time.time() call inside codexrpc.py, which can land after an
        # earlier `now` here. project_exhaustion discards any history record
        # whose captured_at exceeds the `now` it is given, so a stale `now`
        # would silently exclude the snapshot just appended by the refresh
        # above from every projection this call computes (Codex review,
        # round 1, P1).
        now = time.time()
        state = read_state()
        readings = _readings(state, now)
        projections = _burn_rate_projections(now)
        if arguments.command == "status":
            print(render_report(readings, now))
            burn_lines = render_burn_rate_status_lines(projections, now, readings)
            if burn_lines:
                print("Burn rate")
                for line in burn_lines:
                    print(line)
            _print_status_update()
        elif arguments.command == "json":
            print(
                json.dumps(
                    _json_document(state, readings, projections),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif arguments.command == "hook":
            text = render_hook(readings, now, projections=projections, forced_severity=_forced_hook_severity())
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


def _refresh_codex(deadline: Optional[float] = None) -> _CodexRefreshResult:
    result = _read_codex_on_demand(deadline=deadline)
    diagnostics: Dict[str, Any] = {"codex": _codex_diagnostics(result)}
    save_snapshots(result.snapshots, diagnostics=diagnostics)
    return result


def _read_codex_on_demand(deadline: Optional[float] = None) -> _CodexRefreshResult:
    rpc = read_rate_limits(deadline=deadline)
    if rpc.snapshots:
        return _CodexRefreshResult(rpc.snapshots, "app-server", rpc, None)
    rollout = read_latest(
        deadline=deadline,
        max_files=HOOK_MAX_ROLLOUT_FILES if deadline is not None else None,
    )
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


def _burn_rate_projections(now: float) -> List[BurnRateProjection]:
    """Project quota exhaustion from the persisted history file.

    Reads directly from history.jsonl rather than the in-memory readings
    just refreshed above: burn_rate.project_exhaustion needs the whole
    recent history to fit a rate, not just the latest snapshot per window.
    An unreadable or missing history file yields an empty list (see
    project_exhaustion's own OSError handling), which every caller here
    already treats the same as "nothing to report."
    """

    history_path = resolve_state_dir() / "history.jsonl"
    return project_exhaustion(history_path, now=now)


def _json_document(
    state: Dict[str, Any],
    readings: Sequence[Reading],
    projections: Sequence[BurnRateProjection],
) -> Dict[str, Any]:
    result = dict(state)
    result["readings"] = [reading.to_dict() for reading in readings]
    result["burn_rate_projections"] = [projection.to_dict() for projection in projections]
    return result


def _doctor() -> int:
    install = inspect_install(_installed_version())
    print("Install")
    print("  Path: {}".format(install.path))
    print("  Mode: {}".format(install.mode))
    print("  Version: {}".format(install.version))
    print("  Modified: {}".format(format_modified_time(install.modified_at)))
    print()
    codex_hooks, codex_hook_status = codex_hook_registration()
    print("Codex hooks file: {}".format(codex_hooks))
    print("Codex hook: {}".format(codex_hook_status))
    print("Codex hook command: {}".format(codex_hook_command_availability()))
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
    print()
    print("Burn rate")
    doctor_now = time.time()
    burn_lines = render_burn_rate_doctor_lines(_burn_rate_projections(doctor_now), doctor_now)
    if burn_lines:
        for line in burn_lines:
            print(line)
    else:
        print("  no usage history recorded")
    _print_update_doctor()
    return 0


def _print_status_update() -> None:
    """Make the opt-in update call only from the interactive status command."""

    from .update_check import check_for_update, discovery_line, update_check_enabled

    hint = discovery_line()
    if hint is not None:
        print(hint)
        return
    if not update_check_enabled():
        return
    result = check_for_update(_installed_version())
    if result.update_available:
        print(
            "Update available: headroom-cli {} (installed {}). Run headroom update.".format(
                result.latest_version,
                result.installed_version,
            )
        )


def _print_update_doctor() -> None:
    """Report cached or freshly checked update diagnostics."""

    from .update_check import (
        CACHE_FILENAME,
        check_for_update,
        format_timestamp,
        read_cached_result,
        update_check_enabled,
    )

    enabled = update_check_enabled()
    installed_version = _installed_version()
    result = (
        check_for_update(installed_version)
        if enabled
        else read_cached_result(installed_version=installed_version)
    )
    print()
    print("Update check")
    print("  Enabled: {}".format("yes" if enabled else "no"))
    print("  Cache: {}".format(resolve_state_dir() / CACHE_FILENAME))
    if result is None:
        print("  Last outcome: not checked")
        print("  Last checked: never")
        print("  Next eligible check: now")
        return
    if result.outcome == "update":
        outcome = "update available: {}".format(result.latest_version)
    elif result.outcome == "current":
        outcome = "up to date"
    else:
        outcome = "failed: {}".format(result.reason)
    print("  Last outcome: {}".format(outcome))
    print("  Last checked: {}".format(format_timestamp(result.checked_at)))
    print("  Next eligible check: {}".format(format_timestamp(result.next_check_at)))


def _update() -> int:
    """Print an installation-specific update command without executing it."""

    install = inspect_install(_installed_version())
    if install.update_mode == "uv-tool":
        print("Detected install mode: uv tool")
        print("Run: uv tool upgrade headroom-cli")
    elif install.update_mode == "pip":
        print("Detected install mode: pip")
        print("Run: pip install -U headroom-cli")
    elif install.update_mode == "source":
        print("Detected install mode: source checkout")
        print("In {}, run:".format(install.path.parent))
        print("  git pull")
        print("  pip install -e .")
    else:
        print("Install mode could not be determined confidently.")
        print("No update command is suggested.")
    print("Nothing was changed.")
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
