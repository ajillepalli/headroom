"""Tests for the opt-in PyPI update check."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib import error

from headroom import update_check


def _file(yanked: bool = False) -> dict:
    return {"filename": "headroom.whl", "yanked": yanked}


class _Response:
    def __init__(self, body: bytes, content_length: bool = True) -> None:
        self._body = io.BytesIO(body)
        self.headers = (
            {"Content-Length": str(len(body))} if content_length else {}
        )
        self.closed = False

    def read(self, size: int) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        self.closed = True


class VersionTests(unittest.TestCase):
    def test_numeric_release_comparison(self) -> None:
        installed = update_check.parse_version("0.1.9")
        candidate = update_check.parse_version("0.1.10")

        self.assertIsNotNone(installed)
        self.assertIsNotNone(candidate)
        self.assertTrue(update_check.version_is_newer(candidate, installed))

    def test_prerelease_is_hidden_from_stable_and_offered_to_prerelease(self) -> None:
        document = {"releases": {"2.0rc2": [_file()]}}
        stable = update_check.parse_version("1.9")
        prerelease = update_check.parse_version("2.0rc1")

        self.assertIsNotNone(stable)
        self.assertIsNotNone(prerelease)
        self.assertIsNone(update_check.select_latest_release(document, stable))
        self.assertEqual(
            update_check.select_latest_release(document, prerelease),
            "2.0rc2",
        )

    def test_post_release_is_offered(self) -> None:
        document = {"releases": {"1.2.post1": [_file()]}}
        installed = update_check.parse_version("1.2")

        self.assertIsNotNone(installed)
        self.assertEqual(
            update_check.select_latest_release(document, installed),
            "1.2.post1",
        )

    def test_yanked_latest_release_is_never_offered(self) -> None:
        document = {
            "info": {"version": "9.0"},
            "releases": {
                "1.1": [_file()],
                "9.0": [_file(yanked=True)],
            },
        }
        installed = update_check.parse_version("1.0")

        self.assertIsNotNone(installed)
        self.assertEqual(
            update_check.select_latest_release(document, installed),
            "1.1",
        )


class CheckTests(unittest.TestCase):
    def test_stubbed_response_finds_update_and_writes_separate_cache(self) -> None:
        body = json.dumps(
            {"releases": {"0.1.10": [_file()], "0.2rc1": [_file()]}}
        ).encode("utf-8")
        response = _Response(body)
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch(
                "headroom.update_check.request.urlopen", return_value=response
            ) as opened:
                result = update_check.check_for_update(
                    "0.1.9", state_dir=state_dir, now=100.0
                )

            self.assertTrue(result.update_available)
            self.assertEqual(result.latest_version, "0.1.10")
            self.assertTrue((state_dir / "update-check.json").is_file())
            self.assertFalse((state_dir / "state.json").exists())
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args.kwargs["timeout"], 2.0)
            self.assertTrue(response.closed)

    def test_unparsable_installed_version_offers_nothing_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("headroom.update_check.request.urlopen") as opened:
                result = update_check.check_for_update(
                    "not a version", Path(directory), now=100.0
                )

            self.assertFalse(result.update_available)
            self.assertEqual(result.outcome, "failure")
            self.assertIn("installed version", result.reason or "")
            opened.assert_not_called()

    def test_oversized_body_is_stopped_and_cached_as_failure(self) -> None:
        body = b"{" + b" " * update_check.MAX_RESPONSE_BYTES + b"}"
        response = _Response(body, content_length=False)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch(
                "headroom.update_check.request.urlopen", return_value=response
            ):
                result = update_check.check_for_update(
                    "1.0", Path(directory), now=100.0
                )

            self.assertEqual(result.outcome, "failure")
            self.assertIn("exceeded 262144 bytes", result.reason or "")
            self.assertLess(response._body.tell(), len(body))
            self.assertTrue(response.closed)

    def test_failure_is_cached_and_not_retried_within_24_hours(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch(
                "headroom.update_check.request.urlopen",
                side_effect=error.URLError("endpoint unavailable"),
            ) as opened:
                first = update_check.check_for_update(
                    "1.0", state_dir, now=100.0
                )
                second = update_check.check_for_update(
                    "1.0", state_dir, now=101.0
                )

            self.assertEqual(first.outcome, "failure")
            self.assertEqual(second.outcome, "failure")
            self.assertTrue(second.cached)
            self.assertEqual(first.reason, second.reason)
            self.assertEqual(opened.call_count, 1)

    def test_fresh_cache_prevents_recheck_even_if_installed_version_changes(self) -> None:
        body = json.dumps({"releases": {"1.1": [_file()]}}).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch(
                "headroom.update_check.request.urlopen",
                return_value=_Response(body),
            ) as opened:
                update_check.check_for_update("1.0", state_dir, now=100.0)
                result = update_check.check_for_update("1.0.post1", state_dir, now=101.0)

            self.assertEqual(opened.call_count, 1)
            self.assertEqual(result.outcome, "failure")
            self.assertIn("installed version changed", result.reason or "")


if __name__ == "__main__":
    unittest.main()
