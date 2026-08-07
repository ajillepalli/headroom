#!/usr/bin/env python3
"""Run the quotagauge context hook without disrupting prompt submission."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional


PRIMARY_TIMEOUT_SECONDS = 7.5
STORED_FALLBACK_TIMEOUT_SECONDS = 1.0


def main() -> int:
    """Print actionable quotagauge context, if any, and always allow the prompt."""
    payload = None if sys.stdin.isatty() else sys.stdin.read()
    project_root = Path(__file__).resolve().parent.parent
    try:
        result = _run_hook(project_root, payload, [], PRIMARY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            result = _run_hook(
                project_root,
                payload,
                ["--stored-only"],
                STORED_FALLBACK_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return 0
    except (OSError, subprocess.SubprocessError):
        return 0

    if result.returncode == 0 and result.stdout:
        sys.stdout.write(result.stdout)
    return 0


def _run_hook(
    project_root: Path,
    payload: Optional[str],
    extra_arguments: List[str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "quotagauge.cli", "hook"]
        + extra_arguments
        + sys.argv[1:],
        cwd=str(project_root),
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
