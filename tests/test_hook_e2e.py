"""End-to-end coverage for the Claude Code prompt hook."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Optional


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPOSITORY_ROOT / "hooks" / "headroom-hook.py"


class HookEndToEndTests(unittest.TestCase):
    def test_low_usage_prints_nothing_and_exits_zero(self) -> None:
        result = self._run_hook(used_percent=3.0, resets_at=time.time() + 7_200)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_low_headroom_prints_source_bound_and_reset_time(self) -> None:
        result = self._run_hook(used_percent=94.0, resets_at=time.time() + 7_200)

        self.assertEqual(result.returncode, 0)
        self.assertIn("Codex", result.stdout)
        self.assertIn(">=94% used", result.stdout)
        self.assertIn("resets in", result.stdout)

    def test_post_reset_usage_prints_nothing(self) -> None:
        result = self._run_hook(used_percent=94.0, resets_at=time.time() - 60)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_malformed_rollout_prints_nothing_and_exits_zero(self) -> None:
        result = self._run_hook(malformed=True)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def _run_hook(
        self,
        used_percent: Optional[float] = None,
        resets_at: Optional[float] = None,
        malformed: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as codex_home:
            with tempfile.TemporaryDirectory() as state_dir:
                rollout_dir = (
                    Path(codex_home) / "sessions" / "2026" / "08" / "04"
                )
                rollout_dir.mkdir(parents=True)
                rollout = rollout_dir / "rollout-2026-08-04T00-00-00-test.jsonl"
                if malformed:
                    rollout.write_text("{not json\n", encoding="utf-8")
                else:
                    if used_percent is None or resets_at is None:
                        raise ValueError("valid rollouts require usage and reset values")
                    now = time.time()
                    captured_at = datetime.fromtimestamp(
                        now - 3_600, timezone.utc
                    ).isoformat().replace("+00:00", "Z")
                    payload = {
                        "timestamp": captured_at,
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": None,
                            "rate_limits": {
                                "limit_id": "codex",
                                "limit_name": None,
                                "primary": {
                                    "used_percent": used_percent,
                                    "window_minutes": 10_080,
                                    "resets_at": int(resets_at),
                                },
                                "secondary": None,
                                "credits": {
                                    "has_credits": False,
                                    "unlimited": False,
                                    "balance": "0",
                                },
                                "individual_limit": None,
                                "spend_control_reached": None,
                                "plan_type": "pro",
                                "rate_limit_reached_type": None,
                            },
                        },
                    }
                    rollout.write_text(json.dumps(payload) + "\n", encoding="utf-8")

                environment = os.environ.copy()
                environment["HEADROOM_CODEX_HOME"] = codex_home
                environment["HEADROOM_CODEX_RPC"] = "0"
                environment["HEADROOM_STATE_DIR"] = state_dir
                environment["HEADROOM_FRESH_CODEX_SECONDS"] = "1800"
                return subprocess.run(
                    [sys.executable, str(HOOK)],
                    input="",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=str(REPOSITORY_ROOT),
                    env=environment,
                    timeout=10,
                    check=False,
                )


if __name__ == "__main__":
    unittest.main()
