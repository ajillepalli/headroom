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

import quotagauge.config as config_module
from quotagauge.config import STATE_DIR_ENV, resolve_state_dir


class ResolveStateDirTests(unittest.TestCase):
    def setUp(self) -> None:
        # resolve_state_dir caches its default-path answer for the life of
        # the process (see config.py's _default_state_dir_cache), so every
        # test needs a clean slate -- otherwise an earlier test's answer,
        # computed against ITS OWN fake home directory, would leak into a
        # later test that expects a fresh resolution against a different
        # one. Reset both before and after so a failing test cannot poison
        # the ones that follow it either.
        config_module._default_state_dir_cache = None
        self.addCleanup(self._reset_cache)

    @staticmethod
    def _reset_cache() -> None:
        config_module._default_state_dir_cache = None

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

    def test_default_resolution_stays_sticky_within_one_process_even_if_disk_changes(self) -> None:
        # A single process must never see its OWN default-directory answer
        # change mid-invocation, even if a concurrent process alters what
        # is on disk in between two of this process's own calls -- that
        # would let one process read from a different directory than it
        # just wrote to, within a single command. The first call here
        # falls back to the legacy directory (its rename fails); a
        # DIFFERENT directory then appears on disk out from under it,
        # simulating a concurrent process completing its own migration in
        # between this process's two calls. A second call in the SAME
        # process must still return the original (legacy) answer, not
        # switch to the newly appeared target -- proving the cache, not
        # just a coincidentally identical path value, is what makes this
        # hold (unlike the neither-directory-exists case, a fallback path
        # and a migrated target are genuinely different Path values, so a
        # second call that recomputed from scratch would visibly disagree
        # with the first).
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    with mock.patch.object(Path, "rename", side_effect=PermissionError("locked")):
                        with mock.patch("quotagauge.config.time.sleep"):
                            first = resolve_state_dir()
                    # Simulate a concurrent process completing its own
                    # migration in between this process's two calls: the
                    # target now exists, unlike when `first` was resolved.
                    (home_path / ".quotagauge").mkdir()
                    second = resolve_state_dir()

            self.assertEqual(first, legacy)
            self.assertEqual(second, first)

    def test_failed_rename_falls_back_to_legacy_directory_in_place(self) -> None:
        # Branch 3, failure path: the rename cannot complete even after
        # exhausting the transient-lock retry (a genuine permission
        # problem, a stray file already at the target, a cross-device
        # boundary, ...). The legacy directory -- and every reading in it
        # -- must still be reachable afterward, in place, never destroyed
        # and never copy-then-deleted. PermissionError specifically (not a
        # generic OSError) exercises the retry path this failure goes
        # through before giving up.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()
            (legacy / "history.jsonl").write_text("irreplaceable\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    with mock.patch.object(Path, "rename", side_effect=PermissionError("permission denied")):
                        with mock.patch("quotagauge.config.time.sleep"):
                            result = resolve_state_dir()

            target = home_path / ".quotagauge"
            self.assertEqual(result, legacy)
            self.assertTrue(legacy.is_dir())
            self.assertFalse(target.exists())
            self.assertEqual((legacy / "history.jsonl").read_text(encoding="utf-8"), "irreplaceable\n")

    def test_transient_permission_error_is_retried_and_succeeds(self) -> None:
        # The realistic mechanism behind two processes racing this
        # migration: a rename can fail for a few milliseconds because
        # something else (antivirus, the Windows Search Indexer) briefly
        # has the directory open, not because it can never succeed.
        # Retrying narrows that window instead of prematurely committing
        # to the legacy-directory fallback and potentially writing there
        # while another process goes on to migrate successfully.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()
            (legacy / "history.jsonl").write_text("data\n", encoding="utf-8")

            real_rename = Path.rename
            attempts = {"count": 0}

            def flaky_rename(self: Path, target_path: Path) -> None:
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise PermissionError("transient lock")
                real_rename(self, target_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    with mock.patch.object(Path, "rename", flaky_rename):
                        with mock.patch("quotagauge.config.time.sleep"):
                            result = resolve_state_dir()

            target = home_path / ".quotagauge"
            self.assertEqual(result, target)
            self.assertEqual(attempts["count"], 3)
            self.assertFalse(legacy.exists())
            self.assertEqual((target / "history.jsonl").read_text(encoding="utf-8"), "data\n")

    def test_permission_error_exhausts_retries_then_falls_back_safely(self) -> None:
        # A PermissionError that never clears (a genuine, non-transient
        # problem) must still fall back safely once the retry budget is
        # spent, exactly like any other non-racing failure. Asserts the
        # EXACT attempt and sleep counts and the delay used, not just that
        # a fallback happened, so a future change that silently drops the
        # retry loop (always failing on the first attempt) would not pass
        # this test by accident.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            legacy = home_path / ".headroom"
            legacy.mkdir()
            (legacy / "history.jsonl").write_text("irreplaceable\n", encoding="utf-8")

            attempts = {"count": 0}

            def always_locked(self: Path, target_path: Path) -> None:
                attempts["count"] += 1
                raise PermissionError("locked")

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("quotagauge.config.Path.home", return_value=home_path):
                    with mock.patch.object(Path, "rename", always_locked):
                        with mock.patch("quotagauge.config.time.sleep") as sleep_mock:
                            result = resolve_state_dir()

            target = home_path / ".quotagauge"
            self.assertEqual(result, legacy)
            self.assertFalse(target.exists())
            self.assertEqual(attempts["count"], config_module._MIGRATION_RETRY_ATTEMPTS)
            self.assertEqual(sleep_mock.call_count, config_module._MIGRATION_RETRY_ATTEMPTS - 1)
            sleep_mock.assert_called_with(config_module._MIGRATION_RETRY_DELAY_SECONDS)
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
