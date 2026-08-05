"""Install headroom hooks without discarding user configuration."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import difflib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


def default_settings_path() -> Path:
    """Return the default Claude Code settings path."""
    return Path.home() / ".claude" / "settings.json"


def codex_home_path(override: Optional[Path] = None) -> Path:
    """Return the selected Codex home without touching it."""
    if override is not None:
        return override.expanduser()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def codex_hooks_path(codex_home: Optional[Path] = None) -> Path:
    """Return the Codex hooks file selected by an override or CODEX_HOME."""
    return codex_home_path(codex_home) / "hooks.json"


def shell_command(command_parts: Sequence[str]) -> str:
    """Return a command line quoted for the current platform's shell."""
    return subprocess.list2cmdline(command_parts) if os.name == "nt" else shlex.join(command_parts)


def headroom_command(subcommand: str) -> str:
    """Return an installed command, with a checkout-safe Python fallback."""
    if shutil.which("headroom") is not None:
        return "headroom {}".format(subcommand)

    repository = str(Path(__file__).resolve().parent.parent)
    command = shell_command([sys.executable, "-m", "headroom.cli", subcommand])
    if os.name == "nt":
        return 'set "PYTHONPATH={}" && {}'.format(repository, command)
    return "PYTHONPATH={} {}".format(shlex.quote(repository), command)


def settings_snippet() -> Dict[str, Any]:
    """Return the settings fragment for the current installation mode."""
    return {
        "statusLine": {
            "type": "command",
            "command": headroom_command("statusline"),
            "refreshInterval": 300,
        },
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": headroom_command("hook"),
                        }
                    ]
                }
            ]
        },
    }


def codex_hooks_snippet() -> Dict[str, Any]:
    """Return the hook document accepted by Codex."""
    return {
        "description": "headroom",
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "headroom hook",
                            "timeoutSec": 10,
                        }
                    ]
                }
            ]
        },
    }


def merge_settings(existing: Dict[str, Any], snippet: Dict[str, Any]) -> Dict[str, Any]:
    """Merge headroom settings while preserving unrelated configuration."""
    merged = copy.deepcopy(existing)
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("the existing 'hooks' setting is not a JSON object")

    prompt_hooks = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(prompt_hooks, list):
        raise ValueError("the existing 'hooks.UserPromptSubmit' setting is not a JSON array")

    headroom_hook = snippet["hooks"]["UserPromptSubmit"][0]
    if headroom_hook not in prompt_hooks:
        prompt_hooks.append(copy.deepcopy(headroom_hook))
    merged["statusLine"] = copy.deepcopy(snippet["statusLine"])
    return merged


def merge_codex_hooks(existing: Dict[str, Any], snippet: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the Codex hook while preserving every other entry and event."""
    merged = copy.deepcopy(existing)
    merged.setdefault("description", snippet["description"])
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("the existing 'hooks' setting is not a JSON object")

    prompt_hooks = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(prompt_hooks, list):
        raise ValueError("the existing 'hooks.UserPromptSubmit' setting is not a JSON array")

    headroom_hook = snippet["hooks"]["UserPromptSubmit"][0]
    if headroom_hook not in prompt_hooks:
        prompt_hooks.append(copy.deepcopy(headroom_hook))
    return merged


@dataclass(frozen=True)
class _InstallTarget:
    path: Path
    snippet: Dict[str, Any]
    merger: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


@dataclass(frozen=True)
class _PreparedInstall:
    target: _InstallTarget
    existed: bool
    original_text: str
    rendered: str
    changed: bool


def run_init(
    settings_path: Optional[Path] = None,
    *,
    codex: bool = False,
    all_targets: bool = False,
    codex_home: Optional[Path] = None,
    dry_run: bool = False,
    print_only: bool = False,
) -> int:
    """Print or install the selected Claude Code and Codex fragments."""
    targets: List[_InstallTarget] = []
    if not codex or all_targets:
        targets.append(
            _InstallTarget(
                settings_path if settings_path is not None else default_settings_path(),
                settings_snippet(),
                merge_settings,
            )
        )
    if codex or all_targets:
        targets.append(
            _InstallTarget(
                codex_hooks_path(codex_home),
                codex_hooks_snippet(),
                merge_codex_hooks,
            )
        )

    if print_only:
        document = (
            targets[0].snippet
            if len(targets) == 1
            else {"claude": targets[0].snippet, "codex": targets[1].snippet}
        )
        print(json.dumps(document, indent=2))
        return 0

    prepared: List[_PreparedInstall] = []
    for target in targets:
        try:
            prepared.append(_prepare_install(target))
        except ValueError as error:
            print("headroom init: refusing to change {}: {}".format(target.path, error), file=sys.stderr)
            return 1

    if dry_run:
        for item in prepared:
            if item.changed:
                diff = difflib.unified_diff(
                    item.original_text.splitlines(keepends=True),
                    item.rendered.splitlines(keepends=True),
                    fromfile=str(item.target.path),
                    tofile=str(item.target.path),
                )
                sys.stdout.writelines(diff)
        if not any(item.changed for item in prepared):
            print("headroom init: no changes")
        return 0

    changed = [item for item in prepared if item.changed]
    if not changed:
        for item in prepared:
            print("headroom init: {} is already configured".format(item.target.path))
        return 0

    try:
        for item in changed:
            item.target.path.parent.mkdir(parents=True, exist_ok=True)
            if item.existed:
                backup = _backup_path(item.target.path)
                shutil.copyfile(str(item.target.path), str(backup))
                print("Backup: {}".format(backup))
    except OSError as error:
        print("headroom init: could not create backups: {}".format(error), file=sys.stderr)
        return 1

    applied: List[_PreparedInstall] = []
    try:
        for item in changed:
            _atomic_write(item.target.path, item.rendered)
            applied.append(item)
    except OSError as error:
        rollback_errors = _rollback_installs(applied)
        if rollback_errors:
            print(
                "headroom init: update failed after another target was applied; "
                "rollback was incomplete: {}; original error: {}".format(
                    "; ".join(rollback_errors), error
                ),
                file=sys.stderr,
            )
        elif applied:
            print(
                "headroom init: update failed; restored the previously updated target(s): {}".format(error),
                file=sys.stderr,
            )
        else:
            print("headroom init: update failed: {}".format(error), file=sys.stderr)
        return 1

    for item in changed:
        print("Updated: {}".format(item.target.path))
    for item in prepared:
        if not item.changed:
            print("headroom init: {} is already configured".format(item.target.path))
    return 0


def _prepare_install(target: _InstallTarget) -> _PreparedInstall:
    path = target.path
    existed = path.exists()
    original_text = ""
    existing: Dict[str, Any] = {}
    if existed:
        try:
            original_text = path.read_text(encoding="utf-8")
            parsed = json.loads(original_text)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid JSON ({})".format(error))
        if not isinstance(parsed, dict):
            raise ValueError("the top level must be a JSON object")
        existing = parsed

    merged = target.merger(existing, target.snippet)
    rendered = json.dumps(merged, indent=2) + "\n"
    changed = merged != existing
    return _PreparedInstall(target, existed, original_text, rendered, changed)


def _rollback_installs(applied: Sequence[_PreparedInstall]) -> List[str]:
    errors: List[str] = []
    for item in reversed(applied):
        try:
            if item.existed:
                _atomic_write(item.target.path, item.original_text)
            else:
                item.target.path.unlink()
        except OSError as error:
            errors.append("{}: {}".format(item.target.path, error))
    return errors


def codex_hook_registration(codex_home: Optional[Path] = None) -> Tuple[Path, str]:
    """Return the selected hooks path and its headroom registration status."""
    path = codex_hooks_path(codex_home)
    if not path.exists():
        return path, "not registered (file missing)"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return path, "not registered (invalid JSON)"
    except (OSError, UnicodeError) as error:
        return path, "not registered (unreadable: {})".format(error)
    if not isinstance(parsed, dict):
        return path, "not registered (invalid top level)"
    hooks = parsed.get("hooks")
    prompt_hooks = hooks.get("UserPromptSubmit") if isinstance(hooks, dict) else None
    expected = codex_hooks_snippet()["hooks"]["UserPromptSubmit"][0]
    return (
        (path, "registered")
        if isinstance(prompt_hooks, list) and expected in prompt_hooks
        else (path, "not registered")
    )


def _backup_path(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S.%f")
    return path.with_name("{}.{}.bak".format(path.name, timestamp))


def _atomic_write(path: Path, text: str) -> None:
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(path.parent),
            prefix="{}.".format(path.name),
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(text)
            temporary_name = temporary.name
        os.replace(temporary_name, str(path))
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
