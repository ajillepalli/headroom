"""Tests for quotagauge.config's state directory resolution, including the
one-time migration of the pre-rename ~/.headroom directory to ~/.quotagauge.

Every test here fakes Path.home() to point at a throwaway temporary
directory (never the real home directory) so these tests can freely create,
rename, and inspect ".headroom"/".quotagauge" directories without any risk
to a real user's accumulated history. Assertions run INSIDE the
TemporaryDirectory block deliberately: the directory (and everything under
it) is deleted the moment that block exits.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from quotagauge.config import STATE_DIR_ENV, resolve_state_dir


class ResolveStateDirTests(unittest.TestCase):
    def test_explicit_argument_wins_and_is_never_migrated(self) -> None:
        # Branch 1: an explicit caller-supplied directory is used as-is,
        # even when a legacy directory sits right there waiting to migrate.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()
            (legacy / "history.jsonl").write_text("real data\n", encoding="utf-8")

            with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                result = resolve_state_dir(home_path / "explicit")

            self.assertEqual(result, home_path / "explicit")
            self.assertTrue(legacy.is_dir())
            self.assertEqual((legacy / "history.jsonl").read_text(encoding="utf-8"), "real data\n")
            self.assertFalse((home_path / ".quotagauge").exists())

    def test_env_var_wins_and_is_never_migrated(self) -> None:
        # Branch 1 (environment variant): same as above, but through
        # QUOTAGAUGE_STATE_DIR rather than an explicit parameter.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()

            with mock.patch.dict(os.environ, {STATE_DIR_ENV: str(home_path / "configured")}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    result = resolve_state_dir()

            self.assertEqual(result, home_path / "configured")
            self.assertTrue(legacy.is_dir())

    def test_new_directory_already_present_is_used_without_touching_legacy(self) -> None:
        # Branch 2: ~/.quotagauge already exists (a prior migration, or a
        # fresh install that never saw the old name). It wins outright, and
        # a leftover ~/.headroom -- however it got there -- is left alone.
        #
        # This also covers the unlikely case of an unrelated, non-migration
        # directory happening to occupy ~/.quotagauge already (a manual
        # mkdir, a different local tool): this function cannot distinguish
        # that from a genuine prior migration, and deliberately does not
        # try -- it always prefers an existing target over guessing. The
        # real legacy directory is never deleted or written to either way,
        # so nothing here is destructive: at worst, real history stays
        # sitting at ~/.headroom, fully intact and recoverable by hand,
        # simply not picked up automatically.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            target = home_path / ".quotagauge"
            target.mkdir()
            (target / "marker").write_text("new", encoding="utf-8")
            legacy = home_path / ".headroom"
            legacy.mkdir()
            (legacy / "marker").write_text("legacy", encoding="utf-8")

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    result = resolve_state_dir()

            self.assertEqual(result, target)
            self.assertEqual((target / "marker").read_text(encoding="utf-8"), "new")
            self.assertTrue(legacy.is_dir())
            self.assertEqual((legacy / "marker").read_text(encoding="utf-8"), "legacy")

    def test_legacy_directory_is_migrated_when_new_one_is_missing(self) -> None:
        # Branch 3, success path: the only real-world case this migration
        # exists for -- an install that still has ~/.headroom and nothing
        # at ~/.quotagauge yet. The directory itself is renamed (not
        # copied), so history.jsonl arrives intact under the new path and
        # nothing is left behind at the old one.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()
            (legacy / "history.jsonl").write_text("one\ntwo\nthree\n", encoding="utf-8")
            (legacy / "state.json").write_text("{}", encoding="utf-8")

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    result = resolve_state_dir()

            target = home_path / ".quotagauge"
            self.assertEqual(result, target)
            self.assertFalse(legacy.exists())
            self.assertTrue(target.is_dir())
            self.assertEqual((target / "history.jsonl").read_text(encoding="utf-8"), "one\ntwo\nthree\n")
            self.assertEqual((target / "state.json").read_text(encoding="utf-8"), "{}")

    def test_neither_directory_exists_returns_new_default_uncreated(self) -> None:
        # Branch 4: an ordinary fresh install. Nothing to migrate, and
        # resolve_state_dir itself creates nothing -- callers still do that
        # on first write, exactly as before this rename.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    result = resolve_state_dir()

            self.assertEqual(result, home_path / ".quotagauge")
            self.assertFalse(result.exists())
            self.assertFalse((home_path / ".headroom").exists())

    def test_migration_runs_at_most_once_across_repeated_calls(self) -> None:
        # Idempotency: once migrated, later calls -- in the same process or
        # a later one, since this tool has no persistent daemon -- must
        # land on the new directory without ever touching the (now gone)
        # legacy path again.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()
            (legacy / "history.jsonl").write_text("data\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    first = resolve_state_dir()
                    second = resolve_state_dir()

            target = home_path / ".quotagauge"
            self.assertEqual(first, target)
            self.assertEqual(second, target)
            self.assertFalse(legacy.exists())
            self.assertEqual((target / "history.jsonl").read_text(encoding="utf-8"), "data\n")

    def test_failed_rename_falls_back_to_legacy_directory_in_place(self) -> None:
        # Branch 3, failure path: the rename cannot complete (permissions,
        # a stray file already at the target, a cross-device boundary,
        # ...). The legacy directory -- and every reading in it -- must
        # still be reachable afterward, in place, never destroyed and
        # never copy-then-deleted.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()
            (legacy / "history.jsonl").write_text("irreplaceable\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    with mock.patch.object(Path, "rename", side_effect=OSError("permission denied")):
                        result = resolve_state_dir()

            target = home_path / ".quotagauge"
            self.assertEqual(result, legacy)
            self.assertTrue(legacy.is_dir())
            self.assertFalse(target.exists())
            self.assertEqual((legacy / "history.jsonl").read_text(encoding="utf-8"), "irreplaceable\n")

    def test_failed_rename_never_destroys_data_across_repeated_attempts(self) -> None:
        # A still-failing migration must stay safe under retries too: every
        # later call starts from the same on-disk state and falls back the
        # same way, never partially applying the rename.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()
            (legacy / "history.jsonl").write_text("irreplaceable\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    with mock.patch.object(Path, "rename", side_effect=OSError("permission denied")):
                        first = resolve_state_dir()
                        second = resolve_state_dir()

            self.assertEqual(first, legacy)
            self.assertEqual(second, legacy)
            self.assertEqual((legacy / "history.jsonl").read_text(encoding="utf-8"), "irreplaceable\n")

    def test_concurrent_migration_is_detected_and_uses_the_winner(self) -> None:
        # A rename can fail because ANOTHER process's migration already won
        # the exact same race (visible mainly on Windows, where a rename
        # never silently replaces an existing target). That must be told
        # apart from a genuine failure: re-checking the target and using it
        # is the only way, and doing so must never fall back to the legacy
        # directory once a winner already exists. The fake winner below
        # renames the SAME legacy directory this process sees (moving its
        # real content, not just creating an empty target) and removes it,
        # exactly like a real concurrent process's os.rename would -- so
        # this test also proves the winner's actual data, not just an empty
        # directory, is what gets used.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()
            (legacy / "history.jsonl").write_text("winner's data\n", encoding="utf-8")
            target = home_path / ".quotagauge"

            def fake_rename(target_path: Path) -> None:
                # Simulate a concurrent process's own migration completing
                # in the window between this process's existence check and
                # its own rename attempt: it moves the real legacy
                # directory (using the real os.rename, not this mocked
                # one) so the legacy path is gone afterward too, exactly
                # as a genuine winner would leave things.
                os.rename(str(legacy), str(target))
                raise OSError("target exists")

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    with mock.patch.object(Path, "rename", side_effect=fake_rename):
                        result = resolve_state_dir()

            self.assertEqual(result, target)
            self.assertTrue(target.is_dir())
            self.assertFalse(legacy.exists())
            self.assertEqual((target / "history.jsonl").read_text(encoding="utf-8"), "winner's data\n")


if __name__ == "__main__":
    unittest.main()
