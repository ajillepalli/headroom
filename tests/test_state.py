"""Tests for isolated and atomic state persistence."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from headroom.bounds import Snapshot
from headroom.cli import main
from headroom.state import (
    context_captures_from_state,
    read_state,
    save_snapshots,
    write_state,
)


class StateTests(unittest.TestCase):
    def test_interrupted_replace_leaves_previous_state_complete(self) -> None:
        original = {"version": 1, "sources": {"claude": {"marker": "original"}}}
        replacement = {"version": 1, "sources": {"claude": {"marker": "new"}}}

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            write_state(original, state_dir)

            with mock.patch("headroom.state.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    write_state(replacement, state_dir)

            self.assertEqual(read_state(state_dir), original)
            with (state_dir / "state.json").open("r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), original)
            self.assertEqual(list(state_dir.glob(".state-*.tmp")), [])

    def test_reset_command_clears_state_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            save_snapshots(
                (
                    Snapshot(
                        used_percentage=12.0,
                        captured_at=1_800_000_000.0,
                        resets_at=None,
                        window="short",
                        source="codex",
                    ),
                ),
                state_dir,
                diagnostics={"codex": {"notes": ["test note"]}},
            )
            unrelated = state_dir / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")
            output = StringIO()

            with mock.patch.dict(
                os.environ, {"HEADROOM_STATE_DIR": directory}, clear=True
            ):
                with redirect_stdout(output):
                    result = main(["reset"])

            self.assertEqual(result, 0)
            self.assertIn("Cleared stored state", output.getvalue())
            self.assertFalse((state_dir / "state.json").exists())
            self.assertFalse((state_dir / "history.jsonl").exists())
            self.assertTrue(unrelated.is_file())

    def test_older_snapshot_never_replaces_newer_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            newer = Snapshot(
                used_percentage=91.0,
                captured_at=200.0,
                resets_at=None,
                window="weekly",
                source="codex",
            )
            older = Snapshot(
                used_percentage=9.0,
                captured_at=100.0,
                resets_at=None,
                window="weekly",
                source="codex",
            )

            save_snapshots((newer,), state_dir)
            state = save_snapshots((older,), state_dir)

            persisted = state["sources"]["codex"]["weekly"]
            self.assertEqual(persisted["used_percentage"], 91.0)
            self.assertEqual(persisted["captured_at"], 200.0)

    def test_context_capture_is_stored_keyed_by_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            capture = {
                "used_percentage": 42.0,
                "size": 200_000,
                "session_id": "session-a",
                "captured_at": 1_000.0,
                "source": "claude",
            }

            state = save_snapshots((), state_dir, context_capture=capture)

            self.assertEqual(
                state["sources"]["claude"]["context"]["session-a"]["used_percentage"], 42.0
            )
            self.assertEqual(context_captures_from_state(state)["session-a"], capture)

    def test_two_sessions_do_not_overwrite_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            session_a = {
                "used_percentage": 92.0,
                "size": None,
                "session_id": "session-a",
                "captured_at": 1_000.0,
                "source": "claude",
            }
            session_b = {
                "used_percentage": 8.0,
                "size": None,
                "session_id": "session-b",
                "captured_at": 1_001.0,
                "source": "claude",
            }

            save_snapshots((), state_dir, context_capture=session_a)
            state = save_snapshots((), state_dir, context_capture=session_b)

            captures = context_captures_from_state(state)
            # This is the exact bug the ENG review phase caught: a flat,
            # non-session-keyed slot would let session B's low reading
            # answer for session A's high one. Both must still be present.
            self.assertEqual(captures["session-a"]["used_percentage"], 92.0)
            self.assertEqual(captures["session-b"]["used_percentage"], 8.0)

    def test_stale_context_entries_are_pruned_on_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            old = {
                "used_percentage": 50.0,
                "size": None,
                "session_id": "old-session",
                "captured_at": 0.0,
                "source": "claude",
            }
            save_snapshots((), state_dir, context_capture=old)

            # A new capture 400s later, past the 300s default context
            # freshness window, triggers the sweep.
            new = {
                "used_percentage": 10.0,
                "size": None,
                "session_id": "new-session",
                "captured_at": 400.0,
                "source": "claude",
            }
            state = save_snapshots((), state_dir, context_capture=new)

            captures = context_captures_from_state(state)
            self.assertNotIn("old-session", captures)
            self.assertIn("new-session", captures)

    def test_context_capture_write_is_folded_into_one_transaction(self) -> None:
        # Regression guard for the ENG review's "two round trips are not
        # atomic with each other" finding: a rate-limit snapshot and a
        # context capture given to the SAME save_snapshots call must not
        # require write_state to be called more than once.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            snapshot = Snapshot(
                used_percentage=12.0,
                captured_at=1_000.0,
                resets_at=None,
                window="short",
                source="codex",
            )
            capture = {
                "used_percentage": 30.0,
                "size": None,
                "session_id": "session-a",
                "captured_at": 1_000.0,
                "source": "claude",
            }

            with mock.patch("headroom.state.write_state") as write_mock:
                save_snapshots((snapshot,), state_dir, context_capture=capture)

            self.assertEqual(write_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
