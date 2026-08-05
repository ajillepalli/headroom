"""Tests for isolated and atomic state persistence."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from headroom.state import read_state, write_state


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


if __name__ == "__main__":
    unittest.main()
