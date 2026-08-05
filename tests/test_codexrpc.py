"""Tests for on-demand Codex app-server rate-limit refresh."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from headroom import codexrpc
from headroom.bounds import Snapshot
from headroom.codexrpc import CodexRpcResult
from headroom.codexsrc import CodexResult
from headroom import cli, codexsrc
from headroom.cli import main
from headroom.state import read_state, save_snapshots


STUB = Path(__file__).with_name("codex_app_server_stub.py")


class CodexRpcTests(unittest.TestCase):
    def test_statusline_never_starts_app_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = self._environment(root, "success")
            environment["HEADROOM_TEST_RPC_STARTED"] = str(root / "started")
            output = StringIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch("sys.stdin", StringIO("{}")):
                    with redirect_stdout(output):
                        result = main(["statusline"])

            self.assertEqual(result, 0)
            self.assertTrue(output.getvalue().strip())
            self.assertFalse((root / "started").exists())

    def test_success_uses_codex_bucket_and_window_duration_mins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = self._environment(root, "success")
            environment["HEADROOM_TEST_RPC_LOG"] = str(root / "requests.jsonl")

            document = self._run_json(environment)

            snapshot = document["sources"]["codex"]["weekly"]
            self.assertEqual(snapshot["used_percentage"], 4.0)
            self.assertEqual(snapshot["raw"]["windowDurationMins"], 10_080)
            self.assertEqual(document["diagnostics"]["codex"]["source"], "app-server")
            requests = [
                json.loads(line)
                for line in (root / "requests.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(len(requests), 3)
            self.assertEqual(requests[0]["params"]["clientInfo"]["name"], "headroom")
            self.assertEqual(requests[1]["method"], "initialized")
            self.assertIsNone(requests[2]["params"])

    def test_doctor_surfaces_app_server_as_winning_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory), "success")
            output = StringIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                with redirect_stdout(output):
                    result = main(["doctor"])

            self.assertEqual(result, 0)
            self.assertIn("Codex source: app-server", output.getvalue())
            self.assertIn("Codex sessions: not checked", output.getvalue())

    def test_timeout_falls_back_and_kills_the_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_rollout(root, used_percent=41)
            environment = self._environment(root, "timeout")
            # A 1s timeout used to flake under full-suite load: process spawn
            # plus interpreter startup for the stub child can itself exceed
            # 1s once the machine is busy (measured up to ~2.8s under heavy
            # synthetic CPU contention on a 24-core box), so the parent could
            # kill the child before it was ever scheduled to run its first
            # line of Python, i.e. before "started" could be written. This
            # cannot be made fully race-proof without a product-code hook for
            # "child has been scheduled" (there is no such signal available
            # today, and this is a test-synchronisation issue, not a product
            # bug), so instead we use a budget close to the product's own
            # DEFAULT_TIMEOUT_SECONDS (6s) in codexrpc.py -- comfortably more
            # than double the worst spawn jitter we measured -- to make the
            # residual race negligible in practice. Deliberately not exactly
            # 6s: that would make this test pass even if the
            # HEADROOM_CODEX_RPC_TIMEOUT override were silently ignored and
            # the default always won, see test_timeout_seconds_honors_a_
            # non_default_override for the direct, timing-independent check
            # of that parsing path.
            environment["HEADROOM_CODEX_RPC_TIMEOUT"] = "7"
            environment["HEADROOM_TEST_RPC_STARTED"] = str(root / "started")
            environment["HEADROOM_TEST_RPC_SURVIVED"] = str(root / "survived")
            environment["HEADROOM_TEST_RPC_HEARTBEAT"] = str(root / "heartbeat")

            document = self._run_json(environment)

            self.assertEqual(
                document["sources"]["codex"]["weekly"]["used_percentage"],
                41.0,
            )
            diagnostics = document["diagnostics"]["codex"]
            self.assertEqual(diagnostics["source"], "rollout")
            self.assertTrue(any("timed out" in note for note in diagnostics["notes"]))
            # Poll instead of a single is_file() check: the write already
            # landed by the time _run_json() returns (the child is reaped
            # before main() gives control back), but polling with a
            # generous deadline still proves the child genuinely started
            # and gives a clear failure if it somehow never did, rather
            # than depending on exact same-tick filesystem visibility.
            self._wait_for_marker(root / "started")
            # Prove the kill actually happened without waiting out the
            # stub's entire (long, deliberately unresponsive) lifetime: the
            # stub heartbeats every 0.1s while stalled, so if it is still
            # alive after _run_json() returned (i.e. after the RPC timeout
            # should have killed it), a fresh heartbeat will appear within
            # a short grace window. A fixed sleep sized to the stub's full
            # stall was tried first and was correct but made this one test
            # dominate the suite's runtime; comparing heartbeats gives the
            # same guarantee (confirmed by mocking _stop_process as a
            # no-op and seeing this correctly fail) much faster.
            heartbeat_path = root / "heartbeat"
            self._wait_for_marker(heartbeat_path)
            before = heartbeat_path.read_text(encoding="utf-8")
            time.sleep(0.5)
            after = (
                heartbeat_path.read_text(encoding="utf-8")
                if heartbeat_path.exists()
                else before
            )
            self.assertEqual(
                before,
                after,
                "child kept heartbeating after its RPC timeout should have killed it",
            )
            self.assertFalse((root / "survived").exists())

    def test_timeout_seconds_honors_a_non_default_override(self) -> None:
        # A subprocess-timing integration test alone can't prove
        # HEADROOM_CODEX_RPC_TIMEOUT is actually read: any value close
        # enough to DEFAULT_TIMEOUT_SECONDS to keep that test fast is
        # indistinguishable, by wall-clock behavior, from the override
        # being silently ignored. Exercise the parsing directly instead.
        notes: list = []
        self.assertEqual(
            codexrpc._timeout_seconds({"HEADROOM_CODEX_RPC_TIMEOUT": "7"}, notes),
            7.0,
        )
        self.assertEqual(notes, [])
        self.assertNotEqual(7.0, codexrpc.DEFAULT_TIMEOUT_SECONDS)

    def test_garbage_output_falls_back_to_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_rollout(root, used_percent=42)
            document = self._run_json(self._environment(root, "garbage"))

            self.assertEqual(
                document["sources"]["codex"]["weekly"]["used_percentage"],
                42.0,
            )
            diagnostics = document["diagnostics"]["codex"]
            self.assertEqual(diagnostics["source"], "rollout")
            self.assertTrue(
                any("non-JSON" in note for note in diagnostics["rpc_notes"])
            )

    def test_rpc_zero_skips_process_and_uses_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_rollout(root, used_percent=43)
            environment = self._environment(root, "success")
            environment["HEADROOM_CODEX_RPC"] = "0"
            environment["HEADROOM_TEST_RPC_STARTED"] = str(root / "started")

            document = self._run_json(environment)

            self.assertEqual(
                document["sources"]["codex"]["weekly"]["used_percentage"],
                43.0,
            )
            diagnostics = document["diagnostics"]["codex"]
            self.assertEqual(diagnostics["source"], "rollout")
            self.assertFalse(diagnostics["rpc_attempted"])
            self.assertFalse((root / "started").exists())

    def test_hook_uses_total_deadline_bounded_scan_and_stored_state_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            environment = {
                "CODEX_HOME": str(root / "codex-hooks"),
                "HEADROOM_CODEX_HOME": str(root / "codex-home"),
                "HEADROOM_STATE_DIR": str(root / "state"),
            }
            stored = Snapshot(
                used_percentage=94.0,
                captured_at=now,
                resets_at=now + 7_200,
                window="weekly",
                source="codex",
            )
            rpc_result = CodexRpcResult((), True, ("hook deadline reached",))
            rollout_result = CodexResult((), None, 0, ("rollout scan deadline reached",))
            output = StringIO()
            started = time.monotonic()

            with mock.patch.dict(os.environ, environment, clear=True):
                save_snapshots((stored,))
                with mock.patch("headroom.cli.read_rate_limits", return_value=rpc_result) as rpc:
                    with mock.patch("headroom.cli.read_latest", return_value=rollout_result) as rollout:
                        with redirect_stdout(output):
                            result = main(["hook", "--plain"])

            self.assertEqual(result, 0)
            self.assertIn("94% used", output.getvalue())
            deadline = rpc.call_args.kwargs["deadline"]
            self.assertIsNotNone(deadline)
            self.assertLessEqual(deadline, started + cli.HOOK_DEADLINE_SECONDS + 0.25)
            self.assertEqual(rollout.call_args.kwargs["deadline"], deadline)
            self.assertEqual(
                rollout.call_args.kwargs["max_files"],
                cli.HOOK_MAX_ROLLOUT_FILES,
            )

    def test_deadline_mid_rollout_discards_partial_snapshot_and_preserves_newer_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            rollout_dir = root / "codex-home" / "sessions" / "2026" / "08" / "04"
            rollout_dir.mkdir(parents=True)
            now = time.time()
            records = []
            for captured_at, used_percent in ((now - 120, 12), (now + 1, 96)):
                records.append(
                    {
                        "timestamp": datetime.fromtimestamp(
                            captured_at, timezone.utc
                        ).isoformat().replace("+00:00", "Z"),
                        "payload": {
                            "rate_limits": {
                                "limit_id": "codex",
                                "primary": {
                                    "used_percent": used_percent,
                                    "window_minutes": 10_080,
                                    "resets_at": now + 86_400,
                                },
                                "secondary": None,
                            }
                        },
                    }
                )
            (rollout_dir / "rollout-deadline.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            stored = Snapshot(
                used_percentage=94.0,
                captured_at=now,
                resets_at=now + 7_200,
                window="weekly",
                source="codex",
            )
            environment = {
                "CODEX_HOME": str(root / "codex-hooks"),
                "HEADROOM_CODEX_HOME": str(root / "codex-home"),
                "HEADROOM_STATE_DIR": str(state_dir),
            }
            rollout_results = []

            def read_real_rollout(**kwargs):
                result = codexsrc.read_latest(**kwargs)
                rollout_results.append(result)
                return result

            # This is a synthetic fixture (the 94% figure below is fake),
            # so its hook text must never reach the real stdout: unlike
            # every sibling test here, this call was missing its
            # redirect_stdout wrapper, letting the fake reading print to
            # the terminal and look exactly like genuine usage output.
            output = StringIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                save_snapshots((stored,))
                with mock.patch(
                    "headroom.cli.read_rate_limits",
                    return_value=CodexRpcResult((), True, ("RPC unavailable",)),
                ):
                    with mock.patch(
                        "headroom.cli.read_latest", side_effect=read_real_rollout
                    ):
                        with mock.patch(
                            "headroom.codexsrc.time.monotonic",
                            side_effect=[0.0] * 6 + [float("inf")],
                        ):
                            with redirect_stdout(output):
                                result = main(["hook", "--plain"])

            self.assertEqual(result, 0)
            self.assertIn("94% used", output.getvalue())
            self.assertEqual(len(rollout_results), 1)
            self.assertEqual(rollout_results[0].snapshots, ())
            self.assertIn("deadline reached while reading", " ".join(rollout_results[0].notes))
            persisted = read_state(state_dir)["sources"]["codex"]["weekly"]
            self.assertEqual(persisted["used_percentage"], 94.0)
            self.assertEqual(persisted["captured_at"], now)

    def _environment(self, root: Path, mode: str) -> dict[str, str]:
        return {
            "CODEX_HOME": str(root / "codex-hooks"),
            "HEADROOM_CODEX_HOME": str(root / "codex-home"),
            "HEADROOM_CODEX_RPC_CMD": json.dumps([sys.executable, str(STUB)]),
            "HEADROOM_STATE_DIR": str(root / "state"),
            "HEADROOM_TEST_RPC_MODE": mode,
        }

    def _wait_for_marker(self, path: Path, timeout: float = 10.0) -> None:
        """Poll for a marker file with a generous deadline instead of a
        single point-in-time check, so a slow-scheduled child under load
        still gets a fair chance to be observed before we fail the test."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file():
                return
            time.sleep(0.02)
        self.fail("{} was never created within {}s".format(path, timeout))

    def _run_json(self, environment: dict[str, str]) -> dict:
        output = StringIO()
        with mock.patch.dict(os.environ, environment, clear=True):
            with redirect_stdout(output):
                result = main(["json"])
        self.assertEqual(result, 0)
        return json.loads(output.getvalue())

    def _write_rollout(self, root: Path, used_percent: int) -> None:
        rollout_dir = root / "codex-home" / "sessions" / "2026" / "08" / "04"
        rollout_dir.mkdir(parents=True)
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = {
            "timestamp": timestamp,
            "payload": {
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": used_percent,
                        "window_minutes": 10_080,
                        "resets_at": time.time() + 86_400,
                    },
                    "secondary": None,
                }
            },
        }
        (rollout_dir / "rollout-test.jsonl").write_text(
            json.dumps(payload) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
