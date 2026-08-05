#!/usr/bin/env python3
"""Run the headroom context hook without disrupting prompt submission."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Print actionable headroom context, if any, and always allow the prompt."""
    try:
        project_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "-m", "headroom.cli", "hook"],
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0

    if result.returncode == 0 and result.stdout:
        sys.stdout.write(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
