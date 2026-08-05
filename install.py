#!/usr/bin/env python3
"""Print the Claude Code settings needed by headroom."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


def shell_command(command_parts: list[str]) -> str:
    """Return a command line quoted for the current platform's shell."""
    return (
        subprocess.list2cmdline(command_parts)
        if os.name == "nt"
        else shlex.join(command_parts)
    )


def statusline_command() -> str:
    """Return a statusline command that can run outside the repository."""
    repository = str(Path(__file__).resolve().parent)
    command = shell_command(
        [sys.executable, "-m", "headroom.cli", "statusline"]
    )
    if os.name == "nt":
        return 'set "PYTHONPATH={}" && {}'.format(repository, command)
    return "PYTHONPATH={} {}".format(shlex.quote(repository), command)


def settings_snippet() -> dict[str, object]:
    """Return the settings fragment without changing the user's configuration."""
    hook = Path(__file__).resolve().parent / "hooks" / "headroom-hook.py"
    hook_command = shell_command([sys.executable, str(hook)])
    return {
        "statusLine": {
            "type": "command",
            "command": statusline_command(),
            "refreshInterval": 300,
        },
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command,
                        }
                    ]
                }
            ]
        },
    }


def main() -> int:
    """Print a settings.json fragment for manual installation."""
    print(json.dumps(settings_snippet(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
