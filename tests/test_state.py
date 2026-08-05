"""Tests for isolated and atomic state persistence."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from headroom.bounds import Snapshot
from headroom.cli import main
from headroom.state import (
    _MAX_TRACKED_CONTEXT_SESSIONS,
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

    def test_delayed_older_context_capture_does_not_overwrite_a_newer_one(self) -> None:
        # finding #2 (context-window adversarial review): a delayed capture
        # (captured_at=1000, 92% used) arriving AFTER a newer, already-
        # stored capture for the same session (captured_at=1001, 8% used)
        # must not resurrect the stale reading -- that would manufacture a
        # false critical warning out of a race between two writes.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            newer = {
                "used_percentage": 8.0,
                "size": None,
                "session_id": "session-a",
                "captured_at": 1_001.0,
                "source": "claude",
            }
            older = {
                "used_percentage": 92.0,
                "size": None,
                "session_id": "session-a",
                "captured_at": 1_000.0,
                "source": "claude",
            }

            save_snapshots((), state_dir, context_capture=newer)
            state = save_snapshots((), state_dir, context_capture=older)

            captures = context_captures_from_state(state)
            self.assertEqual(captures["session-a"]["used_percentage"], 8.0)
            self.assertEqual(captures["session-a"]["captured_at"], 1_001.0)

    def test_concurrent_writers_do_not_lose_either_sessions_update(self) -> None:
        """Barrier-forced regression test for finding #1: two save_snapshots
        calls that both read the same on-disk state before either writes
        must not let the second writer silently drop the first writer's
        update.

        A purely sequential test (test_two_sessions_do_not_overwrite_each_
        other below) cannot exercise this: sequential calls never race.
        This monkeypatches read_state to rendezvous both threads on a
        barrier immediately after each has read state but before
        save_snapshots goes on to merge and write it, forcing the exact
        interleaving the lock in state.py exists to prevent. With the lock
        in place, the two calls to save_snapshots never overlap enough for
        both to reach the barrier at once (the second is still waiting to
        acquire the lock while the first is inside its own critical
        section), so the barrier simply times out for whichever thread gets
        there alone, and both are correctly serialized regardless; without
        the lock, both threads DO rendezvous, proving they read the same
        stale state, and the second writer silently overwrites the first.
        """

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            barrier = threading.Barrier(2)
            real_read_state = read_state

            def synced_read_state(passed_state_dir=None):
                result = real_read_state(passed_state_dir)
                try:
                    barrier.wait(timeout=1.0)
                except threading.BrokenBarrierError:
                    pass
                return result

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

            def write_a() -> None:
                save_snapshots((), state_dir, context_capture=session_a)

            def write_b() -> None:
                save_snapshots((), state_dir, context_capture=session_b)

            with mock.patch("headroom.state.read_state", side_effect=synced_read_state):
                thread_a = threading.Thread(target=write_a)
                thread_b = threading.Thread(target=write_b)
                thread_a.start()
                thread_b.start()
                thread_a.join(timeout=10)
                thread_b.join(timeout=10)

            self.assertFalse(thread_a.is_alive())
            self.assertFalse(thread_b.is_alive())
            final = read_state(state_dir)
            captures = context_captures_from_state(final)
            self.assertIn("session-a", captures)
            self.assertIn("session-b", captures)
            self.assertEqual(captures["session-a"]["used_percentage"], 92.0)
            self.assertEqual(captures["session-b"]["used_percentage"], 8.0)

    def test_context_tracking_evicts_the_oldest_session_once_the_cap_is_exceeded(self) -> None:
        # finding #8 (context-window adversarial review): staleness pruning
        # alone does not stop many distinct, all-still-fresh session_ids
        # from growing state.json without bound. Every capture below is
        # timestamped close enough together to stay within the default
        # 300s context freshness window relative to the last one, so only
        # the cap -- not staleness pruning -- can be responsible for
        # keeping the tracked count bounded.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            total = _MAX_TRACKED_CONTEXT_SESSIONS + 5
            state = None
            for index in range(total):
                capture = {
                    "used_percentage": 10.0,
                    "size": None,
                    "session_id": "session-{}".format(index),
                    "captured_at": float(index),
                    "source": "claude",
                }
                state = save_snapshots((), state_dir, context_capture=capture)

            captures = context_captures_from_state(state)
            self.assertLessEqual(len(captures), _MAX_TRACKED_CONTEXT_SESSIONS)
            self.assertNotIn("session-0", captures)
            self.assertIn("session-{}".format(total - 1), captures)

    def test_future_dated_context_entry_is_pruned_not_retained_forever(self) -> None:
        # finding #3 (context-window adversarial review), the pruning half:
        # a stored entry dated far in the future relative to the new
        # capture's own captured_at (a clock rollback, or corrupt data)
        # must not be treated as perpetually fresh and therefore never
        # swept -- the naive (now - captured_at) clamp used before this fix
        # made that subtraction negative, which is <= any non-negative
        # freshness window, so the sweep never removed it.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            future_dated = {
                "used_percentage": 92.0,
                "size": None,
                "session_id": "future-session",
                "captured_at": 1_000_000.0,
                "source": "claude",
            }
            save_snapshots((), state_dir, context_capture=future_dated)

            new = {
                "used_percentage": 10.0,
                "size": None,
                "session_id": "new-session",
                "captured_at": 400.0,
                "source": "claude",
            }
            state = save_snapshots((), state_dir, context_capture=new)

            captures = context_captures_from_state(state)
            self.assertNotIn("future-session", captures)
            self.assertIn("new-session", captures)

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
