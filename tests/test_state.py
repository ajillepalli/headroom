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
from headroom.state import read_state, save_snapshots, write_state


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


if __name__ == "__main__":
    unittest.main()
