"""Install headroom's Claude Code settings without discarding user config."""

from __future__ import annotations

import copy
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
from typing import Any, Dict, Optional, Sequence


def default_settings_path() -> Path:
    """Return the default Claude Code settings path."""
    return Path.home() / ".claude" / "settings.json"


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


def run_init(
    settings_path: Optional[Path] = None,
    *,
    dry_run: bool = False,
    print_only: bool = False,
) -> int:
    """Print or install the Claude Code settings fragment."""
    snippet = settings_snippet()
    if print_only:
        print(json.dumps(snippet, indent=2))
        return 0

    path = settings_path if settings_path is not None else default_settings_path()
    original_text = ""
    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            original_text = path.read_text(encoding="utf-8")
            parsed = json.loads(original_text)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            print("headroom init: refusing to change {}: invalid JSON ({})".format(path, error), file=sys.stderr)
            return 1
        if not isinstance(parsed, dict):
            print("headroom init: refusing to change {}: the top level must be a JSON object".format(path), file=sys.stderr)
            return 1
        existing = parsed

    try:
        merged = merge_settings(existing, snippet)
    except ValueError as error:
        print("headroom init: refusing to change {}: {}".format(path, error), file=sys.stderr)
        return 1

    rendered = json.dumps(merged, indent=2) + "\n"
    changed = merged != existing
    if dry_run:
        if changed:
            diff = difflib.unified_diff(
                original_text.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
            )
            sys.stdout.writelines(diff)
        else:
            print("headroom init: no changes")
        return 0

    if not changed:
        print("headroom init: {} is already configured".format(path))
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = _backup_path(path)
        shutil.copyfile(str(path), str(backup))
        print("Backup: {}".format(backup))
    _atomic_write(path, rendered)
    print("Updated: {}".format(path))
    return 0


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
