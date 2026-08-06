"""Tests for isolated and atomic state persistence."""

from __future__ import annotations

from contextlib import redirect_stdout
import errno
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from headroom.bounds import Snapshot
from headroom.cli import main
from headroom.state import (
    _MAX_TRACKED_CONTEXT_SESSIONS,
    _acquire_lock,
    _context_entry_is_fresh,
    _LOCK_TIMEOUT_SECONDS,
    _should_replace_context_capture,
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

    def test_write_state_retries_a_transient_windows_access_denied_failure(self) -> None:
        # Reproduced directly under real, heavy concurrent multi-process
        # load on Windows: os.replace (MoveFileEx) can fail with
        # PermissionError for a few milliseconds if something external
        # (antivirus real-time scanning, the Windows Search Indexer) has
        # the target file open at the exact instant of the rename. This
        # is not a headroom locking bug -- the lock already guarantees
        # only one headroom process is ever inside write_state at a time
        # -- so retrying a BOUNDED number of times, rather than failing
        # the whole write outright, is the correct response to a purely
        # external, momentary conflict.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            new_state = {"version": 1, "sources": {"claude": {"marker": "new"}}}
            real_replace = os.replace
            calls = []

            def flaky_replace(source, target):
                calls.append(1)
                if len(calls) < 3:
                    raise PermissionError(5, "Access is denied")
                return real_replace(source, target)

            with mock.patch("headroom.state.os.replace", side_effect=flaky_replace):
                with mock.patch("headroom.state.time.sleep"):
                    write_state(new_state, state_dir)

            self.assertEqual(len(calls), 3)
            self.assertEqual(read_state(state_dir), new_state)

    def test_write_state_gives_up_after_bounded_retries_on_persistent_denial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch(
                "headroom.state.os.replace",
                side_effect=PermissionError(5, "Access is denied"),
            ) as replace_mock:
                with mock.patch("headroom.state.time.sleep"):
                    with self.assertRaises(PermissionError):
                        write_state({"version": 1, "sources": {}}, state_dir)

            from headroom.state import _REPLACE_RETRY_ATTEMPTS

            self.assertEqual(replace_mock.call_count, _REPLACE_RETRY_ATTEMPTS)
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

    def test_rejected_out_of_order_capture_does_not_prune_other_sessions(self) -> None:
        # Codex review (round 1, P2) of the finding #2 fix above: a REJECTED
        # capture is still a real, valid captured_at -- just an older one.
        # The old version of _merge_context_capture used it as "now" for
        # the pruning sweep regardless of whether it was accepted, which
        # made every genuinely newer entry (for ANY session, not just the
        # rejected one's own) look implausibly future-dated and erased it.
        # session-a is captured well ahead of the rejected, out-of-order
        # write for session-b; it must survive.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            session_a = {
                "used_percentage": 40.0,
                "size": None,
                "session_id": "session-a",
                "captured_at": 1_000.0,
                "source": "claude",
            }
            session_b_newer = {
                "used_percentage": 10.0,
                "size": None,
                "session_id": "session-b",
                "captured_at": 1_000.0,
                "source": "claude",
            }
            session_b_stale_out_of_order = {
                # 30s older than both entries above -- realistically
                # delayed (well past CLOCK_SKEW_ALLOWANCE_SECONDS's 5s),
                # so it is correctly rejected for session-b by the
                # ordinary "incoming >= current" comparison, but still
                # comfortably inside _CROSS_SESSION_REORDERING_TOLERANCE_
                # SECONDS so it does not also trip the SEPARATE "current
                # is implausibly future-dated" self-heal check that
                # protects a session from a genuinely corrupt stored
                # entry (see test_session_recovers_on_its_own_from_a_
                # stuck_future_dated_entry) -- this test is specifically
                # about an ordinary late write, not a corrupt one.
                "used_percentage": 5.0,
                "size": None,
                "session_id": "session-b",
                "captured_at": 970.0,
                "source": "claude",
            }

            save_snapshots((), state_dir, context_capture=session_a)
            save_snapshots((), state_dir, context_capture=session_b_newer)
            state = save_snapshots((), state_dir, context_capture=session_b_stale_out_of_order)

            captures = context_captures_from_state(state)
            self.assertIn("session-a", captures)
            self.assertEqual(captures["session-a"]["used_percentage"], 40.0)
            self.assertEqual(captures["session-b"]["used_percentage"], 10.0)

    def test_non_finite_stored_captured_at_does_not_block_a_valid_replacement(self) -> None:
        # Codex review (round 1, P2): float("nan")/float("inf") decode
        # successfully as Python floats (json.loads accepts those
        # non-standard tokens), so a corrupt PRE-EXISTING stored
        # captured_at of NaN used to be treated as a "valid, decodable"
        # timestamp. Every comparison against NaN is false, so a
        # legitimate new capture for the same session could never satisfy
        # "incoming >= current" and was rejected forever, leaving the
        # corrupt entry stuck in place with no way to ever replace it.
        #
        # The corrupt entry is written directly into state.json here,
        # bypassing save_snapshots: routing it through save_snapshots
        # itself would immediately prune it right back out again in that
        # SAME call (its own captured_at, NaN, is what that call's pruning
        # sweep uses as "now", and resolve_age already rejects a
        # non-finite "now" independently of this fix) -- so a directly
        # written hostile state.json (a hand edit, or a file from a
        # pre-fix version of headroom) is what actually exercises the
        # "already sitting on disk" scenario this fix targets.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            hostile_state = {
                "version": 1,
                "sources": {
                    "claude": {
                        "context": {
                            "session-a": {
                                "used_percentage": 92.0,
                                "size": None,
                                "session_id": "session-a",
                                "captured_at": float("nan"),
                                "source": "claude",
                            }
                        }
                    }
                },
            }
            (state_dir / "state.json").write_text(
                json.dumps(hostile_state, allow_nan=True), encoding="utf-8"
            )

            valid = {
                "used_percentage": 10.0,
                "size": None,
                "session_id": "session-a",
                "captured_at": 1_000.0,
                "source": "claude",
            }
            state = save_snapshots((), state_dir, context_capture=valid)

            captures = context_captures_from_state(state)
            self.assertIn("session-a", captures)
            self.assertEqual(captures["session-a"]["used_percentage"], 10.0)

    def test_acquire_lock_gives_up_immediately_on_a_permanent_failure(self) -> None:
        # Codex review (round 1, P2): a broad `except OSError` retried
        # EVERY failure for the full _LOCK_TIMEOUT_SECONDS window,
        # including a PERMANENT one (locking unsupported on this
        # filesystem, a bad descriptor) that retrying can never fix --
        # adding a needless multi-second stall to every write on such a
        # host. Only a CONTENDED attempt (another holder in the way,
        # EACCES/EAGAIN/EWOULDBLOCK) should retry; anything else must give
        # up on the very first attempt.
        import headroom.state as state_module

        permanent_error = OSError()
        permanent_error.errno = errno.ENOTSUP

        with tempfile.TemporaryDirectory() as directory:
            handle = open(str(Path(directory) / "lock-test"), "a+b")
            try:
                if state_module.fcntl is not None:
                    patcher = mock.patch.object(
                        state_module.fcntl, "flock", side_effect=permanent_error
                    )
                else:
                    patcher = mock.patch.object(
                        state_module.msvcrt, "locking", side_effect=permanent_error
                    )
                with patcher:
                    started = time.monotonic()
                    result = _acquire_lock(handle)
                    elapsed = time.monotonic() - started
            finally:
                handle.close()

        self.assertFalse(result)
        self.assertLess(elapsed, _LOCK_TIMEOUT_SECONDS / 2)

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

    def test_reordered_cross_session_writes_do_not_prune_each_other(self) -> None:
        # Found during verification of the Codex round-1 fixes above, by
        # stress-testing the real cross-platform lock with genuine
        # concurrent OS processes rather than only in-process threads: two
        # DIFFERENT sessions' writes can be reordered in EXECUTION relative
        # to when they were originally captured (lock contention or
        # process scheduling can delay one session's write behind
        # another's faster one). session-30 captures first (captured_at
        # 30.0) and its write commits first; session-5 captures an
        # EARLIER wall-clock moment (captured_at 5.0) but its write is
        # delayed and processes SECOND. Reusing resolve_age's tight,
        # decode-time clock-skew allowance for this multi-session pruning
        # sweep let session-5's older timestamp treat session-30's
        # already-stored, perfectly valid entry as "implausibly future"
        # and prune it -- reproduced directly with 30 real concurrent
        # `python` subprocesses before this fix, intermittently losing
        # entries with no error of any kind.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            save_snapshots(
                (),
                state_dir,
                context_capture={
                    "used_percentage": 1.0,
                    "size": None,
                    "session_id": "session-30",
                    "captured_at": 30.0,
                    "source": "claude",
                },
            )
            state = save_snapshots(
                (),
                state_dir,
                context_capture={
                    "used_percentage": 2.0,
                    "size": None,
                    "session_id": "session-5",
                    "captured_at": 5.0,
                    "source": "claude",
                },
            )

            captures = context_captures_from_state(state)
            self.assertIn("session-30", captures)
            self.assertIn("session-5", captures)

    def test_pruning_tolerance_is_not_coupled_to_a_small_configured_freshness_window(
        self,
    ) -> None:
        # Codex review (round 2, P2): an earlier version of the reordering
        # fix above reused fresh_for_seconds (HEADROOM_FRESH_CONTEXT_SECONDS,
        # user-configurable) as the pruning sweep's future-tolerance. That
        # reintroduces the exact same reordering bug the moment someone
        # configures a freshness window shorter than realistic scheduling
        # delay -- "how long should a reading stay visible" and "how much
        # concurrent-writer timing slop is plausible" are unrelated
        # questions and must not share one knob.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch.dict(os.environ, {"HEADROOM_FRESH_CONTEXT_SECONDS": "10"}):
                save_snapshots(
                    (),
                    state_dir,
                    context_capture={
                        "used_percentage": 1.0,
                        "size": None,
                        "session_id": "session-30",
                        "captured_at": 30.0,
                        "source": "claude",
                    },
                )
                state = save_snapshots(
                    (),
                    state_dir,
                    context_capture={
                        "used_percentage": 2.0,
                        "size": None,
                        "session_id": "session-5",
                        "captured_at": 5.0,
                        "source": "claude",
                    },
                )

            captures = context_captures_from_state(state)
            self.assertIn("session-30", captures)
            self.assertIn("session-5", captures)

    def test_a_moderate_clock_skew_does_not_survive_for_up_to_an_hour(self) -> None:
        # Codex review (round 3, P2): an earlier version of the reordering
        # tolerance above was set to 3600s (one hour) to comfortably clear
        # "genuine corruption" scenarios, reasoning only about the upper
        # bound. That choice had a real cost: a stored entry that is
        # merely moderately future-dated (minutes, e.g. a real ~30-minute
        # clock rollback, not an attack) would then survive BOTH the
        # pruning sweep and the same-session self-heal check for up to an
        # hour, even though it could never actually be DISPLAYED (
        # ContextReading.from_dict's own much tighter decode-time check
        # already refuses that) -- so the hour-long tolerance bought no
        # soundness benefit, only an hour of an otherwise-healthy session
        # going dark. This asserts BOTH surfaces recognize a 30-minute
        # future entry as implausible promptly, not eventually.
        thirty_minutes = 30 * 60.0
        future_entry = {
            "used_percentage": 92.0,
            "size": None,
            "session_id": "session-a",
            "captured_at": 1_000.0 + thirty_minutes,
            "source": "claude",
        }

        # Pruning: a DIFFERENT session's ordinary write must not retain
        # the 30-minute-future entry as if it were merely a faster
        # session that captured first.
        self.assertFalse(_context_entry_is_fresh(future_entry, 1_000.0, 300.0))

        # Same-session self-heal: a legitimate new capture for session-a
        # itself must not be blocked by its own 30-minute-future entry.
        self.assertTrue(
            _should_replace_context_capture(
                future_entry,
                {
                    "used_percentage": 10.0,
                    "session_id": "session-a",
                    "captured_at": 1_000.0,
                },
            )
        )

    def test_session_recovers_on_its_own_from_a_stuck_future_dated_entry(self) -> None:
        # Codex review (round 2, P2): a stored entry that is FINITE but
        # wildly future-dated (not NaN/inf, so the round-1 fix alone does
        # not catch it -- a hand-edited state.json, or corrupt data from
        # some other source) made every subsequent legitimate capture for
        # the SAME session compare as "older" and get rejected forever.
        # Since a rejected capture also skips the pruning sweep (see
        # _merge_context_capture), that session could never recover on its
        # own -- only a DIFFERENT session's write happening to run
        # afterward could ever clean it up. This writes several ordinary,
        # real-looking captures for session-a ALONE (no other session
        # ever writes) and confirms the LATEST one wins.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            hostile_state = {
                "version": 1,
                "sources": {
                    "claude": {
                        "context": {
                            "session-a": {
                                "used_percentage": 92.0,
                                "size": None,
                                "session_id": "session-a",
                                "captured_at": 10_000_000_000.0,
                                "source": "claude",
                            }
                        }
                    }
                },
            }
            (state_dir / "state.json").write_text(
                json.dumps(hostile_state), encoding="utf-8"
            )

            state = None
            for captured_at in (1_000.0, 1_001.0, 1_002.0):
                state = save_snapshots(
                    (),
                    state_dir,
                    context_capture={
                        "used_percentage": 10.0,
                        "size": None,
                        "session_id": "session-a",
                        "captured_at": captured_at,
                        "source": "claude",
                    },
                )

            captures = context_captures_from_state(state)
            self.assertEqual(captures["session-a"]["captured_at"], 1_002.0)
            self.assertEqual(captures["session-a"]["used_percentage"], 10.0)

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
