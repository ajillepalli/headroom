"""Tests for the opt-in PyPI update check."""

from __future__ import annotations

import io
import json
import os
from email.message import Message
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
from urllib import error, request, response

from quotagauge import update_check


def _file(yanked: bool = False) -> dict:
    return {"filename": "quotagauge.whl", "yanked": yanked}


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


class _RedirectTransport(request.HTTPSHandler):
    def __init__(self, location: str, status_code: int = 302) -> None:
        super().__init__()
        self.location = location
        self.status_code = status_code
        self.contacted_urls = []
        self.body = io.BytesIO(b"redirect")

    def https_open(self, request_value: request.Request):
        self.contacted_urls.append(request_value.full_url)
        headers = Message()
        headers["Location"] = self.location
        result = response.addinfourl(
            self.body,
            headers,
            request_value.full_url,
            self.status_code,
        )
        result.msg = "Redirect"
        return result


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

    def test_empty_release_catalogue_is_not_evidence_of_current_version(self) -> None:
        installed = update_check.parse_version("1.0")

        self.assertIsNotNone(installed)
        with self.assertRaisesRegex(ValueError, "no credible releases"):
            update_check.select_latest_release({"releases": {}}, installed)


class CheckTests(unittest.TestCase):
    def test_stubbed_response_finds_update_and_writes_separate_cache(self) -> None:
        body = json.dumps(
            {"releases": {"0.1.10": [_file()], "0.2rc1": [_file()]}}
        ).encode("utf-8")
        response = _Response(body)
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch(
                "quotagauge.update_check._open_pypi", return_value=response
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
            with mock.patch("quotagauge.update_check._open_pypi") as opened:
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
                "quotagauge.update_check._open_pypi", return_value=response
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
                "quotagauge.update_check._open_pypi",
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

    def test_cache_for_another_installed_version_is_rechecked(self) -> None:
        body = json.dumps({"releases": {"1.1": [_file()]}}).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch(
                "quotagauge.update_check._open_pypi",
                side_effect=(_Response(body), _Response(body)),
            ) as opened:
                update_check.check_for_update("1.0", state_dir, now=100.0)
                result = update_check.check_for_update("1.0.post1", state_dir, now=101.0)

            self.assertEqual(opened.call_count, 2)
            self.assertEqual(result.outcome, "update")
            self.assertEqual(result.installed_version, "1.0.post1")
            self.assertFalse(result.cached)

    def test_future_cache_timestamp_is_invalid_and_does_not_suppress_check(self) -> None:
        body = json.dumps({"releases": {"1.0": [_file()]}}).encode("utf-8")
        future_results = (
            update_check.UpdateResult(
                "failure",
                1000.0,
                "1.0",
                reason="cached while the clock was wrong",
            ),
            update_check.UpdateResult(
                "update",
                1000.0,
                "1.0",
                latest_version="2.0",
            ),
        )
        for future_result in future_results:
            with self.subTest(outcome=future_result.outcome):
                with tempfile.TemporaryDirectory() as directory:
                    state_dir = Path(directory)
                    update_check._write_cache(future_result, state_dir)
                    with mock.patch(
                        "quotagauge.update_check._open_pypi",
                        return_value=_Response(body),
                    ) as opened:
                        result = update_check.check_for_update(
                            "1.0",
                            state_dir,
                            now=100.0,
                        )

                    opened.assert_called_once()
                    self.assertEqual(result.outcome, "current")
                    self.assertFalse(result.cached)

    def test_small_clock_skew_does_not_invalidate_a_good_cache(self) -> None:
        # Ordinary NTP correction can leave a cache timestamped a few seconds
        # ahead of "now". That is not the future-dated-cache bug (a cache
        # dated years ahead suppressing every future check); it is normal
        # clock behaviour, and should not throw away a valid cache or cost a
        # network round trip.
        cached_result = update_check.UpdateResult(
            "current",
            105.0,
            "1.0",
        )
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            update_check._write_cache(cached_result, state_dir)
            with mock.patch(
                "quotagauge.update_check._open_pypi"
            ) as opened:
                result = update_check.check_for_update(
                    "1.0",
                    state_dir,
                    now=100.0,
                )

            opened.assert_not_called()
            self.assertEqual(result.outcome, "current")
            self.assertTrue(result.cached)

    def test_empty_catalogue_is_cached_as_failure_not_current(self) -> None:
        body = json.dumps({"releases": {}}).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch(
                "quotagauge.update_check._open_pypi",
                return_value=_Response(body),
            ):
                result = update_check.check_for_update("1.0", state_dir, now=100.0)

            cached = update_check.read_cached_result(state_dir, "1.0")
            self.assertEqual(result.outcome, "failure")
            self.assertIn("no credible releases", result.reason or "")
            self.assertIsNotNone(cached)
            self.assertEqual(cached.outcome, "failure")

    def test_http_error_is_closed_and_cached_with_sanitised_reason(self) -> None:
        body = io.BytesIO(b"hostile response details")
        failure = error.HTTPError(
            update_check.PYPI_URL,
            500,
            "hostile\nserver text",
            {},
            body,
        )
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch(
                "quotagauge.update_check._open_pypi",
                side_effect=failure,
            ):
                result = update_check.check_for_update("1.0", state_dir, now=100.0)

            self.assertTrue(body.closed)
            self.assertEqual(result.outcome, "failure")
            self.assertEqual(result.reason, "PyPI returned HTTP status 500")
            cached = update_check.read_cached_result(state_dir, "1.0")
            self.assertIsNotNone(cached)
            self.assertEqual(cached.reason, result.reason)

    def test_cross_origin_redirect_is_not_contacted(self) -> None:
        # Covers every redirect status the fix claims to block. 301/302/303/307
        # are handled by our overridden HTTPRedirectHandler.redirect_request,
        # which returns None to refuse the redirect. 308 has no http_error_308
        # method on Python 3.9's HTTPRedirectHandler at all, so it falls
        # through to HTTPDefaultErrorHandler instead -- a genuinely different
        # code path that deserves its own assertion, not just an assumption
        # that "redirects are blocked" covers it too.
        redirect_target = "http://attacker.invalid/collect"
        for status_code in (301, 302, 303, 307, 308):
            with self.subTest(status_code=status_code):
                transport = _RedirectTransport(redirect_target, status_code)
                opener = request.build_opener(
                    update_check._NoRedirectHandler(),
                    transport,
                )
                with tempfile.TemporaryDirectory() as directory:
                    with mock.patch(
                        "quotagauge.update_check.request.build_opener",
                        return_value=opener,
                    ):
                        result = update_check.check_for_update(
                            "1.0",
                            Path(directory),
                            now=100.0,
                        )

                self.assertEqual(transport.contacted_urls, [update_check.PYPI_URL])
                self.assertNotIn(redirect_target, transport.contacted_urls)
                self.assertTrue(transport.body.closed)
                self.assertEqual(result.outcome, "failure")
                self.assertEqual(
                    result.reason,
                    "PyPI returned HTTP status {}".format(status_code),
                )

    def test_deadline_is_rechecked_after_each_response_read(self) -> None:
        # The whole body fits in the first response.read() call, so a second
        # read() is needed only to observe end-of-stream (an empty chunk).
        # That second read is the one that matters here: if it were the read
        # that blocked past the deadline (e.g. a slow/hanging server taking
        # its time to send EOF), a version of this loop that only checks the
        # deadline *before* each read would sail straight through
        # `if not chunk: break` and successfully parse the (valid) body,
        # silently ignoring that DEADLINE_SECONDS had already elapsed. Only a
        # recheck immediately *after* that read catches it. Five monotonic()
        # values are consumed in order: started, pre-read #1, post-read #1
        # (not yet expired), pre-read #2 (not yet expired), post-read #2
        # (expired).
        body = json.dumps({"releases": {"1.0": [_file()]}}).encode("utf-8")
        response_value = _Response(body)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch(
                "quotagauge.update_check._open_pypi",
                return_value=response_value,
            ):
                with mock.patch(
                    "quotagauge.update_check.time.monotonic",
                    side_effect=(0.0, 0.1, 0.2, 0.3, 2.5),
                ):
                    result = update_check.check_for_update(
                        "1.0",
                        Path(directory),
                        now=100.0,
                    )

        self.assertTrue(response_value.closed)
        self.assertEqual(result.outcome, "failure")
        self.assertIn("deadline exceeded", result.reason or "")


class CacheTests(unittest.TestCase):
    def test_hostile_cache_decode_and_validation_failures_never_escape(self) -> None:
        payloads = {
            "deeply nested": "[" * 1100 + "0" + "]" * 1100,
            "overflowing timestamp": (
                '{"version":1,"checked_at":'
                + "9" * 309
                + ',"installed_version":"1.0","outcome":"current",'
                '"latest_version":null,"reason":null}'
            ),
        }
        for label, payload in payloads.items():
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as directory:
                    cache = Path(directory) / update_check.CACHE_FILENAME
                    cache.write_text(payload, encoding="utf-8")

                    self.assertIsNone(update_check.read_cached_result(Path(directory)))

    def test_cache_read_is_bounded_and_rejects_non_regular_files(self) -> None:
        # A payload of pure whitespace (the original oversized case) is
        # invalid JSON on its own, so json.loads() rejects it regardless of
        # whether the MAX_CACHE_BYTES read-size bound exists. That made the
        # first assertion below pass vacuously. To actually exercise the
        # bound, pad a fully VALID cache document with leading whitespace
        # (json.loads tolerates leading/trailing whitespace) until the file
        # is exactly one byte over the limit. With the bound in place, the
        # bounded read sees more bytes than MAX_CACHE_BYTES and bails before
        # ever parsing. Without it, the bounded read still happens to capture
        # the whole (valid) file, and it would decode successfully.
        valid_document = json.dumps(
            {
                "version": 1,
                "checked_at": 100.0,
                "installed_version": "1.0",
                "outcome": "current",
                "latest_version": None,
                "reason": None,
            }
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            cache = state_dir / update_check.CACHE_FILENAME
            padding = b" " * (update_check.MAX_CACHE_BYTES + 1 - len(valid_document))
            cache.write_bytes(padding + valid_document)
            self.assertIsNone(update_check.read_cached_result(state_dir))

        # A directory placed at the cache path already raises PermissionError
        # (Windows) or IsADirectoryError (POSIX) at path.open("rb") time, so
        # it never reaches the stat.S_ISREG check at all: any OSError is
        # already caught by the generic handler below, making the original
        # directory-based case pass regardless of that check's presence.
        # Exercise the S_ISREG gate directly instead, by making fstat() report
        # a non-regular mode for an otherwise perfectly valid, in-bounds file.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            cache = state_dir / update_check.CACHE_FILENAME
            cache.write_bytes(valid_document)
            fake_stat = os.stat_result((stat.S_IFDIR | 0o755,) + (0,) * 9)
            with mock.patch(
                "quotagauge.update_check.os.fstat", return_value=fake_stat
            ):
                self.assertIsNone(update_check.read_cached_result(state_dir))

    def test_cache_validation_uses_the_open_file_without_a_pre_open_stat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            update_check._write_cache(
                update_check.UpdateResult("current", 100.0, "1.0"),
                state_dir,
            )
            with mock.patch.object(
                Path,
                "stat",
                side_effect=AssertionError("pre-open stat used"),
            ):
                result = update_check.read_cached_result(state_dir, "1.0")

        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, "current")


if __name__ == "__main__":
    unittest.main()
